"""Validation and materialization service for one workbench draft package.

The service is intentionally explicit about templates.  Claims authorize
provenance, while the caller supplied templates provide the machine fields;
the workbench never invents a ruleset from a board name or from an LLM
summary.  A successful machine gate ends at ``READY_TO_PUBLISH`` and still
requires a separate human review and publisher invocation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from werewolf.persistence import atomic_write_bytes, resolve_contained_path

from .bundle_codec import bundle_sha256
from .bundle_store import WorkbenchBundleStore
from .bundles import ResearchBundle
from .coverage import CoverageReport, CoverageRequirement
from .coverage_artifacts import render_coverage_artifacts
from .draft_generation import (
    DraftDocumentTemplate,
    DraftGenerationBlockedError,
    DraftGenerationContext,
    DraftGenerationError,
    DraftPackage,
    generate_draft_package,
    materialize_draft_package,
)
from .jobs import ResearchJob, ResearchJobStatus
from .store import WorkbenchJobStore


class DraftValidationError(RuntimeError):
    """Raised when a validation service request cannot be completed."""


@dataclass(frozen=True, slots=True)
class DraftValidationResult:
    """Inspectable result of one validation attempt."""

    job: ResearchJob
    package: DraftPackage | None = None
    error: str | None = None
    coverage: CoverageReport | None = None

    @property
    def status(self) -> ResearchJobStatus:
        """Return the durable workflow state after the attempt."""

        return self.job.status

    @property
    def machine_gates_passed(self) -> bool:
        """Whether coverage passed; human review remains outstanding."""

        return self.package is not None and self.package.coverage.passed

    @property
    def ready_to_publish(self) -> bool:
        """Whether the machine gate passed without implying publication."""

        return self.status is ResearchJobStatus.READY_TO_PUBLISH and self.machine_gates_passed


DraftTemplateProvider = Callable[
    [ResearchBundle, DraftGenerationContext],
    Iterable[DraftDocumentTemplate],
]
DraftRequirementsProvider = Callable[
    [ResearchBundle, DraftGenerationContext],
    Iterable[CoverageRequirement | Mapping[str, object]] | None,
]


async def _persist_blocked_coverage(
    job_root: Path,
    bundle: ResearchBundle,
    coverage: CoverageReport,
) -> None:
    """Persist a blocked validation's evidence without creating a package marker.

    A blocked draft has no complete document package, so ``publish-manifest``
    must never be written.  The coverage report is still useful to the human
    reviewer and is written independently.  Repeating the same blocked
    validation is idempotent; a different existing report is treated as a
    conflict instead of being overwritten.
    """

    coverage_path = resolve_contained_path(job_root, "coverage.json")
    coverage_bytes = render_coverage_artifacts(coverage, bundle_sha256(bundle)).coverage
    if coverage_path.exists() or coverage_path.is_symlink():
        if coverage_path.is_symlink() or not coverage_path.is_file():
            raise DraftValidationError("existing coverage.json is not a regular file")
        try:
            existing = await asyncio.to_thread(coverage_path.read_bytes)
        except OSError as exc:
            raise DraftValidationError("existing coverage.json could not be read") from exc
        if existing != coverage_bytes:
            raise DraftValidationError("existing coverage.json contains different coverage")
        return
    await atomic_write_bytes(coverage_path, coverage_bytes)


class WorkbenchDraftValidationService:
    """Run deterministic draft generation against a persisted workbench job."""

    def __init__(
        self,
        job_store: WorkbenchJobStore,
        *,
        bundle_store: WorkbenchBundleStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(job_store, WorkbenchJobStore):
            raise TypeError("job_store must be a WorkbenchJobStore")
        self.job_store = job_store
        self.bundle_store = bundle_store or WorkbenchBundleStore(job_store)
        self._now = now or (lambda: datetime.now(UTC))

    async def validate(
        self,
        job_dir_name: str,
        *,
        context: DraftGenerationContext,
        templates: Iterable[DraftDocumentTemplate],
        requirements: Iterable[CoverageRequirement | Mapping[str, object]] | None = None,
    ) -> DraftValidationResult:
        """Validate, materialize, and advance one persisted draft job.

        ``DraftGenerationError`` is a machine gate and moves the job to
        ``NEEDS_DECISION``.  Storage or unexpected failures are retained as a
        ``FAILED`` job with ``VALIDATING`` as the recovery checkpoint.
        """

        current = await self.job_store.load_job(job_dir_name)
        if current.status is ResearchJobStatus.DRAFTED:
            current = await self.job_store.advance_job(
                job_dir_name,
                ResearchJobStatus.VALIDATING,
                self._next_time(current.updated_at),
            )
        elif current.status is not ResearchJobStatus.VALIDATING:
            raise DraftValidationError(
                "draft validation requires a DRAFTED or VALIDATING job; "
                f"found {current.status.value}"
            )

        try:
            bundle = await self.bundle_store.load_bundle(job_dir_name)
            package = generate_draft_package(
                bundle,
                templates,
                context=context,
                requirements=requirements,
            )
            job_root = resolve_contained_path(self.job_store.root, job_dir_name)
            await materialize_draft_package(package, job_root)
            target = (
                ResearchJobStatus.READY_TO_PUBLISH
                if package.coverage.passed
                else ResearchJobStatus.NEEDS_DECISION
            )
            updated = await self.job_store.advance_job(
                job_dir_name,
                target,
                self._next_time(current.updated_at),
            )
            return DraftValidationResult(job=updated, package=package)
        except DraftGenerationError as exc:
            if isinstance(exc, DraftGenerationBlockedError) and exc.coverage is not None:
                try:
                    await _persist_blocked_coverage(
                        resolve_contained_path(self.job_store.root, job_dir_name),
                        bundle,
                        exc.coverage,
                    )
                except Exception as coverage_exc:
                    updated = await self.job_store.fail_job(
                        job_dir_name,
                        f"{exc}; coverage persistence failed: {coverage_exc}"[:2_000],
                        ResearchJobStatus.VALIDATING,
                        self._next_time(current.updated_at),
                    )
                    return DraftValidationResult(
                        job=updated,
                        error=f"{exc}; coverage persistence failed: {coverage_exc}",
                        coverage=exc.coverage,
                    )
            updated = await self.job_store.advance_job(
                job_dir_name,
                ResearchJobStatus.NEEDS_DECISION,
                self._next_time(current.updated_at),
            )
            return DraftValidationResult(
                job=updated,
                error=str(exc),
                coverage=(exc.coverage if isinstance(exc, DraftGenerationBlockedError) else None),
            )
        except Exception as exc:
            updated = await self.job_store.fail_job(
                job_dir_name,
                str(exc)[:2_000] or exc.__class__.__name__,
                ResearchJobStatus.VALIDATING,
                self._next_time(current.updated_at),
            )
            return DraftValidationResult(job=updated, error=str(exc))

    async def run(
        self,
        job_dir_name: str,
        *,
        context: DraftGenerationContext,
        templates: Iterable[DraftDocumentTemplate],
        requirements: Iterable[CoverageRequirement | Mapping[str, object]] | None = None,
    ) -> DraftValidationResult:
        """Descriptive alias for :meth:`validate`."""

        return await self.validate(
            job_dir_name,
            context=context,
            templates=templates,
            requirements=requirements,
        )

    def _next_time(self, previous: datetime) -> datetime:
        current = self._now()
        if current.tzinfo is None or current.utcoffset() != datetime.now(UTC).utcoffset():
            raise ValueError("draft validation clock must return an aware UTC datetime")
        return max(current.astimezone(UTC), previous)


DraftValidationService = WorkbenchDraftValidationService


__all__ = [
    "DraftRequirementsProvider",
    "DraftTemplateProvider",
    "DraftValidationError",
    "DraftValidationResult",
    "DraftValidationService",
    "WorkbenchDraftValidationService",
]
