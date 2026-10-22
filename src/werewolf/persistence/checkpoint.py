"""Private, content addressed checkpoints for one real player choice.

Public cycle snapshots remain deliberately closed to in-flight trigger work.
This store has a separate format and path for the one private continuation
that can be safely resumed: a frozen ``WAITING_CHOICE`` occurrence.  It
copies the game's verified knowledge snapshot files into the checkpoint so a
restart never consults the live compiled package directory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

from werewolf.domain.enums import GamePhase
from werewolf.game.actions import ActionRegistry, ActionWindow
from werewolf.game.events import EventType, GameEvent
from werewolf.game.state import GameState
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.runtime_loader import (
    RuntimeKnowledgeBundle,
    load_runtime_knowledge_bundle_from_snapshot,
)
from werewolf.knowledge.snapshot import KnowledgeSnapshot, KnowledgeSnapshotBuilder
from werewolf.rules.models import ExecutionPackage

if TYPE_CHECKING:
    from werewolf.game.manager import GameManager

CHECKPOINT_SCHEMA_VERSION: Final = 1
CHECKPOINT_DIRECTORY: Final = "checkpoints"
CHECKPOINT_MANIFEST_FILENAME: Final = "manifest.json"
CHECKPOINT_PAYLOAD_FILENAME: Final = "payload.json"
MAX_CHECKPOINT_BYTES: Final = 32 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_GAME_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.ASCII)
_CHECKPOINT_ID_RE = re.compile(r"^checkpoint-[0-9a-f]{64}$", re.ASCII)
_ACTIVE_REQUEST_STATUSES = frozenset(
    {"OPEN", "REQUESTED", "SUBMITTING", "IN_FLIGHT", "PROCESSING", "PENDING"}
)
_SECRET_PARTS = frozenset({"token", "secret", "password", "credential", "private_key"})


class PrivateCheckpointError(ValueError):
    """Base error for private checkpoint creation and verification."""


class PrivateCheckpointBoundaryError(PrivateCheckpointError):
    """Raised when the game is not at a durable player-choice boundary."""


class PrivateCheckpointIdentityError(PrivateCheckpointError):
    """Raised when a checkpoint does not match the frozen game identity."""


class CorruptPrivateCheckpointError(PrivateCheckpointError):
    """Raised when checkpoint files, permissions, or hashes do not verify."""


@dataclass(frozen=True, slots=True)
class PrivateCheckpointIdentity:
    """The independent game, execution, and knowledge identities to restore."""

    game_id: str
    board_ref: str
    package_id: str
    execution_package_id: str
    execution_digest: str
    package_identity: str

    def __post_init__(self) -> None:
        if _GAME_ID_RE.fullmatch(self.game_id) is None:
            raise ValueError("checkpoint game_id is invalid")
        try:
            ref = VersionedRef.parse(self.board_ref)
        except (TypeError, ValueError) as exc:
            raise ValueError("checkpoint board_ref is invalid") from exc
        if ref.format() != self.board_ref or self.package_id != self.board_ref:
            raise ValueError("checkpoint knowledge package_id must be the exact board_id@version")
        if _SHA256_RE.fullmatch(self.execution_package_id) is None:
            raise ValueError("checkpoint execution_package_id must be a SHA-256 digest")
        if _SHA256_RE.fullmatch(self.execution_digest) is None:
            raise ValueError("checkpoint execution_digest must be a SHA-256 digest")
        if _SHA256_RE.fullmatch(self.package_identity) is None:
            raise ValueError("checkpoint package_identity must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class PrivateCheckpointManifest:
    """Verified public metadata for an opaque private checkpoint payload."""

    checkpoint_id: str
    identity: PrivateCheckpointIdentity
    created_at: str
    payload_sha256: str
    payload_size: int


@dataclass(frozen=True, slots=True)
class VerifiedPrivateCheckpoint:
    """Detached, validated game state and frozen artifacts from one checkpoint."""

    path: Path
    manifest: PrivateCheckpointManifest
    state: GameState
    knowledge_snapshot_manifest: Mapping[str, object]
    frozen_files: Mapping[str, bytes]


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _decode_canonical_json(raw: bytes, *, filename: str) -> Mapping[str, object]:
    if len(raw) > MAX_CHECKPOINT_BYTES or b"\r" in raw or not raw.endswith(b"\n"):
        raise CorruptPrivateCheckpointError(f"{filename} is oversized or not canonical JSON")
    try:
        text = raw[:-1].decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CorruptPrivateCheckpointError(f"{filename} is invalid JSON") from exc
    if not isinstance(value, dict) or _canonical_json_bytes(value) != raw:
        raise CorruptPrivateCheckpointError(f"{filename} is not canonical JSON")
    return cast(Mapping[str, object], value)


def _json_safe(value: object, *, path: str = "root") -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PrivateCheckpointError(f"non-string JSON key at {path}")
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized.split("_") for part in _SECRET_PARTS):
                raise PrivateCheckpointError(
                    f"credential-shaped field is not persistable: {path}.{key}"
                )
            result[key] = _json_safe(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, path=f"{path}[]") for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise PrivateCheckpointError(f"unsupported value in checkpoint at {path}")


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PrivateCheckpointError("frozen file paths must be normalized relative POSIX paths")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.parts != tuple(part for part in value.split("/") if part)
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise PrivateCheckpointError("frozen file paths must be normalized relative POSIX paths")
    if any(part.casefold() in {".runtime", "checkpoints"} for part in path.parts):
        raise PrivateCheckpointError("runtime and checkpoint files cannot be frozen as knowledge")
    return value


def _identity_payload(identity: PrivateCheckpointIdentity) -> dict[str, object]:
    return {
        "board_ref": identity.board_ref,
        "execution_digest": identity.execution_digest,
        "execution_package_id": identity.execution_package_id,
        "game_id": identity.game_id,
        "package_id": identity.package_id,
        "package_identity": identity.package_identity,
    }


def _identity_from_payload(value: object) -> PrivateCheckpointIdentity:
    if not isinstance(value, Mapping) or set(value) != {
        "board_ref",
        "execution_digest",
        "execution_package_id",
        "game_id",
        "package_id",
        "package_identity",
    }:
        raise CorruptPrivateCheckpointError("checkpoint identity fields are invalid")
    try:
        return PrivateCheckpointIdentity(
            game_id=cast(str, value["game_id"]),
            board_ref=cast(str, value["board_ref"]),
            package_id=cast(str, value["package_id"]),
            execution_package_id=cast(str, value["execution_package_id"]),
            execution_digest=cast(str, value["execution_digest"]),
            package_identity=cast(str, value["package_identity"]),
        )
    except (TypeError, ValueError) as exc:
        raise CorruptPrivateCheckpointError("checkpoint identity is invalid") from exc


def _request_is_in_flight(raw: object) -> bool:
    if not isinstance(raw, Mapping):
        return True
    status = raw.get("status")
    return not isinstance(status, str) or status.upper() in _ACTIVE_REQUEST_STATUSES


def _state_identity(
    state: GameState,
    *,
    execution_digest: str,
    package_identity: str,
) -> PrivateCheckpointIdentity:
    ruleset = state.ruleset
    execution = state.execution_identity
    if ruleset is None or execution is None:
        raise PrivateCheckpointIdentityError(
            "checkpoint requires frozen ruleset and execution identities"
        )
    board_ref = f"{ruleset.board_id}@{ruleset.version}"
    if execution.board_id != ruleset.board_id:
        raise PrivateCheckpointIdentityError("state execution board_id does not match board_ref")
    if execution.board_version != ruleset.version:
        raise PrivateCheckpointIdentityError("state execution version does not match board_ref")
    return PrivateCheckpointIdentity(
        game_id=state.game_id,
        board_ref=board_ref,
        package_id=board_ref,
        execution_package_id=execution.package_id,
        execution_digest=execution_digest,
        package_identity=package_identity,
    )


def _state_identity_matches(state: GameState, identity: PrivateCheckpointIdentity) -> None:
    actual = _state_identity(
        state,
        execution_digest=identity.execution_digest,
        package_identity=identity.package_identity,
    )
    if actual != identity:
        raise PrivateCheckpointIdentityError(
            "checkpoint identity does not match authoritative state"
        )


def _validate_waiting_choice(state: GameState) -> None:
    cursor = state.rule_workflow_cursor
    if state.phase is not GamePhase.TRIGGER_ACTION or cursor is None:
        raise PrivateCheckpointBoundaryError(
            "private checkpoint requires a real WAITING_CHOICE trigger"
        )
    if cursor.status != "WAITING_CHOICE" or cursor.active_occurrence_id is None:
        raise PrivateCheckpointBoundaryError(
            "private checkpoint requires cursor status WAITING_CHOICE"
        )
    if cursor.steps_used >= cursor.budget_limit:
        raise PrivateCheckpointBoundaryError("waiting choice has exhausted its workflow budget")
    active = tuple(
        item
        for item in state.rule_trigger_queue
        if item.status == "WAITING_CHOICE" and item.mode == "PLAYER_CHOICE"
    )
    if len(active) != 1 or active[0].occurrence_id != cursor.active_occurrence_id:
        raise PrivateCheckpointBoundaryError(
            "private checkpoint requires one active player-choice occurrence"
        )
    occurrence = active[0]
    if (
        occurrence.window_id is None
        or occurrence.source_fact_id == ""
        or occurrence.source_batch_id == ""
    ):
        raise PrivateCheckpointBoundaryError(
            "waiting choice is missing its source or physical window"
        )
    if state.serial_turn is not None or state.pending_resolution is None:
        raise PrivateCheckpointBoundaryError(
            "waiting choice has an in-flight or missing workflow request"
        )
    pending = state.pending_resolution
    if (
        not isinstance(pending, Mapping)
        or pending.get("operation") != "RULE_TRIGGER"
        or pending.get("status") != "RULE_TRIGGER_ACTION_REQUIRED"
        or pending.get("occurrence_id") != occurrence.occurrence_id
        or pending.get("window_id") != occurrence.window_id
        or pending.get("source_fact_id") != occurrence.source_fact_id
    ):
        raise PrivateCheckpointBoundaryError(
            "waiting choice does not match its manager-owned request"
        )
    if occurrence.window_id not in cursor.active_window_ids:
        raise PrivateCheckpointBoundaryError("waiting choice window is not active in its cursor")
    raw_window = state.action_windows.get(occurrence.window_id)
    if not isinstance(raw_window, Mapping):
        raise PrivateCheckpointBoundaryError("waiting choice physical window is missing")
    try:
        window = ActionWindow.model_validate_json(
            json.dumps(raw_window, ensure_ascii=False, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise PrivateCheckpointBoundaryError("waiting choice physical window is invalid") from exc
    if (
        window.window_id != occurrence.window_id
        or window.game_id != state.game_id
        or window.phase is not GamePhase.TRIGGER_ACTION
        or window.closed_at is not None
        or window.opened_at is None
        or window.allowed_seats != (occurrence.actor_seat,)
        or occurrence.actor_seat not in window.allowed_seats
        or window.submitted_request_ids
    ):
        raise PrivateCheckpointBoundaryError(
            "waiting choice window is not a pristine physical window"
        )
    open_windows = tuple(
        raw
        for raw in state.action_windows.values()
        if isinstance(raw, Mapping)
        and raw.get("closed_at") is None
        and raw.get("opened_at") is not None
    )
    if len(open_windows) != 1 or open_windows[0].get("window_id") != occurrence.window_id:
        raise PrivateCheckpointBoundaryError("waiting choice has another open physical window")
    if any(
        isinstance(raw, Mapping) and raw.get("window_id") == occurrence.window_id
        for raw in state.action_requests.values()
    ):
        raise PrivateCheckpointBoundaryError("waiting choice already has a physical action request")
    if any(_request_is_in_flight(raw) for raw in state.action_requests.values()):
        raise PrivateCheckpointBoundaryError("game has an in-flight physical action request")
    if any(player.current_request_id is not None for player in state.players.values()):
        raise PrivateCheckpointBoundaryError("game has a current physical request binding")
    if any(
        delivery.in_flight_request_id is not None or delivery.in_flight_event_ids
        for delivery in state.delivery_cursors.values()
    ):
        raise PrivateCheckpointBoundaryError("game has an in-flight event delivery")
    player = state.players.get(occurrence.actor_seat)
    if player is None or player.session_epoch != window.session_epoch:
        raise PrivateCheckpointBoundaryError(
            "waiting choice is detached from its player session epoch"
        )
    ability = next(
        (
            item
            for item in state.ability_instances
            if item.ability_instance_id == occurrence.ability_instance_id
        ),
        None,
    )
    if (
        ability is None
        or ability.actor_seat != occurrence.actor_seat
        or ability.skill_id != occurrence.skill_id
        or not ability.enabled
        or ability.consumed
    ):
        raise PrivateCheckpointBoundaryError("waiting choice ability binding is not active")
    expected_codes = (ability.action_code, 299) if window.allow_pass else (ability.action_code,)
    if window.allowed_action_codes != expected_codes:
        raise PrivateCheckpointBoundaryError(
            "waiting choice window does not match its ability action"
        )
    visible_context = window.visible_context
    if (
        visible_context.get("rule_occurrence_id") != occurrence.occurrence_id
        or visible_context.get("rule_source_fact_id") != occurrence.source_fact_id
        or visible_context.get("rule_actor_seat") != occurrence.actor_seat
        or visible_context.get("rule_ability_instance_id") != occurrence.ability_instance_id
        or visible_context.get("rule_skill_id") != occurrence.skill_id
        or visible_context.get("rule_action_code") != ability.action_code
    ):
        raise PrivateCheckpointBoundaryError(
            "waiting choice window context is detached from its occurrence"
        )
    if occurrence.kind == "TRIGGER":
        state_execution = state.execution_identity
        if state_execution is None:
            raise PrivateCheckpointBoundaryError("waiting choice has no pinned execution identity")
        matching_ledgers = tuple(
            entry
            for entry in state.rule_ledger
            if entry.batch_id == occurrence.source_batch_id
            and any(fact.fact_id == occurrence.source_fact_id for fact in entry.facts)
        )
        matching_receipts = tuple(
            receipt
            for receipt in state.rule_receipts
            if receipt.batch_id == occurrence.source_batch_id
        )
        if len(matching_ledgers) != 1 or len(matching_receipts) != 1:
            raise PrivateCheckpointBoundaryError(
                "waiting choice source lacks unique ledger and receipt"
            )
        ledger = matching_ledgers[0]
        receipt = matching_receipts[0]
        if (
            ledger.package_id != state_execution.package_id
            or receipt.package_id != ledger.package_id
            or receipt.group_id != ledger.group_id
            or receipt.timing != ledger.timing
            or receipt.read_revision != ledger.read_revision
            or receipt.committed_revision != ledger.committed_revision
            or receipt.request_ids != ledger.request_ids
            or receipt.outcome_digest != ledger.outcome_digest
        ):
            raise PrivateCheckpointBoundaryError(
                "waiting choice source receipt is detached from its ledger"
            )
    elif occurrence.kind == "HOOK":
        return_point = cursor.return_point
        if (
            return_point is None
            or return_point.phase is not GamePhase.DAY_SPEECH
            or return_point.hook_id != occurrence.hook_id
            or occurrence.hook_id not in {"DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"}
        ):
            raise PrivateCheckpointBoundaryError(
                "waiting hook choice lacks its ordinary speech source"
            )
        if occurrence.hook_id == "DAY_SPEECH_BEFORE":
            if (
                not state.current_queue
                or state.current_queue[0] != return_point.speaker_seat
                or return_point.serial_turn_id != f"before-{occurrence.source_fact_id}"
            ):
                raise PrivateCheckpointBoundaryError(
                    "waiting hook is detached from its before-speech source"
                )
        else:
            turn = state.last_serial_turn
            if (
                turn is None
                or turn.request_id != return_point.serial_turn_id
                or turn.seat != return_point.speaker_seat
                or turn.event_ids != return_point.event_ids
                or not any(
                    event.event_id in turn.event_ids
                    and event.event_type is EventType.SPEECH
                    and event.phase is GamePhase.DAY_SPEECH
                    and event.actor_seat == turn.seat
                    for event in state.events
                    if isinstance(event, GameEvent)
                )
            ):
                raise PrivateCheckpointBoundaryError(
                    "waiting hook is detached from its after-speech source"
                )
    else:  # pragma: no cover - the typed state model currently closes this set.
        raise PrivateCheckpointBoundaryError("unsupported waiting-choice source kind")


def _validate_knowledge_closure(
    identity: PrivateCheckpointIdentity,
    state: GameState,
    snapshot_manifest: Mapping[str, object],
    frozen_files: Mapping[str, bytes],
) -> dict[str, bytes]:
    ruleset = state.ruleset
    if ruleset is None:
        raise PrivateCheckpointIdentityError("checkpoint has no frozen ruleset reference")
    expected_snapshot_fields = {
        "board_ref": identity.board_ref,
        "game_id": identity.game_id,
        "package_id": identity.package_id,
        "package_identity": identity.package_identity,
        "snapshot_id": ruleset.snapshot_id,
    }
    for field, expected in expected_snapshot_fields.items():
        if snapshot_manifest.get(field) != expected:
            raise PrivateCheckpointIdentityError(
                f"knowledge snapshot manifest does not match {field}"
            )
    raw_files = snapshot_manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise PrivateCheckpointIdentityError(
            "knowledge snapshot manifest has no frozen file closure"
        )
    digests: dict[str, str] = {}
    for row in raw_files:
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256"}:
            raise PrivateCheckpointIdentityError("knowledge snapshot file manifest is invalid")
        path = _validate_relative_path(row.get("path"))
        digest = row.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None or path in digests:
            raise PrivateCheckpointIdentityError("knowledge snapshot file digest is invalid")
        digests[path] = digest
    files: dict[str, bytes] = {}
    for relative, content in frozen_files.items():
        path = _validate_relative_path(relative)
        if path in files or not isinstance(content, bytes):
            raise PrivateCheckpointError("frozen file set is invalid")
        files[path] = bytes(content)
    if set(files) != set(digests):
        raise PrivateCheckpointIdentityError(
            "checkpoint must contain the complete knowledge snapshot closure"
        )
    if any(_sha256(content) != digests[path] for path, content in files.items()):
        raise PrivateCheckpointIdentityError(
            "frozen knowledge file digest does not match its manifest"
        )
    package_raw = files.get("package.json")
    execution_raw = files.get("execution.json")
    if package_raw is None or execution_raw is None:
        raise PrivateCheckpointIdentityError(
            "frozen checkpoint is missing package or execution artifacts"
        )
    package_payload = _decode_canonical_json(package_raw, filename="package.json")
    executable_payload = _decode_canonical_json(execution_raw, filename="execution.json")
    package_manifest = package_payload.get("manifest")
    if (
        package_payload.get("package_id") != identity.package_id
        or package_payload.get("board_ref") != identity.board_ref
        or not isinstance(package_manifest, Mapping)
        or package_manifest.get("execution_sha256") != identity.execution_digest
    ):
        raise PrivateCheckpointIdentityError(
            "frozen package artifact identity does not match checkpoint"
        )
    package_digest = _sha256(
        json.dumps(
            package_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    executable_digest = _sha256(
        json.dumps(
            executable_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    if (
        package_digest != identity.package_identity
        or executable_digest != identity.execution_digest
    ):
        raise PrivateCheckpointIdentityError(
            "frozen execution artifacts do not match their content identities"
        )
    execution_model_raw = executable_payload.get("execution")
    action_registry_raw = executable_payload.get("action_registry")
    try:
        execution = ExecutionPackage.model_validate(execution_model_raw)
        action_registry = ActionRegistry.model_validate(action_registry_raw)
    except (TypeError, ValueError) as exc:
        raise PrivateCheckpointIdentityError(
            "frozen execution package failed schema validation"
        ) from exc
    if execution.package_id != identity.execution_package_id:
        raise PrivateCheckpointIdentityError(
            "frozen execution package digest does not match checkpoint"
        )
    registry_digest = _sha256(
        json.dumps(
            action_registry.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    state_execution = state.execution_identity
    if state_execution is None or state_execution.action_registry_digest != registry_digest:
        raise PrivateCheckpointIdentityError(
            "frozen action registry digest does not match game state"
        )
    return files


async def _validate_detached_bundle(
    identity: PrivateCheckpointIdentity,
    state: GameState,
    snapshot_manifest: Mapping[str, object],
    frozen_files: Mapping[str, bytes],
) -> None:
    """Load the frozen bundle as a new runtime would, without a live package tree."""

    ruleset_ref = state.ruleset
    if ruleset_ref is None:
        raise PrivateCheckpointIdentityError("checkpoint has no frozen ruleset reference")
    if (
        set(snapshot_manifest)
        != {
            "board_ref",
            "compiler_version",
            "created_at",
            "files",
            "game_id",
            "package_id",
            "package_identity",
            "schema_version",
            "snapshot_id",
        }
        or type(snapshot_manifest.get("schema_version")) is not int
        or snapshot_manifest.get("schema_version") != 1
    ):
        raise PrivateCheckpointIdentityError("knowledge snapshot manifest schema is invalid")
    with tempfile.TemporaryDirectory(prefix="werewolf-private-checkpoint-") as temporary:
        game_root = Path(temporary) / identity.game_id
        ruleset_root = game_root / "ruleset"
        ruleset_root.mkdir(parents=True, mode=0o700)
        for relative, content in frozen_files.items():
            filename = _validate_relative_path(relative)
            target = ruleset_root / filename
            if target.parent != ruleset_root:
                raise PrivateCheckpointIdentityError(
                    "snapshot files must be flat package artifacts"
                )
            with target.open("xb") as output:
                output.write(content)
        snapshot_bytes = _canonical_json_bytes(dict(snapshot_manifest))
        with (ruleset_root / "snapshot.json").open("xb") as output:
            output.write(snapshot_bytes)
        builder = KnowledgeSnapshotBuilder.from_game_root(
            CompiledKnowledgeStore(Path(temporary) / "unused-compiled"),
            game_root,
            game_id=identity.game_id,
        )
        try:
            snapshot = await builder.load(identity.game_id)
            if (
                snapshot.game_id != identity.game_id
                or snapshot.package_id != identity.package_id
                or snapshot.board_ref.format() != identity.board_ref
                or snapshot.package_identity != identity.package_identity
                or snapshot.snapshot_id != ruleset_ref.snapshot_id
                or snapshot.manifest_sha256 != ruleset_ref.manifest_sha256
            ):
                raise PrivateCheckpointIdentityError(
                    "detached snapshot does not match game identity"
                )
            bundle = await load_runtime_knowledge_bundle_from_snapshot(snapshot)
        except PrivateCheckpointIdentityError:
            raise
        except (TypeError, ValueError, OSError) as exc:
            raise PrivateCheckpointIdentityError(
                "detached frozen knowledge bundle failed runtime verification"
            ) from exc
        package = bundle.package
        execution = package.execution
        registry = package.action_registry
        if (
            execution is None
            or registry is None
            or execution.package_id != identity.execution_package_id
            or package.package_identity != identity.package_identity
            or bundle.board.board_ref.format() != identity.board_ref
        ):
            raise PrivateCheckpointIdentityError(
                "runtime bundle identity does not match checkpoint"
            )
        state_execution = state.execution_identity
        if (
            state_execution is None
            or _sha256(
                json.dumps(
                    registry.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            != state_execution.action_registry_digest
        ):
            raise PrivateCheckpointIdentityError(
                "runtime action registry does not match game state"
            )
        from werewolf.game.manager import GameManager

        try:
            detached_manager = GameManager(
                state,
                registry=registry,
                execution_package=execution,
                package_identity=package.package_identity,
            )
            await detached_manager.assert_private_checkpoint_waiting_choice(state)
        except (TypeError, ValueError) as exc:
            raise PrivateCheckpointBoundaryError(
                "detached manager rejected the waiting-choice source proof"
            ) from exc
        cursor = state.rule_workflow_cursor
        if cursor is None or cursor.active_occurrence_id is None:
            raise PrivateCheckpointBoundaryError(
                "waiting choice cursor disappeared during validation"
            )
        occurrence = next(
            (
                item
                for item in state.rule_trigger_queue
                if item.occurrence_id == cursor.active_occurrence_id
            ),
            None,
        )
        if occurrence is None:
            raise PrivateCheckpointBoundaryError("active waiting occurrence is missing")
        ability = next(
            (
                item
                for item in state.ability_instances
                if item.ability_instance_id == occurrence.ability_instance_id
            ),
            None,
        )
        if ability is None:
            raise PrivateCheckpointBoundaryError("active ability instance is missing")
        skills = tuple(skill for skill in execution.skills if skill.skill_id == occurrence.skill_id)
        if len(skills) != 1:
            raise PrivateCheckpointIdentityError(
                "waiting occurrence skill is not uniquely declared"
            )
        skill = skills[0]
        if (
            skill.action_code != ability.action_code
            or all(grant.grant_id != ability.grant_id for grant in skill.grants)
            or occurrence.actor_seat != ability.actor_seat
        ):
            raise PrivateCheckpointIdentityError(
                "waiting ability does not match its frozen skill grant"
            )
        if occurrence.kind == "TRIGGER":
            matching_facts = tuple(
                fact
                for ledger in state.rule_ledger
                if ledger.batch_id == occurrence.source_batch_id
                for fact in ledger.facts
                if fact.fact_id == occurrence.source_fact_id
            )
            if (
                skill.trigger is None
                or skill.trigger.mode != "PLAYER_CHOICE"
                or len(matching_facts) != 1
                or matching_facts[0].fact_type not in skill.trigger.fact_types
            ):
                raise PrivateCheckpointIdentityError(
                    "waiting trigger is not declared by the frozen skill"
                )
        elif occurrence.kind == "HOOK" and occurrence.hook_id not in skill.hook_ids:
            raise PrivateCheckpointIdentityError("waiting hook is not declared by the frozen skill")


async def load_verified_checkpoint_bundle(
    checkpoint: VerifiedPrivateCheckpoint,
    temporary_root: str | os.PathLike[str],
) -> tuple[KnowledgeSnapshot, RuntimeKnowledgeBundle]:
    """Materialize the checkpoint's frozen package into a detached runtime bundle.

    ``temporary_root`` must be a fresh directory owned by the caller.  This
    intentionally does not consult the live compiled store: recovery remains
    possible after that package has been removed or replaced.
    """

    identity = checkpoint.manifest.identity
    root = Path(temporary_root)
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise PrivateCheckpointIdentityError(
            "checkpoint runtime staging root must be a fresh regular directory"
        )
    game_root = root / identity.game_id
    ruleset_root = game_root / "ruleset"
    ruleset_root.mkdir(parents=True, mode=0o700)
    for relative, content in checkpoint.frozen_files.items():
        filename = _validate_relative_path(relative)
        target = ruleset_root / filename
        if target.parent != ruleset_root:
            raise PrivateCheckpointIdentityError("checkpoint artifact path is not flat")
        with target.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    with (ruleset_root / "snapshot.json").open("xb") as output:
        output.write(_canonical_json_bytes(dict(checkpoint.knowledge_snapshot_manifest)))
        output.flush()
        os.fsync(output.fileno())
    builder = KnowledgeSnapshotBuilder.from_game_root(
        CompiledKnowledgeStore(root / "unused-compiled"),
        game_root,
        game_id=identity.game_id,
    )
    try:
        snapshot = await builder.load(identity.game_id)
        bundle = await load_runtime_knowledge_bundle_from_snapshot(snapshot)
    except (TypeError, ValueError, OSError) as exc:
        raise PrivateCheckpointIdentityError(
            "detached checkpoint runtime bundle failed verification"
        ) from exc
    execution = bundle.package.execution
    registry = bundle.package.action_registry
    if (
        execution is None
        or registry is None
        or execution.package_id != identity.execution_package_id
        or bundle.package.package_identity != identity.package_identity
        or bundle.board.board_ref.format() != identity.board_ref
        or snapshot.game_id != identity.game_id
        or snapshot.package_id != identity.package_id
    ):
        raise PrivateCheckpointIdentityError(
            "detached checkpoint runtime bundle identity changed"
        )
    return snapshot, bundle


def _payload_bytes(
    identity: PrivateCheckpointIdentity,
    state: GameState,
    snapshot_manifest: Mapping[str, object],
    frozen_files: Mapping[str, bytes],
) -> bytes:
    _state_identity_matches(state, identity)
    _validate_waiting_choice(state)
    files = _validate_knowledge_closure(identity, state, snapshot_manifest, frozen_files)
    state_data = cast(dict[str, object], state.model_dump(mode="json"))
    payload = {
        "identity": _identity_payload(identity),
        "knowledge_snapshot_manifest": _json_safe(snapshot_manifest),
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "state": _json_safe(state_data),
        "frozen_files": {
            path: base64.b64encode(content).decode("ascii")
            for path, content in sorted(files.items())
        },
    }
    raw = _canonical_json_bytes(payload)
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise PrivateCheckpointError("private checkpoint exceeds the 32 MiB limit")
    return raw


def _manifest_bytes(
    *, identity: PrivateCheckpointIdentity, payload: bytes, created_at: str
) -> tuple[str, bytes]:
    payload_digest = _sha256(payload)
    checkpoint_id = f"checkpoint-{payload_digest}"
    manifest = {
        "checkpoint_id": checkpoint_id,
        "created_at": created_at,
        "identity": _identity_payload(identity),
        "payload_sha256": payload_digest,
        "payload_size": len(payload),
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
    }
    return checkpoint_id, _canonical_json_bytes(manifest)


def _write_exclusive(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_private_directory(path: Path, *, create: bool) -> None:
    if path.is_symlink():
        raise CorruptPrivateCheckpointError("checkpoint directory may not be a symlink")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise CorruptPrivateCheckpointError("checkpoint parent must be a regular directory")
    if create and hasattr(os, "chmod"):
        path.chmod(0o700)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise CorruptPrivateCheckpointError("checkpoint directory permissions must be private")


def _assert_regular_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise CorruptPrivateCheckpointError("game root must be a regular directory")


def _assert_private_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CorruptPrivateCheckpointError(
            f"checkpoint file could not be opened: {path.name}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CorruptPrivateCheckpointError(f"checkpoint file is not regular: {path.name}")
        if os.name != "nt" and metadata.st_mode & 0o077:
            raise CorruptPrivateCheckpointError(
                f"checkpoint file permissions are too broad: {path.name}"
            )
        if metadata.st_size > MAX_CHECKPOINT_BYTES:
            raise CorruptPrivateCheckpointError(f"checkpoint file is oversized: {path.name}")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_CHECKPOINT_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_CHECKPOINT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_CHECKPOINT_BYTES:
            raise CorruptPrivateCheckpointError(f"checkpoint file is oversized: {path.name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_payload(
    raw: bytes,
    *,
    path: Path,
    manifest: PrivateCheckpointManifest,
    expected_identity: PrivateCheckpointIdentity,
) -> VerifiedPrivateCheckpoint:
    payload = _decode_canonical_json(raw, filename=CHECKPOINT_PAYLOAD_FILENAME)
    if set(payload) != {
        "frozen_files",
        "identity",
        "knowledge_snapshot_manifest",
        "schema_version",
        "state",
    }:
        raise CorruptPrivateCheckpointError("checkpoint payload fields are invalid")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
    ):
        raise CorruptPrivateCheckpointError("checkpoint payload schema is unsupported")
    identity = _identity_from_payload(payload.get("identity"))
    if identity != expected_identity or identity != manifest.identity:
        raise PrivateCheckpointIdentityError("checkpoint belongs to another game or frozen package")
    state_raw = payload.get("state")
    if not isinstance(state_raw, Mapping):
        raise CorruptPrivateCheckpointError("checkpoint state is invalid")
    try:
        state = GameState.model_validate_json(_canonical_json_bytes(state_raw))
    except (TypeError, ValueError) as exc:
        raise CorruptPrivateCheckpointError("checkpoint state failed schema validation") from exc
    _state_identity_matches(state, identity)
    _validate_waiting_choice(state)
    manifest_raw = payload.get("knowledge_snapshot_manifest")
    files_raw = payload.get("frozen_files")
    if not isinstance(manifest_raw, Mapping) or not isinstance(files_raw, Mapping):
        raise CorruptPrivateCheckpointError("checkpoint frozen knowledge closure is invalid")
    decoded_files: dict[str, bytes] = {}
    try:
        for relative, encoded in files_raw.items():
            path_value = _validate_relative_path(relative)
            if not isinstance(encoded, str):
                raise ValueError("frozen file content is invalid")
            decoded_files[path_value] = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError) as exc:
        raise CorruptPrivateCheckpointError("checkpoint frozen file encoding is invalid") from exc
    checked_files = _validate_knowledge_closure(identity, state, manifest_raw, decoded_files)
    return VerifiedPrivateCheckpoint(
        path=path,
        manifest=manifest,
        state=state,
        knowledge_snapshot_manifest=MappingProxyType(dict(manifest_raw)),
        frozen_files=MappingProxyType(checked_files),
    )


def _manifest_from_bytes(raw: bytes) -> PrivateCheckpointManifest:
    value = _decode_canonical_json(raw, filename=CHECKPOINT_MANIFEST_FILENAME)
    if set(value) != {
        "checkpoint_id",
        "created_at",
        "identity",
        "payload_sha256",
        "payload_size",
        "schema_version",
    }:
        raise CorruptPrivateCheckpointError("checkpoint manifest fields are invalid")
    checkpoint_id = value.get("checkpoint_id")
    created_at = value.get("created_at")
    digest = value.get("payload_sha256")
    size = value.get("payload_size")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or not isinstance(checkpoint_id, str)
        or _CHECKPOINT_ID_RE.fullmatch(checkpoint_id) is None
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or checkpoint_id != f"checkpoint-{digest}"
        or type(size) is not int
        or not 0 < size <= MAX_CHECKPOINT_BYTES
    ):
        raise CorruptPrivateCheckpointError("checkpoint manifest values are invalid")
    return PrivateCheckpointManifest(
        checkpoint_id=checkpoint_id,
        identity=_identity_from_payload(value.get("identity")),
        created_at=created_at,
        payload_sha256=digest,
        payload_size=size,
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_checkpoint_directory(
    checkpoints_root: Path,
    checkpoint_id: str,
    manifest_data: bytes,
    payload: bytes,
) -> str:
    """Write one content-addressed checkpoint and publish it with one rename."""

    game_root = checkpoints_root.parent.parent
    private_root = checkpoints_root.parent
    _assert_regular_directory(game_root)
    _assert_private_directory(private_root, create=True)
    _assert_private_directory(checkpoints_root, create=True)
    destination = checkpoints_root / checkpoint_id
    staging: Path | None = None
    try:
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".tmp-checkpoint-{os.getpid()}-",
                dir=checkpoints_root,
            )
        )
        if hasattr(os, "chmod"):
            staging.chmod(0o700)
        _write_exclusive(staging / CHECKPOINT_PAYLOAD_FILENAME, payload)
        _write_exclusive(staging / CHECKPOINT_MANIFEST_FILENAME, manifest_data)
        _fsync_directory(staging)
        try:
            os.rename(staging, destination)
        except OSError:
            if not destination.exists() and not destination.is_symlink():
                raise
        _fsync_directory(checkpoints_root)
        return checkpoint_id
    finally:
        if staging is not None and (staging.exists() or staging.is_symlink()):
            shutil.rmtree(staging, ignore_errors=True)


async def _shielded_to_thread(function: Callable[..., object], *args: object) -> object:
    """Wait for a writer thread to finish before propagating cancellation."""

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            pass
        raise


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _cleanup_temp_sync(private_root: Path, checkpoints_root: Path) -> tuple[str, ...]:
    _assert_private_directory(private_root, create=False)
    _assert_private_directory(checkpoints_root, create=False)
    removed: list[str] = []
    for child in checkpoints_root.iterdir():
        if not child.name.startswith(".tmp-checkpoint-"):
            continue
        match = re.fullmatch(r"\.tmp-checkpoint-([1-9][0-9]*)-[A-Za-z0-9_-]+", child.name, re.ASCII)
        if match is None:
            continue
        if child.is_symlink() or not child.is_dir():
            raise CorruptPrivateCheckpointError("temporary checkpoint entry is unsafe")
        if _process_is_alive(int(match.group(1))):
            continue
        shutil.rmtree(child)
        removed.append(child.name)
    return tuple(sorted(removed))


class PrivateCheckpointStore:
    """Store and verify private choices below one active game's private directory."""

    def __init__(self, game_root: str | os.PathLike[str]) -> None:
        supplied = Path(game_root).expanduser()
        if supplied.is_symlink():
            raise CorruptPrivateCheckpointError("game root may not be a symlink")
        self.game_root = supplied.resolve()
        self.private_root = self.game_root / "private"
        self.checkpoints_root = self.private_root / CHECKPOINT_DIRECTORY

    async def create(
        self,
        state: GameState,
        *,
        manager: GameManager,
        knowledge_snapshot_manifest: Mapping[str, object],
        frozen_files: Mapping[str, bytes],
        identity: PrivateCheckpointIdentity | None = None,
        created_at: str,
    ) -> VerifiedPrivateCheckpoint:
        """Persist one verified ``WAITING_CHOICE`` state atomically."""

        from werewolf.game.manager import GameManager as _GameManager

        if not isinstance(manager, _GameManager):
            raise TypeError("manager must be the authoritative GameManager for this state")
        try:
            await manager.assert_private_checkpoint_waiting_choice(state)
        except ValueError as exc:
            raise PrivateCheckpointBoundaryError(
                "authoritative manager rejected the waiting-choice source proof"
            ) from exc
        manifest_identity = knowledge_snapshot_manifest.get("package_identity")
        package_payload = frozen_files.get("package.json")
        execution_raw = frozen_files.get("execution.json")
        if (
            not isinstance(manifest_identity, str)
            or package_payload is None
            or execution_raw is None
        ):
            raise PrivateCheckpointIdentityError(
                "frozen snapshot lacks its package identity artifacts"
            )
        try:
            decoded_package = _decode_canonical_json(package_payload, filename="package.json")
            decoded_execution = _decode_canonical_json(execution_raw, filename="execution.json")
            package_manifest = decoded_package.get("manifest")
        except (TypeError, ValueError) as exc:
            raise PrivateCheckpointIdentityError("frozen execution artifacts are invalid") from exc
        if not isinstance(package_manifest, Mapping):
            raise PrivateCheckpointIdentityError("frozen package has no logical manifest")
        execution_digest = _sha256(
            json.dumps(
                decoded_execution,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        actual_identity = _state_identity(
            state,
            execution_digest=execution_digest,
            package_identity=manifest_identity,
        )
        if (
            package_manifest.get("execution_sha256") != execution_digest
            or decoded_package.get("package_id") != actual_identity.package_id
        ):
            raise PrivateCheckpointIdentityError(
                "frozen execution manifest does not match its artifacts"
            )
        chosen_identity = actual_identity if identity is None else identity
        if chosen_identity != actual_identity:
            raise PrivateCheckpointIdentityError(
                "checkpoint identity differs from authoritative game state"
            )
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("created_at must be a non-empty timestamp")
        await _validate_detached_bundle(
            chosen_identity,
            state,
            knowledge_snapshot_manifest,
            frozen_files,
        )
        payload = _payload_bytes(chosen_identity, state, knowledge_snapshot_manifest, frozen_files)
        checkpoint_id, manifest_data = _manifest_bytes(
            identity=chosen_identity, payload=payload, created_at=created_at
        )
        await _shielded_to_thread(
            _create_checkpoint_directory,
            self.checkpoints_root,
            checkpoint_id,
            manifest_data,
            payload,
        )
        return await self.verify(checkpoint_id, expected_identity=chosen_identity)

    async def verify(
        self,
        checkpoint: str | os.PathLike[str],
        *,
        expected_identity: PrivateCheckpointIdentity,
    ) -> VerifiedPrivateCheckpoint:
        """Verify all bytes, permissions, path containment, and frozen identities."""

        path = self._resolve_checkpoint(checkpoint)
        _assert_regular_directory(self.game_root)
        _assert_private_directory(self.private_root, create=False)
        _assert_private_directory(self.checkpoints_root, create=False)
        _assert_private_directory(path, create=False)
        entries = {item.name for item in path.iterdir()}
        if entries != {CHECKPOINT_MANIFEST_FILENAME, CHECKPOINT_PAYLOAD_FILENAME}:
            raise CorruptPrivateCheckpointError(
                "checkpoint directory has missing or unexpected files"
            )
        raw_manifest = _assert_private_file(path / CHECKPOINT_MANIFEST_FILENAME)
        raw_payload = _assert_private_file(path / CHECKPOINT_PAYLOAD_FILENAME)
        manifest = _manifest_from_bytes(raw_manifest)
        if manifest.identity != expected_identity:
            raise PrivateCheckpointIdentityError(
                "checkpoint belongs to another game or frozen package"
            )
        if (
            path.name != manifest.checkpoint_id
            or len(raw_payload) != manifest.payload_size
            or _sha256(raw_payload) != manifest.payload_sha256
        ):
            raise CorruptPrivateCheckpointError("checkpoint payload hash or location is invalid")
        verified = _decode_payload(
            raw_payload,
            path=path,
            manifest=manifest,
            expected_identity=expected_identity,
        )
        await _validate_detached_bundle(
            verified.manifest.identity,
            verified.state,
            verified.knowledge_snapshot_manifest,
            verified.frozen_files,
        )
        return verified

    async def cleanup_temp(self) -> tuple[str, ...]:
        """Remove only abandoned private checkpoint staging directories."""

        return cast(
            tuple[str, ...],
            await asyncio.to_thread(_cleanup_temp_sync, self.private_root, self.checkpoints_root),
        )

    def _resolve_checkpoint(self, checkpoint: str | os.PathLike[str]) -> Path:
        candidate = Path(checkpoint)
        if candidate.is_absolute():
            path = candidate
        else:
            value = os.fspath(checkpoint)
            if _CHECKPOINT_ID_RE.fullmatch(value) is None:
                raise CorruptPrivateCheckpointError(
                    "checkpoint selector must be a canonical checkpoint id"
                )
            path = self.checkpoints_root / value
        if path.is_symlink():
            raise CorruptPrivateCheckpointError("checkpoint path may not be a symlink")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.checkpoints_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise CorruptPrivateCheckpointError(
                "checkpoint path escapes the private checkpoint root"
            ) from exc
        if resolved != path.absolute():
            raise CorruptPrivateCheckpointError("checkpoint path contains a symbolic link")
        return resolved


__all__ = [
    "CHECKPOINT_DIRECTORY",
    "CHECKPOINT_MANIFEST_FILENAME",
    "CHECKPOINT_PAYLOAD_FILENAME",
    "CHECKPOINT_SCHEMA_VERSION",
    "CorruptPrivateCheckpointError",
    "MAX_CHECKPOINT_BYTES",
    "PrivateCheckpointBoundaryError",
    "PrivateCheckpointError",
    "PrivateCheckpointIdentity",
    "PrivateCheckpointIdentityError",
    "PrivateCheckpointManifest",
    "PrivateCheckpointStore",
    "VerifiedPrivateCheckpoint",
]
