"""Pure coverage analysis for a ruleset research bundle.

Coverage is deliberately driven by a caller supplied matrix.  This module
does not know about any particular board and does not fill gaps from common
rules.  It only evaluates one candidate against the claims in one validated
research bundle.

The bundle is canonicalized once at the analysis boundary.  ``RuleClaim`` is
not deeply immutable, so using that detached snapshot for every requirement
keeps one report from observing different nested values during iteration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum, unique
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

from .bundle_codec import decode_bundle, encode_bundle
from .bundles import ResearchBundle
from .claims import ClaimScope, ClaimStatus, RuleClaim

_LOGICAL_ID_PATTERN = re.compile(r"[a-z0-9_-]+", re.ASCII)
_KEY_SEGMENT = r"[a-z](?:[a-z0-9]*)(?:_[a-z0-9]+)*"
_CLAIM_KEY_PATTERN = re.compile(
    rf"{_KEY_SEGMENT}(?:\.{_KEY_SEGMENT})+",
    re.ASCII,
)

_LOGICAL_ID = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, strict=True),
]
_CLAIM_KEY = Annotated[
    str,
    StringConstraints(min_length=3, max_length=128, strict=True),
]


@unique
class CoverageStatus(StrEnum):
    """The result for one coverage requirement."""

    MISSING = "MISSING"
    PENDING = "PENDING"
    CONFLICTING = "CONFLICTING"
    SATISFIED = "SATISFIED"


class CoverageRequirement(BaseModel):
    """One caller supplied rule dimension in a board coverage matrix.

    ``conditions`` identifies a precise rule context.  An omitted conditions
    object means the unconditional context (``{}``); it is not a wildcard.
    ``min_independent_evidence_count`` counts distinct source content hashes,
    so mirrors of one document cannot manufacture independent support.  The
    conservative default is two independent sources because this model does
    not currently record whether an official source is verified applicable to
    the selected board/version.  Callers may explicitly set the threshold to
    one after making that determination or recording a human decision, but
    this threshold alone never grants publish permission.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    requirement_id: _LOGICAL_ID
    scope: ClaimScope
    key: _CLAIM_KEY
    conditions: dict[str, JsonValue] = Field(default_factory=dict)
    required: bool = True
    min_independent_evidence_count: int = Field(
        default=2,
        ge=1,
        validation_alias=AliasChoices(
            "min_independent_evidence_count",
            "min_independent_evidence",
            "min_independent_sources",
            "minimum_independent_evidence",
            "minimum_evidence_count",
        ),
    )

    @field_validator("requirement_id")
    @classmethod
    def validate_requirement_id(cls, value: str) -> str:
        """Keep requirement IDs stable and independent of paths or URLs."""

        if _LOGICAL_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "requirement_id must contain only lowercase ASCII letters, digits, '-' or '_',",
            )
        return value

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        """Require the same dotted snake_case key shape as ``RuleClaim``."""

        if _CLAIM_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "key must contain at least two lowercase snake_case segments separated by '.',",
            )
        return value

    @field_validator("conditions", mode="before")
    @classmethod
    def normalise_conditions(cls, value: object) -> object:
        """Treat an explicitly omitted JSON object as the empty context."""

        return {} if value is None else value

    @property
    def min_independent_sources(self) -> int:
        """Readable compatibility name for the evidence threshold."""

        return self.min_independent_evidence_count

    @property
    def minimum_independent_evidence(self) -> int:
        """Compatibility name used by coverage matrix callers."""

        return self.min_independent_evidence_count


class CoverageItem(BaseModel):
    """Evaluation of one requirement in a :class:`CoverageReport`."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    requirement: CoverageRequirement
    status: CoverageStatus
    matched_claim_ids: tuple[_LOGICAL_ID, ...] = ()
    active_claim_ids: tuple[_LOGICAL_ID, ...] = ()
    evidence_ids: tuple[_LOGICAL_ID, ...] = ()
    independent_evidence_count: int = Field(default=0, ge=0)
    reason: str

    @property
    def requirement_id(self) -> str:
        """Expose the stable requirement ID without unpacking ``requirement``."""

        return self.requirement.requirement_id

    @property
    def scope(self) -> ClaimScope:
        """Expose the requirement scope for report consumers."""

        return self.requirement.scope

    @property
    def key(self) -> str:
        """Expose the requirement key for report consumers."""

        return self.requirement.key

    @property
    def blocking(self) -> bool:
        """Whether this item prevents the required coverage gate."""

        return self.requirement.required and self.status is not CoverageStatus.SATISFIED


class CoverageReport(BaseModel):
    """Stable, read-only coverage output for one ruleset candidate."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    ruleset_candidate_id: _LOGICAL_ID
    items: tuple[CoverageItem, ...]
    required_count: int = Field(ge=0)
    satisfied_count: int = Field(ge=0)
    coverage_percentage: float = Field(ge=0.0, le=100.0)
    blocking_requirement_ids: tuple[_LOGICAL_ID, ...] = ()
    blocking_items: tuple[CoverageItem, ...] = ()

    @property
    def required_total(self) -> int:
        """Alias for the total number of required matrix entries."""

        return self.required_count

    @property
    def required_satisfied(self) -> int:
        """Alias for the number of satisfied required entries."""

        return self.satisfied_count

    @property
    def coverage_percent(self) -> float:
        """Alias for ``coverage_percentage``."""

        return self.coverage_percentage

    @property
    def required_coverage(self) -> float:
        """Alias for ``coverage_percentage`` used by publish gates."""

        return self.coverage_percentage

    @property
    def is_complete(self) -> bool:
        """Whether every required matrix item is satisfied."""

        return not self.blocking_requirement_ids

    @property
    def passed(self) -> bool:
        """Readable publish-gate predicate for this pure report."""

        return self.is_complete


def _canonical_json(value: JsonValue) -> str:
    """Return deterministic JSON identity for conditions and values."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _snapshot_requirement(value: CoverageRequirement | Mapping[str, Any]) -> CoverageRequirement:
    """Detach one requirement from mutable caller-owned JSON values."""

    if not isinstance(value, CoverageRequirement):
        try:
            value = CoverageRequirement.model_validate(value, strict=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "coverage requirements must be valid CoverageRequirement values",
            ) from exc

    try:
        payload = value.model_dump(mode="json", round_trip=True)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return CoverageRequirement.model_validate_json(encoded)
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("coverage requirement cannot be canonicalized") from exc


def _snapshot_requirements(
    requirements: Iterable[CoverageRequirement | Mapping[str, Any]],
) -> tuple[CoverageRequirement, ...]:
    """Validate, detach, reject duplicate IDs, and sort a coverage matrix."""

    if isinstance(requirements, (str, bytes)):
        raise TypeError("requirements must be an iterable of CoverageRequirement values")

    snapshots = tuple(_snapshot_requirement(requirement) for requirement in requirements)
    seen: set[str] = set()
    duplicates: list[str] = []
    for requirement in snapshots:
        if requirement.requirement_id in seen and requirement.requirement_id not in duplicates:
            duplicates.append(requirement.requirement_id)
        seen.add(requirement.requirement_id)
    if duplicates:
        raise ValueError(
            "coverage requirements must not contain duplicate requirement_id values: "
            + ", ".join(duplicates),
        )
    return tuple(sorted(snapshots, key=lambda requirement: requirement.requirement_id))


def _candidate_id(value: str) -> str:
    """Validate a candidate ID before looking at the bundle."""

    if not isinstance(value, str):
        raise TypeError("ruleset_candidate_id must be a string")
    if _LOGICAL_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "ruleset_candidate_id must contain only lowercase ASCII letters, digits, '-' or '_',",
        )
    return value


def _claim_sort_key(claim: RuleClaim) -> tuple[str, str, str]:
    """Sort claims independently of imported collection order."""

    return (claim.claim_id, _canonical_json(claim.value), _canonical_json(claim.conditions))


def _evaluate_requirement(
    requirement: CoverageRequirement,
    claims: tuple[RuleClaim, ...],
    source_hashes: Mapping[str, str],
) -> CoverageItem:
    """Evaluate one exact context against the detached candidate claims."""

    matching = tuple(
        sorted(
            (
                claim
                for claim in claims
                if claim.scope is requirement.scope
                and claim.key == requirement.key
                and _canonical_json(claim.conditions) == _canonical_json(requirement.conditions)
            ),
            key=_claim_sort_key,
        ),
    )
    matched_claim_ids = tuple(claim.claim_id for claim in matching)
    active = tuple(claim for claim in matching if claim.status is not ClaimStatus.REJECTED)
    active_claim_ids = tuple(claim.claim_id for claim in active)
    supported = tuple(claim for claim in active if claim.status is ClaimStatus.SUPPORTED)
    evidence_ids = tuple(
        sorted({evidence_id for claim in active for evidence_id in claim.evidence_ids}),
    )
    supported_evidence_ids = tuple(
        sorted({evidence_id for claim in supported for evidence_id in claim.evidence_ids}),
    )
    independent_hashes = {
        source_hashes[evidence_id]
        for evidence_id in supported_evidence_ids
        if evidence_id in source_hashes
    }
    independent_count = len(independent_hashes)

    if not active:
        reason = "all matching claims are rejected" if matching else "no matching claim"
        return CoverageItem(
            requirement=requirement,
            status=CoverageStatus.MISSING,
            matched_claim_ids=matched_claim_ids,
            active_claim_ids=active_claim_ids,
            evidence_ids=evidence_ids,
            independent_evidence_count=0,
            reason=reason,
        )

    values = {_canonical_json(claim.value) for claim in active}
    has_explicit_conflict = any(claim.status is ClaimStatus.CONFLICTING for claim in active)
    if has_explicit_conflict or len(values) > 1:
        reason = (
            "matching claims contain conflicting values"
            if len(values) > 1
            else "a matching claim is marked conflicting"
        )
        return CoverageItem(
            requirement=requirement,
            status=CoverageStatus.CONFLICTING,
            matched_claim_ids=matched_claim_ids,
            active_claim_ids=active_claim_ids,
            evidence_ids=evidence_ids,
            independent_evidence_count=independent_count,
            reason=reason,
        )

    if not supported:
        return CoverageItem(
            requirement=requirement,
            status=CoverageStatus.PENDING,
            matched_claim_ids=matched_claim_ids,
            active_claim_ids=active_claim_ids,
            evidence_ids=evidence_ids,
            independent_evidence_count=0,
            reason="matching claims are not yet supported",
        )

    minimum = requirement.min_independent_evidence_count
    if independent_count < minimum:
        return CoverageItem(
            requirement=requirement,
            status=CoverageStatus.PENDING,
            matched_claim_ids=matched_claim_ids,
            active_claim_ids=active_claim_ids,
            evidence_ids=evidence_ids,
            independent_evidence_count=independent_count,
            reason=(
                "supported claims have insufficient independent evidence: "
                f"{independent_count} < {minimum}"
            ),
        )

    return CoverageItem(
        requirement=requirement,
        status=CoverageStatus.SATISFIED,
        matched_claim_ids=matched_claim_ids,
        active_claim_ids=active_claim_ids,
        evidence_ids=evidence_ids,
        independent_evidence_count=independent_count,
        reason="supported claims meet the independent evidence threshold",
    )


def analyze_coverage(
    bundle: ResearchBundle,
    ruleset_candidate_id: str,
    requirements: Iterable[CoverageRequirement | Mapping[str, Any]],
) -> CoverageReport:
    """Build one deterministic coverage report for a candidate board.

    Only ``SUPPORTED`` claims can satisfy a requirement.  ``UNVERIFIED`` or
    insufficiently evidenced support remains ``PENDING``; ``CONFLICTING`` and
    mutually exclusive active values remain blocking.  Rejected claims are
    retained in the item's trace but never count as evidence or support.
    """

    if not isinstance(bundle, ResearchBundle):
        raise TypeError("analyze_coverage() expects a ResearchBundle")
    candidate_id = _candidate_id(ruleset_candidate_id)
    matrix = _snapshot_requirements(requirements)
    if not matrix or not any(requirement.required for requirement in matrix):
        raise ValueError(
            "coverage requirements must contain at least one required requirement; "
            "empty or optional-only matrices cannot pass the publish gate",
        )

    # Encode/decode exactly once.  All subsequent passes use this detached
    # snapshot, including source hash lookup and claim matching.
    snapshot = decode_bundle(encode_bundle(bundle))
    source_hashes = {source.source_id: source.content_sha256 for source in snapshot.sources}
    candidate_claims = tuple(
        sorted(
            (claim for claim in snapshot.claims if claim.ruleset_candidate_id == candidate_id),
            key=_claim_sort_key,
        ),
    )

    items = tuple(
        _evaluate_requirement(requirement, candidate_claims, source_hashes)
        for requirement in matrix
    )
    required_items = tuple(item for item in items if item.requirement.required)
    required_count = len(required_items)
    satisfied_count = sum(item.status is CoverageStatus.SATISFIED for item in required_items)
    coverage_percentage = (
        100.0 if required_count == 0 else (satisfied_count / required_count) * 100.0
    )
    blocking_items = tuple(item for item in required_items if item.blocking)
    return CoverageReport(
        ruleset_candidate_id=candidate_id,
        items=items,
        required_count=required_count,
        satisfied_count=satisfied_count,
        coverage_percentage=coverage_percentage,
        blocking_requirement_ids=tuple(item.requirement_id for item in blocking_items),
        blocking_items=blocking_items,
    )


def check_coverage(
    bundle: ResearchBundle,
    ruleset_candidate_id: str,
    requirements: Iterable[CoverageRequirement | Mapping[str, Any]],
) -> CoverageReport:
    """Descriptive alias for :func:`analyze_coverage`."""

    return analyze_coverage(bundle, ruleset_candidate_id, requirements)


def build_coverage_report(
    bundle: ResearchBundle,
    ruleset_candidate_id: str,
    requirements: Iterable[CoverageRequirement | Mapping[str, Any]],
) -> CoverageReport:
    """Descriptive alias used by workbench pipeline callers."""

    return analyze_coverage(bundle, ruleset_candidate_id, requirements)


class CoverageAnalyzer:
    """Stateless facade for callers that model workbench stages as services."""

    def analyze(
        self,
        bundle: ResearchBundle,
        ruleset_candidate_id: str,
        requirements: Iterable[CoverageRequirement | Mapping[str, Any]],
    ) -> CoverageReport:
        return analyze_coverage(bundle, ruleset_candidate_id, requirements)


# Short aliases keep older workbench call sites readable while preserving one
# implementation and one report schema.
CoverageResult = CoverageItem
CoverageState = CoverageStatus


__all__ = [
    "CoverageAnalyzer",
    "CoverageItem",
    "CoverageReport",
    "CoverageRequirement",
    "CoverageResult",
    "CoverageState",
    "CoverageStatus",
    "analyze_coverage",
    "build_coverage_report",
    "check_coverage",
]
