"""Deterministic validation of Pi ruleset claim drafts.

Pi is only an authoring assistant.  This module is the boundary at which its
untrusted ``DraftClaim`` values become ``RuleClaim`` values.  It keeps the
evidence batch beside every citation so a model cannot join facts from another
prompt batch, and it records the exact quoted text used for that conversion.

The extractor deliberately does not fetch a source or recompute the digest of
the complete source document.  ``EvidenceExcerpt.content_sha256`` is the
digest supplied by the evidence/archive boundary; this module only carries it
into an auditable citation anchor.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from werewolf.knowledge.refs import KnowledgeId

from .claims import ClaimScope, ClaimStatus, RuleClaim
from .pi_synthesis import (
    DEFAULT_MAX_EVIDENCE_CHARACTERS,
    DraftClaim,
    EvidenceExcerpt,
    SynthesisBatchResult,
    SynthesisResult,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_LOGICAL_ID_RE = re.compile(r"[a-z0-9_-]+", re.ASCII)


class ClaimExtractionError(ValueError):
    """Raised when an untrusted synthesis result cannot be accepted."""


class ClaimCitationError(ClaimExtractionError):
    """Raised when a model citation is not closed over its evidence batch."""


class DuplicateClaimIdError(ClaimExtractionError):
    """Raised when two batches produce the same logical claim ID."""


class ClaimEvidenceAnchor(BaseModel):
    """One exact citation anchor retained next to an extracted claim.

    ``quote`` is the text supplied by Pi after strict parsing.  Its presence
    in the matching frozen excerpt is checked by :class:`ClaimExtractor`; the
    model itself remains a small persistence-safe record.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    claim_id: KnowledgeId
    source_id: KnowledgeId
    quote: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=DEFAULT_MAX_EVIDENCE_CHARACTERS,
            strict=True,
        ),
    ]
    content_sha256: str
    batch_index: Annotated[int, Field(ge=0)]

    @field_validator("claim_id", "source_id")
    @classmethod
    def validate_logical_id(cls, value: str) -> str:
        if _LOGICAL_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "anchor IDs must contain only lowercase ASCII letters, digits, '-' or '_'"
            )
        return value

    @field_validator("content_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("content_sha256 must be exactly 64 lowercase hexadecimal characters")
        return value

    @property
    def excerpt(self) -> str:
        """Compatibility spelling for callers using ``EvidenceExcerpt``."""

        return self.quote


def _list_to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


class ClaimExtractionResult(BaseModel):
    """Validated claims and their source anchors.

    The collection is immutable at the model boundary.  The renderer still
    revalidates it because nested ``RuleClaim`` JSON values are intentionally
    mutable in the existing workbench schema.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    ruleset_candidate_id: KnowledgeId
    claims: Annotated[tuple[RuleClaim, ...], BeforeValidator(_list_to_tuple)] = ()
    anchors: Annotated[tuple[ClaimEvidenceAnchor, ...], BeforeValidator(_list_to_tuple)] = ()

    @field_validator("ruleset_candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        if _LOGICAL_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "ruleset_candidate_id must contain only lowercase ASCII letters, digits, '-' or '_'"
            )
        return value

    @model_validator(mode="after")
    def validate_anchor_closure(self) -> ClaimExtractionResult:
        claim_ids = {claim.claim_id for claim in self.claims}
        if len(claim_ids) != len(self.claims):
            raise ValueError("claims must contain globally unique claim IDs")
        for anchor in self.anchors:
            if anchor.claim_id not in claim_ids:
                raise ValueError(
                    f"anchor references claim ID absent from claims: {anchor.claim_id}"
                )
        for claim in self.claims:
            claim_anchors = [anchor for anchor in self.anchors if anchor.claim_id == claim.claim_id]
            if [anchor.source_id for anchor in claim_anchors] != claim.evidence_ids:
                raise ValueError(
                    f"anchors do not close over evidence_ids for claim {claim.claim_id}"
                )
        return self

    def json_bytes(self) -> bytes:
        """Return the stable compact UTF-8 representation of this result."""

        return render_claim_extraction(self)

    # A more explicit spelling for callers persisting workbench artifacts.
    render_json_bytes = json_bytes


SynthesisBatches: TypeAlias = Sequence[SynthesisBatchResult]
SynthesisInput: TypeAlias = SynthesisResult | SynthesisBatches


class ClaimExtractor:
    """Convert strict Pi drafts into unverified, source-closed claims."""

    def extract(
        self,
        evidence_batches: Sequence[Sequence[EvidenceExcerpt]],
        synthesis: SynthesisInput,
        *,
        ruleset_candidate_id: str | None = None,
    ) -> ClaimExtractionResult:
        """Validate and convert one synthesis result.

        ``synthesis`` should normally be the :class:`SynthesisResult` returned
        by ``PiRuleSynthesisProvider``.  A sequence of batch results is also
        accepted for workbench callers that persist each Pi response
        separately; those callers must pass ``ruleset_candidate_id``.
        """

        evidence = _normalize_evidence_batches(evidence_batches)
        batch_results, candidate = _normalize_synthesis(synthesis, ruleset_candidate_id)
        if len(evidence) != len(batch_results):
            raise ClaimExtractionError(
                "evidence_batches and synthesis batches must have the same length"
            )

        claims: list[RuleClaim] = []
        anchors: list[ClaimEvidenceAnchor] = []
        seen_claim_ids: set[str] = set()

        for batch_index, (batch_evidence, batch_result) in enumerate(
            zip(evidence, batch_results, strict=True)
        ):
            evidence_by_source = {item.source_id: item for item in batch_evidence}
            for draft in batch_result.claims:
                claim = _extract_one_claim(
                    draft,
                    evidence_by_source,
                    batch_index=batch_index,
                    ruleset_candidate_id=candidate,
                    seen_claim_ids=seen_claim_ids,
                )
                claims.append(claim.claim)
                anchors.extend(claim.anchors)

        return ClaimExtractionResult(
            ruleset_candidate_id=candidate,
            claims=tuple(claims),
            anchors=tuple(anchors),
        )

    # This alias reads naturally at call sites that already use the provider's
    # ``extract_claims`` terminology.
    extract_claims = extract


class _ExtractedOne(BaseModel):
    """Internal pair used to keep claim and anchor creation together."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    claim: RuleClaim
    anchors: tuple[ClaimEvidenceAnchor, ...]


def _normalize_evidence_batches(
    evidence_batches: Sequence[Sequence[EvidenceExcerpt]],
) -> tuple[tuple[EvidenceExcerpt, ...], ...]:
    if isinstance(evidence_batches, (str, bytes, bytearray, Mapping)):
        raise ClaimExtractionError("evidence_batches must be a sequence of evidence batches")
    try:
        batches = tuple(tuple(batch) for batch in evidence_batches)
    except (TypeError, ValueError) as exc:
        raise ClaimExtractionError(
            "evidence_batches must be a sequence of evidence batches"
        ) from exc
    if not batches:
        raise ClaimExtractionError("evidence_batches must contain at least one batch")

    normalized: list[tuple[EvidenceExcerpt, ...]] = []
    for batch_index, batch in enumerate(batches):
        if not batch:
            raise ClaimExtractionError(f"evidence batch {batch_index} must not be empty")
        records: list[EvidenceExcerpt] = []
        seen_source_ids: set[str] = set()
        for item in batch:
            try:
                if isinstance(item, EvidenceExcerpt):
                    # Validate the serialized data too; model_construct() can
                    # otherwise bypass the frozen model's normal validators.
                    item = EvidenceExcerpt.model_validate(
                        item.model_dump(mode="python", round_trip=True), strict=True
                    )
                else:
                    item = EvidenceExcerpt.model_validate(item, strict=True)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ClaimExtractionError(
                    f"evidence batch {batch_index} contains invalid evidence"
                ) from exc
            if item.source_id in seen_source_ids:
                raise ClaimExtractionError(
                    f"evidence batch {batch_index} contains duplicate source_id {item.source_id}"
                )
            seen_source_ids.add(item.source_id)
            records.append(item)
        normalized.append(tuple(records))
    return tuple(normalized)


def _normalize_synthesis(
    synthesis: SynthesisInput,
    requested_candidate: str | None,
) -> tuple[tuple[SynthesisBatchResult, ...], str]:
    if isinstance(synthesis, SynthesisResult):
        _reject_status_in_synthesis(synthesis)
        try:
            validated = SynthesisResult.model_validate(
                synthesis.model_dump(mode="python", round_trip=True), strict=True
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ClaimExtractionError("synthesis result failed strict validation") from exc
        candidate = validated.ruleset_candidate_id
        if requested_candidate is not None and requested_candidate != candidate:
            raise ClaimExtractionError(
                "ruleset_candidate_id does not match the synthesis result candidate"
            )
        return validated.batches, candidate

    if requested_candidate is None:
        raise ClaimExtractionError(
            "ruleset_candidate_id is required when passing batch results directly"
        )
    candidate = _validate_candidate_id(requested_candidate)
    if isinstance(synthesis, (str, bytes, bytearray, Mapping)):
        raise ClaimExtractionError(
            "synthesis must be SynthesisResult or a sequence of batch results"
        )
    try:
        raw_batches = tuple(synthesis)
    except (TypeError, ValueError) as exc:
        raise ClaimExtractionError(
            "synthesis must be SynthesisResult or a sequence of batch results"
        ) from exc
    if not raw_batches:
        raise ClaimExtractionError("synthesis must contain at least one batch result")

    batches: list[SynthesisBatchResult] = []
    for batch_index, item in enumerate(raw_batches):
        _reject_status_in_batch(item, batch_index=batch_index)
        try:
            if isinstance(item, SynthesisBatchResult):
                item = SynthesisBatchResult.model_validate(
                    item.model_dump(mode="python", round_trip=True), strict=True
                )
            else:
                item = SynthesisBatchResult.model_validate(item, strict=True)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ClaimExtractionError(
                f"synthesis batch {batch_index} failed strict validation"
            ) from exc
        batches.append(item)
    return tuple(batches), candidate


def _reject_status_in_synthesis(synthesis: SynthesisResult) -> None:
    """Catch ``model_construct`` objects that bypass ``extra='forbid'``."""

    for batch_index, batch in enumerate(synthesis.batches):
        _reject_status_in_batch(batch, batch_index=batch_index)


def _reject_status_in_batch(item: object, *, batch_index: int) -> None:
    claims = item.claims if isinstance(item, SynthesisBatchResult) else None
    if claims is None and isinstance(item, Mapping):
        claims = item.get("claims")
    if claims is None:
        return
    for claim_index, claim in enumerate(claims):
        if isinstance(claim, DraftClaim):
            raw = claim.__dict__
            extra = claim.__pydantic_extra__ or {}
            has_status = "status" in raw or "status" in extra
        elif isinstance(claim, Mapping):
            has_status = "status" in claim
        else:
            has_status = False
        if has_status:
            raise ClaimExtractionError(
                "synthesis claim failed strict validation: status field is forbidden "
                f"(batch {batch_index}, claim {claim_index})"
            )


def _validate_candidate_id(candidate: object) -> str:
    if not isinstance(candidate, str) or _LOGICAL_ID_RE.fullmatch(candidate) is None:
        raise ClaimExtractionError(
            "ruleset_candidate_id must contain only lowercase ASCII letters, digits, '-' or '_'"
        )
    return candidate


def _extract_one_claim(
    draft: DraftClaim,
    evidence_by_source: Mapping[str, EvidenceExcerpt],
    *,
    batch_index: int,
    ruleset_candidate_id: str,
    seen_claim_ids: set[str],
) -> _ExtractedOne:
    # Revalidate even typed models to reject a model_construct() object carrying
    # an injected status or other untrusted extra field.
    try:
        validated_draft = DraftClaim.model_validate(
            draft.model_dump(mode="python", round_trip=True), strict=True
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ClaimExtractionError("synthesis claim failed strict validation") from exc
    if validated_draft.claim_id in seen_claim_ids:
        raise DuplicateClaimIdError(
            f"claim_id is duplicated across synthesis batches: {validated_draft.claim_id}"
        )
    seen_claim_ids.add(validated_draft.claim_id)

    evidence_ids: list[str] = []
    claim_anchors: list[ClaimEvidenceAnchor] = []
    for citation in validated_draft.evidence:
        if citation.source_id in evidence_ids:
            raise ClaimCitationError(
                f"claim {validated_draft.claim_id} cites source_id more than once: "
                f"{citation.source_id}"
            )
        source = evidence_by_source.get(citation.source_id)
        if source is None:
            raise ClaimCitationError(
                f"claim {validated_draft.claim_id} cites source {citation.source_id!r} "
                f"which is absent from evidence batch {batch_index}"
            )
        # Deliberately use exact substring membership.  Do not normalize
        # whitespace, punctuation, Unicode forms, or case at this boundary.
        if citation.excerpt not in source.excerpt:
            raise ClaimCitationError(
                f"claim {validated_draft.claim_id} quote is not an exact substring "
                f"of source {citation.source_id!r} in evidence batch {batch_index}"
            )
        evidence_ids.append(citation.source_id)
        claim_anchors.append(
            ClaimEvidenceAnchor(
                claim_id=validated_draft.claim_id,
                source_id=citation.source_id,
                quote=citation.excerpt,
                content_sha256=source.content_sha256,
                batch_index=batch_index,
            )
        )

    try:
        scope = ClaimScope(validated_draft.scope)
        claim = RuleClaim.model_validate(
            {
                "schema_version": 1,
                "claim_id": validated_draft.claim_id,
                "ruleset_candidate_id": ruleset_candidate_id,
                "key": validated_draft.key,
                "value": validated_draft.value,
                "scope": scope,
                "conditions": validated_draft.conditions,
                "evidence_ids": evidence_ids,
                # A missing model confidence is retained conservatively as
                # zero; it cannot imply support for an unverified claim.
                "confidence": (
                    validated_draft.confidence if validated_draft.confidence is not None else 0.0
                ),
                "extraction_note": validated_draft.extraction_note,
                "status": ClaimStatus.UNVERIFIED,
            },
            strict=True,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ClaimExtractionError(
            f"claim {validated_draft.claim_id} failed RuleClaim validation"
        ) from exc

    return _ExtractedOne(claim=claim, anchors=tuple(claim_anchors))


def extract_claims(
    evidence_batches: Sequence[Sequence[EvidenceExcerpt]],
    synthesis: SynthesisInput,
    *,
    ruleset_candidate_id: str | None = None,
) -> ClaimExtractionResult:
    """Functional facade for :meth:`ClaimExtractor.extract`."""

    return ClaimExtractor().extract(
        evidence_batches,
        synthesis,
        ruleset_candidate_id=ruleset_candidate_id,
    )


def _canonical_payload(result: ClaimExtractionResult) -> dict[str, Any]:
    try:
        validated = ClaimExtractionResult.model_validate(
            result.model_dump(mode="python", round_trip=True), strict=True
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ClaimExtractionError("claim extraction result failed strict validation") from exc
    payload = validated.model_dump(mode="json", round_trip=True)
    claims = payload.get("claims")
    anchors = payload.get("anchors")
    if not isinstance(claims, list) or not isinstance(anchors, list):
        raise ClaimExtractionError("claim extraction result collections must be arrays")
    payload["claims"] = sorted(claims, key=lambda item: item["claim_id"])
    payload["anchors"] = sorted(
        anchors,
        key=lambda item: (
            item["claim_id"],
            item["source_id"],
            item["batch_index"],
            item["quote"],
        ),
    )
    return payload


def render_claim_extraction(result: ClaimExtractionResult) -> bytes:
    """Render one extraction result as stable compact UTF-8 JSON bytes."""

    if not isinstance(result, ClaimExtractionResult):
        raise TypeError("render_claim_extraction() expects a ClaimExtractionResult")
    try:
        return json.dumps(
            _canonical_payload(result),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ClaimExtractionError("claim extraction result cannot be encoded as JSON") from exc


# Explicit aliases make the artifact vocabulary easy to discover without
# creating a second schema.
ClaimAnchor = ClaimEvidenceAnchor
ExtractionResult = ClaimExtractionResult
render_claims = render_claim_extraction


__all__ = [
    "ClaimAnchor",
    "ClaimCitationError",
    "ClaimEvidenceAnchor",
    "ClaimExtractionError",
    "ClaimExtractionResult",
    "ClaimExtractor",
    "DuplicateClaimIdError",
    "ExtractionResult",
    "extract_claims",
    "render_claim_extraction",
    "render_claims",
]
