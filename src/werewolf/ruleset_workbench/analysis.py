"""Deterministic variant clustering and conflict analysis for research bundles.

The workbench deliberately keeps this module as a pure read-only step.  A
``ResearchBundle`` is already structurally validated when it reaches the
analyzer; this module groups its claims and reports the decisions that still
need a human.  It does not rank sources, change claim status, or write any
workbench artifacts.

Contexts are identified by the candidate ID, claim scope, claim key, and a
canonical JSON representation of the claim conditions.  Values are compared
using the same canonical JSON representation.  This means that dictionary
ordering in imported JSON cannot change either the clustering result or the
conflict report.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Annotated, ClassVar, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from .bundle_codec import decode_bundle, encode_bundle
from .bundles import ResearchBundle
from .claims import ClaimScope, ClaimStatus, RuleClaim

_LOGICAL_ID = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, strict=True),
]
_CLAIM_KEY = Annotated[
    str,
    StringConstraints(min_length=3, max_length=128, strict=True),
]


def canonical_json(value: JsonValue) -> str:
    """Return the stable JSON form used for context and value identity.

    Pydantic has already restricted claim values and conditions to JSON values,
    so serializing with sorted keys is sufficient to make equivalent object
    key orderings compare equal.  Compact separators also keep the persisted
    conflict keys readable and independent of formatting choices.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decoded_canonical_json(value: str) -> JsonValue:
    """Decode a canonical JSON string for deterministic model output."""

    return cast(JsonValue, json.loads(value))


def _decoded_canonical_object(value: str) -> dict[str, JsonValue]:
    """Decode a canonical JSON object used for a rule context."""

    decoded = _decoded_canonical_json(value)
    if not isinstance(decoded, dict):
        raise ValueError("canonical conditions must decode to a JSON object")
    return decoded


class RuleContext(BaseModel):
    """One candidate-scoped rule context shared by one or more claims."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    ruleset_candidate_id: _LOGICAL_ID
    scope: ClaimScope
    key: _CLAIM_KEY
    conditions: dict[str, JsonValue]
    canonical_conditions: str

    @classmethod
    def from_claim(cls, claim: RuleClaim) -> Self:
        """Build a context from one validated claim."""

        canonical_conditions = canonical_json(claim.conditions)
        return cls(
            ruleset_candidate_id=claim.ruleset_candidate_id,
            scope=claim.scope,
            key=claim.key,
            conditions=_decoded_canonical_object(canonical_conditions),
            canonical_conditions=canonical_conditions,
        )

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        """Return the complete deterministic context ordering key."""

        return (
            self.ruleset_candidate_id,
            self.scope.value,
            self.key,
            self.canonical_conditions,
        )


class VariantContext(BaseModel):
    """Claims belonging to one context inside a candidate variant."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    context: RuleContext
    claim_ids: tuple[_LOGICAL_ID, ...]
    active_claim_ids: tuple[_LOGICAL_ID, ...]


class CandidateVariant(BaseModel):
    """A deterministic cluster of all claims for one candidate board variant."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    ruleset_candidate_id: _LOGICAL_ID
    claim_ids: tuple[_LOGICAL_ID, ...]
    active_claim_ids: tuple[_LOGICAL_ID, ...]
    contexts: tuple[VariantContext, ...]

    @property
    def context_keys(self) -> tuple[RuleContext, ...]:
        """Return contexts in their stable order for callers writing variants."""

        return tuple(item.context for item in self.contexts)


class ConflictValue(BaseModel):
    """One distinct value asserted for a conflicting rule context.

    Evidence IDs are de-duplicated across claims.  The independent evidence
    count is an estimate based on distinct content hashes for those sources;
    it is not proof that the sources have independent real-world provenance.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    value: JsonValue
    canonical_value: str
    claim_ids: tuple[_LOGICAL_ID, ...]
    evidence_ids: tuple[_LOGICAL_ID, ...]
    independent_evidence_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_independent_evidence_count(self) -> Self:
        """Keep the estimate bounded by the evidence IDs it summarizes."""

        if self.independent_evidence_count > len(self.evidence_ids):
            raise ValueError(
                "independent_evidence_count cannot exceed evidence_ids",
            )
        return self


class RuleConflict(BaseModel):
    """A blocking disagreement that requires a human decision."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    context: RuleContext
    values: tuple[ConflictValue, ...]
    claim_ids: tuple[_LOGICAL_ID, ...]
    evidence_ids: tuple[_LOGICAL_ID, ...]
    unresolved: Literal[True] = True
    needs_human_decision: Literal[True] = True
    resolution: Literal["NEEDS_DECISION"] = "NEEDS_DECISION"

    @property
    def key(self) -> str:
        """Expose the rule key directly for report and CLI consumers."""

        return self.context.key

    @property
    def ruleset_candidate_id(self) -> str:
        """Expose the candidate ID directly for report and CLI consumers."""

        return self.context.ruleset_candidate_id


class AnalysisReport(BaseModel):
    """Complete pure analysis output for a validated research bundle."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    variants: tuple[CandidateVariant, ...]
    conflicts: tuple[RuleConflict, ...]
    unresolved_conflicts: tuple[RuleConflict, ...]
    keys_needing_decision: tuple[_CLAIM_KEY, ...]
    decision_contexts: tuple[RuleContext, ...]

    _active_statuses: ClassVar[frozenset[ClaimStatus]] = frozenset(
        status for status in ClaimStatus if status is not ClaimStatus.REJECTED
    )

    @property
    def has_unresolved_conflicts(self) -> bool:
        """Whether publishing must wait for a human decision."""

        return bool(self.unresolved_conflicts)

    @property
    def candidate_variants(self) -> tuple[CandidateVariant, ...]:
        """Compatibility name for callers using the design-doc terminology."""

        return self.variants


def _context_sort_key(claim: RuleClaim) -> tuple[str, str, str, str]:
    return (
        claim.ruleset_candidate_id,
        claim.scope.value,
        claim.key,
        canonical_json(claim.conditions),
    )


def _claim_sort_key(claim: RuleClaim) -> tuple[str, str, str, str, str, str]:
    return (
        *_context_sort_key(claim),
        canonical_json(claim.value),
        claim.claim_id,
    )


def _unique_sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _canonical_snapshot(bundle: ResearchBundle) -> ResearchBundle:
    """Return one validated detached snapshot for the complete analysis.

    ``RuleClaim`` intentionally contains mutable JSON fields.  Encoding and
    decoding once at the boundary both revalidates those fields and gives the
    variants and conflict passes the same detached input.
    """

    return decode_bundle(encode_bundle(bundle))


def _build_variants(bundle: ResearchBundle) -> tuple[CandidateVariant, ...]:
    by_candidate: dict[str, list[RuleClaim]] = defaultdict(list)
    for claim in bundle.claims:
        by_candidate[claim.ruleset_candidate_id].append(claim)

    variants: list[CandidateVariant] = []
    for candidate_id in sorted(by_candidate):
        claims = sorted(by_candidate[candidate_id], key=_claim_sort_key)
        by_context: dict[tuple[str, str, str], list[RuleClaim]] = defaultdict(list)
        for claim in claims:
            by_context[(claim.scope.value, claim.key, canonical_json(claim.conditions))].append(
                claim,
            )

        contexts: list[VariantContext] = []
        for context_claims in by_context.values():
            ordered_claims = sorted(context_claims, key=_claim_sort_key)
            context = RuleContext.from_claim(ordered_claims[0])
            contexts.append(
                VariantContext(
                    context=context,
                    claim_ids=tuple(claim.claim_id for claim in ordered_claims),
                    active_claim_ids=tuple(
                        claim.claim_id
                        for claim in ordered_claims
                        if claim.status in AnalysisReport._active_statuses
                    ),
                ),
            )

        contexts.sort(key=lambda item: item.context.sort_key)
        variants.append(
            CandidateVariant(
                ruleset_candidate_id=candidate_id,
                claim_ids=tuple(claim.claim_id for claim in claims),
                active_claim_ids=tuple(
                    claim.claim_id
                    for claim in claims
                    if claim.status in AnalysisReport._active_statuses
                ),
                contexts=tuple(contexts),
            ),
        )
    return tuple(variants)


def _build_conflicts(bundle: ResearchBundle) -> tuple[RuleConflict, ...]:
    source_hashes = {source.source_id: source.content_sha256 for source in bundle.sources}
    by_context: dict[tuple[str, str, str, str], list[RuleClaim]] = defaultdict(list)
    for claim in bundle.claims:
        if claim.status is ClaimStatus.REJECTED:
            continue
        by_context[
            (
                claim.ruleset_candidate_id,
                claim.scope.value,
                claim.key,
                canonical_json(claim.conditions),
            )
        ].append(claim)

    conflicts: list[RuleConflict] = []
    for context_claims in by_context.values():
        ordered_claims = sorted(context_claims, key=_claim_sort_key)
        by_value: dict[str, list[RuleClaim]] = defaultdict(list)
        for claim in ordered_claims:
            by_value[canonical_json(claim.value)].append(claim)
        if len(by_value) < 2:
            continue

        context = RuleContext.from_claim(ordered_claims[0])
        values: list[ConflictValue] = []
        for canonical_value in sorted(by_value):
            value_claims = sorted(by_value[canonical_value], key=_claim_sort_key)
            evidence_ids = _unique_sorted(
                evidence_id for claim in value_claims for evidence_id in claim.evidence_ids
            )
            independent_content_hashes = {
                source_hashes[evidence_id] for evidence_id in evidence_ids
            }
            values.append(
                ConflictValue(
                    value=_decoded_canonical_json(canonical_value),
                    canonical_value=canonical_value,
                    claim_ids=tuple(claim.claim_id for claim in value_claims),
                    evidence_ids=evidence_ids,
                    independent_evidence_count=len(independent_content_hashes),
                ),
            )

        claim_ids = _unique_sorted(claim.claim_id for claim in ordered_claims)
        evidence_ids = _unique_sorted(
            evidence_id for claim in ordered_claims for evidence_id in claim.evidence_ids
        )
        conflicts.append(
            RuleConflict(
                context=context,
                values=tuple(values),
                claim_ids=claim_ids,
                evidence_ids=evidence_ids,
            ),
        )

    conflicts.sort(
        key=lambda conflict: (
            conflict.context.sort_key,
            tuple(value.canonical_value for value in conflict.values),
        ),
    )
    return tuple(conflicts)


def analyze_bundle(bundle: ResearchBundle) -> AnalysisReport:
    """Cluster and analyze one validated bundle without mutating it."""

    snapshot = _canonical_snapshot(bundle)
    variants = _build_variants(snapshot)
    conflicts = _build_conflicts(snapshot)
    decision_contexts = tuple(conflict.context for conflict in conflicts)
    return AnalysisReport(
        variants=variants,
        conflicts=conflicts,
        unresolved_conflicts=conflicts,
        keys_needing_decision=tuple(sorted({conflict.key for conflict in conflicts})),
        decision_contexts=decision_contexts,
    )


def analyze_research_bundle(bundle: ResearchBundle) -> AnalysisReport:
    """Descriptive alias for :func:`analyze_bundle`."""

    return analyze_bundle(bundle)


def cluster_variants(bundle: ResearchBundle) -> tuple[CandidateVariant, ...]:
    """Return only the deterministic candidate clusters for one bundle."""

    return _build_variants(_canonical_snapshot(bundle))


class VariantClusterer:
    """Stateless facade for callers that model workbench stages as services."""

    def cluster(self, bundle: ResearchBundle) -> tuple[CandidateVariant, ...]:
        return cluster_variants(bundle)


class ConflictAnalyzer:
    """Stateless facade for the pure conflict-analysis stage."""

    def analyze(self, bundle: ResearchBundle) -> AnalysisReport:
        return analyze_bundle(bundle)


# Names used by different workbench call sites remain intentionally small and
# descriptive; aliases avoid forcing serializers to know which facade was used.
VariantCluster = CandidateVariant
ConflictReport = AnalysisReport


__all__ = [
    "AnalysisReport",
    "CandidateVariant",
    "ConflictAnalyzer",
    "ConflictReport",
    "ConflictValue",
    "RuleConflict",
    "RuleContext",
    "VariantCluster",
    "VariantClusterer",
    "VariantContext",
    "analyze_bundle",
    "analyze_research_bundle",
    "canonical_json",
    "cluster_variants",
]
