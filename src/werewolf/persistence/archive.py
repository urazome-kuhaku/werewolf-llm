"""Immutable end-of-game archives and conservative recovery checks.

The snapshot store owns complete-cycle snapshots.  This module owns the
separate ``games/archive`` lifecycle: it verifies the final snapshot, copies
the active game directory into a staging directory, records a complete file
inventory, and publishes the result with one same-volume rename.  The active
directory is deliberately left in place.

Recovery is intentionally conservative.  A durable snapshot can prove the
game state, ruleset and persisted session epochs.  It cannot prove where a
running Pi process stopped unless the caller supplies a matching runtime
probe.  In that case the service returns ``rebuild-sessions`` rather than
guessing that an old Session is safe to continue.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast

from werewolf.game.state import GameState

from .atomic import atomic_write_bytes, resolve_contained_path
from .snapshot import (
    SNAPSHOT_MANIFEST_FILENAME,
    CorruptSnapshotError,
    GameSnapshotStore,
    SnapshotManifest,
    _manifest_from_bytes,
)

ARCHIVE_SCHEMA_VERSION: Final[Literal[1]] = 1
ARCHIVE_MANIFEST_FILENAME: Final[str] = "archive_manifest.json"
ArchiveDecision = Literal["resume", "rebuild-sessions", "abandon"]

_ARCHIVE_NAME_RE = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6}Z)_(?P<game>[a-z0-9][a-z0-9_-]{0,63})$",
    re.ASCII,
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SECRET_NAME_RE = re.compile(
    r"(?i)(?:^|[._-])(?:\.env|api[_-]?key|access[_-]?token|bearer[_-]?token|"
    r"client[_-]?secret|password|credential|private[_-]?key|secret)(?:$|[._-])"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:[\"']?)(?:api[_-]?key|access[_-]?token|bearer[_-]?token|token|"
    r"client[_-]?secret|password|credential|private[_-]?key|secret)"
    r"(?:[\"']?\s*[:=]\s*)(?:[\"']?)(?![\"']?\s*(?:null|none)\b)[^\s,}\"']+"
)
_ENV_REFERENCE_RE = re.compile(r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z_][A-Za-z0-9_]*%)")
_ARCHIVE_LOCKS: dict[Path, asyncio.Lock] = {}

# The active directory is a durable projection, not a general purpose
# runtime directory.  Keep this allow-list here (instead of relying on the
# secret scanner) so a new private/runtime artifact cannot silently become
# part of an immutable archive.
_ACTIVE_ROOT_FILES = frozenset({"state.json", "public.md"})
_ACTIVE_ROOT_DIRECTORIES = frozenset({"private", "ruleset", "snapshots"})
# The Pi adapter keeps live process/session state here.  It is an explicit
# active-directory exclusion: validate only that the path is a real directory
# and never recurse into or copy its contents.
_ACTIVE_EXCLUDED_DIRECTORIES = frozenset({".runtime"})
_ACTIVE_PRIVATE_FILES = frozenset({"private/gm.md", "private/runtime_refs.json"})
_ACTIVE_PRIVATE_CHANNEL_FILES = frozenset({"private/channels/wolves.md"})
_RULESET_SNAPSHOT_FILENAME: Final[str] = "snapshot.json"


class GameArchiveError(ValueError):
    """Base error for archive creation, verification or recovery checks."""


class ArchiveAlreadyExistsError(GameArchiveError):
    """Raised when an immutable timestamped archive already exists."""


class ArchiveSecurityError(GameArchiveError):
    """Raised when an archive would contain credentials or unsafe links."""


class CorruptArchiveError(GameArchiveError):
    """Raised when an archive or its final snapshot cannot be verified."""


class ArchiveInputError(GameArchiveError):
    """Raised when an active game has no usable final snapshot."""


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    """One copied archive file and its content digest."""

    relative_path: str
    sha256: str
    size: int

    @property
    def path(self) -> str:
        """Compatibility spelling for callers that use ``path``."""

        return self.relative_path


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    """Canonical metadata stored at the archive root."""

    schema_version: Literal[1]
    archive_id: str
    game_id: str
    archived_at: str
    final_snapshot_path: str
    final_snapshot_id: str
    final_snapshot_manifest_sha256: str
    final_snapshot_revision: int
    final_state_revision: int
    file_count: int
    total_size: int
    files: tuple[ArchiveFile, ...]

    @property
    def manifest_sha256(self) -> str:
        """Digest of the canonical archive manifest bytes."""

        return _sha256(_canonical_json_bytes(_archive_manifest_payload(self)))


@dataclass(frozen=True, slots=True)
class GameArchive:
    """Result of one successfully published game archive."""

    path: Path
    manifest: ArchiveManifest

    @property
    def archive_id(self) -> str:
        return self.manifest.archive_id

    @property
    def game_id(self) -> str:
        return self.manifest.game_id


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    """Evidence-backed decision for reconnecting or rebuilding Sessions."""

    decision: ArchiveDecision
    reason: str
    game_id: str
    snapshot_id: str | None
    checked_seats: tuple[int, ...] = ()

    @property
    def can_resume(self) -> bool:
        return self.decision == "resume"


ArchiveResult = GameArchive


def _archive_lock(root: Path) -> asyncio.Lock:
    return _ARCHIVE_LOCKS.setdefault(root, asyncio.Lock())


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


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


def _validate_relative_path(value: object, *, field_name: str = "archive path") -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CorruptArchiveError(f"{field_name} must be a normalized relative POSIX path")
    path = Path(value)
    if path.is_absolute() or value.startswith("/") or ".." in path.parts:
        raise CorruptArchiveError(f"{field_name} must be a normalized relative path")
    normalized = value.replace("\\", "/")
    if normalized != value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise CorruptArchiveError(f"{field_name} must be a normalized relative path")
    return value


def _archive_manifest_payload(manifest: ArchiveManifest) -> dict[str, object]:
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "archive_id": manifest.archive_id,
        "game_id": manifest.game_id,
        "archived_at": manifest.archived_at,
        "final_snapshot": {
            "path": manifest.final_snapshot_path,
            "snapshot_id": manifest.final_snapshot_id,
            "manifest_sha256": manifest.final_snapshot_manifest_sha256,
            "snapshot_revision": manifest.final_snapshot_revision,
            "state_revision": manifest.final_state_revision,
        },
        "file_count": manifest.file_count,
        "total_size": manifest.total_size,
        "files": [
            {"path": item.relative_path, "sha256": item.sha256, "size": item.size}
            for item in manifest.files
        ],
    }


def _manifest_bytes(manifest: ArchiveManifest) -> bytes:
    return _canonical_json_bytes(_archive_manifest_payload(manifest))


def _parse_archive_manifest(raw: bytes) -> ArchiveManifest:
    if b"\r" in raw or not raw.endswith(b"\n"):
        raise CorruptArchiveError("archive manifest is not canonical UTF-8 JSON")
    try:
        payload = json.loads(
            raw[:-1].decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptArchiveError("archive manifest is not valid JSON") from exc
    if _canonical_json_bytes(payload) != raw:
        raise CorruptArchiveError("archive manifest is not canonical JSON")
    if not isinstance(payload, dict) or payload.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise CorruptArchiveError("unsupported archive manifest schema")
    required = {
        "schema_version",
        "archive_id",
        "game_id",
        "archived_at",
        "final_snapshot",
        "file_count",
        "total_size",
        "files",
    }
    if set(payload) != required:
        raise CorruptArchiveError("archive manifest fields are invalid")
    archive_id = payload.get("archive_id")
    game_id = payload.get("game_id")
    archived_at = payload.get("archived_at")
    if (
        not isinstance(archive_id, str)
        or _ARCHIVE_NAME_RE.fullmatch(archive_id) is None
        or not isinstance(game_id, str)
        or not isinstance(archived_at, str)
    ):
        raise CorruptArchiveError("archive manifest identity fields are invalid")
    final_raw = payload.get("final_snapshot")
    if not isinstance(final_raw, dict) or set(final_raw) != {
        "path",
        "snapshot_id",
        "manifest_sha256",
        "snapshot_revision",
        "state_revision",
    }:
        raise CorruptArchiveError("archive final snapshot metadata is invalid")
    final_path = _validate_relative_path(final_raw.get("path"), field_name="final snapshot path")
    snapshot_id = final_raw.get("snapshot_id")
    digest = final_raw.get("manifest_sha256")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise CorruptArchiveError("final snapshot ID is invalid")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise CorruptArchiveError("final snapshot manifest digest is invalid")
    revisions: list[int] = []
    for key in ("snapshot_revision", "state_revision"):
        value = final_raw.get(key)
        if type(value) is not int or value < 0:
            raise CorruptArchiveError("final snapshot revisions are invalid")
        revisions.append(value)
    file_count = payload.get("file_count")
    total_size = payload.get("total_size")
    files_raw = payload.get("files")
    if (
        type(file_count) is not int
        or file_count < 1
        or type(total_size) is not int
        or total_size < 0
    ):
        raise CorruptArchiveError("archive file counters are invalid")
    if not isinstance(files_raw, list) or len(files_raw) != file_count:
        raise CorruptArchiveError("archive file inventory is incomplete")
    files: list[ArchiveFile] = []
    previous: str | None = None
    for entry in files_raw:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            raise CorruptArchiveError("invalid archive file entry")
        path = _validate_relative_path(entry.get("path"), field_name="archive file path")
        digest = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise CorruptArchiveError("invalid archive file digest")
        if type(size) is not int or size < 0:
            raise CorruptArchiveError("invalid archive file size")
        if path == ARCHIVE_MANIFEST_FILENAME or path in {item.relative_path for item in files}:
            raise CorruptArchiveError("archive manifest contains a duplicate or reserved path")
        if previous is not None and path <= previous:
            raise CorruptArchiveError("archive file inventory is not sorted and unique")
        previous = path
        files.append(ArchiveFile(path, digest, size))
    if sum(item.size for item in files) != total_size:
        raise CorruptArchiveError("archive total size does not match its file inventory")
    return ArchiveManifest(
        schema_version=ARCHIVE_SCHEMA_VERSION,
        archive_id=archive_id,
        game_id=game_id,
        archived_at=archived_at,
        final_snapshot_path=final_path,
        final_snapshot_id=snapshot_id,
        final_snapshot_manifest_sha256=cast(str, final_raw["manifest_sha256"]),
        final_snapshot_revision=revisions[0],
        final_state_revision=revisions[1],
        file_count=file_count,
        total_size=total_size,
        files=tuple(files),
    )


def _assert_safe_content(path: Path, data: bytes) -> None:
    """Reject credentials and environment substitutions before publication."""

    for component in path.parts:
        if component.casefold() in {".env", ".env.local", ".env.production"}:
            raise ArchiveSecurityError("archive contains an environment file")
        if _SECRET_NAME_RE.search(component):
            raise ArchiveSecurityError("archive contains a credential-shaped filename")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    if _SECRET_ASSIGNMENT_RE.search(text) or _ENV_REFERENCE_RE.search(text):
        raise ArchiveSecurityError("archive contains a credential or environment value")


def _snapshot_expected_paths(path: Path, manifest: SnapshotManifest) -> set[str]:
    """Return the complete file set permitted by one snapshot manifest."""

    return {item.relative_path for item in manifest.files} | {SNAPSHOT_MANIFEST_FILENAME}


def _assert_snapshot_layout(path: Path, manifest: SnapshotManifest) -> None:
    """Reject files or directories which are outside the formal snapshot."""

    expected_files = _snapshot_expected_paths(path, manifest)
    actual_files = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    if actual_files != expected_files:
        raise ArchiveSecurityError("snapshot contains an unapproved file")
    expected_dirs = {
        part
        for relative in expected_files
        for index in range(1, len(Path(relative).parts))
        for part in (Path(*Path(relative).parts[:index]).as_posix(),)
    }
    actual_dirs = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_dir()}
    if actual_dirs != expected_dirs:
        raise ArchiveSecurityError("snapshot contains an unapproved directory")


def _source_files(root: Path) -> list[tuple[str, Path]]:
    """List only the durable active projections allowed by §14.1/§14.3.

    Every snapshot is independently verified before its files are copied.  A
    staging directory, model transcript, extension cache or other future
    private file therefore fails closed instead of being included by a broad
    recursive copy.
    """

    if root.is_symlink() or not root.is_dir():
        raise ArchiveInputError("active game directory must be a real directory")
    for item in root.iterdir():
        if item.is_symlink():
            raise ArchiveSecurityError("active game directory contains a symlink")
        if item.name in _ACTIVE_EXCLUDED_DIRECTORIES:
            if not item.is_dir():
                raise ArchiveSecurityError(f"active excluded path is not a directory: {item.name}")
            continue
        if item.name in _ACTIVE_ROOT_FILES and not item.is_file():
            raise ArchiveSecurityError(f"active path is not a file: {item.name}")
        if item.name in _ACTIVE_ROOT_DIRECTORIES and not item.is_dir():
            raise ArchiveSecurityError(f"active path is not a directory: {item.name}")
        if item.name not in _ACTIVE_ROOT_FILES | _ACTIVE_ROOT_DIRECTORIES:
            raise ArchiveSecurityError(
                f"active game directory contains an unapproved path: {item.name}"
            )

    result: list[tuple[str, Path]] = []
    for relative in sorted(_ACTIVE_ROOT_FILES):
        source = root / relative
        if not source.is_file():
            raise ArchiveInputError(f"active game projection is missing: {relative}")
        result.append((relative, source))

    private = root / "private"
    if private.exists():
        for item in private.rglob("*"):
            if item.is_symlink():
                raise ArchiveSecurityError("active private projection contains a symlink")
            if item.is_dir():
                relative_dir = item.relative_to(root).as_posix()
                if relative_dir not in {"private", "private/channels"}:
                    raise ArchiveSecurityError(
                        "active private projection contains an unapproved directory: "
                        f"{relative_dir}"
                    )
                continue
            relative = item.relative_to(root).as_posix()
            if relative not in _ACTIVE_PRIVATE_FILES | _ACTIVE_PRIVATE_CHANNEL_FILES:
                raise ArchiveSecurityError(
                    f"active private projection contains an unapproved file: {relative}"
                )
            result.append((relative, item))
        for required in _ACTIVE_PRIVATE_FILES | _ACTIVE_PRIVATE_CHANNEL_FILES:
            candidate = root / required
            if candidate.exists() and not candidate.is_file():
                raise ArchiveSecurityError(f"active private projection is not a file: {required}")

    ruleset = root / "ruleset"
    if ruleset.exists():
        for item in ruleset.rglob("*"):
            if item.is_symlink():
                raise ArchiveSecurityError("active ruleset projection contains a symlink")
            if item.is_dir():
                continue
            relative = item.relative_to(root).as_posix()
            result.append((relative, item))

    snapshots = root / "snapshots"
    snapshot_entries = sorted(snapshots.iterdir())
    if any(item.is_symlink() or not item.is_dir() for item in snapshot_entries):
        raise ArchiveSecurityError("active snapshots contain an unapproved path")
    snapshot_dirs = snapshot_entries
    if not snapshot_dirs:
        raise ArchiveInputError("active game has no formal snapshots")
    for snapshot_dir in snapshot_dirs:
        if snapshot_dir.is_symlink():
            raise ArchiveSecurityError("active snapshots contain a symlink")
        manifest, _ = _snapshot_manifest(snapshot_dir)
        _assert_snapshot_layout(snapshot_dir, manifest)
        for relative in sorted(_snapshot_expected_paths(snapshot_dir, manifest)):
            result.append((f"snapshots/{snapshot_dir.name}/{relative}", snapshot_dir / relative))

    if not result:
        raise ArchiveInputError("active game directory is empty")
    return result


def _assert_archive_projection_paths(path: Path, manifest: ArchiveManifest) -> None:
    """Apply the active projection allow-list to an already-built archive."""

    for item in path.iterdir():
        if item.is_dir() and item.name not in _ACTIVE_ROOT_DIRECTORIES:
            raise CorruptArchiveError(f"archive contains an unapproved directory: {item.name}")

    for relative in (item.relative_path for item in manifest.files):
        parts = Path(relative).parts
        if relative in _ACTIVE_ROOT_FILES:
            continue
        if relative in _ACTIVE_PRIVATE_FILES | _ACTIVE_PRIVATE_CHANNEL_FILES:
            continue
        if parts and parts[0] == "ruleset" and len(parts) >= 2:
            continue
        if len(parts) >= 3 and parts[0] == "snapshots":
            continue
        raise CorruptArchiveError(f"archive contains an unapproved path: {relative}")

    snapshots = path / "snapshots"
    if not snapshots.is_dir():
        raise CorruptArchiveError("archive snapshots directory is missing")
    entries = sorted(snapshots.iterdir())
    if any(item.is_symlink() or not item.is_dir() for item in entries):
        raise CorruptArchiveError("archive snapshots contain an unapproved path")
    if not entries:
        raise CorruptArchiveError("archive has no formal snapshots")
    for snapshot_dir in entries:
        snapshot_manifest, _ = _snapshot_manifest(snapshot_dir)
        _assert_snapshot_layout(snapshot_dir, snapshot_manifest)


def _read_state(path: Path) -> GameState:
    try:
        return GameState.model_validate_json(path.read_bytes())
    except Exception as exc:
        raise CorruptArchiveError("active or archived state.json failed schema validation") from exc


def _snapshot_manifest(path: Path) -> tuple[SnapshotManifest, bytes]:
    manifest_path = path / SNAPSHOT_MANIFEST_FILENAME
    try:
        raw = manifest_path.read_bytes()
        manifest = _manifest_from_bytes(raw)
    except (OSError, CorruptSnapshotError, ValueError) as exc:
        raise ArchiveInputError("final snapshot manifest is unavailable or invalid") from exc
    try:
        GameSnapshotStore._verify_directory_sync(path, manifest.game_id, _sha256(raw))
    except Exception as exc:
        raise CorruptArchiveError("final snapshot failed integrity verification") from exc
    return manifest, raw


def _last_snapshot_matches(
    state: GameState, snapshot: SnapshotManifest, manifest_raw: bytes
) -> None:
    reference = state.last_snapshot
    if not isinstance(reference, Mapping):
        raise ArchiveInputError("active state has no last_snapshot reference")
    expected = {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_revision": snapshot.snapshot_revision,
        "state_revision": snapshot.state_revision,
        "created_at": snapshot.created_at,
        "manifest_sha256": _sha256(manifest_raw),
    }
    for key, value in expected.items():
        if reference.get(key) != value:
            raise CorruptArchiveError(f"active last_snapshot does not match final snapshot: {key}")


def _state_without_snapshot_reference(state: GameState) -> dict[str, object]:
    payload = state.model_dump(mode="json")
    payload.pop("last_snapshot", None)
    return payload


def _assert_runtime_refs_match_state(path: Path, state: GameState) -> None:
    """Ensure the persisted runtime references describe exactly this state."""

    rows = _runtime_rows(path)
    expected_seats = set(state.players)
    seen: set[int] = set()
    for row in rows:
        if set(row) != {
            "seat",
            "runtime_ref",
            "session_epoch",
            "confirmed_event_cursor",
            "delivery_cursor",
        }:
            raise CorruptArchiveError("runtime_refs.json contains unknown fields")
        seat = row.get("seat")
        if type(seat) is not int or seat in seen or seat not in expected_seats:
            raise CorruptArchiveError("runtime_refs.json has duplicate or invalid seats")
        seen.add(seat)
        player = state.players[seat]
        cursor = state.delivery_cursors.get(seat)
        expected = {
            "seat": seat,
            "runtime_ref": player.runtime_ref,
            "session_epoch": player.session_epoch,
            "confirmed_event_cursor": player.confirmed_event_cursor,
            "delivery_cursor": cursor.model_dump(mode="json") if cursor is not None else None,
        }
        if dict(row) != expected:
            raise CorruptArchiveError(f"runtime_refs.json does not match state at seat {seat}")
    if seen != expected_seats:
        raise CorruptArchiveError("runtime_refs.json does not cover every player")


def _assert_active_projections(
    active: Path,
    active_state: GameState,
    final_snapshot_path: Path,
    final_snapshot: SnapshotManifest,
) -> None:
    """Verify active projections are derived from the selected final snapshot."""

    try:
        active_public = (active / "public.md").read_bytes()
        snapshot_public = (final_snapshot_path / "public.md").read_bytes()
    except OSError as exc:
        raise CorruptArchiveError("active or final public projection could not be read") from exc
    if active_public != snapshot_public:
        raise CorruptArchiveError("active public.md does not match the final snapshot")

    snapshot_state = _read_state(final_snapshot_path / "state.json")
    if _state_without_snapshot_reference(active_state) != _state_without_snapshot_reference(
        snapshot_state
    ):
        raise CorruptArchiveError(
            "active state does not match final snapshot rules or runtime refs"
        )
    if active_state.ruleset != snapshot_state.ruleset:
        raise CorruptArchiveError("active ruleset does not match the final snapshot")

    final_runtime_refs = final_snapshot_path / "private" / "runtime_refs.json"
    _assert_runtime_refs_match_state(final_runtime_refs, snapshot_state)

    optional_projections = {
        "private/gm.md": "private/gm.md",
        "private/channels/wolves.md": "private/channels/wolves.md",
    }
    for active_relative, snapshot_relative in optional_projections.items():
        active_path = active / active_relative
        if active_path.exists():
            try:
                if (
                    active_path.read_bytes()
                    != (final_snapshot_path / snapshot_relative).read_bytes()
                ):
                    raise CorruptArchiveError(
                        f"active {active_relative} does not match the final snapshot"
                    )
            except OSError as exc:
                raise CorruptArchiveError(
                    f"active projection could not be read: {active_relative}"
                ) from exc
    active_runtime_refs = active / "private" / "runtime_refs.json"
    if active_runtime_refs.exists():
        _assert_runtime_refs_match_state(active_runtime_refs, active_state)

    ruleset = active / "ruleset"
    if ruleset.exists():
        expected_digests = dict(final_snapshot.ruleset.file_digests or {})
        if not expected_digests:
            raise CorruptArchiveError("active ruleset projection has no verifiable frozen files")
        # A per-game knowledge snapshot contains the copied package files
        # plus its own immutable identity manifest.  The latter is not part
        # of ``FrozenRulesetSnapshot.file_digests`` because it describes that
        # detached ruleset, so add the state-bound manifest digest explicitly
        # before comparing the active projection inventory.
        expected_digests[_RULESET_SNAPSHOT_FILENAME] = final_snapshot.ruleset.manifest_sha256
        actual_files = {
            item.relative_to(ruleset).as_posix() for item in ruleset.rglob("*") if item.is_file()
        }
        if actual_files != set(expected_digests):
            raise CorruptArchiveError("active ruleset projection does not match the final snapshot")
        for relative, expected_digest in expected_digests.items():
            try:
                content = (ruleset / relative).read_bytes()
            except OSError as exc:
                raise CorruptArchiveError(
                    f"active ruleset file could not be read: {relative}"
                ) from exc
            if _sha256(content) != expected_digest:
                raise CorruptArchiveError(f"active ruleset file mismatch: {relative}")


def _resolve_active(root: Path, value: str | os.PathLike[str] | None) -> Path:
    active_root = root / "active"
    if value is None:
        return active_root.resolve()
    candidate = Path(value)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else resolve_contained_path(active_root, candidate)
    )
    try:
        resolved.relative_to(active_root.resolve())
    except ValueError as exc:
        raise ArchiveInputError("active game path escapes games/active") from exc
    return resolved


def _resolve_snapshot(active: Path, value: str | os.PathLike[str] | None, state: GameState) -> Path:
    snapshots = active / "snapshots"
    if value is not None:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = snapshots / candidate
        return candidate.resolve()
    reference = state.last_snapshot
    if not isinstance(reference, Mapping) or not isinstance(reference.get("snapshot_id"), str):
        raise ArchiveInputError("a final snapshot path is required when last_snapshot is absent")
    matches: list[Path] = []
    if snapshots.is_dir():
        for directory in snapshots.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest_path = directory / SNAPSHOT_MANIFEST_FILENAME
            try:
                manifest = _manifest_from_bytes(manifest_path.read_bytes())
            except Exception:
                continue
            if manifest.snapshot_id == reference["snapshot_id"]:
                matches.append(directory.resolve())
    if len(matches) != 1:
        raise ArchiveInputError("could not identify exactly one final snapshot")
    return matches[0]


def _validate_archive_directory(
    path: Path, manifest: ArchiveManifest, *, require_name: bool = True
) -> None:
    if path.is_symlink() or not path.is_dir():
        raise CorruptArchiveError("archive directory is unavailable")
    if require_name and path.name != manifest.archive_id:
        raise CorruptArchiveError("archive directory name does not match archive manifest")
    if any(item.is_symlink() for item in path.rglob("*")):
        raise CorruptArchiveError("archive contains a symlink")
    try:
        archive_manifest_raw = (path / ARCHIVE_MANIFEST_FILENAME).read_bytes()
    except OSError as exc:
        raise CorruptArchiveError("archive manifest could not be read") from exc
    _assert_safe_content(Path(ARCHIVE_MANIFEST_FILENAME), archive_manifest_raw)
    expected = {item.relative_path for item in manifest.files} | {ARCHIVE_MANIFEST_FILENAME}
    actual = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    if actual != expected:
        raise CorruptArchiveError("archive file set is incomplete")
    _assert_archive_projection_paths(path, manifest)
    for item in manifest.files:
        target = resolve_contained_path(path, item.relative_path)
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise CorruptArchiveError(
                f"archive file could not be read: {item.relative_path}"
            ) from exc
        _assert_safe_content(target.relative_to(path), data)
        if len(data) != item.size or _sha256(data) != item.sha256:
            raise CorruptArchiveError(f"archive hash mismatch: {item.relative_path}")
    state = _read_state(path / "state.json")
    if state.game_id != manifest.game_id:
        raise CorruptArchiveError("archive state game_id does not match its manifest")
    final_path = resolve_contained_path(path, manifest.final_snapshot_path)
    if not final_path.is_dir():
        raise CorruptArchiveError("archive final snapshot is missing")
    final_manifest, final_raw = _snapshot_manifest(final_path)
    if (
        final_manifest.game_id != manifest.game_id
        or final_manifest.snapshot_id != manifest.final_snapshot_id
        or _sha256(final_raw) != manifest.final_snapshot_manifest_sha256
        or final_manifest.snapshot_revision != manifest.final_snapshot_revision
        or final_manifest.state_revision != manifest.final_state_revision
    ):
        raise CorruptArchiveError("archive final snapshot metadata does not match")
    _last_snapshot_matches(state, final_manifest, final_raw)
    _assert_active_projections(path, state, final_path, final_manifest)


def _runtime_rows(path: Path) -> list[Mapping[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptArchiveError("runtime_refs.json is invalid") from exc
    if not isinstance(payload, list):
        raise CorruptArchiveError("runtime_refs.json must contain a list")
    rows: list[Mapping[str, object]] = []
    for row in payload:
        if not isinstance(row, Mapping):
            raise CorruptArchiveError("runtime_refs.json contains an invalid row")
        rows.append(row)
    return rows


class GameArchiveStore:
    """Create, verify and assess recovery for timestamped game archives."""

    def __init__(
        self,
        games_root: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        supplied_root = Path(games_root).resolve()
        if supplied_root.name.casefold() == "active":
            self.games_root = supplied_root.parent
            self.active_root = supplied_root
        else:
            self.games_root = supplied_root
            self.active_root = supplied_root / "active"
        self.archive_root = self.games_root / "archive"
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(
        self,
        active_game: str | os.PathLike[str],
        final_snapshot: str | os.PathLike[str] | None = None,
        *,
        archived_at: datetime | None = None,
    ) -> GameArchive:
        """Publish one verified active game as an immutable archive."""

        active = _resolve_active(self.games_root, active_game)
        state = _read_state(active / "state.json")
        if active.name != state.game_id:
            raise ArchiveInputError("active directory name does not match state.game_id")
        snapshot_path = _resolve_snapshot(active, final_snapshot, state)
        try:
            snapshot_path.relative_to((active / "snapshots").resolve())
        except ValueError as exc:
            raise ArchiveInputError("final snapshot must be inside active/snapshots") from exc
        snapshot, snapshot_raw = _snapshot_manifest(snapshot_path)
        if snapshot.game_id != state.game_id:
            raise CorruptArchiveError("final snapshot game_id does not match active state")
        _last_snapshot_matches(state, snapshot, snapshot_raw)
        _assert_active_projections(active, state, snapshot_path, snapshot)
        timestamp = self._clock() if archived_at is None else archived_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("archived_at must include a timezone")
        timestamp = timestamp.astimezone(UTC)
        archive_id = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{state.game_id}"
        destination = resolve_contained_path(self.archive_root, archive_id)
        source_files = _source_files(active)
        await asyncio.to_thread(self.archive_root.mkdir, parents=True, exist_ok=True)
        async with _archive_lock(self.archive_root):
            if destination.exists() or destination.is_symlink():
                raise ArchiveAlreadyExistsError(f"archive directory already exists: {archive_id}")
            staging = Path(
                await asyncio.to_thread(
                    tempfile.mkdtemp,
                    prefix=f".staging-{state.game_id}-",
                    dir=self.archive_root,
                )
            )
            committed = False
            try:
                copied: list[ArchiveFile] = []
                for relative, source in source_files:
                    data = await asyncio.to_thread(source.read_bytes)
                    _assert_safe_content(Path(relative), data)
                    target = resolve_contained_path(staging, relative)
                    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                    await atomic_write_bytes(target, data)
                    copied.append(ArchiveFile(relative, _sha256(data), len(data)))
                copied.sort(key=lambda item: item.relative_path)
                manifest = ArchiveManifest(
                    schema_version=ARCHIVE_SCHEMA_VERSION,
                    archive_id=archive_id,
                    game_id=state.game_id,
                    archived_at=timestamp.isoformat().replace("+00:00", "Z"),
                    final_snapshot_path=snapshot_path.relative_to(active).as_posix(),
                    final_snapshot_id=snapshot.snapshot_id,
                    final_snapshot_manifest_sha256=_sha256(snapshot_raw),
                    final_snapshot_revision=snapshot.snapshot_revision,
                    final_state_revision=snapshot.state_revision,
                    file_count=len(copied),
                    total_size=sum(item.size for item in copied),
                    files=tuple(copied),
                )
                await atomic_write_bytes(
                    resolve_contained_path(staging, ARCHIVE_MANIFEST_FILENAME),
                    _manifest_bytes(manifest),
                )
                await asyncio.to_thread(
                    _validate_archive_directory, staging, manifest, require_name=False
                )
                await asyncio.to_thread(os.rename, staging, destination)
                committed = True
            finally:
                if not committed:
                    await asyncio.to_thread(_remove_tree, staging)
            return GameArchive(path=destination, manifest=manifest)

    async def archive(
        self,
        active_game: str | os.PathLike[str],
        final_snapshot: str | os.PathLike[str] | None = None,
        *,
        archived_at: datetime | None = None,
    ) -> GameArchive:
        """Alias for :meth:`create`."""

        return await self.create(active_game, final_snapshot, archived_at=archived_at)

    async def finish(
        self,
        active_game: str | os.PathLike[str],
        final_snapshot: str | os.PathLike[str] | None = None,
        *,
        archived_at: datetime | None = None,
    ) -> GameArchive:
        """Lifecycle spelling used by a moderator finish operation."""

        return await self.create(active_game, final_snapshot, archived_at=archived_at)

    async def verify(self, archive: str | os.PathLike[str]) -> ArchiveManifest:
        """Verify archive inventory, hashes, snapshots and sensitive fields."""

        candidate = Path(archive)
        path = (
            candidate.resolve()
            if candidate.is_absolute()
            else resolve_contained_path(self.archive_root, candidate)
        )
        try:
            raw = await asyncio.to_thread((path / ARCHIVE_MANIFEST_FILENAME).read_bytes)
        except OSError as exc:
            raise CorruptArchiveError("archive manifest could not be read") from exc
        manifest = _parse_archive_manifest(raw)
        await asyncio.to_thread(_validate_archive_directory, path, manifest)
        return manifest

    async def assess_recovery(
        self,
        archive: str | os.PathLike[str],
        *,
        session_probe: Mapping[int, Mapping[str, object]] | None = None,
    ) -> RecoveryAssessment:
        """Judge whether archived Sessions can resume without guessing.

        ``session_probe`` is a trusted, live probe supplied by the runtime
        manager.  Each seat must report ``runtime_ref`` and ``session_epoch``;
        ``last_task_ref`` is required when a runtime exposes task progress.
        Missing probes result in ``rebuild-sessions``.  Malformed archive data
        results in ``abandon`` because state identity itself cannot be proved.
        """

        try:
            manifest = await self.verify(archive)
        except (GameArchiveError, OSError, ValueError) as exc:
            return RecoveryAssessment(
                "abandon", f"archive verification failed: {exc}", "unknown", None
            )
        path = (
            Path(archive).resolve()
            if Path(archive).is_absolute()
            else resolve_contained_path(self.archive_root, archive)
        )
        state = _read_state(path / "state.json")
        refs_path = path / manifest.final_snapshot_path / "private" / "runtime_refs.json"
        try:
            rows = _runtime_rows(refs_path)
        except CorruptArchiveError as exc:
            return RecoveryAssessment(
                "abandon", str(exc), state.game_id, manifest.final_snapshot_id
            )
        by_seat: dict[int, Mapping[str, object]] = {}
        for row in rows:
            seat = row.get("seat")
            if type(seat) is not int or seat in by_seat:
                return RecoveryAssessment(
                    "abandon",
                    "runtime_refs.json has duplicate or invalid seats",
                    state.game_id,
                    manifest.final_snapshot_id,
                )
            by_seat[seat] = row
        expected_seats = set(state.players)
        if set(by_seat) != expected_seats:
            return RecoveryAssessment(
                "abandon",
                "runtime_refs.json does not cover every player",
                state.game_id,
                manifest.final_snapshot_id,
            )
        persisted: dict[int, tuple[object, object]] = {}
        for seat, player in state.players.items():
            row = by_seat[seat]
            if (
                row.get("session_epoch") != player.session_epoch
                or row.get("runtime_ref") != player.runtime_ref
            ):
                return RecoveryAssessment(
                    "abandon",
                    f"persisted runtime reference mismatch at seat {seat}",
                    state.game_id,
                    manifest.final_snapshot_id,
                )
            persisted[seat] = (player.runtime_ref, player.session_epoch)
        if session_probe is None:
            return RecoveryAssessment(
                "rebuild-sessions",
                "live Session progress was not supplied; old Sessions cannot be resumed safely",
                state.game_id,
                manifest.final_snapshot_id,
                tuple(sorted(expected_seats)),
            )
        if set(session_probe) != expected_seats:
            return RecoveryAssessment(
                "rebuild-sessions",
                "live Session probe does not cover every player",
                state.game_id,
                manifest.final_snapshot_id,
            )
        for seat in sorted(expected_seats):
            probe = session_probe[seat]
            if not isinstance(probe, Mapping):
                return RecoveryAssessment(
                    "rebuild-sessions",
                    f"Session probe for seat {seat} is unavailable",
                    state.game_id,
                    manifest.final_snapshot_id,
                )
            if (
                probe.get("runtime_ref") != persisted[seat][0]
                or probe.get("session_epoch") != persisted[seat][1]
            ):
                return RecoveryAssessment(
                    "rebuild-sessions",
                    f"Session identity mismatch at seat {seat}",
                    state.game_id,
                    manifest.final_snapshot_id,
                )
            if "last_task_ref" in probe and probe["last_task_ref"] not in {
                None,
                state.players[seat].current_request_id,
            }:
                return RecoveryAssessment(
                    "rebuild-sessions",
                    f"Session task progress mismatch at seat {seat}",
                    state.game_id,
                    manifest.final_snapshot_id,
                )
        return RecoveryAssessment(
            "resume",
            "ruleset, last_snapshot, runtime references and live Session probes agree",
            state.game_id,
            manifest.final_snapshot_id,
            tuple(sorted(expected_seats)),
        )

    async def check_consistency(
        self,
        archive: str | os.PathLike[str],
        *,
        session_probe: Mapping[int, Mapping[str, object]] | None = None,
    ) -> RecoveryAssessment:
        """Compatibility alias for :meth:`assess_recovery`."""

        return await self.assess_recovery(archive, session_probe=session_probe)

    async def verify_archive(self, archive: str | os.PathLike[str]) -> ArchiveManifest:
        """Compatibility alias for :meth:`verify`."""

        return await self.verify(archive)

    async def assess_consistency(
        self,
        archive: str | os.PathLike[str],
        *,
        session_probe: Mapping[int, Mapping[str, object]] | None = None,
    ) -> RecoveryAssessment:
        """Compatibility alias for :meth:`assess_recovery`."""

        return await self.assess_recovery(archive, session_probe=session_probe)


def _remove_tree(path: Path) -> None:
    if path.exists() or path.is_symlink():
        shutil.rmtree(path)


ArchiveStore = GameArchiveStore
GameArchiveService = GameArchiveStore
ArchiveService = GameArchiveStore


__all__ = [
    "ARCHIVE_MANIFEST_FILENAME",
    "ARCHIVE_SCHEMA_VERSION",
    "ArchiveAlreadyExistsError",
    "ArchiveDecision",
    "ArchiveFile",
    "ArchiveInputError",
    "ArchiveManifest",
    "ArchiveResult",
    "ArchiveService",
    "ArchiveSecurityError",
    "ArchiveStore",
    "CorruptArchiveError",
    "GameArchive",
    "GameArchiveError",
    "GameArchiveService",
    "GameArchiveStore",
    "RecoveryAssessment",
]
