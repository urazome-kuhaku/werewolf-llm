"""Durable storage for one candidate's workbench coverage report.

Coverage is the first workbench stage whose input is a caller supplied
matrix.  The matrix is therefore persisted inside ``coverage.json`` by the
pure renderer.  This module only owns the completion boundary: it verifies
the completed bundle, rendered source artifacts, and analysis artifacts,
then writes ``coverage.json`` followed by ``coverage.manifest.json``.

The marker is the commit record.  A missing marker leaves an orphan report
that can be retried, while a present marker makes the report immutable.  A
verification pass never trusts the report's counts or status fields: it
extracts the requirements from the report, recomputes coverage from the
completed bundle, and compares the exact bytes that would have been written.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from werewolf.persistence import (
    PathSecurityError,
    atomic_write_bytes,
    atomic_write_text,
    resolve_contained_path,
)

from .analysis_store import WorkbenchAnalysisStore
from .artifact_store import WorkbenchArtifactStore
from .bundle_codec import bundle_sha256
from .bundle_store import WorkbenchBundleStore
from .bundles import ResearchBundle
from .coverage import CoverageReport, CoverageRequirement, analyze_coverage
from .coverage_artifacts import (
    COVERAGE_ARTIFACT_SCHEMA_VERSION,
    CoverageArtifacts,
    render_coverage_artifacts,
)
from .store import WorkbenchJobStore

WORKBENCH_COVERAGE_SCHEMA_VERSION: Literal[1] = 1
WORKBENCH_COVERAGE_MANIFEST_FILENAME = "coverage.manifest.json"
_COVERAGE_FILENAME = "coverage.json"
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_COVERAGE_BYTES = 4 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_LOCKS: dict[Path, asyncio.Lock] = {}


class WorkbenchCoverageStoreError(ValueError):
    """Raised when a coverage report cannot be safely stored or verified."""


class CoverageAlreadyExistsError(WorkbenchCoverageStoreError):
    """Raised when a completed coverage report would be overwritten."""


class CorruptWorkbenchCoverageError(WorkbenchCoverageStoreError):
    """Raised when a coverage marker or report is incomplete or invalid."""


# Short aliases keep callers consistent with the other workbench stores.
CorruptCoverageError = CorruptWorkbenchCoverageError


@dataclass(frozen=True, slots=True)
class WorkbenchCoverageFile:
    """One coverage manifest entry with a normalized relative path."""

    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkbenchCoverageManifest:
    """Immutable completion record for one coverage artifact group."""

    schema_version: Literal[1]
    bundle_sha256: str
    files: tuple[WorkbenchCoverageFile, ...]


CoverageFile = WorkbenchCoverageFile
CoverageManifest = WorkbenchCoverageManifest


@dataclass(frozen=True, slots=True)
class WorkbenchCoverageLoad:
    """Verified coverage report and its completion marker."""

    report: CoverageReport
    manifest: WorkbenchCoverageManifest


CoverageLoad = WorkbenchCoverageLoad


def _coverage_lock(marker_path: Path) -> asyncio.Lock:
    """Return the process-local lock for one coverage completion marker."""

    return _LOCKS.setdefault(marker_path, asyncio.Lock())


def _exists_including_symlink(path: Path) -> bool:
    """Treat a broken symlink as occupied so it cannot become a marker."""

    return path.exists() or path.is_symlink()


def _is_regular_file(path: Path) -> bool:
    return path.is_file()


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as report_file:
        data = report_file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("coverage artifact exceeds its size limit")
    return data


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate coverage JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite coverage JSON number: {value}")


def _manifest_payload(manifest: WorkbenchCoverageManifest) -> dict[str, object]:
    return {
        "bundle_sha256": manifest.bundle_sha256,
        "files": [{"path": item.relative_path, "sha256": item.sha256} for item in manifest.files],
        "schema_version": manifest.schema_version,
    }


def _manifest_bytes(manifest: WorkbenchCoverageManifest) -> bytes:
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
        raise ValueError("coverage manifest file path must be a non-empty string")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or path.parts != tuple(part for part in value.split("/") if part)
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValueError("coverage manifest file path must be normalized relative POSIX path")
    return value


def _parse_manifest(raw: bytes) -> WorkbenchCoverageManifest:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptWorkbenchCoverageError(
            "coverage manifest is not valid UTF-8 JSON",
        ) from exc

    if not isinstance(payload, dict):
        raise CorruptWorkbenchCoverageError("coverage manifest must be a JSON object")
    if set(payload) != {"bundle_sha256", "files", "schema_version"}:
        raise CorruptWorkbenchCoverageError("coverage manifest has an unsupported schema")
    if payload.get("schema_version") != WORKBENCH_COVERAGE_SCHEMA_VERSION:
        raise CorruptWorkbenchCoverageError("unsupported coverage manifest schema_version")

    bundle_digest = payload.get("bundle_sha256")
    if not isinstance(bundle_digest, str) or _SHA256_RE.fullmatch(bundle_digest) is None:
        raise CorruptWorkbenchCoverageError("coverage manifest bundle_sha256 is invalid")

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise CorruptWorkbenchCoverageError("coverage manifest files must be non-empty")
    files: list[WorkbenchCoverageFile] = []
    seen_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {"path", "sha256"}:
            raise CorruptWorkbenchCoverageError("coverage manifest file entry is invalid")
        try:
            relative_path = _safe_relative_path(raw_file.get("path"))
        except ValueError as exc:
            raise CorruptWorkbenchCoverageError(
                "coverage manifest file path is invalid",
            ) from exc
        digest = raw_file.get("sha256")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise CorruptWorkbenchCoverageError("coverage manifest file sha256 is invalid")
        if relative_path in seen_paths:
            raise CorruptWorkbenchCoverageError("coverage manifest contains duplicate paths")
        seen_paths.add(relative_path)
        files.append(WorkbenchCoverageFile(relative_path=relative_path, sha256=digest))

    manifest = WorkbenchCoverageManifest(
        schema_version=WORKBENCH_COVERAGE_SCHEMA_VERSION,
        bundle_sha256=bundle_digest,
        files=tuple(files),
    )
    if _manifest_bytes(manifest) != raw:
        raise CorruptWorkbenchCoverageError(
            "coverage manifest is not canonical UTF-8 JSON",
        )
    if tuple(files) != tuple(sorted(files, key=lambda item: item.relative_path)):
        raise CorruptWorkbenchCoverageError("coverage manifest files are not sorted")
    return manifest


def _manifest_from_artifacts(
    bundle: ResearchBundle,
    artifacts: CoverageArtifacts,
) -> WorkbenchCoverageManifest:
    files = tuple(
        WorkbenchCoverageFile(
            relative_path=relative_path,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for relative_path, content in artifacts.files
    )
    return WorkbenchCoverageManifest(
        schema_version=WORKBENCH_COVERAGE_SCHEMA_VERSION,
        bundle_sha256=bundle_sha256(bundle),
        files=tuple(sorted(files, key=lambda item: item.relative_path)),
    )


def _report_requirements(raw: bytes) -> tuple[str, tuple[CoverageRequirement, ...]]:
    """Extract the persisted matrix without trusting derived report fields."""

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptWorkbenchCoverageError(
            "coverage.json is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise CorruptWorkbenchCoverageError("coverage.json must be a JSON object")
    if payload.get("schema_version") != COVERAGE_ARTIFACT_SCHEMA_VERSION:
        raise CorruptWorkbenchCoverageError("unsupported coverage artifact schema_version")
    candidate_id = payload.get("ruleset_candidate_id")
    if not isinstance(candidate_id, str):
        raise CorruptWorkbenchCoverageError("coverage.json candidate ID is invalid")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise CorruptWorkbenchCoverageError("coverage.json items must be non-empty")

    requirements: list[CoverageRequirement] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise CorruptWorkbenchCoverageError("coverage.json item is invalid")
        raw_requirement = raw_item.get("requirement")
        if not isinstance(raw_requirement, dict):
            raise CorruptWorkbenchCoverageError(
                "coverage.json item requirement is invalid",
            )
        try:
            requirement = CoverageRequirement.model_validate_json(
                json.dumps(
                    raw_requirement,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            raise CorruptWorkbenchCoverageError(
                "coverage.json contains an invalid requirement",
            ) from exc
        if raw_item.get("requirement_id") != requirement.requirement_id:
            raise CorruptWorkbenchCoverageError(
                "coverage.json requirement ID does not match its requirement",
            )
        requirements.append(requirement)
    return candidate_id, tuple(requirements)


class WorkbenchCoverageStore:
    """Persist and verify one deterministic coverage report per job."""

    def __init__(
        self,
        job_store: WorkbenchJobStore,
        bundle_store: WorkbenchBundleStore,
        artifact_store: WorkbenchArtifactStore,
        analysis_store: WorkbenchAnalysisStore,
    ) -> None:
        if not isinstance(job_store, WorkbenchJobStore):
            raise TypeError("job_store must be a WorkbenchJobStore")
        if not isinstance(bundle_store, WorkbenchBundleStore):
            raise TypeError("bundle_store must be a WorkbenchBundleStore")
        if not isinstance(artifact_store, WorkbenchArtifactStore):
            raise TypeError("artifact_store must be a WorkbenchArtifactStore")
        if not isinstance(analysis_store, WorkbenchAnalysisStore):
            raise TypeError("analysis_store must be a WorkbenchAnalysisStore")
        self._job_store = job_store
        self._bundle_store = bundle_store
        self._artifact_store = artifact_store
        self._analysis_store = analysis_store

    def _paths(self, job_dir_name: str) -> tuple[Path, Path, Path]:
        try:
            job_dir = resolve_contained_path(self._job_store.root, job_dir_name)
            report_path = resolve_contained_path(job_dir, _COVERAGE_FILENAME)
            marker_path = resolve_contained_path(
                job_dir,
                WORKBENCH_COVERAGE_MANIFEST_FILENAME,
            )
        except PathSecurityError as exc:
            raise WorkbenchCoverageStoreError(
                "workbench coverage path escapes the job directory",
            ) from exc
        return job_dir, report_path, marker_path

    async def _verified_bundle(self, job_dir_name: str) -> tuple[ResearchBundle, str]:
        """Verify every preceding completed stage before reading the bundle."""

        # Keep both calls explicit: coverage is not valid when either source
        # artifacts or conflict analysis is incomplete.  Analysis verification
        # also checks its own bundle, but the direct artifact check makes the
        # required dependency boundary visible and guards alternate adapters.
        await self._artifact_store.verify(job_dir_name)
        await self._analysis_store.verify(job_dir_name)
        bundle = await self._bundle_store.load_bundle(job_dir_name)
        return bundle, bundle_sha256(bundle)

    @staticmethod
    def _expected(
        bundle: ResearchBundle,
        candidate_id: str,
        requirements: Iterable[CoverageRequirement | Mapping[str, Any]],
    ) -> tuple[CoverageReport, bytes, WorkbenchCoverageManifest]:
        report = analyze_coverage(bundle, candidate_id, requirements)
        artifacts = render_coverage_artifacts(report, bundle_sha256(bundle))
        coverage_bytes = artifacts.coverage
        manifest = _manifest_from_artifacts(bundle, artifacts)
        return report, coverage_bytes, manifest

    async def materialize(
        self,
        job_dir_name: str,
        candidate_id: str,
        requirements: Iterable[CoverageRequirement | Mapping[str, Any]],
    ) -> WorkbenchCoverageManifest:
        """Compute and commit one candidate coverage report.

        An unsatisfied requirement is intentionally still materialized.  The
        report is evidence for a later decision; the publish gate decides
        whether its required coverage is complete.
        """

        if isinstance(requirements, (str, bytes)):
            raise TypeError("requirements must be an iterable of coverage requirements")
        matrix = tuple(requirements)
        bundle, digest = await self._verified_bundle(job_dir_name)
        report = analyze_coverage(bundle, candidate_id, matrix)
        artifacts = render_coverage_artifacts(report, digest)
        coverage_bytes = artifacts.coverage
        manifest = _manifest_from_artifacts(bundle, artifacts)
        if manifest.bundle_sha256 != digest:
            raise WorkbenchCoverageStoreError(
                "coverage manifest bundle digest is not canonical",
            )

        job_dir, report_path, marker_path = self._paths(job_dir_name)
        del job_dir  # Path resolution above authorizes every destination.
        async with _coverage_lock(marker_path):
            if await asyncio.to_thread(_exists_including_symlink, marker_path):
                raise CoverageAlreadyExistsError(
                    f"completed coverage already exists for job {job_dir_name}",
                )
            await atomic_write_bytes(report_path, coverage_bytes)
            await atomic_write_text(marker_path, _manifest_bytes(manifest).decode("utf-8"))
        return manifest

    async def _load_verified(self, job_dir_name: str) -> WorkbenchCoverageLoad:
        """Recompute expected coverage and compare every persisted byte."""

        bundle, digest = await self._verified_bundle(job_dir_name)
        job_dir, report_path, marker_path = self._paths(job_dir_name)

        async with _coverage_lock(marker_path):
            if not await asyncio.to_thread(_is_regular_file, marker_path):
                raise CorruptWorkbenchCoverageError(
                    "coverage manifest is required before coverage is loadable",
                )
            try:
                raw_marker = await asyncio.to_thread(
                    _read_bounded,
                    marker_path,
                    _MAX_MANIFEST_BYTES,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise CorruptWorkbenchCoverageError(
                    "coverage manifest could not be read",
                ) from exc
            manifest = _parse_manifest(raw_marker)
            if manifest.bundle_sha256 != digest:
                raise CorruptWorkbenchCoverageError(
                    "coverage manifest does not match the completed bundle",
                )

            if not await asyncio.to_thread(_is_regular_file, report_path):
                raise CorruptWorkbenchCoverageError("coverage artifact file is missing")
            try:
                raw_coverage = await asyncio.to_thread(
                    _read_bounded,
                    report_path,
                    _MAX_COVERAGE_BYTES,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise CorruptWorkbenchCoverageError(
                    "coverage artifact file could not be read",
                ) from exc

            if len(manifest.files) != 1 or manifest.files[0].relative_path != _COVERAGE_FILENAME:
                raise CorruptWorkbenchCoverageError(
                    "coverage manifest must contain coverage.json only",
                )
            file_entry = manifest.files[0]
            if hashlib.sha256(raw_coverage).hexdigest() != file_entry.sha256:
                raise CorruptWorkbenchCoverageError("coverage artifact digest mismatch")

            candidate_id, requirements = _report_requirements(raw_coverage)
            expected_report, expected_bytes, expected_manifest = self._expected(
                bundle,
                candidate_id,
                requirements,
            )
            if raw_coverage != expected_bytes:
                raise CorruptWorkbenchCoverageError(
                    "coverage artifact bytes mismatch",
                )
            if manifest != expected_manifest:
                raise CorruptWorkbenchCoverageError(
                    "coverage manifest does not match recomputed coverage",
                )
        return WorkbenchCoverageLoad(report=expected_report, manifest=manifest)

    async def load(self, job_dir_name: str) -> CoverageReport:
        """Load and fully verify the persisted coverage report."""

        return (await self._load_verified(job_dir_name)).report

    async def load_report(self, job_dir_name: str) -> CoverageReport:
        """Descriptive alias for :meth:`load`."""

        return await self.load(job_dir_name)

    async def load_manifest(self, job_dir_name: str) -> WorkbenchCoverageManifest:
        """Load and fully verify the persisted coverage marker."""

        return (await self._load_verified(job_dir_name)).manifest

    async def verify(self, job_dir_name: str) -> WorkbenchCoverageManifest:
        """Verify preceding artifacts, coverage bytes, and completion marker."""

        return await self.load_manifest(job_dir_name)

    async def verify_report(self, job_dir_name: str) -> WorkbenchCoverageLoad:
        """Return the verified report and marker for audit-oriented callers."""

        return await self._load_verified(job_dir_name)


__all__ = [
    "CorruptCoverageError",
    "CorruptWorkbenchCoverageError",
    "CoverageAlreadyExistsError",
    "CoverageFile",
    "CoverageLoad",
    "CoverageManifest",
    "WORKBENCH_COVERAGE_MANIFEST_FILENAME",
    "WORKBENCH_COVERAGE_SCHEMA_VERSION",
    "WorkbenchCoverageFile",
    "WorkbenchCoverageLoad",
    "WorkbenchCoverageManifest",
    "WorkbenchCoverageStore",
    "WorkbenchCoverageStoreError",
]
