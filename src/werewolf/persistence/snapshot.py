"""Atomic, visibility-safe game cycle snapshots.

This module is deliberately independent from :mod:`werewolf.game.manager`.
The manager owns the authoritative state and decides when a complete cycle is
available; :class:`GameSnapshotStore` only accepts an immutable ``GameState``
value and materializes it.  In particular, it never changes a ``GameState``
in place and it never derives the public record by first rendering the full
private log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from werewolf.domain.enums import Channel, GamePhase
from werewolf.game.actions import ActionWindow
from werewolf.game.events import GameEvent
from werewolf.game.state import GameState, RulesetRef

from .atomic import atomic_write_bytes, resolve_contained_path

SNAPSHOT_SCHEMA_VERSION: Final[Literal[1]] = 1
SNAPSHOT_MANIFEST_FILENAME: Final[str] = "snapshot_manifest.json"
_JSON_KWARGS: Final[dict[str, object]] = {
    "allow_nan": False,
    "ensure_ascii": False,
    "separators": (",", ":"),
    "sort_keys": True,
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SNAPSHOT_DIR_RE = re.compile(r"^(?P<timestamp>\d{8}T\d{6}Z)_round_(?P<round>\d{3})$")
_SECRETS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "api_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
_PUBLIC_EVENT_TYPES = frozenset({"announcement", "speech", "vote_result"})
_RESOLVED_ACTION_REQUEST_STATUSES = frozenset({"CONFIRMED", "OVERRIDDEN", "CANCELLED"})
_MATERIALIZE_LOCKS: dict[Path, asyncio.Lock] = {}


class GameSnapshotError(ValueError):
    """Base error for invalid or incomplete game snapshots."""


class SnapshotBoundaryError(GameSnapshotError):
    """Raised when a state is not at a durable complete-cycle boundary."""


class SnapshotSecurityError(GameSnapshotError):
    """Raised when a value could disclose credentials or private projection data."""


class SnapshotAlreadyExistsError(GameSnapshotError):
    """Raised when an immutable snapshot directory already exists."""


class CorruptSnapshotError(GameSnapshotError):
    """Raised when a snapshot directory or manifest cannot be verified."""


@dataclass(frozen=True, slots=True)
class FrozenRulesetSnapshot:
    """Detached, immutable ruleset metadata optionally copied into a snapshot."""

    board_id: str
    version: str
    snapshot_id: str
    manifest_sha256: str
    files: Mapping[str, bytes] = field(default_factory=lambda: MappingProxyType({}))
    file_digests: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.board_id or not self.version or not self.snapshot_id:
            raise ValueError("ruleset identity fields must be non-empty")
        if _SHA256_RE.fullmatch(self.manifest_sha256) is None:
            raise ValueError("ruleset manifest_sha256 must be a lowercase SHA-256 digest")
        copied: dict[str, bytes] = {}
        for relative_path, content in self.files.items():
            _validate_relative_file(relative_path, field_name="ruleset file")
            if not isinstance(content, bytes):
                raise TypeError("ruleset file content must be bytes")
            copied[relative_path] = bytes(content)
        object.__setattr__(self, "files", MappingProxyType(copied))
        if self.file_digests is None:
            digests = {
                path: hashlib.sha256(content).hexdigest()
                for path, content in sorted(copied.items())
            }
        else:
            digests = {}
            for path, digest in self.file_digests.items():
                _validate_relative_file(path, field_name="ruleset file")
                if _SHA256_RE.fullmatch(digest) is None:
                    raise ValueError("ruleset file digest must be a lowercase SHA-256 digest")
                digests[path] = digest
        # A materialized ruleset has both content and digests.  A manifest
        # stores only the latter, so verification must also be able to carry
        # a digest-only detached description back through this model.
        if copied and set(digests) != set(copied):
            raise ValueError("ruleset file digests must match the supplied file set")
        object.__setattr__(self, "file_digests", MappingProxyType(digests))

    @classmethod
    def from_ref(
        cls,
        ref: RulesetRef,
        *,
        files: Mapping[str, bytes] | None = None,
    ) -> FrozenRulesetSnapshot:
        """Detach a validated game ruleset reference and optional file set."""

        if not isinstance(ref, RulesetRef):
            raise TypeError("ref must be a RulesetRef")
        return cls(
            board_id=ref.board_id,
            version=ref.version,
            snapshot_id=ref.snapshot_id,
            manifest_sha256=ref.manifest_sha256,
            files={} if files is None else files,
        )

    def model_dump(self) -> dict[str, object]:
        """Return a JSON-shaped detached ruleset description."""

        assert self.file_digests is not None
        return {
            "board_id": self.board_id,
            "version": self.version,
            "snapshot_id": self.snapshot_id,
            "manifest_sha256": self.manifest_sha256,
            "files": dict(self.file_digests),
        }


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One relative file and its verified digest in a game snapshot."""

    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Canonical metadata written as ``snapshot_manifest.json``."""

    schema_version: Literal[1]
    snapshot_id: str
    game_id: str
    snapshot_revision: int
    state_revision: int
    round_no: int
    created_at: str
    ruleset: FrozenRulesetSnapshot
    files: tuple[SnapshotFile, ...]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    """Result of one successful atomic materialization."""

    path: Path
    manifest: SnapshotManifest

    @property
    def snapshot_id(self) -> str:
        return self.manifest.snapshot_id

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.manifest_sha256


# A descriptive alias used by callers that model this as a service operation.
SnapshotResult = GameSnapshot


def _materialize_lock(path: Path) -> asyncio.Lock:
    return _MATERIALIZE_LOCKS.setdefault(path, asyncio.Lock())


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, **cast(Any, _JSON_KWARGS)) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


def _validate_relative_file(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.parts != tuple(part for part in value.split("/") if part)
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
        or value.startswith(".")
    ):
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")
    return value


def _json_safe(value: object, *, path: str = "root") -> object:
    """Detach JSON data while rejecting credential-shaped fields."""

    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SnapshotSecurityError(f"non-string JSON key at {path}")
            normalized = key.casefold().replace("-", "_")
            if normalized in _SECRETS or any(
                part in normalized.split("_") for part in ("secret", "password", "token")
            ):
                raise SnapshotSecurityError(
                    f"credential-shaped field is not persistable: {path}.{key}"
                )
            result[key] = _json_safe(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, path=f"{path}[]") for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SnapshotSecurityError(f"unsupported value in snapshot at {path}")


def _state_payload(state: GameState) -> dict[str, object]:
    payload = state.model_dump(mode="json")
    checked = _json_safe(payload)
    if not isinstance(checked, dict):  # pragma: no cover - model_dump is always an object
        raise SnapshotSecurityError("GameState did not serialize as an object")
    return cast(dict[str, object], checked)


def _thaw_json_containers(value: object) -> object:
    """Copy immutable state containers into JSON-shaped mutable containers.

    ``GameState`` keeps extension records in JSON-shaped fields, but some
    commit paths deliberately build those records from frozen protocol models
    and retain tuples inside nested values.  Pydantic's strict ``JsonValue``
    validator accepts the already validated state container, while a fresh
    ``model_validate`` of the nested protocol model does not accept those
    tuples.  Copying mappings and sequences here keeps the boundary check
    strict while making it independent of the container implementation used
    by the preceding commit.
    """

    if isinstance(value, Mapping):
        return {key: _thaw_json_containers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json_containers(item) for item in value]
    return value


def _coerce_ruleset(
    state: GameState,
    ruleset: FrozenRulesetSnapshot | RulesetRef | Mapping[str, object] | None,
) -> FrozenRulesetSnapshot:
    if ruleset is None:
        if state.ruleset is None:
            raise GameSnapshotError("a frozen ruleset is required before snapshot creation")
        return FrozenRulesetSnapshot.from_ref(state.ruleset)
    if isinstance(ruleset, FrozenRulesetSnapshot):
        frozen = ruleset
    elif isinstance(ruleset, RulesetRef):
        frozen = FrozenRulesetSnapshot.from_ref(ruleset)
    elif isinstance(ruleset, Mapping):
        try:
            ref = RulesetRef.model_validate(dict(ruleset))
        except Exception as exc:
            raise GameSnapshotError("ruleset input is not a valid RulesetRef") from exc
        frozen = FrozenRulesetSnapshot.from_ref(ref)
    else:
        raise TypeError("ruleset must be FrozenRulesetSnapshot, RulesetRef, mapping, or None")
    if state.ruleset is not None:
        state_ref = state.ruleset
        if (
            frozen.board_id != state_ref.board_id
            or frozen.version != state_ref.version
            or frozen.snapshot_id != state_ref.snapshot_id
            or frozen.manifest_sha256 != state_ref.manifest_sha256
        ):
            raise GameSnapshotError("frozen ruleset does not match GameState.ruleset")
    return frozen


def _assert_cycle_boundary(state: GameState) -> None:
    """Reject in-flight work without inventing phase-specific game rules."""

    if state.phase is GamePhase.NIGHT_TEAM_CHAT:
        # This phase is also used for the first night and for ordinary night
        # coordination.  It is a durable cycle boundary only immediately
        # after a successful victory check selected the ongoing branch.  The
        # audit must be the final mutation at this revision; this prevents a
        # caller from manufacturing an arbitrary night snapshot merely by
        # moving the phase to NIGHT_TEAM_CHAT.
        if not state.moderator_audit:
            raise SnapshotBoundaryError(
                "snapshots are only allowed at VICTORY_CHECK or FINISHED boundaries; "
                "NIGHT_TEAM_CHAT requires a completed victory check"
            )
        audit = state.moderator_audit[-1]
        if not isinstance(audit, Mapping) or audit.get("operation") != "VICTORY_CHECK":
            raise SnapshotBoundaryError(
                "snapshots are only allowed at VICTORY_CHECK or FINISHED boundaries; "
                "NIGHT_TEAM_CHAT requires a completed victory check"
            )
        if audit.get("status") != "ONGOING" or audit.get("winner") is not None:
            raise SnapshotBoundaryError(
                "NIGHT_TEAM_CHAT boundary must contain an ongoing victory result"
            )
        if audit.get("committed_revision") != state.state_revision:
            raise SnapshotBoundaryError(
                "NIGHT_TEAM_CHAT victory boundary is not the current committed state"
            )
        if state.winner is not None:
            raise SnapshotBoundaryError(
                "NIGHT_TEAM_CHAT ongoing victory boundary cannot contain a winner"
            )
    elif state.phase not in {GamePhase.VICTORY_CHECK, GamePhase.FINISHED}:
        raise SnapshotBoundaryError(
            "snapshots are only allowed at VICTORY_CHECK or FINISHED boundaries; "
            "NIGHT_TEAM_CHAT requires a completed victory check"
        )
    if state.serial_turn is not None:
        raise SnapshotBoundaryError("cannot snapshot while a serial turn is active")
    if state.pending_resolution is not None:
        raise SnapshotBoundaryError("cannot snapshot with an unconfirmed resolution")
    for window_id, window in state.action_windows.items():
        if not isinstance(window, Mapping):
            raise SnapshotBoundaryError(f"action window {window_id!r} is malformed")
        try:
            window_data = _thaw_json_containers(window)
            if not isinstance(window_data, dict):  # pragma: no cover - Mapping above guarantees it
                raise TypeError("action window did not thaw to a mapping")
            phase = window_data.get("phase")
            if isinstance(phase, str):
                window_data["phase"] = GamePhase(phase)
            typed_window = ActionWindow.model_validate(window_data)
        except Exception as exc:
            raise SnapshotBoundaryError(f"action window {window_id!r} is malformed") from exc
        if typed_window.is_open:
            raise SnapshotBoundaryError(f"action window {window_id!r} is still active")
    for request_id, request in state.action_requests.items():
        if not isinstance(request, Mapping):
            raise SnapshotBoundaryError(f"action request {request_id!r} is malformed")
        status = request.get("status")
        if not isinstance(status, str) or status.upper() not in _RESOLVED_ACTION_REQUEST_STATUSES:
            raise SnapshotBoundaryError(f"action request {request_id!r} is still active")
    for seat, player in state.players.items():
        if player.current_request_id is not None:
            raise SnapshotBoundaryError(f"seat {seat} still has an active action request binding")
    for seat, cursor in state.delivery_cursors.items():
        if cursor.in_flight_request_id is not None or cursor.in_flight_event_ids:
            raise SnapshotBoundaryError(f"seat {seat} still has an in-flight delivery cursor")


def _typed_events(state: GameState) -> tuple[GameEvent, ...]:
    events = tuple(event for event in state.events if isinstance(event, GameEvent))
    if len(events) != len(state.events):
        raise GameSnapshotError("legacy untyped events must be migrated before snapshotting")
    return events


def _event_payload(event: GameEvent) -> dict[str, object]:
    payload = (
        event.public_projection or event.payload
        if event.channel is Channel.PUBLIC
        else event.payload
    )
    checked = _json_safe(payload.model_dump(mode="json"), path=f"event[{event.event_id}].payload")
    if not isinstance(checked, dict):  # pragma: no cover
        raise SnapshotSecurityError("event payload did not serialize as an object")
    return cast(dict[str, object], checked)


def _render_event_line(event: GameEvent, *, public: bool) -> str:
    payload = _event_payload(event)
    return json.dumps(
        {
            "event_id": event.event_id,
            "round_no": event.round_no,
            "phase": event.phase.value,
            "event_type": event.event_type.value,
            "actor_seat": event.actor_seat,
            "payload": payload,
        },
        **cast(Any, _JSON_KWARGS),
    )


def _render_public(events: Sequence[GameEvent]) -> bytes:
    lines = ["# Public record", "", "Events below are the authorized PUBLIC projection.", ""]
    for event in events:
        if event.channel is not Channel.PUBLIC:
            continue
        if event.event_type.value not in _PUBLIC_EVENT_TYPES:
            raise SnapshotSecurityError(
                f"event type {event.event_type.value!r} is not allowed in public projection"
            )
        lines.append(_render_event_line(event, public=True))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_team(events: Sequence[GameEvent]) -> bytes:
    lines = ["# Team record", "", "This file contains authorized TEAM events for GM audit.", ""]
    for event in events:
        if event.channel is Channel.TEAM:
            lines.append(_render_event_line(event, public=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_gm(state: GameState, events: Sequence[GameEvent]) -> bytes:
    lines = ["# GM record", "", "## Players", ""]
    for seat, player in sorted(state.players.items()):
        lines.append(
            f"- seat {seat}: role={player.role_id}, faction={player.faction_id}, "
            f"alive={str(player.alive).lower()}, can_vote={str(player.can_vote).lower()}"
        )
    lines.extend(["", "## Events", ""])
    for event in events:
        lines.append(_render_event_line(event, public=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _runtime_refs(state: GameState) -> bytes:
    refs = []
    for seat, player in sorted(state.players.items()):
        cursor = state.delivery_cursors.get(seat)
        refs.append(
            {
                "seat": seat,
                "runtime_ref": player.runtime_ref,
                "session_epoch": player.session_epoch,
                "confirmed_event_cursor": player.confirmed_event_cursor,
                "delivery_cursor": cursor.model_dump(mode="json") if cursor is not None else None,
            }
        )
    return _canonical_json_bytes(_json_safe(refs, path="runtime_refs"))


def _next_revision_sync(snapshots_dir: Path, state: GameState) -> int:
    previous = state.last_snapshot
    previous_revision = previous.get("snapshot_revision") if isinstance(previous, Mapping) else None
    if isinstance(previous_revision, int) and not isinstance(previous_revision, bool):
        return previous_revision + 1
    revisions = [
        int(match.group("round"))
        for item in snapshots_dir.iterdir()
        if (match := _SNAPSHOT_DIR_RE.fullmatch(item.name)) is not None and item.is_dir()
    ]
    return max(revisions, default=0) + 1


def _manifest_payload(
    *,
    snapshot_id: str,
    game_id: str,
    snapshot_revision: int,
    state_revision: int,
    round_no: int,
    created_at: str,
    ruleset: FrozenRulesetSnapshot,
    files: Sequence[SnapshotFile],
) -> dict[str, object]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "game_id": game_id,
        "snapshot_revision": snapshot_revision,
        "state_revision": state_revision,
        "round_no": round_no,
        "created_at": created_at,
        "ruleset": {
            "board_id": ruleset.board_id,
            "version": ruleset.version,
            "snapshot_id": ruleset.snapshot_id,
            "manifest_sha256": ruleset.manifest_sha256,
            "files": dict(cast(Mapping[str, str], ruleset.model_dump()["files"])),
        },
        "files": [
            {"path": item.relative_path, "sha256": item.sha256, "size": item.size} for item in files
        ],
    }


def _build_snapshot_files(
    state: GameState,
    ruleset: FrozenRulesetSnapshot,
) -> dict[str, bytes]:
    if ruleset.file_digests and not ruleset.files:
        raise GameSnapshotError("ruleset file contents are required to create a snapshot")
    events = _typed_events(state)
    files: dict[str, bytes] = {
        "state.json": _canonical_json_bytes(_state_payload(state)),
        "public.md": _render_public(events),
        "private/gm.md": _render_gm(state, events),
        "private/channels/wolves.md": _render_team(events),
        "private/runtime_refs.json": _runtime_refs(state),
        "private/ruleset.json": _canonical_json_bytes(_json_safe(ruleset.model_dump())),
    }
    for relative_path, content in ruleset.files.items():
        checked = _json_safe(content.decode("utf-8"), path=f"ruleset.files.{relative_path}")
        if not isinstance(checked, str):  # pragma: no cover
            raise SnapshotSecurityError("ruleset file did not contain text")
        files[f"private/ruleset/files/{relative_path}"] = checked.encode("utf-8")
    return files


def _manifest_from_bytes(raw: bytes) -> SnapshotManifest:
    if b"\r" in raw or not raw.endswith(b"\n"):
        raise CorruptSnapshotError("snapshot manifest is not canonical UTF-8 JSON")
    try:
        payload = json.loads(
            raw[:-1].decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptSnapshotError("snapshot manifest is not valid JSON") from exc
    if _canonical_json_bytes(payload) != raw:
        raise CorruptSnapshotError("snapshot manifest is not canonical JSON")
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise CorruptSnapshotError("unsupported snapshot manifest schema")
    files_raw = payload.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise CorruptSnapshotError("snapshot manifest files are missing")
    files: list[SnapshotFile] = []
    previous: str | None = None
    for entry in files_raw:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            raise CorruptSnapshotError("invalid snapshot manifest file entry")
        try:
            path = _validate_relative_file(entry.get("path"), field_name="manifest path")
        except ValueError as exc:
            raise CorruptSnapshotError("invalid snapshot manifest path") from exc
        digest = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise CorruptSnapshotError("invalid snapshot manifest digest")
        if not isinstance(size, int) or size < 0:
            raise CorruptSnapshotError("invalid snapshot manifest size")
        if previous is not None and path <= previous:
            raise CorruptSnapshotError("snapshot manifest files are not sorted")
        previous = path
        files.append(SnapshotFile(relative_path=path, sha256=digest, size=size))
    ruleset_raw = payload.get("ruleset")
    if not isinstance(ruleset_raw, dict):
        raise CorruptSnapshotError("snapshot manifest ruleset is missing")
    try:
        ruleset = FrozenRulesetSnapshot(
            board_id=cast(str, ruleset_raw["board_id"]),
            version=cast(str, ruleset_raw["version"]),
            snapshot_id=cast(str, ruleset_raw["snapshot_id"]),
            manifest_sha256=cast(str, ruleset_raw["manifest_sha256"]),
            files={},
            file_digests=cast(Mapping[str, str], ruleset_raw.get("files", {})),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptSnapshotError("snapshot manifest ruleset is invalid") from exc
    # ``manifest_sha256`` is derived from the exact canonical bytes and is not
    # duplicated in the JSON body, avoiding a self-referential hash.
    manifest_digest = _sha256(raw)
    required = {
        "schema_version",
        "snapshot_id",
        "game_id",
        "snapshot_revision",
        "state_revision",
        "round_no",
        "created_at",
        "ruleset",
        "files",
    }
    if set(payload) != required:
        raise CorruptSnapshotError("snapshot manifest has unsupported fields")
    scalar_fields = ("snapshot_id", "game_id", "created_at")
    if any(
        not isinstance(payload.get(field), str) or not payload[field] for field in scalar_fields
    ):
        raise CorruptSnapshotError("snapshot manifest identity fields are invalid")
    for field_name in ("snapshot_revision", "state_revision", "round_no"):
        if not isinstance(payload.get(field_name), int) or payload[field_name] < 0:
            raise CorruptSnapshotError("snapshot manifest revision fields are invalid")
    return SnapshotManifest(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=cast(str, payload["snapshot_id"]),
        game_id=cast(str, payload["game_id"]),
        snapshot_revision=cast(int, payload["snapshot_revision"]),
        state_revision=cast(int, payload["state_revision"]),
        round_no=cast(int, payload["round_no"]),
        created_at=cast(str, payload["created_at"]),
        ruleset=ruleset,
        files=tuple(files),
        manifest_sha256=manifest_digest,
    )


def _remove_tree(path: Path) -> None:
    if path.exists() or path.is_symlink():
        shutil.rmtree(path)


class GameSnapshotStore:
    """Materialize and verify one game's complete-cycle snapshots."""

    def __init__(
        self,
        active_root: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.active_root = Path(active_root).resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.snapshots_root = self.active_root / "snapshots"

    def _snapshot_dir(self, name: str | os.PathLike[str]) -> Path:
        try:
            return resolve_contained_path(self.snapshots_root, name)
        except Exception as exc:
            raise CorruptSnapshotError("snapshot path escapes active game root") from exc

    async def create(
        self,
        state: GameState,
        ruleset: FrozenRulesetSnapshot | RulesetRef | Mapping[str, object] | None = None,
        *,
        created_at: datetime | None = None,
    ) -> GameSnapshot:
        """Write one snapshot and publish active projections after verification."""

        if not isinstance(state, GameState):
            raise TypeError("state must be a GameState")
        _assert_cycle_boundary(state)
        frozen_ruleset = _coerce_ruleset(state, ruleset)
        timestamp = self._clock() if created_at is None else created_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        timestamp = timestamp.astimezone(UTC)
        created_text = timestamp.isoformat().replace("+00:00", "Z")
        files = _build_snapshot_files(state, frozen_ruleset)
        snapshot_files = tuple(
            SnapshotFile(relative_path=path, sha256=_sha256(content), size=len(content))
            for path, content in sorted(files.items())
        )

        await asyncio.to_thread(self.snapshots_root.mkdir, parents=True, exist_ok=True)
        lock_path = self.snapshots_root.resolve()
        async with _materialize_lock(lock_path):
            revision = await asyncio.to_thread(_next_revision_sync, self.snapshots_root, state)
            base_manifest = _manifest_payload(
                snapshot_id="pending",
                game_id=state.game_id,
                snapshot_revision=revision,
                state_revision=state.state_revision,
                round_no=state.round_no,
                created_at=created_text,
                ruleset=frozen_ruleset,
                files=snapshot_files,
            )
            base_manifest["snapshot_id"] = "pending"
            snapshot_id = "snapshot-" + _sha256(_canonical_json_bytes(base_manifest))
            manifest_payload = _manifest_payload(
                snapshot_id=snapshot_id,
                game_id=state.game_id,
                snapshot_revision=revision,
                state_revision=state.state_revision,
                round_no=state.round_no,
                created_at=created_text,
                ruleset=frozen_ruleset,
                files=snapshot_files,
            )
            manifest_bytes = _canonical_json_bytes(manifest_payload)
            manifest_sha256 = _sha256(manifest_bytes)
            # Keep the directory name human-readable and platform-safe.  A
            # fixed clock plus round is an immutable conflict, not an overwrite.
            directory_name = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_round_{revision:03d}"
            final_dir = self._snapshot_dir(directory_name)
            if final_dir.exists() or final_dir.is_symlink():
                raise SnapshotAlreadyExistsError(
                    f"snapshot directory already exists: {directory_name}"
                )
            staging_dir = Path(
                await asyncio.to_thread(
                    tempfile.mkdtemp,
                    prefix=".tmp-",
                    dir=self.snapshots_root,
                )
            )
            active_projection_paths = (
                self.active_root / "state.json",
                self.active_root / "public.md",
            )
            previous_active_projection: dict[Path, bytes | None] = {}
            for projection_path in active_projection_paths:
                try:
                    previous_active_projection[projection_path] = await asyncio.to_thread(
                        projection_path.read_bytes
                    )
                except FileNotFoundError:
                    previous_active_projection[projection_path] = None
            committed = False
            try:
                for relative_path, content in files.items():
                    destination = resolve_contained_path(staging_dir, relative_path)
                    await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
                    await atomic_write_bytes(destination, content)
                manifest_path = resolve_contained_path(staging_dir, SNAPSHOT_MANIFEST_FILENAME)
                await atomic_write_bytes(manifest_path, manifest_bytes)
                await asyncio.to_thread(
                    self._verify_directory_sync,
                    staging_dir,
                    state.game_id,
                    manifest_sha256,
                )
                await asyncio.to_thread(os.rename, staging_dir, final_dir)
                committed = True

                # The active view is refreshed only after the formal directory
                # has been verified and atomically published.
                active_state = _state_payload(state)
                active_state["last_snapshot"] = {
                    "snapshot_id": snapshot_id,
                    "snapshot_revision": revision,
                    "state_revision": state.state_revision,
                    "created_at": created_text,
                    "manifest_sha256": manifest_sha256,
                }
                await atomic_write_bytes(
                    resolve_contained_path(self.active_root, "state.json"),
                    _canonical_json_bytes(active_state),
                )
                await atomic_write_bytes(
                    resolve_contained_path(self.active_root, "public.md"),
                    files["public.md"],
                )
            except BaseException:
                # A failure after the directory rename must remain retryable.
                # Restore the previous active projection before removing this
                # attempt's immutable directory; otherwise a second attempt
                # with the same clock and revision would hit a false conflict.
                if committed:
                    await asyncio.to_thread(_remove_tree, final_dir)
                    for projection_path, previous_content in previous_active_projection.items():
                        if previous_content is None:
                            try:
                                await asyncio.to_thread(projection_path.unlink)
                            except FileNotFoundError:
                                pass
                        else:
                            await asyncio.to_thread(projection_path.write_bytes, previous_content)
                raise
            finally:
                if not committed:
                    await asyncio.to_thread(_remove_tree, staging_dir)

            manifest = SnapshotManifest(
                schema_version=SNAPSHOT_SCHEMA_VERSION,
                snapshot_id=snapshot_id,
                game_id=state.game_id,
                snapshot_revision=revision,
                state_revision=state.state_revision,
                round_no=state.round_no,
                created_at=created_text,
                ruleset=frozen_ruleset,
                files=snapshot_files,
                manifest_sha256=manifest_sha256,
            )
            return GameSnapshot(path=final_dir, manifest=manifest)

    async def materialize(
        self,
        state: GameState,
        ruleset: FrozenRulesetSnapshot | RulesetRef | Mapping[str, object] | None = None,
        *,
        created_at: datetime | None = None,
    ) -> GameSnapshot:
        """Alias for :meth:`create` used by persistence-oriented call sites."""

        return await self.create(state, ruleset, created_at=created_at)

    @staticmethod
    def _verify_directory_sync(path: Path, game_id: str, expected_manifest_sha256: str) -> None:
        if path.is_symlink() or not path.is_dir():
            raise CorruptSnapshotError("snapshot staging directory is unavailable")
        if any(item.is_symlink() for item in path.rglob("*")):
            raise CorruptSnapshotError("snapshot contains a symlink")
        manifest_path = path / SNAPSHOT_MANIFEST_FILENAME
        try:
            raw_manifest = manifest_path.read_bytes()
        except OSError as exc:
            raise CorruptSnapshotError("snapshot manifest could not be read") from exc
        manifest = _manifest_from_bytes(raw_manifest)
        if manifest.game_id != game_id or manifest.manifest_sha256 != expected_manifest_sha256:
            raise CorruptSnapshotError("snapshot manifest identity or digest mismatch")
        expected_id_payload = _manifest_payload(
            snapshot_id="pending",
            game_id=manifest.game_id,
            snapshot_revision=manifest.snapshot_revision,
            state_revision=manifest.state_revision,
            round_no=manifest.round_no,
            created_at=manifest.created_at,
            ruleset=manifest.ruleset,
            files=manifest.files,
        )
        expected_snapshot_id = "snapshot-" + _sha256(_canonical_json_bytes(expected_id_payload))
        if manifest.snapshot_id != expected_snapshot_id:
            raise CorruptSnapshotError("snapshot ID does not match manifest contents")
        expected_names = {item.relative_path for item in manifest.files} | {
            SNAPSHOT_MANIFEST_FILENAME
        }
        actual_names = {
            item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()
        }
        if actual_names != expected_names:
            raise CorruptSnapshotError("snapshot file set is incomplete")
        for item in manifest.files:
            target = path / item.relative_path
            try:
                content = target.read_bytes()
            except OSError as exc:
                raise CorruptSnapshotError(
                    f"snapshot file could not be read: {item.relative_path}"
                ) from exc
            if len(content) != item.size or _sha256(content) != item.sha256:
                raise CorruptSnapshotError(f"snapshot hash mismatch: {item.relative_path}")
        # The nested ruleset manifest is an independent integrity boundary.
        # Check its digests against the copied ruleset files as well as the
        # outer snapshot file table, so a malformed nested manifest cannot be
        # accepted merely because the outer file table is self-consistent.
        if manifest.ruleset.file_digests:
            for relative_path, expected_digest in manifest.ruleset.file_digests.items():
                target = resolve_contained_path(path, f"private/ruleset/files/{relative_path}")
                try:
                    content = target.read_bytes()
                except OSError as exc:
                    raise CorruptSnapshotError(
                        f"ruleset file could not be read: {relative_path}"
                    ) from exc
                if _sha256(content) != expected_digest:
                    raise CorruptSnapshotError(f"ruleset file digest mismatch: {relative_path}")
        try:
            GameState.model_validate_json((path / "state.json").read_bytes())
        except Exception as exc:
            raise CorruptSnapshotError("snapshot state.json failed schema validation") from exc
        public_text = (path / "public.md").read_text(encoding="utf-8")
        forbidden = ("team_speech", "team_notice", "private_notice", "role_assignment", "gm_audit")
        if any(marker in public_text for marker in forbidden):
            raise SnapshotSecurityError("public.md contains a private event marker")

    async def verify(self, snapshot: str | os.PathLike[str]) -> SnapshotManifest:
        """Verify every file and return the immutable manifest description."""

        candidate = Path(snapshot)
        if candidate.is_absolute():
            path = candidate.resolve()
            try:
                path.relative_to(self.snapshots_root.resolve())
            except ValueError as exc:
                raise CorruptSnapshotError("snapshot path escapes active game root") from exc
        else:
            path = self._snapshot_dir(candidate)
        try:
            raw = await asyncio.to_thread((path / SNAPSHOT_MANIFEST_FILENAME).read_bytes)
        except OSError as exc:
            raise CorruptSnapshotError("snapshot manifest could not be read") from exc
        manifest = _manifest_from_bytes(raw)
        await asyncio.to_thread(self._verify_directory_sync, path, manifest.game_id, _sha256(raw))
        return manifest

    async def load(self, snapshot: str | os.PathLike[str]) -> SnapshotManifest:
        """Alias for :meth:`verify`."""

        return await self.verify(snapshot)


# Friendly aliases for integrations that call the component a service/store.
SnapshotStore = GameSnapshotStore
GameSnapshotService = GameSnapshotStore


__all__ = [
    "CorruptSnapshotError",
    "FrozenRulesetSnapshot",
    "GameSnapshot",
    "GameSnapshotError",
    "GameSnapshotService",
    "GameSnapshotStore",
    "SNAPSHOT_MANIFEST_FILENAME",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotAlreadyExistsError",
    "SnapshotBoundaryError",
    "SnapshotFile",
    "SnapshotManifest",
    "SnapshotResult",
    "SnapshotSecurityError",
    "SnapshotStore",
]
