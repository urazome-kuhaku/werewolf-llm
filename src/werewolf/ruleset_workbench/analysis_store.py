"""Durable storage for deterministic workbench analysis artifacts.

The analysis stage consumes only a completed source-artifact group and its
completed canonical research bundle.  ``variants.json`` and
``conflicts.json`` are written as one group, with ``analysis.manifest.json``
written last as the completion marker.  A missing marker therefore leaves an
incomplete, retryable attempt; a present marker makes the group immutable.

This module is intentionally confined to the workbench job directory.  It
does not write published knowledge and it does not resolve conflicts: the
``needs_human_decision`` markers produced by the pure analyzer are preserved
in the rendered JSON.
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

from .analysis import AnalysisReport, analyze_bundle
from .analysis_artifacts import AnalysisArtifacts, render_analysis_artifacts
from .artifact_store import WorkbenchArtifactManifest, WorkbenchArtifactStore
from .bundle_codec import bundle_sha256
from .bundle_store import WorkbenchBundleStore
from .bundles import ResearchBundle
from .store import WorkbenchJobStore

WORKBENCH_ANALYSIS_SCHEMA_VERSION: Literal[1] = 1
WORKBENCH_ANALYSIS_MANIFEST_FILENAME = "analysis.manifest.json"
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_ANALYSIS_ARTIFACT_BYTES = 4 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_ANALYSIS_LOCKS: dict[Path, asyncio.Lock] = {}


class WorkbenchAnalysisStoreError(ValueError):
    """Raised when an analysis artifact group cannot be safely stored."""


class AnalysisAlreadyExistsError(WorkbenchAnalysisStoreError):
    """Raised when a completed analysis group would be overwritten."""


class CorruptWorkbenchAnalysisError(WorkbenchAnalysisStoreError):
    """Raised when an analysis marker or one of its files is invalid."""


# Concise aliases make the persistence boundary convenient to callers while
# retaining the fully qualified error name used by diagnostics.
CorruptAnalysisError = CorruptWorkbenchAnalysisError


@dataclass(frozen=True, slots=True)
class WorkbenchAnalysisFile:
    """One analysis manifest entry with a workbench-relative path and digest."""

    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkbenchAnalysisManifest:
    """The immutable completion record for one analysis artifact group."""

    schema_version: Literal[1]
    bundle_sha256: str
    files: tuple[WorkbenchAnalysisFile, ...]


AnalysisFile = WorkbenchAnalysisFile
AnalysisManifest = WorkbenchAnalysisManifest


def _analysis_lock(path: Path) -> asyncio.Lock:
    """Return the process-local lock for one analysis completion marker."""

    return _ANALYSIS_LOCKS.setdefault(path, asyncio.Lock())


def _exists_including_symlink(path: Path) -> bool:
    """Treat a broken symlink as occupied so it cannot become a marker."""

    return path.exists() or path.is_symlink()


def _is_regular_file(path: Path) -> bool:
    return path.is_file()


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as artifact_file:
        data = artifact_file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("analysis artifact exceeds its size limit")
    return data


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate analysis manifest key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite analysis manifest number: {value}")


def _manifest_payload(manifest: WorkbenchAnalysisManifest) -> dict[str, object]:
    return {
        "bundle_sha256": manifest.bundle_sha256,
        "files": [{"path": item.relative_path, "sha256": item.sha256} for item in manifest.files],
        "schema_version": manifest.schema_version,
    }


def _manifest_bytes(manifest: WorkbenchAnalysisManifest) -> bytes:
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


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("analysis manifest file path must be a non-empty string")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or path.parts != tuple(part for part in value.split("/") if part)
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValueError("analysis manifest file path must be normalized relative POSIX path")
    return value


def _parse_manifest(raw: bytes) -> WorkbenchAnalysisManifest:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptWorkbenchAnalysisError(
            "analysis manifest is not valid UTF-8 JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise CorruptWorkbenchAnalysisError("analysis manifest must be a JSON object")
    if set(payload) != {"bundle_sha256", "files", "schema_version"}:
        raise CorruptWorkbenchAnalysisError("analysis manifest has an unsupported schema")
    if payload.get("schema_version") != WORKBENCH_ANALYSIS_SCHEMA_VERSION:
        raise CorruptWorkbenchAnalysisError("unsupported analysis manifest schema_version")

    bundle_digest = payload.get("bundle_sha256")
    if not isinstance(bundle_digest, str) or _SHA256_RE.fullmatch(bundle_digest) is None:
        raise CorruptWorkbenchAnalysisError("analysis manifest bundle_sha256 is invalid")

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise CorruptWorkbenchAnalysisError("analysis manifest files must be non-empty")
    files: list[WorkbenchAnalysisFile] = []
    seen_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {"path", "sha256"}:
            raise CorruptWorkbenchAnalysisError("analysis manifest file entry is invalid")
        try:
            relative_path = _safe_relative_path(raw_file.get("path"))
        except ValueError as exc:
            raise CorruptWorkbenchAnalysisError(
                "analysis manifest file path is invalid",
            ) from exc
        digest = raw_file.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise CorruptWorkbenchAnalysisError("analysis manifest file sha256 is invalid")
        if relative_path in seen_paths:
            raise CorruptWorkbenchAnalysisError("analysis manifest contains duplicate paths")
        seen_paths.add(relative_path)
        files.append(WorkbenchAnalysisFile(relative_path=relative_path, sha256=digest))

    manifest = WorkbenchAnalysisManifest(
        schema_version=WORKBENCH_ANALYSIS_SCHEMA_VERSION,
        bundle_sha256=bundle_digest,
        files=tuple(files),
    )
    if _manifest_bytes(manifest) != raw:
        raise CorruptWorkbenchAnalysisError(
            "analysis manifest is not canonical UTF-8 JSON",
        )
    if tuple(files) != tuple(sorted(files, key=lambda item: item.relative_path)):
        raise CorruptWorkbenchAnalysisError("analysis manifest files are not sorted")
    return manifest


def _manifest_from_artifacts(
    bundle: ResearchBundle,
    artifacts: AnalysisArtifacts,
) -> WorkbenchAnalysisManifest:
    # Compute each digest from the exact immutable byte string that will be
    # persisted.  This avoids separately encoding nested report data for the
    # digest and the file write.
    files = tuple(
        WorkbenchAnalysisFile(
            relative_path=relative_path,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for relative_path, content in artifacts.files
    )
    return WorkbenchAnalysisManifest(
        schema_version=WORKBENCH_ANALYSIS_SCHEMA_VERSION,
        bundle_sha256=bundle_sha256(bundle),
        files=tuple(sorted(files, key=lambda item: item.relative_path)),
    )


@dataclass(frozen=True, slots=True)
class WorkbenchAnalysisLoad:
    """Verified analysis report and completion manifest loaded together."""

    report: AnalysisReport
    manifest: WorkbenchAnalysisManifest


AnalysisLoad = WorkbenchAnalysisLoad


class WorkbenchAnalysisStore:
    """Persist and verify deterministic analysis artifacts for one job."""

    def __init__(
        self,
        job_store: WorkbenchJobStore,
        bundle_store: WorkbenchBundleStore,
        artifact_store: WorkbenchArtifactStore,
    ) -> None:
        if not isinstance(job_store, WorkbenchJobStore):
            raise TypeError("job_store must be a WorkbenchJobStore")
        if not isinstance(bundle_store, WorkbenchBundleStore):
            raise TypeError("bundle_store must be a WorkbenchBundleStore")
        if not isinstance(artifact_store, WorkbenchArtifactStore):
            raise TypeError("artifact_store must be a WorkbenchArtifactStore")
        self._job_store = job_store
        self._bundle_store = bundle_store
        self._artifact_store = artifact_store

    def _paths(self, job_dir_name: str) -> tuple[Path, Path, Path, Path]:
        """Resolve the job directory and all analysis paths with containment."""

        try:
            job_dir = resolve_contained_path(self._job_store.root, job_dir_name)
            marker_path = resolve_contained_path(
                job_dir,
                WORKBENCH_ANALYSIS_MANIFEST_FILENAME,
            )
            variants_path = resolve_contained_path(job_dir, "variants.json")
            conflicts_path = resolve_contained_path(job_dir, "conflicts.json")
        except PathSecurityError as exc:
            raise WorkbenchAnalysisStoreError(
                "workbench analysis path escapes the job directory",
            ) from exc
        return job_dir, marker_path, variants_path, conflicts_path

    async def _verified_bundle(
        self,
        job_dir_name: str,
    ) -> tuple[ResearchBundle, str, WorkbenchArtifactManifest]:
        """Verify source artifacts before loading the completed bundle."""

        source_manifest = await self._artifact_store.verify(job_dir_name)
        bundle = await self._bundle_store.load_bundle(job_dir_name)
        digest = bundle_sha256(bundle)
        if source_manifest.bundle_sha256 != digest:
            raise WorkbenchAnalysisStoreError(
                "source artifact manifest does not match the completed bundle",
            )
        return bundle, digest, source_manifest

    @staticmethod
    def _expected(
        bundle: ResearchBundle,
        digest: str,
    ) -> tuple[AnalysisReport, AnalysisArtifacts, WorkbenchAnalysisManifest]:
        # ``digest`` is checked against the canonical bundle by the caller;
        # pass the same value to rendering and derive the manifest from the
        # exact bytes returned by that renderer.
        report = analyze_bundle(bundle)
        artifacts = render_analysis_artifacts(report, digest)
        manifest = _manifest_from_artifacts(bundle, artifacts)
        if manifest.bundle_sha256 != digest:
            raise WorkbenchAnalysisStoreError(
                "analysis manifest bundle digest is not canonical",
            )
        return report, artifacts, manifest

    async def materialize(self, job_dir_name: str) -> WorkbenchAnalysisManifest:
        """Analyze one verified source group and persist its two JSON files."""

        bundle, digest, _ = await self._verified_bundle(job_dir_name)
        report, artifacts, manifest = self._expected(bundle, digest)
        del report  # The report is represented by the exact rendered bytes.
        job_dir, marker_path, variants_path, conflicts_path = self._paths(job_dir_name)

        # Resolve every destination before writing any output.  Existing links
        # that point outside the job directory therefore fail before a write.
        destinations = (
            ("variants.json", variants_path, artifacts.variants),
            ("conflicts.json", conflicts_path, artifacts.conflicts),
        )
        del job_dir  # Kept in _paths to validate all paths under one job root.

        async with _analysis_lock(marker_path):
            if await asyncio.to_thread(_exists_including_symlink, marker_path):
                raise AnalysisAlreadyExistsError(
                    f"completed analysis already exists for job {job_dir_name}",
                )
            for _, destination, content in destinations:
                await atomic_write_bytes(destination, content)
            await atomic_write_text(marker_path, _manifest_bytes(manifest).decode("utf-8"))
        return manifest

    async def _load_verified(
        self,
        job_dir_name: str,
    ) -> WorkbenchAnalysisLoad:
        """Recompute expected analysis bytes and verify every persisted byte."""

        bundle, digest, _ = await self._verified_bundle(job_dir_name)
        report, expected_artifacts, expected_manifest = self._expected(bundle, digest)
        job_dir, marker_path, variants_path, conflicts_path = self._paths(job_dir_name)

        async with _analysis_lock(marker_path):
            if not await asyncio.to_thread(_is_regular_file, marker_path):
                raise CorruptWorkbenchAnalysisError(
                    "analysis manifest is required before analysis is loadable",
                )
            try:
                raw_manifest = await asyncio.to_thread(
                    _read_bounded,
                    marker_path,
                    _MAX_MANIFEST_BYTES,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise CorruptWorkbenchAnalysisError(
                    "analysis manifest could not be read",
                ) from exc
            manifest = _parse_manifest(raw_manifest)
            if manifest != expected_manifest:
                raise CorruptWorkbenchAnalysisError(
                    "analysis manifest does not match the completed bundle",
                )

            expected_by_path = dict(expected_artifacts.files)
            for item in manifest.files:
                try:
                    path = resolve_contained_path(job_dir, item.relative_path)
                except PathSecurityError as exc:
                    raise CorruptWorkbenchAnalysisError(
                        "analysis manifest path escapes the job directory",
                    ) from exc
                if not await asyncio.to_thread(_is_regular_file, path):
                    raise CorruptWorkbenchAnalysisError(
                        f"analysis artifact file is missing: {item.relative_path}",
                    )
                try:
                    content = await asyncio.to_thread(
                        _read_bounded,
                        path,
                        _MAX_ANALYSIS_ARTIFACT_BYTES,
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    raise CorruptWorkbenchAnalysisError(
                        f"analysis artifact file could not be read: {item.relative_path}",
                    ) from exc
                if item.relative_path not in expected_by_path:
                    raise CorruptWorkbenchAnalysisError(
                        f"unexpected analysis artifact file: {item.relative_path}",
                    )
                if hashlib.sha256(content).hexdigest() != item.sha256:
                    raise CorruptWorkbenchAnalysisError(
                        f"analysis artifact digest mismatch: {item.relative_path}",
                    )
                if content != expected_by_path[item.relative_path]:
                    raise CorruptWorkbenchAnalysisError(
                        f"analysis artifact bytes mismatch: {item.relative_path}",
                    )
        return WorkbenchAnalysisLoad(report=report, manifest=manifest)

    async def load(self, job_dir_name: str) -> AnalysisReport:
        """Load and fully verify the persisted report, returning its report."""

        return (await self._load_verified(job_dir_name)).report

    async def load_report(self, job_dir_name: str) -> AnalysisReport:
        """Descriptive alias for :meth:`load`."""

        return await self.load(job_dir_name)

    async def load_manifest(self, job_dir_name: str) -> WorkbenchAnalysisManifest:
        """Load and fully verify the persisted report, returning its marker."""

        return (await self._load_verified(job_dir_name)).manifest

    async def verify(self, job_dir_name: str) -> WorkbenchAnalysisManifest:
        """Verify source and analysis artifacts and return the completion marker."""

        return await self.load_manifest(job_dir_name)

    async def verify_report(self, job_dir_name: str) -> WorkbenchAnalysisLoad:
        """Return the verified report and marker for audit-oriented callers."""

        return await self._load_verified(job_dir_name)


__all__ = [
    "AnalysisAlreadyExistsError",
    "AnalysisFile",
    "AnalysisLoad",
    "AnalysisManifest",
    "CorruptAnalysisError",
    "CorruptWorkbenchAnalysisError",
    "WORKBENCH_ANALYSIS_MANIFEST_FILENAME",
    "WORKBENCH_ANALYSIS_SCHEMA_VERSION",
    "WorkbenchAnalysisFile",
    "WorkbenchAnalysisLoad",
    "WorkbenchAnalysisManifest",
    "WorkbenchAnalysisStore",
    "WorkbenchAnalysisStoreError",
]
