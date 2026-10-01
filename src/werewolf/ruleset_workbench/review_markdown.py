"""Deterministic human-review Markdown for one workbench research job.

The review document is deliberately a workbench artifact.  It is generated
from a completed :class:`ResearchBundle` and :class:`AnalysisReport`, keeps
all claim-to-source references closed, and never turns model output into an
executable ruleset.  A missing coverage report is represented explicitly as
"not evaluated" so the document cannot be mistaken for a passed gate.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from werewolf.persistence import (
    PathSecurityError,
    atomic_write_bytes,
    resolve_contained_path,
)

from .analysis import AnalysisReport, RuleConflict, RuleContext
from .analysis_store import WorkbenchAnalysisStore
from .bundle_codec import bundle_sha256
from .bundle_store import WorkbenchBundleStore
from .bundles import ResearchBundle
from .claims import ClaimScope, ClaimStatus, RuleClaim
from .coverage import CoverageReport
from .evidence import SourceEvidence
from .store import WorkbenchJobStore

REVIEW_MARKDOWN_FILENAME = "review.md"
_SCOPE_ORDER = {
    ClaimScope.BOARD: 0,
    ClaimScope.ROLE: 1,
    ClaimScope.MECHANIC: 2,
    ClaimScope.INTERACTION: 3,
}
_REVIEW_LOCKS: dict[Path, asyncio.Lock] = {}


class ReviewMarkdownError(ValueError):
    """Raised when a review document cannot be rendered or verified."""


class ReviewMarkdownAlreadyExistsError(ReviewMarkdownError):
    """Raised when a different completed review document occupies the path."""


class CorruptReviewMarkdownError(ReviewMarkdownError):
    """Raised when a persisted review document differs from its source data."""


@dataclass(frozen=True, slots=True)
class ReviewMarkdown:
    """Immutable UTF-8 bytes for the single workbench review file."""

    content: bytes

    @property
    def markdown(self) -> bytes:
        """Return the UTF-8 Markdown bytes."""

        return self.content

    @property
    def text(self) -> str:
        """Return the Markdown decoded as UTF-8 text."""

        return self.content.decode("utf-8")

    @property
    def files(self) -> tuple[tuple[str, bytes], ...]:
        """Return the workbench-relative file and its exact bytes."""

        return ((REVIEW_MARKDOWN_FILENAME, self.content),)


def _lock(path: Path) -> asyncio.Lock:
    return _REVIEW_LOCKS.setdefault(path, asyncio.Lock())


def _normalise_text(value: str) -> str:
    return " ".join(value.replace("\r\n", "\n").replace("\r", "\n").splitlines()).strip()


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReviewMarkdownError("review data contains a non-JSON value") from exc


def _inline(value: object) -> str:
    """Make a value safe for a Markdown inline code span."""

    return str(value).replace("`", "' ").replace("\r", " ").replace("\n", " ").strip()


def _quote(excerpt: str) -> str:
    """Render an excerpt as a line-stable Markdown block quote."""

    normalised = excerpt.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return "\n".join("> " + line for line in normalised.split("\n"))


def _context(context: RuleContext) -> str:
    return (
        f"candidate `{_inline(context.ruleset_candidate_id)}`, "
        f"scope `{context.scope.value}`, key `{_inline(context.key)}`, "
        f"conditions `{_inline(context.canonical_conditions)}`"
    )


def _source_line(source: SourceEvidence) -> str:
    title = _normalise_text(source.title).replace("|", "\\|")
    url = str(source.url).replace("|", "\\|")
    return (
        f"| `{_inline(source.source_id)}` | {title} | <{url}> | "
        f"`{source.source_class.value}` | `{source.fetched_at.isoformat()}` |"
    )


def _claim_sort_key(claim: RuleClaim) -> tuple[object, ...]:
    return (
        _SCOPE_ORDER[claim.scope],
        claim.scope.value,
        claim.ruleset_candidate_id,
        claim.key,
        _json(claim.conditions),
        claim.claim_id,
    )


def _source_map(bundle: ResearchBundle) -> dict[str, SourceEvidence]:
    sources = {source.source_id: source for source in bundle.sources}
    if len(sources) != len(bundle.sources):
        raise ReviewMarkdownError("research bundle contains duplicate source IDs")
    return sources


def _claim_evidence_lines(claim: RuleClaim, sources: dict[str, SourceEvidence]) -> list[str]:
    lines: list[str] = []
    for evidence_id in sorted(claim.evidence_ids):
        source = sources.get(evidence_id)
        if source is None:
            raise ReviewMarkdownError(
                f"claim {claim.claim_id} references missing evidence {evidence_id}",
            )
        lines.extend(
            [
                f"- Source `{_inline(source.source_id)}`: {_normalise_text(source.title)} "
                f"(<{source.url}>)",
                _quote(source.excerpt),
            ],
        )
    return lines


def _render_claim(claim: RuleClaim, sources: dict[str, SourceEvidence]) -> list[str]:
    lines = [
        f"### `{_inline(claim.claim_id)}` — `{_inline(claim.key)}`",
        f"- Status: `{claim.status.value}` (human review is still required)",
        f"- Candidate: `{_inline(claim.ruleset_candidate_id)}`",
        f"- Scope: `{claim.scope.value}`",
        f"- Conditions: `{_inline(_json(claim.conditions))}`",
        f"- Value: `{_inline(_json(claim.value))}`",
        f"- Confidence: `{claim.confidence:.6f}`",
    ]
    if claim.extraction_note:
        lines.append(f"- Extraction note: {_normalise_text(claim.extraction_note)}")
    lines.extend(["- Evidence excerpts:", *_claim_evidence_lines(claim, sources), ""])
    return lines


def _conflict_values(conflict: RuleConflict) -> list[str]:
    lines: list[str] = []
    for value in conflict.values:
        lines.append(
            f"  - Value `{_inline(value.canonical_value)}`; claims "
            f"`{', '.join(sorted(value.claim_ids))}`; evidence "
            f"`{', '.join(sorted(value.evidence_ids))}`; "
            f"independent sources `{value.independent_evidence_count}`",
        )
    return lines


def _render_conflict(conflict: RuleConflict) -> list[str]:
    return [
        f"### `{_inline(conflict.context.key)}`",
        f"- Context: {_context(conflict.context)}",
        "- Resolution: `NEEDS_DECISION`",
        "- Conflicting values:",
        *_conflict_values(conflict),
        "",
    ]


def _render_coverage(
    coverage: CoverageReport | None,
    candidate_ids: tuple[str, ...],
) -> list[str]:
    lines = ["## Coverage assessment", ""]
    if coverage is None:
        return lines + [
            "- Status: `NOT_EVALUATED`",
            "- No `CoverageReport` was supplied or persisted. Coverage has not been assessed "
            "and must not be treated as passed.",
            "",
        ]
    if coverage.ruleset_candidate_id not in candidate_ids:
        raise ReviewMarkdownError(
            "coverage report candidate ID is absent from the research bundle",
        )
    gate = "PASSED" if coverage.passed else "BLOCKED"
    lines.extend(
        [
            f"- Status: `ASSESSED`; candidate `{_inline(coverage.ruleset_candidate_id)}`",
            f"- Required coverage: `{coverage.satisfied_count}/{coverage.required_count}` "
            f"(`{coverage.coverage_percentage:.6f}%`); gate: `{gate}`",
        ],
    )
    if coverage.blocking_items:
        lines.append("- Blocking requirements:")
        lines.extend(
            f"  - `{_inline(item.requirement_id)}`: `{item.status.value}` — "
            f"{_normalise_text(item.reason)}"
            for item in sorted(coverage.blocking_items, key=lambda item: item.requirement_id)
        )
    else:
        lines.append("- Blocking requirements: none")
    lines.append("")
    return lines


def render_review_markdown(
    bundle: ResearchBundle,
    analysis: AnalysisReport,
    coverage: CoverageReport | None = None,
) -> bytes:
    """Render one deterministic review document as UTF-8/LF bytes.

    The renderer validates the two source models, checks every claim citation
    against the bundle, and preserves unresolved analysis decisions.  It does
    not select a conflict value or add review metadata.
    """

    if not isinstance(bundle, ResearchBundle):
        raise TypeError("render_review_markdown() expects a ResearchBundle")
    if not isinstance(analysis, AnalysisReport):
        raise TypeError("render_review_markdown() expects an AnalysisReport")
    if coverage is not None and not isinstance(coverage, CoverageReport):
        raise TypeError("coverage must be a CoverageReport or None")

    sources = _source_map(bundle)
    candidate_ids = tuple(
        sorted(
            {claim.ruleset_candidate_id for claim in bundle.claims}
            | {variant.ruleset_candidate_id for variant in analysis.variants},
        ),
    )
    claims = tuple(sorted(bundle.claims, key=_claim_sort_key))
    unverified = tuple(claim for claim in claims if claim.status is ClaimStatus.UNVERIFIED)
    status_counts = {
        status.value: sum(claim.status is status for claim in claims) for status in ClaimStatus
    }
    conflicts = tuple(
        sorted(
            analysis.unresolved_conflicts,
            key=lambda conflict: (
                conflict.context.sort_key,
                tuple(value.canonical_value for value in conflict.values),
            ),
        ),
    )

    lines = [
        "# Ruleset review draft",
        "",
        f"- Board: {_normalise_text(bundle.board_name)}",
        f"- Candidate IDs: {', '.join(f'`{_inline(item)}`' for item in candidate_ids)}",
        f"- Bundle SHA-256: `{bundle_sha256(bundle)}`",
        "- Review state: `HUMAN_REVIEW_REQUIRED`",
        "- Reviewer: not assigned",
        "- This document is a workbench review record. Extracted claims remain "
        "non-authoritative until an explicit review and release workflow accepts them.",
        "",
        "## Sources",
        "",
        "| ID | Title | URL | Class | Fetched at |",
        "| --- | --- | --- | --- | --- |",
        *[
            _source_line(source)
            for source in sorted(bundle.sources, key=lambda item: item.source_id)
        ],
        "",
        "## Claim status summary",
        "",
        *[f"- `{status}`: `{status_counts[status]}`" for status in sorted(status_counts)],
        "- All extracted claims remain subject to human review; this summary does not "
        "authorize any value.",
        "",
        "## UNVERIFIED claims by scope",
        "",
    ]

    for scope in ClaimScope:
        scope_claims = tuple(claim for claim in unverified if claim.scope is scope)
        lines.extend([f"### {scope.value}", ""])
        if not scope_claims:
            lines.extend(["_No UNVERIFIED claims in this scope._", ""])
            continue
        for claim in scope_claims:
            lines.extend(_render_claim(claim, sources))

    lines.extend(["## Candidate variants", ""])
    if not analysis.variants:
        lines.extend(["_No candidate variants were produced._", ""])
    else:
        for variant in sorted(analysis.variants, key=lambda item: item.ruleset_candidate_id):
            lines.extend(
                [
                    f"- Candidate `{_inline(variant.ruleset_candidate_id)}`: "
                    f"`{len(variant.claim_ids)}` claims, `{len(variant.contexts)}` contexts, "
                    f"`{len(variant.active_claim_ids)}` non-rejected claims",
                ],
            )
        lines.append("")

    lines.extend(["## Variant conflicts", ""])
    if not conflicts:
        lines.extend(["_No unresolved variant conflicts were detected._", ""])
    else:
        for conflict in conflicts:
            lines.extend(_render_conflict(conflict))

    lines.extend(["## Host decisions required", ""])
    decision_contexts = tuple(sorted(analysis.decision_contexts, key=lambda item: item.sort_key))
    if not decision_contexts:
        lines.extend(["_No host decisions are currently recorded._", ""])
    else:
        lines.append(
            "The following contexts remain unresolved and require an explicit host decision:",
        )
        lines.extend(
            f"- `{_inline(context.key)}`: {_context(context)}" for context in decision_contexts
        )
        lines.append("")

    lines.extend(_render_coverage(coverage, candidate_ids))
    lines.extend(
        [
            "## Review boundary",
            "",
            "No reviewer identity, approval, machine rule, or release metadata is inferred "
            "by this document.",
            "",
        ],
    )
    text = "\n".join(lines).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewMarkdownError("review Markdown is not valid UTF-8") from exc


def render_review_document(
    bundle: ResearchBundle,
    analysis: AnalysisReport,
    coverage: CoverageReport | None = None,
) -> ReviewMarkdown:
    """Return the immutable wrapper used by callers handling artifacts."""

    return ReviewMarkdown(content=render_review_markdown(bundle, analysis, coverage))


class WorkbenchReviewMarkdownStore:
    """Persist and verify the review Markdown below one workbench job."""

    def __init__(
        self,
        job_store: WorkbenchJobStore,
        bundle_store: WorkbenchBundleStore,
        analysis_store: WorkbenchAnalysisStore,
    ) -> None:
        if not isinstance(job_store, WorkbenchJobStore):
            raise TypeError("job_store must be a WorkbenchJobStore")
        if not isinstance(bundle_store, WorkbenchBundleStore):
            raise TypeError("bundle_store must be a WorkbenchBundleStore")
        if not isinstance(analysis_store, WorkbenchAnalysisStore):
            raise TypeError("analysis_store must be a WorkbenchAnalysisStore")
        self._job_store = job_store
        self._bundle_store = bundle_store
        self._analysis_store = analysis_store

    def _path(self, job_dir_name: str) -> Path:
        try:
            job_dir = resolve_contained_path(self._job_store.root, job_dir_name)
            return resolve_contained_path(job_dir, REVIEW_MARKDOWN_FILENAME)
        except PathSecurityError as exc:
            raise ReviewMarkdownError("review Markdown path escapes the job directory") from exc

    async def _expected(
        self,
        job_dir_name: str,
        coverage: CoverageReport | None = None,
    ) -> bytes:
        bundle = await self._bundle_store.load_bundle(job_dir_name)
        analysis = await self._analysis_store.load(job_dir_name)
        return render_review_markdown(bundle, analysis, coverage)

    async def materialize(
        self,
        job_dir_name: str,
        coverage: CoverageReport | None = None,
    ) -> bytes:
        """Atomically write the review file after verifying saved inputs.

        Repeating the call with the same inputs is idempotent.  A different
        render never replaces an existing review file.
        """

        content = await self._expected(job_dir_name, coverage)
        path = self._path(job_dir_name)
        async with _lock(path):
            if path.exists() or path.is_symlink():
                if not path.is_file():
                    raise ReviewMarkdownAlreadyExistsError(
                        f"review Markdown path is occupied for job {job_dir_name}",
                    )
                try:
                    existing = await asyncio.to_thread(path.read_bytes)
                except (OSError, UnicodeError) as exc:
                    raise CorruptReviewMarkdownError(
                        f"review Markdown could not be read for job {job_dir_name}",
                    ) from exc
                if existing != content:
                    raise ReviewMarkdownAlreadyExistsError(
                        f"different review Markdown already exists for job {job_dir_name}",
                    )
                return existing
            await atomic_write_bytes(path, content)
        return content

    async def load(self, job_dir_name: str, coverage: CoverageReport | None = None) -> bytes:
        """Load and verify the deterministic review file."""

        expected = await self._expected(job_dir_name, coverage)
        path = self._path(job_dir_name)
        async with _lock(path):
            if not path.is_file():
                raise CorruptReviewMarkdownError(
                    "review Markdown is required before it is loadable",
                )
            try:
                actual = await asyncio.to_thread(path.read_bytes)
            except (OSError, UnicodeError) as exc:
                raise CorruptReviewMarkdownError("review Markdown could not be read") from exc
            if actual != expected:
                raise CorruptReviewMarkdownError(
                    "review Markdown does not match the completed bundle and analysis",
                )
        return actual

    async def verify(self, job_dir_name: str, coverage: CoverageReport | None = None) -> bytes:
        """Alias for :meth:`load` used by draft review gates."""

        return await self.load(job_dir_name, coverage)


ReviewMarkdownStore = WorkbenchReviewMarkdownStore


__all__ = [
    "CorruptReviewMarkdownError",
    "REVIEW_MARKDOWN_FILENAME",
    "ReviewMarkdown",
    "ReviewMarkdownAlreadyExistsError",
    "ReviewMarkdownError",
    "ReviewMarkdownStore",
    "WorkbenchReviewMarkdownStore",
    "render_review_document",
    "render_review_markdown",
]
