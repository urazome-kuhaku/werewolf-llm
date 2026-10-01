"""Evidence-to-draft orchestration for one ruleset authoring job.

This module is the boundary between evidence acquisition and human review.  It
passes only bounded excerpts to the Pi synthesis provider, closes every model
citation with :class:`ClaimExtractor`, and persists a structurally complete
``ResearchBundle`` before materializing the deterministic workbench artifacts.
No claim is promoted to ``SUPPORTED`` and no published knowledge is touched.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .analysis import AnalysisReport
from .analysis_store import (
    AnalysisAlreadyExistsError,
    WorkbenchAnalysisStore,
)
from .artifact_store import (
    ArtifactAlreadyExistsError,
    WorkbenchArtifactStore,
)
from .bundle_store import WorkbenchBundleStore
from .bundles import ResearchBundle
from .claim_extractor import ClaimExtractionError, ClaimExtractionResult, ClaimExtractor
from .coverage import CoverageRequirement
from .draft_generation import DraftDocumentTemplate, DraftGenerationContext
from .draft_validation import DraftValidationResult, WorkbenchDraftValidationService
from .evidence import SourceEvidence
from .jobs import ResearchJob, ResearchJobStatus
from .pi_synthesis import (
    DEFAULT_MAX_EVIDENCE_CHARACTERS,
    EvidenceExcerpt,
    RuleSynthesisProvider,
    SynthesisResult,
)
from .research_pipeline import ResearchPipeline, ResearchPipelineResult
from .review_markdown import WorkbenchReviewMarkdownStore
from .store import WorkbenchJobStore

DEFAULT_MAX_SOURCES_PER_BATCH = 8
_CANDIDATE_ID_RE = re.compile(r"[^a-z0-9]+")


class AuthoringPipelineError(RuntimeError):
    """Base error for the evidence-to-review orchestration stage."""


@dataclass(frozen=True, slots=True)
class AuthoringPipelineFailure:
    """One bounded diagnostic retained with an authoring result."""

    phase: str
    message: str


@dataclass(frozen=True, slots=True)
class AuthoringPipelineResult:
    """All inspectable outputs of one authoring attempt."""

    board_name: str
    candidate_id: str
    job_dir_name: str
    research: ResearchPipelineResult
    job: ResearchJob
    evidence_batches: tuple[tuple[EvidenceExcerpt, ...], ...] = ()
    synthesis: SynthesisResult | None = None
    extraction: ClaimExtractionResult | None = None
    bundle: ResearchBundle | None = None
    analysis: AnalysisReport | None = None
    review_markdown: bytes | None = None
    failures: tuple[AuthoringPipelineFailure, ...] = ()

    @property
    def status(self) -> ResearchJobStatus:
        """Return the final persisted job status for this attempt."""

        return self.job.status

    @property
    def succeeded(self) -> bool:
        """Whether a reviewable draft and its analysis were persisted."""

        return self.status is ResearchJobStatus.DRAFTED and self.bundle is not None


CandidateIdFactory = Callable[[str], str]


def default_candidate_id(board_name: str) -> str:
    """Create a stable logical candidate ID from an arbitrary board name."""

    normalized = unicodedata.normalize("NFKC", board_name).casefold()
    slug = _CANDIDATE_ID_RE.sub("-", normalized).strip("-")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    prefix = slug[:50] or "board"
    return f"{prefix}-{digest}"


class ResearchAuthoringPipeline:
    """Run evidence collection, restricted synthesis, and review artifacts."""

    def __init__(
        self,
        research_pipeline: ResearchPipeline,
        synthesis_provider: RuleSynthesisProvider,
        *,
        claim_extractor: ClaimExtractor | None = None,
        job_store: WorkbenchJobStore | None = None,
        bundle_store: WorkbenchBundleStore | None = None,
        artifact_store: WorkbenchArtifactStore | None = None,
        analysis_store: WorkbenchAnalysisStore | None = None,
        review_markdown_store: WorkbenchReviewMarkdownStore | None = None,
        max_sources_per_batch: int = DEFAULT_MAX_SOURCES_PER_BATCH,
        candidate_id_factory: CandidateIdFactory = default_candidate_id,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(research_pipeline, ResearchPipeline):
            raise TypeError("research_pipeline must be a ResearchPipeline")
        if synthesis_provider is None:
            raise TypeError("synthesis_provider is required")
        if (
            isinstance(max_sources_per_batch, bool)
            or not isinstance(max_sources_per_batch, int)
            or max_sources_per_batch < 1
        ):
            raise ValueError("max_sources_per_batch must be a positive integer")
        if not callable(candidate_id_factory):
            raise TypeError("candidate_id_factory must be callable")

        self.research_pipeline = research_pipeline
        self.synthesis_provider = synthesis_provider
        self.claim_extractor = claim_extractor or ClaimExtractor()
        self.job_store = job_store or research_pipeline.job_store
        self.bundle_store = bundle_store or WorkbenchBundleStore(self.job_store)
        self.artifact_store = artifact_store or WorkbenchArtifactStore(
            self.job_store,
            self.bundle_store,
        )
        self.analysis_store = analysis_store or WorkbenchAnalysisStore(
            self.job_store,
            self.bundle_store,
            self.artifact_store,
        )
        self.review_markdown_store = review_markdown_store or WorkbenchReviewMarkdownStore(
            self.job_store,
            self.bundle_store,
            self.analysis_store,
        )
        self.max_sources_per_batch = max_sources_per_batch
        self.candidate_id_factory = candidate_id_factory
        self._now = now or (lambda: datetime.now(UTC))
        self.draft_validation_service = WorkbenchDraftValidationService(
            self.job_store,
            bundle_store=self.bundle_store,
            now=self._now,
        )

    async def run(self, board_name: str) -> AuthoringPipelineResult:
        """Collect evidence and produce a persisted, reviewable draft."""

        research = await self.research_pipeline.run(board_name)
        candidate_id = self.candidate_id_factory(research.board_name)
        if research.status is not ResearchJobStatus.EVIDENCE_COLLECTED:
            research_failures = tuple(
                AuthoringPipelineFailure(item.phase, item.message) for item in research.failures
            )
            if not research_failures:
                research_failures = (
                    AuthoringPipelineFailure(
                        "evidence",
                        f"research ended in {research.status.value}",
                    ),
                )
            return AuthoringPipelineResult(
                board_name=research.board_name,
                candidate_id=candidate_id,
                job_dir_name=research.job_dir_name,
                research=research,
                job=research.job,
                failures=research_failures,
            )

        job = await self.job_store.advance_job(
            research.job_dir_name,
            ResearchJobStatus.EXTRACTING,
            self._next_time(research.job.updated_at),
        )
        evidence_batches = self._build_evidence_batches(research.evidence)
        failures: list[AuthoringPipelineFailure] = []
        synthesis: SynthesisResult | None = None
        extraction: ClaimExtractionResult | None = None
        review_markdown: bytes | None = None

        try:
            synthesis = await self.synthesis_provider.synthesize(
                research.board_name,
                candidate_id,
                evidence_batches,
            )
            extraction = self.claim_extractor.extract(evidence_batches, synthesis)
            if not extraction.claims:
                raise ClaimExtractionError("synthesis produced no valid claims")
        except Exception as exc:
            failure = AuthoringPipelineFailure("extracting", _error_message(exc))
            failures.append(failure)
            job = await self._fail(
                research.job_dir_name,
                job,
                failure.message,
                resume_from=ResearchJobStatus.EXTRACTING,
            )
            return AuthoringPipelineResult(
                board_name=research.board_name,
                candidate_id=candidate_id,
                job_dir_name=research.job_dir_name,
                research=research,
                job=job,
                evidence_batches=evidence_batches,
                synthesis=synthesis,
                extraction=extraction,
                failures=tuple(failures),
            )

        try:
            request = await self.job_store.load_request(research.job_dir_name)
            bundle = ResearchBundle.model_validate(
                {
                    "board_name": research.board_name,
                    "locale": request.locale,
                    "sources": tuple(research.evidence),
                    "claims": tuple(extraction.claims),
                },
                strict=True,
            )
            await self.bundle_store.save_bundle(research.job_dir_name, bundle)
            job = await self.job_store.advance_job(
                research.job_dir_name,
                ResearchJobStatus.ANALYZING,
                self._next_time(job.updated_at),
            )
            await self._materialize_artifacts(research.job_dir_name)
            await self._materialize_analysis(research.job_dir_name)
            review_markdown = await self.review_markdown_store.materialize(
                research.job_dir_name,
            )
            analysis = await self.analysis_store.load(research.job_dir_name)
            job = await self.job_store.advance_job(
                research.job_dir_name,
                ResearchJobStatus.DRAFTED,
                self._next_time(job.updated_at),
            )
        except Exception as exc:
            failure = AuthoringPipelineFailure("analyzing", _error_message(exc))
            failures.append(failure)
            resume_from = (
                ResearchJobStatus.ANALYZING
                if job.status is ResearchJobStatus.ANALYZING
                else ResearchJobStatus.EXTRACTING
            )
            job = await self._fail(
                research.job_dir_name,
                job,
                failure.message,
                resume_from=resume_from,
            )
            return AuthoringPipelineResult(
                board_name=research.board_name,
                candidate_id=candidate_id,
                job_dir_name=research.job_dir_name,
                research=research,
                job=job,
                evidence_batches=evidence_batches,
                synthesis=synthesis,
                extraction=extraction,
                review_markdown=review_markdown,
                failures=tuple(failures),
            )

        return AuthoringPipelineResult(
            board_name=research.board_name,
            candidate_id=candidate_id,
            job_dir_name=research.job_dir_name,
            research=research,
            job=job,
            evidence_batches=evidence_batches,
            synthesis=synthesis,
            extraction=extraction,
            bundle=bundle,
            analysis=analysis,
            review_markdown=review_markdown,
            failures=tuple(failures),
        )

    async def validate_draft(
        self,
        job_dir_name: str,
        *,
        context: DraftGenerationContext,
        templates: Iterable[DraftDocumentTemplate],
        requirements: Iterable[CoverageRequirement | Mapping[str, object]] | None = None,
    ) -> DraftValidationResult:
        """Run the explicit-template validation stage for a ``DRAFTED`` job.

        The method is intentionally separate from :meth:`run`: evidence
        collection can remain reviewable at ``DRAFTED`` while a host supplies
        the exact board/role/mechanic/interaction templates.  Validation then
        advances to ``NEEDS_DECISION`` or ``READY_TO_PUBLISH`` and never calls
        the publisher.
        """

        return await self.draft_validation_service.validate(
            job_dir_name,
            context=context,
            templates=templates,
            requirements=requirements,
        )

    def _build_evidence_batches(
        self,
        evidence: Sequence[SourceEvidence],
    ) -> tuple[tuple[EvidenceExcerpt, ...], ...]:
        excerpts = tuple(
            EvidenceExcerpt(
                source_id=source.source_id,
                content_sha256=source.content_sha256,
                excerpt=source.excerpt[:DEFAULT_MAX_EVIDENCE_CHARACTERS],
            )
            for source in evidence
        )
        return tuple(
            excerpts[index : index + self.max_sources_per_batch]
            for index in range(0, len(excerpts), self.max_sources_per_batch)
        )

    async def _materialize_artifacts(self, job_dir_name: str) -> None:
        try:
            await self.artifact_store.materialize(job_dir_name)
        except ArtifactAlreadyExistsError:
            await self.artifact_store.verify(job_dir_name)

    async def _materialize_analysis(self, job_dir_name: str) -> None:
        try:
            await self.analysis_store.materialize(job_dir_name)
        except AnalysisAlreadyExistsError:
            await self.analysis_store.verify(job_dir_name)

    async def _fail(
        self,
        job_dir_name: str,
        job: ResearchJob,
        message: str,
        *,
        resume_from: ResearchJobStatus,
    ) -> ResearchJob:
        return await self.job_store.fail_job(
            job_dir_name,
            message,
            resume_from,
            self._next_time(job.updated_at),
        )

    def _next_time(self, previous: datetime) -> datetime:
        current = self._now()
        if current.tzinfo is None or current.utcoffset() != datetime.now(UTC).utcoffset():
            raise ValueError("authoring pipeline clock must return an aware UTC datetime")
        current = current.astimezone(UTC)
        return max(current, previous)


# The shorter spelling is convenient for callers and keeps the public entry
# point discoverable beside ``ResearchPipeline``.
AuthoringPipeline = ResearchAuthoringPipeline


def _error_message(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:2_000]


__all__ = [
    "AuthoringPipeline",
    "AuthoringPipelineError",
    "AuthoringPipelineFailure",
    "AuthoringPipelineResult",
    "DEFAULT_MAX_SOURCES_PER_BATCH",
    "ResearchAuthoringPipeline",
    "default_candidate_id",
]
