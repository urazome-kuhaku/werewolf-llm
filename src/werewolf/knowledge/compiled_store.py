"""Atomic, deterministic storage for compiled knowledge packages.

The compiler produces an immutable in-memory package.  This module is the
filesystem boundary for that value: it writes a small set of canonical UTF-8
files below a caller supplied root, records their hashes in a manifest, and
only exposes a package after every byte has been verified.  A compiled package
is self contained; loading it never consults the published Markdown vault.
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
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, cast

from werewolf.persistence import PathSecurityError, resolve_contained_path

from .compiler import CompiledKnowledgePackage
from .refs import VersionedRef

_JSON_KWARGS: Final[dict[str, object]] = {
    "allow_nan": False,
    "ensure_ascii": False,
    "separators": (",", ":"),
    "sort_keys": True,
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PACKAGE_FILES_V1: Final[tuple[str, ...]] = (
    "alias-index.json",
    "documents.jsonl",
    "exact-index.json",
    "package.json",
    "relation-index.json",
    "text-index.json",
    "topic-index.json",
)
_PACKAGE_FILES_V2: Final[tuple[str, ...]] = tuple(sorted((*_PACKAGE_FILES_V1, "execution.json")))
# Kept as the schema-1 name for downstream compatibility. New code should
# select the exact set from the package payload schema.
_PACKAGE_FILES: Final[tuple[str, ...]] = _PACKAGE_FILES_V1
_INDEX_FILES: Final[tuple[tuple[str, str], ...]] = (
    ("exact", "exact-index.json"),
    ("alias", "alias-index.json"),
    ("topic", "topic-index.json"),
    ("relation", "relation-index.json"),
    ("text", "text-index.json"),
)

_PUBLISH_LOCKS: dict[Path, asyncio.Lock] = {}


class CompiledKnowledgeStoreError(ValueError):
    """Base error for invalid compiled package storage operations."""


class CompiledPackageAlreadyExistsError(CompiledKnowledgeStoreError):
    """Raised when a package directory contains a different package."""


class CompiledPackageNotFoundError(FileNotFoundError, CompiledKnowledgeStoreError):
    """Raised when a requested compiled package directory is absent."""


class CorruptCompiledPackageError(CompiledKnowledgeStoreError):
    """Raised when a compiled package is incomplete, altered, or malformed."""


CorruptCompiledPackage = CorruptCompiledPackageError


def _canonical_json(value: object) -> str:
    """Serialize JSON data in the one logical form used by this store."""

    return json.dumps(value, **cast(Any, _JSON_KWARGS))


def _canonical_json_bytes(value: object) -> bytes:
    """Serialize one JSON value as canonical UTF-8 with one LF terminator."""

    return (_canonical_json(value) + "\n").encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _decode_json(raw: bytes, *, filename: str, trailing_lf: bool = True) -> object:
    """Decode one canonical JSON file and reject alternate byte spellings."""

    if b"\r" in raw:
        raise CorruptCompiledPackageError(f"{filename} contains CR line endings")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptCompiledPackageError(f"{filename} is not valid UTF-8") from exc

    if trailing_lf:
        if not text.endswith("\n") or text[:-1].find("\n") >= 0:
            raise CorruptCompiledPackageError(f"{filename} is not one canonical JSON line")
        json_text = text[:-1]
    else:
        if "\n" in text:
            raise CorruptCompiledPackageError(f"{filename} has unexpected line endings")
        json_text = text

    try:
        value = json.loads(
            json_text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptCompiledPackageError(f"{filename} is not valid JSON") from exc
    if _canonical_json(value) != json_text:
        raise CorruptCompiledPackageError(f"{filename} is not canonical JSON")
    return value


def _decode_jsonl(raw: bytes, *, filename: str) -> tuple[Mapping[str, object], ...]:
    """Decode and canonicalize every document record in a JSONL file."""

    if not raw.endswith(b"\n") or b"\r" in raw:
        raise CorruptCompiledPackageError(f"{filename} is not canonical UTF-8 JSONL")
    lines = raw[:-1].split(b"\n")
    if not lines or any(not line for line in lines):
        raise CorruptCompiledPackageError(f"{filename} contains an empty record")

    records: list[Mapping[str, object]] = []
    for index, line in enumerate(lines):
        value = _decode_json(line, filename=f"{filename}[{index}]", trailing_lf=False)
        if not isinstance(value, dict):
            raise CorruptCompiledPackageError(f"{filename}[{index}] must be a JSON object")
        records.append(value)
    return tuple(records)


def _coerce_package_ref(value: str | VersionedRef) -> VersionedRef:
    if isinstance(value, VersionedRef):
        return value
    if isinstance(value, str):
        try:
            return VersionedRef.parse(value)
        except (TypeError, ValueError) as exc:
            raise CompiledKnowledgeStoreError(
                "package ID must be a valid id@X.Y.Z reference"
            ) from exc
    raise TypeError("package ID must be a VersionedRef or id@version string")


def _safe_package_name(value: str | VersionedRef) -> str:
    """Return a validated Windows-safe directory name for one board ref."""

    reference = _coerce_package_ref(value)
    name = reference.format()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or "\x00" in name
    ):
        raise CompiledKnowledgeStoreError("package ID is not a safe directory name")
    return name


def _plain_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise CorruptCompiledPackageError(f"{field_name} must be a JSON object")
    return value


def _string_field(value: Mapping[str, object], key: str, *, field_name: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise CorruptCompiledPackageError(f"{field_name}.{key} must be a non-empty string")
    return result


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_file_set_sync(package_dir: Path) -> dict[str, bytes]:
    """Read every required file after rejecting missing and extra entries."""

    if package_dir.is_symlink() or not package_dir.is_dir():
        raise CompiledPackageNotFoundError(
            f"compiled package directory is absent: {package_dir.name}"
        )
    try:
        children = {child.name for child in package_dir.iterdir()}
    except OSError as exc:
        raise CorruptCompiledPackageError("compiled package directory could not be listed") from exc
    expected_sets = (
        set(_PACKAGE_FILES_V1) | {"manifest.json"},
        set(_PACKAGE_FILES_V2) | {"manifest.json"},
    )
    expected = next((value for value in expected_sets if children == value), None)
    if expected is None:
        allowed = set(_PACKAGE_FILES_V2) | {"manifest.json"}
        missing = sorted(min(expected_sets, key=lambda value: len(value - children)) - children)
        extra = sorted(children - allowed)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise CorruptCompiledPackageError(
            "compiled package file set is incomplete: " + "; ".join(details)
        )

    values: dict[str, bytes] = {}
    for filename in sorted(expected):
        path = package_dir / filename
        if path.is_symlink() or not path.is_file():
            raise CorruptCompiledPackageError(
                f"compiled package entry is not a regular file: {filename}"
            )
        try:
            values[filename] = path.read_bytes()
        except OSError as exc:
            raise CorruptCompiledPackageError(
                f"compiled package file could not be read: {filename}"
            ) from exc
    return values


def _verify_manifest_and_payload(
    package_dir: Path,
    files: Mapping[str, bytes],
    *,
    expected_package_id: str,
    expected_board_ref: VersionedRef | None,
) -> CompiledKnowledgePackageLoad:
    """Verify a complete package and return its detached in-memory view."""

    package_value = _decode_json(files["package.json"], filename="package.json")
    package_payload = _plain_mapping(package_value, field_name="package")
    package_schema_version = package_payload.get("schema_version")
    if type(package_schema_version) is int and package_schema_version == 1:
        package_files = _PACKAGE_FILES_V1
    elif type(package_schema_version) is int and package_schema_version == 2:
        package_files = _PACKAGE_FILES_V2
    else:
        raise CorruptCompiledPackageError("package has an unsupported schema_version")
    if set(files) != set(package_files) | {"manifest.json"}:
        raise CorruptCompiledPackageError(
            "compiled package file set disagrees with package schema_version"
        )

    manifest_value = _decode_json(files["manifest.json"], filename="manifest.json")
    manifest = _plain_mapping(manifest_value, field_name="manifest")
    if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
        raise CorruptCompiledPackageError("manifest has an unsupported schema_version")
    manifest_package_id = _string_field(manifest, "package_id", field_name="manifest")
    manifest_board_ref = _string_field(manifest, "board_ref", field_name="manifest")
    manifest_package_sha = _string_field(manifest, "package_sha256", field_name="manifest")
    logical_manifest_sha = _string_field(manifest, "logical_manifest_sha256", field_name="manifest")
    if _SHA256_RE.fullmatch(manifest_package_sha) is None:
        raise CorruptCompiledPackageError("manifest package_sha256 is invalid")
    if _SHA256_RE.fullmatch(logical_manifest_sha) is None:
        raise CorruptCompiledPackageError("manifest logical_manifest_sha256 is invalid")
    if manifest_package_id != expected_package_id or manifest_board_ref != expected_package_id:
        raise CorruptCompiledPackageError("manifest package identity or board reference disagrees")
    if expected_board_ref is not None and manifest_board_ref != expected_board_ref.format():
        raise CorruptCompiledPackageError("manifest board reference disagrees with requested board")

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise CorruptCompiledPackageError("manifest files must be a non-empty list")
    expected_entries: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CorruptCompiledPackageError("manifest file entry must be an object")
        path = entry.get("path")
        sha256 = entry.get("sha256")
        if not isinstance(path, str) or path not in package_files:
            raise CorruptCompiledPackageError("manifest contains an invalid file path")
        if path in seen_paths:
            raise CorruptCompiledPackageError("manifest contains duplicate file paths")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise CorruptCompiledPackageError(f"manifest hash is invalid for {path}")
        seen_paths.add(path)
        expected_entries.append({"path": path, "sha256": sha256})
    if expected_entries != sorted(expected_entries, key=lambda item: item["path"]):
        raise CorruptCompiledPackageError("manifest files are not sorted")
    if seen_paths != set(package_files):
        raise CorruptCompiledPackageError("manifest does not cover every compiled package file")
    for entry in expected_entries:
        actual = _digest(files[entry["path"]])
        if actual != entry["sha256"]:
            raise CorruptCompiledPackageError(f"compiled package hash mismatch: {entry['path']}")

    package_id = _string_field(package_payload, "package_id", field_name="package")
    board_ref = _string_field(package_payload, "board_ref", field_name="package")
    if package_id != expected_package_id or board_ref != expected_package_id:
        raise CorruptCompiledPackageError("package identity or board reference disagrees")
    if _digest(_canonical_json(package_payload).encode("utf-8")) != manifest_package_sha:
        raise CorruptCompiledPackageError("package identity digest mismatch")
    logical_manifest = package_payload.get("manifest")
    if not isinstance(logical_manifest, dict):
        raise CorruptCompiledPackageError("package manifest is missing")
    if _digest(_canonical_json(logical_manifest).encode("utf-8")) != logical_manifest_sha:
        raise CorruptCompiledPackageError("logical manifest digest mismatch")

    if package_schema_version == 2:
        executable = _plain_mapping(
            package_payload.get("executable"), field_name="package.executable"
        )
        execution_file = _decode_json(files["execution.json"], filename="execution.json")
        if execution_file != executable:
            raise CorruptCompiledPackageError("execution.json does not match package.json")

    documents_value = package_payload.get("documents")
    if not isinstance(documents_value, list) or any(
        not isinstance(item, dict) for item in documents_value
    ):
        raise CorruptCompiledPackageError("package documents must be a list of objects")
    documents = _decode_jsonl(files["documents.jsonl"], filename="documents.jsonl")
    if tuple(cast(object, item) for item in documents) != tuple(documents_value):
        raise CorruptCompiledPackageError("documents.jsonl does not match package.json")

    indexes_value = package_payload.get("indexes")
    indexes = _plain_mapping(indexes_value, field_name="package.indexes")
    if set(indexes) != {name for name, _ in _INDEX_FILES}:
        raise CorruptCompiledPackageError("package indexes do not contain the required index set")
    loaded_indexes: dict[str, Mapping[str, object]] = {}
    for index_name, filename in _INDEX_FILES:
        value = _decode_json(files[filename], filename=filename)
        expected_index = indexes[index_name]
        if value != expected_index:
            raise CorruptCompiledPackageError(f"{filename} does not match package.json")
        loaded_indexes[index_name] = _plain_mapping(value, field_name=filename)

    try:
        parsed_board_ref = VersionedRef.parse(board_ref)
    except (TypeError, ValueError) as exc:
        raise CorruptCompiledPackageError("package board_ref is invalid") from exc
    manifest_payload = dict(manifest)
    return CompiledKnowledgePackageLoad(
        root=package_dir,
        package_id=package_id,
        board_ref=parsed_board_ref,
        package_payload=package_payload,
        documents=documents,
        indexes=loaded_indexes,
        manifest_payload=manifest_payload,
        package_identity=manifest_package_sha,
        file_digests={entry["path"]: entry["sha256"] for entry in expected_entries},
    )


def _verify_directory_sync(
    package_dir: Path,
    *,
    expected_package_id: str,
    expected_board_ref: VersionedRef | None = None,
) -> CompiledKnowledgePackageLoad:
    files = _read_file_set_sync(package_dir)
    return _verify_manifest_and_payload(
        package_dir,
        files,
        expected_package_id=expected_package_id,
        expected_board_ref=expected_board_ref,
    )


def _write_one_sync(path: Path, data: bytes) -> None:
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _materialize_and_verify_sync(staging_dir: Path, files: Mapping[str, bytes]) -> None:
    """Write and verify a private staging directory before it is published."""

    if not staging_dir.is_dir() or staging_dir.is_symlink():
        raise CorruptCompiledPackageError("staging directory is not available")
    for filename in sorted((*files.keys(),)):
        _write_one_sync(staging_dir / filename, files[filename])
    _verify_directory_sync(
        staging_dir,
        expected_package_id=_manifest_package_id(files["manifest.json"]),
        expected_board_ref=None,
    )


def _manifest_package_id(raw: bytes) -> str:
    value = _decode_json(raw, filename="manifest.json")
    manifest = _plain_mapping(value, field_name="manifest")
    return _string_field(manifest, "package_id", field_name="manifest")


def _ensure_root_sync(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise NotADirectoryError(root)


def _create_staging_sync(root: Path, package_name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f".{package_name}.staging-", dir=root))


def _remove_tree_sync(path: Path) -> None:
    if path.exists() or path.is_symlink():
        shutil.rmtree(path)


def _rename_staging_sync(staging_dir: Path, final_dir: Path) -> None:
    if final_dir.exists() or final_dir.is_symlink():
        raise FileExistsError(final_dir)
    os.rename(staging_dir, final_dir)


def _serialized_files(package: CompiledKnowledgePackage) -> dict[str, bytes]:
    """Build all logical output bytes without consulting filesystem state."""

    package_name = _safe_package_name(package.package_id)
    if package.board_ref.format() != package_name:
        raise CompiledKnowledgeStoreError(
            "compiled package board reference does not match package ID"
        )
    try:
        package_value = json.loads(
            package.canonical_package_json,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CompiledKnowledgeStoreError("compiled package JSON is invalid") from exc
    if (
        not isinstance(package_value, dict)
        or _canonical_json(package_value) != package.canonical_package_json
    ):
        raise CompiledKnowledgeStoreError("compiled package JSON is not canonical")
    payload = cast(Mapping[str, object], package_value)
    if payload.get("package_id") != package_name or payload.get("board_ref") != package_name:
        raise CompiledKnowledgeStoreError("compiled package identity or board reference disagrees")
    logical_digest = _digest(package.canonical_package_json.encode("utf-8"))
    if logical_digest != package.package_identity:
        raise CompiledKnowledgeStoreError("compiled package identity digest is inconsistent")

    documents = payload.get("documents")
    if not isinstance(documents, list) or any(not isinstance(item, dict) for item in documents):
        raise CompiledKnowledgeStoreError("compiled package documents must be a list of objects")
    indexes = payload.get("indexes")
    if not isinstance(indexes, dict) or set(indexes) != {name for name, _ in _INDEX_FILES}:
        raise CompiledKnowledgeStoreError("compiled package indexes are incomplete")
    logical_manifest = payload.get("manifest")
    if not isinstance(logical_manifest, dict):
        raise CompiledKnowledgeStoreError("compiled package manifest is missing")

    files: dict[str, bytes] = {
        "package.json": _canonical_json_bytes(payload),
        "documents.jsonl": b"".join(
            _canonical_json(item).encode("utf-8") + b"\n" for item in documents
        ),
    }
    for index_name, filename in _INDEX_FILES:
        index_value = indexes[index_name]
        if not isinstance(index_value, dict):
            raise CompiledKnowledgeStoreError(
                f"compiled package index is not an object: {index_name}"
            )
        files[filename] = _canonical_json_bytes(index_value)

    package_schema_version = payload.get("schema_version")
    if type(package_schema_version) is int and package_schema_version == 1:
        package_files = _PACKAGE_FILES_V1
    elif type(package_schema_version) is int and package_schema_version == 2:
        executable = payload.get("executable")
        if not isinstance(executable, dict):
            raise CompiledKnowledgeStoreError("compiled package execution plan is missing")
        files["execution.json"] = _canonical_json_bytes(executable)
        package_files = _PACKAGE_FILES_V2
    else:
        raise CompiledKnowledgeStoreError("compiled package schema_version is unsupported")

    file_entries = [
        {"path": filename, "sha256": _digest(files[filename])} for filename in sorted(package_files)
    ]
    manifest = {
        "board_ref": package_name,
        "files": file_entries,
        "logical_manifest_sha256": _digest(_canonical_json(logical_manifest).encode("utf-8")),
        "package_id": package_name,
        "package_sha256": logical_digest,
        "schema_version": 1,
    }
    files["manifest.json"] = _canonical_json_bytes(manifest)
    return files


@dataclass(frozen=True, slots=True)
class CompiledKnowledgePackageLoad:
    """Verified, detached view of a compiled package on disk."""

    root: Path
    package_id: str
    board_ref: VersionedRef
    package_payload: Mapping[str, object]
    documents: tuple[Mapping[str, object], ...]
    indexes: Mapping[str, Mapping[str, object]]
    manifest_payload: Mapping[str, object]
    package_identity: str
    file_digests: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())
        object.__setattr__(self, "package_payload", MappingProxyType(dict(self.package_payload)))
        object.__setattr__(self, "indexes", MappingProxyType(dict(self.indexes)))
        object.__setattr__(self, "manifest_payload", MappingProxyType(dict(self.manifest_payload)))
        object.__setattr__(self, "file_digests", MappingProxyType(dict(self.file_digests)))

    @property
    def path(self) -> Path:
        return self.root

    @property
    def package_sha256(self) -> str:
        return self.package_identity

    @property
    def canonical_package_json(self) -> str:
        return _canonical_json(self.package_payload)

    @property
    def package_json(self) -> str:
        return self.canonical_package_json

    @property
    def canonical_manifest_json(self) -> str:
        return _canonical_json(self.manifest_payload)

    @property
    def manifest_json(self) -> str:
        return self.canonical_manifest_json

    @property
    def manifest_sha256(self) -> str:
        return _digest(self.canonical_manifest_json.encode("utf-8"))

    @property
    def board_ref_text(self) -> str:
        return self.board_ref.format()


class CompiledKnowledgeStore:
    """Publish and verify compiled packages below one trusted root."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def package_path(self, package_id: str | VersionedRef) -> Path:
        name = _safe_package_name(package_id)
        try:
            return resolve_contained_path(self._root, name)
        except PathSecurityError as exc:
            raise CompiledKnowledgeStoreError("compiled package path escapes its root") from exc

    path_for = package_path

    async def publish(self, package: CompiledKnowledgePackage) -> Path:
        """Atomically publish one compiled package and return its directory."""

        if not isinstance(package, CompiledKnowledgePackage):
            raise TypeError("publish() expects a CompiledKnowledgePackage")
        package_name = _safe_package_name(package.package_id)
        final_dir = self.package_path(package_name)
        lock = _PUBLISH_LOCKS.setdefault(final_dir, asyncio.Lock())

        async with lock:
            await asyncio.to_thread(_ensure_root_sync, self._root)
            if await asyncio.to_thread(lambda: final_dir.exists() or final_dir.is_symlink()):
                existing = await self.load(package_name)
                if existing.package_identity != package.package_identity:
                    raise CompiledPackageAlreadyExistsError(
                        "compiled package directory already contains a different package: "
                        f"{package_name}"
                    )
                return final_dir

            files = _serialized_files(package)
            staging_dir = await asyncio.to_thread(_create_staging_sync, self._root, package_name)
            committed = False
            try:
                await asyncio.to_thread(_materialize_and_verify_sync, staging_dir, files)
                try:
                    await asyncio.to_thread(_rename_staging_sync, staging_dir, final_dir)
                    committed = True
                except FileExistsError:
                    existing = await self.load(package_name)
                    if existing.package_identity != package.package_identity:
                        raise CompiledPackageAlreadyExistsError(
                            "compiled package directory already contains a different package: "
                            f"{package_name}"
                        )
                    committed = True
            finally:
                if not committed:
                    await asyncio.to_thread(_remove_tree_sync, staging_dir)
        return final_dir

    async def save(self, package: CompiledKnowledgePackage) -> Path:
        return await self.publish(package)

    async def materialize(self, package: CompiledKnowledgePackage) -> Path:
        return await self.publish(package)

    async def load(
        self,
        package_id: str | VersionedRef,
        *,
        expected_board_ref: str | VersionedRef | None = None,
        board_ref: str | VersionedRef | None = None,
    ) -> CompiledKnowledgePackageLoad:
        """Load and fully verify one package without consulting source Vault."""

        requested = _coerce_package_ref(package_id)
        if expected_board_ref is not None and board_ref is not None:
            raise TypeError("pass only one of expected_board_ref or board_ref")
        expected = expected_board_ref if expected_board_ref is not None else board_ref
        expected_ref = _coerce_package_ref(expected) if expected is not None else None
        if expected_ref is not None and expected_ref != requested:
            raise CorruptCompiledPackageError("requested board reference does not match package ID")
        package_dir = self.package_path(requested)
        try:
            return await asyncio.to_thread(
                _verify_directory_sync,
                package_dir,
                expected_package_id=requested.format(),
                expected_board_ref=expected_ref,
            )
        except CompiledPackageNotFoundError:
            raise
        except CorruptCompiledPackageError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise CorruptCompiledPackageError("compiled package could not be verified") from exc

    async def verify(
        self,
        package_id: str | VersionedRef,
        *,
        expected_board_ref: str | VersionedRef | None = None,
    ) -> CompiledKnowledgePackageLoad:
        return await self.load(package_id, expected_board_ref=expected_board_ref)

    async def load_package(
        self,
        package_id: str | VersionedRef,
        *,
        expected_board_ref: str | VersionedRef | None = None,
    ) -> CompiledKnowledgePackageLoad:
        return await self.load(package_id, expected_board_ref=expected_board_ref)


__all__ = [
    "CompiledKnowledgePackageLoad",
    "CompiledKnowledgeStore",
    "CompiledKnowledgeStoreError",
    "CompiledPackageAlreadyExistsError",
    "CompiledPackageNotFoundError",
    "CorruptCompiledPackage",
    "CorruptCompiledPackageError",
]
