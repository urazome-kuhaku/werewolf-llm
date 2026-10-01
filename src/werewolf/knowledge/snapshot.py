"""Immutable per-game snapshots of verified compiled knowledge packages.

The compiled store is the only source consulted while a snapshot is created.
After publication the ruleset directory is self-contained: loading it only
reads the directory and verifies its hashes.  In particular, a damaged
snapshot never falls back to a newer package in the Vault.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from werewolf.persistence import PathSecurityError, resolve_contained_path

from .compiled_store import CompiledKnowledgePackageLoad, CompiledKnowledgeStore
from .refs import VersionedRef

SNAPSHOT_SCHEMA_VERSION: Final = 1
DEFAULT_COMPILER_VERSION: Final = "knowledge-compiler/1"
SNAPSHOT_FILENAME: Final = "snapshot.json"
_GAME_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_JSON_KWARGS: Final[dict[str, object]] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}
_SNAPSHOT_LOCKS: dict[Path, asyncio.Lock] = {}


class KnowledgeSnapshotError(ValueError):
    """Base error for snapshot creation and verification failures."""


class KnowledgeSnapshotPathError(PathSecurityError, KnowledgeSnapshotError):
    """Raised when a game identifier or snapshot path is unsafe."""


class SnapshotAlreadyExistsError(KnowledgeSnapshotError):
    """Raised when a game already has a different immutable ruleset."""


class SnapshotNotFoundError(FileNotFoundError, KnowledgeSnapshotError):
    """Raised when no snapshot has been published for a game."""


class CorruptKnowledgeSnapshotError(KnowledgeSnapshotError):
    """Raised when a snapshot is incomplete, tampered with, or malformed."""


# Short aliases keep call sites readable while retaining the explicit class names.
CorruptSnapshotError = CorruptKnowledgeSnapshotError
SnapshotAlreadyExists = SnapshotAlreadyExistsError


def _canonical_json(value: object) -> str:
    return json.dumps(value, **cast(Any, _JSON_KWARGS))


def _canonical_json_bytes(value: object) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


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


def _decode_canonical_json(raw: bytes, *, filename: str) -> Mapping[str, object]:
    if b"\r" in raw or not raw.endswith(b"\n"):
        raise CorruptKnowledgeSnapshotError(f"{filename} is not canonical UTF-8 JSON")
    try:
        text = raw[:-1].decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CorruptKnowledgeSnapshotError(f"{filename} is not valid JSON") from exc
    if not isinstance(value, dict) or _canonical_json(value) != text:
        raise CorruptKnowledgeSnapshotError(f"{filename} is not canonical JSON")
    return cast(Mapping[str, object], value)


def _string_field(value: Mapping[str, object], name: str, *, filename: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise CorruptKnowledgeSnapshotError(f"{filename}.{name} must be a non-empty string")
    return result


def _validate_game_id(game_id: str) -> str:
    if not isinstance(game_id, str) or _GAME_ID_RE.fullmatch(game_id) is None:
        raise KnowledgeSnapshotPathError(
            "game_id must be 1-64 lowercase ASCII letters, digits, '-' or '_'"
        )
    return game_id


def _snapshot_identity(
    *,
    game_id: str,
    package_id: str,
    board_ref: str,
    package_identity: str,
    compiler_version: str,
    file_digests: Mapping[str, str],
) -> dict[str, object]:
    return {
        "board_ref": board_ref,
        "compiler_version": compiler_version,
        "files": [{"path": path, "sha256": file_digests[path]} for path in sorted(file_digests)],
        "game_id": game_id,
        "package_id": package_id,
        "package_identity": package_identity,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
    }


def _snapshot_id(identity: Mapping[str, object]) -> str:
    return f"ruleset-{_sha256(_canonical_json(identity).encode('utf-8'))}"


def _normalize_created_at(value: datetime | str | None) -> str:
    if value is None:
        current = datetime.now(UTC)
    elif isinstance(value, datetime):
        current = value
    elif isinstance(value, str):
        current_text = value
        if current_text.endswith("Z"):
            current_text = current_text[:-1] + "+00:00"
        try:
            current = datetime.fromisoformat(current_text)
        except ValueError as exc:
            raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    else:
        raise TypeError("created_at must be datetime, ISO-8601 string, or None")
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_regular_file(path: Path, *, error_type: type[Exception]) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise error_type(f"snapshot entry is not a regular file: {path.name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise error_type(f"snapshot entry could not be read: {path.name}") from exc


def _write_exclusive(path: Path, data: bytes) -> None:
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _remove_tree(path: Path) -> None:
    if path.exists() or path.is_symlink():
        shutil.rmtree(path)


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise KnowledgeSnapshotPathError(f"snapshot parent is not a regular directory: {path}")


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshot:
    """Verified metadata and location for one immutable game ruleset."""

    game_id: str
    snapshot_id: str
    package_id: str
    board_ref: VersionedRef
    package_identity: str
    compiler_version: str
    created_at: str
    root: Path
    file_digests: Mapping[str, str]
    manifest_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        object.__setattr__(self, "file_digests", MappingProxyType(dict(self.file_digests)))

    @property
    def path(self) -> Path:
        return self.root

    @property
    def ruleset_path(self) -> Path:
        return self.root

    @property
    def board_ref_text(self) -> str:
        return self.board_ref.format()


class KnowledgeSnapshotBuilder:
    """Create and verify immutable ruleset directories for active games.

    ``root`` is normally the ``games`` directory and snapshots are written to
    ``root/active/<game_id>/ruleset``.  A caller that already owns a single
    game directory can use ``root_kind="game"`` (or
    :meth:`from_game_root`) and the target becomes ``root/ruleset``.
    """

    def __init__(
        self,
        compiled_store: CompiledKnowledgeStore,
        root: str | os.PathLike[str],
        *,
        root_kind: Literal["games", "game"] = "games",
        game_id: str | None = None,
        compiler_version: str = DEFAULT_COMPILER_VERSION,
    ) -> None:
        if not isinstance(compiled_store, CompiledKnowledgeStore):
            raise TypeError("compiled_store must be a CompiledKnowledgeStore")
        if root_kind not in ("games", "game"):
            raise ValueError("root_kind must be 'games' or 'game'")
        if not isinstance(compiler_version, str) or not compiler_version:
            raise ValueError("compiler_version must be a non-empty string")
        self._compiled_store = compiled_store
        self._root = Path(root).resolve()
        self._root_kind = root_kind
        self._fixed_game_id = _validate_game_id(game_id) if game_id is not None else None
        if root_kind == "game" and self._fixed_game_id is None:
            self._fixed_game_id = _validate_game_id(self._root.name)
        self._compiler_version = compiler_version

    @classmethod
    def from_game_root(
        cls,
        compiled_store: CompiledKnowledgeStore,
        game_root: str | os.PathLike[str],
        *,
        compiler_version: str = DEFAULT_COMPILER_VERSION,
        game_id: str | None = None,
    ) -> KnowledgeSnapshotBuilder:
        """Construct a builder rooted at one ``games/active/<game_id>`` dir."""

        return cls(
            compiled_store,
            game_root,
            root_kind="game",
            game_id=game_id,
            compiler_version=compiler_version,
        )

    def _ruleset_path(self, game_id: str) -> Path:
        checked_id = _validate_game_id(game_id)
        if self._fixed_game_id is not None and checked_id != self._fixed_game_id:
            raise KnowledgeSnapshotPathError("game_id does not match the configured game root")
        try:
            child = (
                Path("ruleset")
                if self._root_kind == "game"
                else Path("active") / checked_id / "ruleset"
            )
            return resolve_contained_path(self._root, child)
        except PathSecurityError as exc:
            raise KnowledgeSnapshotPathError("snapshot path escapes its configured root") from exc

    def _parent_path(self, game_id: str) -> Path:
        ruleset = self._ruleset_path(game_id)
        return ruleset.parent

    async def create(
        self,
        game_id: str,
        package: str | VersionedRef | CompiledKnowledgePackageLoad,
        *,
        expected_board_ref: str | VersionedRef | None = None,
        created_at: datetime | str | None = None,
    ) -> KnowledgeSnapshot:
        """Verify a compiled package, copy it, and atomically publish a snapshot."""

        checked_game_id = _validate_game_id(game_id)
        ruleset = self._ruleset_path(checked_game_id)
        lock = _SNAPSHOT_LOCKS.setdefault(ruleset, asyncio.Lock())
        async with lock:
            package_load = (
                package
                if isinstance(package, CompiledKnowledgePackageLoad)
                else await self._compiled_store.load(
                    package,
                    expected_board_ref=expected_board_ref,
                )
            )
            files = await asyncio.to_thread(self._read_verified_package, package_load)
            timestamp = _normalize_created_at(created_at)
            package_id = package_load.package_id
            board_ref = package_load.board_ref_text
            file_digests = {name: _sha256(data) for name, data in files.items()}
            identity = _snapshot_identity(
                game_id=checked_game_id,
                package_id=package_id,
                board_ref=board_ref,
                package_identity=package_load.package_identity,
                compiler_version=self._compiler_version,
                file_digests=file_digests,
            )
            snapshot_id = _snapshot_id(identity)
            manifest = dict(identity)
            manifest.update(
                {
                    "created_at": timestamp,
                    "snapshot_id": snapshot_id,
                }
            )
            manifest_bytes = _canonical_json_bytes(manifest)
            expected = KnowledgeSnapshot(
                game_id=checked_game_id,
                snapshot_id=snapshot_id,
                package_id=package_id,
                board_ref=package_load.board_ref,
                package_identity=package_load.package_identity,
                compiler_version=self._compiler_version,
                created_at=timestamp,
                root=ruleset,
                file_digests=file_digests,
                manifest_sha256=_sha256(manifest_bytes),
            )

            if ruleset.exists() or ruleset.is_symlink():
                existing = await asyncio.to_thread(self._load_sync, checked_game_id)
                if existing.snapshot_id != expected.snapshot_id:
                    raise SnapshotAlreadyExistsError(
                        f"game {checked_game_id!r} already has a different snapshot"
                    )
                return existing

            await asyncio.to_thread(_ensure_directory, self._parent_path(checked_game_id))
            staging = await asyncio.to_thread(
                tempfile.mkdtemp,
                prefix=".ruleset.staging-",
                dir=self._parent_path(checked_game_id),
            )
            staging_path = Path(staging)
            committed = False
            try:
                await asyncio.to_thread(self._materialize, staging_path, files, manifest_bytes)
                await asyncio.to_thread(
                    self._verify_directory,
                    staging_path,
                    checked_game_id,
                )
                try:
                    await asyncio.to_thread(os.rename, staging_path, ruleset)
                    committed = True
                except FileExistsError:
                    existing = await asyncio.to_thread(self._load_sync, checked_game_id)
                    if existing.snapshot_id != expected.snapshot_id:
                        raise SnapshotAlreadyExistsError(
                            f"game {checked_game_id!r} already has a different snapshot"
                        )
                    committed = True
                    return existing
            finally:
                if not committed:
                    await asyncio.to_thread(_remove_tree, staging_path)
            return expected

    async def load(self, game_id: str) -> KnowledgeSnapshot:
        """Verify and load a snapshot without consulting the compiled store."""

        checked_game_id = _validate_game_id(game_id)
        return await asyncio.to_thread(self._load_sync, checked_game_id)

    async def verify(self, game_id: str) -> KnowledgeSnapshot:
        return await self.load(game_id)

    def path_for(self, game_id: str) -> Path:
        """Return the contained ruleset path without reading it."""

        return self._ruleset_path(game_id)

    @staticmethod
    def _read_verified_package(package: CompiledKnowledgePackageLoad) -> dict[str, bytes]:
        source_root = package.root.resolve()
        files: dict[str, bytes] = {}
        for filename, expected_digest in sorted(package.file_digests.items()):
            try:
                source_path = resolve_contained_path(source_root, filename)
            except PathSecurityError as exc:
                raise CorruptKnowledgeSnapshotError(
                    "compiled package path escapes its root"
                ) from exc
            data = _read_regular_file(source_path, error_type=CorruptKnowledgeSnapshotError)
            if _sha256(data) != expected_digest:
                raise CorruptKnowledgeSnapshotError(
                    f"compiled package changed while snapshot was being created: {filename}"
                )
            files[filename] = data

        manifest_path = source_root / "manifest.json"
        manifest_data = _read_regular_file(manifest_path, error_type=CorruptKnowledgeSnapshotError)
        # ``manifest_payload`` is a mapping proxy in the detached compiled
        # view.  Copy its top level before serializing the canonical JSON.
        expected_manifest = _canonical_json_bytes(dict(package.manifest_payload))
        if manifest_data != expected_manifest:
            raise CorruptKnowledgeSnapshotError(
                "compiled package manifest changed during snapshot creation"
            )
        files["manifest.json"] = manifest_data
        return files

    @staticmethod
    def _materialize(staging: Path, files: Mapping[str, bytes], manifest: bytes) -> None:
        if staging.is_symlink() or not staging.is_dir():
            raise CorruptKnowledgeSnapshotError("snapshot staging directory is unavailable")
        for filename in sorted(files):
            _write_exclusive(staging / filename, files[filename])
        _write_exclusive(staging / SNAPSHOT_FILENAME, manifest)

    def _load_sync(self, game_id: str) -> KnowledgeSnapshot:
        ruleset = self._ruleset_path(game_id)
        if not ruleset.exists() or ruleset.is_symlink():
            raise SnapshotNotFoundError(f"snapshot does not exist for game {game_id!r}")
        return self._verify_directory(ruleset, game_id)

    @classmethod
    def _verify_directory(cls, ruleset: Path, game_id: str) -> KnowledgeSnapshot:
        if ruleset.is_symlink() or not ruleset.is_dir():
            raise CorruptKnowledgeSnapshotError("snapshot ruleset path is not a directory")
        snapshot_path = ruleset / SNAPSHOT_FILENAME
        snapshot_data = _read_regular_file(
            snapshot_path,
            error_type=CorruptKnowledgeSnapshotError,
        )
        metadata = _decode_canonical_json(snapshot_data, filename=SNAPSHOT_FILENAME)
        if metadata.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise CorruptKnowledgeSnapshotError("snapshot schema_version is unsupported")
        manifest_game_id = _string_field(metadata, "game_id", filename=SNAPSHOT_FILENAME)
        if manifest_game_id != game_id:
            raise CorruptKnowledgeSnapshotError("snapshot game_id does not match its directory")
        package_id = _string_field(metadata, "package_id", filename=SNAPSHOT_FILENAME)
        board_ref = _string_field(metadata, "board_ref", filename=SNAPSHOT_FILENAME)
        package_identity = _string_field(metadata, "package_identity", filename=SNAPSHOT_FILENAME)
        compiler_version = _string_field(metadata, "compiler_version", filename=SNAPSHOT_FILENAME)
        created_at = _string_field(metadata, "created_at", filename=SNAPSHOT_FILENAME)
        snapshot_id = _string_field(metadata, "snapshot_id", filename=SNAPSHOT_FILENAME)
        if not _SHA256_RE.fullmatch(package_identity):
            raise CorruptKnowledgeSnapshotError("snapshot package_identity is invalid")
        if not snapshot_id.startswith("ruleset-") or not _SHA256_RE.fullmatch(snapshot_id[8:]):
            raise CorruptKnowledgeSnapshotError("snapshot_id is invalid")
        try:
            parsed_board_ref = VersionedRef.parse(board_ref)
        except (TypeError, ValueError) as exc:
            raise CorruptKnowledgeSnapshotError("snapshot board_ref is invalid") from exc
        if package_id != board_ref:
            raise CorruptKnowledgeSnapshotError("snapshot package_id and board_ref disagree")

        raw_files = metadata.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise CorruptKnowledgeSnapshotError("snapshot files must be a non-empty list")
        file_digests: dict[str, str] = {}
        previous_path: str | None = None
        for entry in raw_files:
            if not isinstance(entry, dict):
                raise CorruptKnowledgeSnapshotError("snapshot file entry must be an object")
            path = entry.get("path")
            digest = entry.get("sha256")
            if (
                not isinstance(path, str)
                or path in ("", ".", "..", SNAPSHOT_FILENAME)
                or "/" in path
                or "\\" in path
                or path.startswith(".")
            ):
                raise CorruptKnowledgeSnapshotError("snapshot contains an invalid file path")
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise CorruptKnowledgeSnapshotError(f"snapshot hash is invalid for {path}")
            if path in file_digests:
                raise CorruptKnowledgeSnapshotError("snapshot contains duplicate file paths")
            if previous_path is not None and path <= previous_path:
                raise CorruptKnowledgeSnapshotError("snapshot files are not sorted")
            previous_path = path
            file_digests[path] = digest

        actual_entries = list(ruleset.iterdir())
        if any(entry.is_symlink() for entry in actual_entries):
            raise CorruptKnowledgeSnapshotError("snapshot contains a symlink")
        if any(entry.is_dir() for entry in actual_entries):
            raise CorruptKnowledgeSnapshotError("snapshot contains an unexpected directory")
        actual_names = {entry.name for entry in actual_entries}
        expected_names = set(file_digests) | {SNAPSHOT_FILENAME}
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            extra = sorted(actual_names - expected_names)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unexpected " + ", ".join(extra))
            raise CorruptKnowledgeSnapshotError(
                "snapshot file set is incomplete: " + "; ".join(details)
            )
        for filename, expected_digest in file_digests.items():
            data = _read_regular_file(ruleset / filename, error_type=CorruptKnowledgeSnapshotError)
            if _sha256(data) != expected_digest:
                raise CorruptKnowledgeSnapshotError(f"snapshot hash mismatch: {filename}")

        package_manifest = _decode_canonical_json(
            _read_regular_file(ruleset / "manifest.json", error_type=CorruptKnowledgeSnapshotError),
            filename="manifest.json",
        )
        if package_manifest.get("package_id") != package_id:
            raise CorruptKnowledgeSnapshotError("compiled package identity disagrees with snapshot")
        if package_manifest.get("board_ref") != board_ref:
            raise CorruptKnowledgeSnapshotError(
                "compiled package board reference disagrees with snapshot"
            )
        if package_manifest.get("package_sha256") != package_identity:
            raise CorruptKnowledgeSnapshotError("compiled package digest disagrees with snapshot")
        package_files = package_manifest.get("files")
        if not isinstance(package_files, list) or not package_files:
            raise CorruptKnowledgeSnapshotError("compiled package manifest files are missing")
        package_file_digests: dict[str, str] = {}
        for entry in package_files:
            if not isinstance(entry, dict):
                raise CorruptKnowledgeSnapshotError("compiled package manifest entry is invalid")
            path = entry.get("path")
            digest = entry.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                raise CorruptKnowledgeSnapshotError("compiled package manifest entry is invalid")
            package_file_digests[path] = digest
        if set(package_file_digests) != set(file_digests) - {"manifest.json"}:
            raise CorruptKnowledgeSnapshotError("compiled package manifest file set disagrees")
        for path, digest in package_file_digests.items():
            if file_digests[path] != digest:
                raise CorruptKnowledgeSnapshotError(f"compiled package hash mismatch: {path}")
        package_json = _read_regular_file(
            ruleset / "package.json", error_type=CorruptKnowledgeSnapshotError
        )
        if not package_json.endswith(b"\n") or _sha256(package_json[:-1]) != package_identity:
            raise CorruptKnowledgeSnapshotError("compiled package identity digest mismatch")

        identity = _snapshot_identity(
            game_id=game_id,
            package_id=package_id,
            board_ref=board_ref,
            package_identity=package_identity,
            compiler_version=compiler_version,
            file_digests=file_digests,
        )
        if snapshot_id != _snapshot_id(identity):
            raise CorruptKnowledgeSnapshotError("snapshot_id does not match snapshot contents")
        return KnowledgeSnapshot(
            game_id=game_id,
            snapshot_id=snapshot_id,
            package_id=package_id,
            board_ref=parsed_board_ref,
            package_identity=package_identity,
            compiler_version=compiler_version,
            created_at=created_at,
            root=ruleset,
            file_digests=file_digests,
            manifest_sha256=_sha256(snapshot_data),
        )


__all__ = [
    "CorruptKnowledgeSnapshotError",
    "CorruptSnapshotError",
    "DEFAULT_COMPILER_VERSION",
    "KnowledgeSnapshot",
    "KnowledgeSnapshotBuilder",
    "KnowledgeSnapshotError",
    "KnowledgeSnapshotPathError",
    "SNAPSHOT_FILENAME",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotAlreadyExists",
    "SnapshotAlreadyExistsError",
    "SnapshotNotFoundError",
]
