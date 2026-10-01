"""Durable storage for rendered research workbench artifacts.

The renderer in :mod:`werewolf.ruleset_workbench.artifacts` is deliberately
pure.  This module is the small persistence boundary that turns its bytes
into the ``sources`` and ``claims.jsonl`` files described by the workbench
plan.  The manifest is written last and is the completion record for the
whole group.  A directory without the manifest is therefore an incomplete
attempt that can be retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from werewolf.persistence import (
    PathSecurityError,
    atomic_write_bytes,
    atomic_write_text,
    resolve_contained_path,
)

from .artifacts import WorkbenchArtifacts, render_workbench_artifacts
from .bundle_codec import bundle_sha256
from .bundle_store import WorkbenchBundleStore
from .bundles import ResearchBundle
from .store import WorkbenchJobStore

WORKBENCH_ARTIFACT_SCHEMA_VERSION: Literal[1] = 1
WORKBENCH_ARTIFACT_MANIFEST_FILENAME = "artifacts.manifest.json"
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_MATERIALIZE_LOCKS: dict[Path, asyncio.Lock] = {}


class WorkbenchArtifactStoreError(ValueError):
    """Raised when a workbench artifact group cannot be safely stored."""


class ArtifactAlreadyExistsError(WorkbenchArtifactStoreError):
    """Raised when a completed artifact group would be overwritten."""


class CorruptWorkbenchArtifactsError(WorkbenchArtifactStoreError):
    """Raised when the artifact manifest or one of its files is invalid."""


@dataclass(frozen=True, slots=True)
class WorkbenchArtifactFile:
    """One manifest entry with a workbench-relative path and content digest."""

    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkbenchArtifactManifest:
    """The immutable completion record for one rendered artifact group."""

    schema_version: Literal[1]
    bundle_sha256: str
    files: tuple[WorkbenchArtifactFile, ...]


# A shorter name is convenient for callers that do not need the workbench
# prefix.  Both names are the same frozen value type.
ArtifactManifest = WorkbenchArtifactManifest


def _materialize_lock(path: Path) -> asyncio.Lock:
    """Return the process-local lock for one completion manifest path."""

    return _MATERIALIZE_LOCKS.setdefault(path, asyncio.Lock())


def _exists_including_symlink(path: Path) -> bool:
    """Treat a broken symlink as occupied so it cannot become a marker."""

    return path.exists() or path.is_symlink()


def _is_regular_file(path: Path) -> bool:
    """Return whether a resolved artifact path is a regular file."""

    return path.is_file()


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    """Read a file while bounding memory used by a corrupt artifact."""

    with path.open("rb") as artifact_file:
        data = artifact_file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("artifact exceeds its size limit")
    return data


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys instead of silently accepting the latter."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    """Reject JSON extensions such as NaN and Infinity."""

    raise ValueError(f"non-finite manifest number: {value}")


def _manifest_payload(manifest: WorkbenchArtifactManifest) -> dict[str, object]:
    """Convert a validated manifest to its canonical JSON object."""

    return {
        "bundle_sha256": manifest.bundle_sha256,
        "files": [{"path": item.relative_path, "sha256": item.sha256} for item in manifest.files],
        "schema_version": manifest.schema_version,
    }


def _manifest_bytes(manifest: WorkbenchArtifactManifest) -> bytes:
    """Encode a manifest deterministically as UTF-8 JSON with one final LF."""

    return (
        json.dumps(
            _manifest_payload(manifest),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _manifest_from_artifacts(
    bundle: ResearchBundle,
    artifacts: WorkbenchArtifacts,
) -> WorkbenchArtifactManifest:
    """Build the completion record from exactly the bytes to be written."""

    files = tuple(
        WorkbenchArtifactFile(
            relative_path=relative_path,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for relative_path, content in artifacts.files
    )
    files = tuple(sorted(files, key=lambda item: item.relative_path))
    return WorkbenchArtifactManifest(
        schema_version=WORKBENCH_ARTIFACT_SCHEMA_VERSION,
        bundle_sha256=bundle_sha256(bundle),
        files=files,
    )


def _safe_relative_path(value: object) -> str:
    """Validate the manifest's relative POSIX path representation."""

    if not isinstance(value, str) or not value:
        raise ValueError("manifest file path must be a non-empty string")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or path.parts != tuple(part for part in value.split("/") if part)
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValueError("manifest file path must be a normalized relative POSIX path")
    return value


def _parse_manifest(raw: bytes) -> WorkbenchArtifactManifest:
    """Decode and validate one canonical completion manifest."""

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptWorkbenchArtifactsError(
            "artifact manifest is not valid UTF-8 JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise CorruptWorkbenchArtifactsError("artifact manifest must be a JSON object")
    if set(payload) != {"bundle_sha256", "files", "schema_version"}:
        raise CorruptWorkbenchArtifactsError("artifact manifest has an unsupported schema")
    if payload.get("schema_version") != WORKBENCH_ARTIFACT_SCHEMA_VERSION:
        raise CorruptWorkbenchArtifactsError("unsupported artifact manifest schema_version")
    bundle_digest = payload.get("bundle_sha256")
    if not isinstance(bundle_digest, str) or _SHA256_RE.fullmatch(bundle_digest) is None:
        raise CorruptWorkbenchArtifactsError("artifact manifest bundle_sha256 is invalid")

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise CorruptWorkbenchArtifactsError("artifact manifest files must be non-empty")
    files: list[WorkbenchArtifactFile] = []
    seen_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {"path", "sha256"}:
            raise CorruptWorkbenchArtifactsError("artifact manifest file entry is invalid")
        try:
            relative_path = _safe_relative_path(raw_file.get("path"))
        except ValueError as exc:
            raise CorruptWorkbenchArtifactsError("artifact manifest file path is invalid") from exc
        digest = raw_file.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise CorruptWorkbenchArtifactsError("artifact manifest file sha256 is invalid")
        if relative_path in seen_paths:
            raise CorruptWorkbenchArtifactsError("artifact manifest contains duplicate paths")
        seen_paths.add(relative_path)
        files.append(WorkbenchArtifactFile(relative_path=relative_path, sha256=digest))

    manifest = WorkbenchArtifactManifest(
        schema_version=WORKBENCH_ARTIFACT_SCHEMA_VERSION,
        bundle_sha256=bundle_digest,
        files=tuple(files),
    )
    if _manifest_bytes(manifest) != raw:
        raise CorruptWorkbenchArtifactsError("artifact manifest is not canonical UTF-8 JSON")
    if tuple(files) != tuple(sorted(files, key=lambda item: item.relative_path)):
        raise CorruptWorkbenchArtifactsError("artifact manifest files are not sorted")
    return manifest


class WorkbenchArtifactStore:
    """Persist and verify the rendered artifacts for one workbench job."""

    def __init__(
        self,
        job_store: WorkbenchJobStore,
        bundle_store: WorkbenchBundleStore,
    ) -> None:
        if not isinstance(job_store, WorkbenchJobStore):
            raise TypeError("job_store must be a WorkbenchJobStore")
        if not isinstance(bundle_store, WorkbenchBundleStore):
            raise TypeError("bundle_store must be a WorkbenchBundleStore")
        self._job_store = job_store
        self._bundle_store = bundle_store

    def _paths(self, job_dir_name: str) -> tuple[Path, Path]:
        """Resolve the job directory and manifest without following escapes."""

        try:
            job_dir = resolve_contained_path(self._job_store.root, job_dir_name)
            manifest_path = resolve_contained_path(
                job_dir,
                WORKBENCH_ARTIFACT_MANIFEST_FILENAME,
            )
        except PathSecurityError as exc:
            raise WorkbenchArtifactStoreError(
                "workbench artifact path escapes the job directory",
            ) from exc
        return job_dir, manifest_path

    @staticmethod
    def _destination_paths(
        job_dir: Path,
        artifacts: WorkbenchArtifacts,
    ) -> tuple[tuple[str, Path, bytes], ...]:
        """Resolve every output before writing any output bytes."""

        try:
            destinations = tuple(
                (
                    relative_path,
                    resolve_contained_path(job_dir, relative_path),
                    content,
                )
                for relative_path, content in artifacts.files
            )
        except PathSecurityError as exc:
            raise WorkbenchArtifactStoreError(
                "workbench artifact path escapes the job directory",
            ) from exc
        return destinations

    async def materialize(self, job_dir_name: str) -> WorkbenchArtifactManifest:
        """Write a completed bundle's artifacts and return its manifest.

        The bundle is loaded through ``WorkbenchBundleStore`` before any
        output is touched.  A pre-existing manifest makes the artifact group
        immutable.  If writing fails before the final manifest write, a later
        call can replace the orphaned files and retry safely.
        """

        bundle = await self._bundle_store.load_bundle(job_dir_name)
        artifacts = render_workbench_artifacts(bundle)
        manifest = _manifest_from_artifacts(bundle, artifacts)
        job_dir, manifest_path = self._paths(job_dir_name)
        destinations = self._destination_paths(job_dir, artifacts)

        async with _materialize_lock(manifest_path):
            if await asyncio.to_thread(_exists_including_symlink, manifest_path):
                raise ArtifactAlreadyExistsError(
                    f"completed workbench artifacts already exist for job {job_dir_name}",
                )

            source_dir = resolve_contained_path(job_dir, "sources")
            await asyncio.to_thread(source_dir.mkdir, parents=False, exist_ok=True)
            if not await asyncio.to_thread(source_dir.is_dir):
                raise WorkbenchArtifactStoreError("sources path is not a directory")

            # All destination paths were resolved before the first write, so a
            # pre-existing symlink that points outside the job directory is
            # rejected without leaving a partially refreshed group.
            for _, destination, content in destinations:
                await atomic_write_bytes(destination, content)
            await atomic_write_text(manifest_path, _manifest_bytes(manifest).decode("utf-8"))
        return manifest

    async def load_manifest(self, job_dir_name: str) -> WorkbenchArtifactManifest:
        """Load and fully verify one completed artifact group.

        Verification re-renders the immutable completed bundle.  This makes
        a modified manifest fail even when an attacker also changes its file
        digests, while the bundle store independently protects the bundle
        itself with its own completion marker.
        """

        bundle = await self._bundle_store.load_bundle(job_dir_name)
        expected_artifacts = render_workbench_artifacts(bundle)
        expected_manifest = _manifest_from_artifacts(bundle, expected_artifacts)
        job_dir, manifest_path = self._paths(job_dir_name)

        async with _materialize_lock(manifest_path):
            if not await asyncio.to_thread(_is_regular_file, manifest_path):
                raise CorruptWorkbenchArtifactsError(
                    "artifact manifest is required before artifacts are loadable",
                )
            try:
                raw_manifest = await asyncio.to_thread(
                    _read_bounded,
                    manifest_path,
                    _MAX_MANIFEST_BYTES,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise CorruptWorkbenchArtifactsError(
                    "artifact manifest could not be read",
                ) from exc
            manifest = _parse_manifest(raw_manifest)
            if manifest != expected_manifest:
                raise CorruptWorkbenchArtifactsError(
                    "artifact manifest does not match the completed bundle",
                )

            expected_by_path = dict(expected_artifacts.files)
            for item in manifest.files:
                try:
                    path = resolve_contained_path(job_dir, item.relative_path)
                except PathSecurityError as exc:
                    raise CorruptWorkbenchArtifactsError(
                        "artifact manifest path escapes the job directory",
                    ) from exc
                if not await asyncio.to_thread(_is_regular_file, path):
                    raise CorruptWorkbenchArtifactsError(
                        f"artifact file is missing: {item.relative_path}",
                    )
                try:
                    content = await asyncio.to_thread(
                        _read_bounded,
                        path,
                        _MAX_ARTIFACT_BYTES,
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    raise CorruptWorkbenchArtifactsError(
                        f"artifact file could not be read: {item.relative_path}",
                    ) from exc
                if hashlib.sha256(content).hexdigest() != item.sha256:
                    raise CorruptWorkbenchArtifactsError(
                        f"artifact file digest mismatch: {item.relative_path}",
                    )
                # This lookup makes the expected output set explicit and
                # protects this verifier if the renderer later gains paths
                # with unusual semantics.
                if item.relative_path not in expected_by_path:
                    raise CorruptWorkbenchArtifactsError(
                        f"unexpected artifact file: {item.relative_path}",
                    )
        return manifest

    async def verify(self, job_dir_name: str) -> WorkbenchArtifactManifest:
        """Alias for :meth:`load_manifest` used by review and publish gates."""

        return await self.load_manifest(job_dir_name)


__all__ = [
    "ArtifactAlreadyExistsError",
    "ArtifactManifest",
    "CorruptWorkbenchArtifactsError",
    "WORKBENCH_ARTIFACT_MANIFEST_FILENAME",
    "WORKBENCH_ARTIFACT_SCHEMA_VERSION",
    "WorkbenchArtifactFile",
    "WorkbenchArtifactManifest",
    "WorkbenchArtifactStore",
    "WorkbenchArtifactStoreError",
]
