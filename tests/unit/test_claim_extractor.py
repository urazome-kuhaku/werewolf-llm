"""Deterministic boundary tests for Pi claim extraction."""

from __future__ import annotations

import json

import pytest

from werewolf.ruleset_workbench.claim_extractor import (
    ClaimCitationError,
    ClaimExtractionError,
    ClaimExtractor,
    DuplicateClaimIdError,
    extract_claims,
    render_claim_extraction,
)
from werewolf.ruleset_workbench.claims import ClaimStatus
from werewolf.ruleset_workbench.pi_synthesis import (
    DraftClaim,
    DraftEvidenceReference,
    EvidenceExcerpt,
    SynthesisBatchResult,
    SynthesisResult,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def _evidence(source_id: str, excerpt: str, digest: str = HASH_A) -> EvidenceExcerpt:
    return EvidenceExcerpt(source_id=source_id, content_sha256=digest, excerpt=excerpt)


def _draft(
    claim_id: str,
    source_id: str,
    quote: str,
    *,
    key: str = "witch.can_self_heal",
    scope: str = "ROLE",
    confidence: float | None = 0.8,
) -> DraftClaim:
    return DraftClaim(
        claim_id=claim_id,
        key=key,
        value=False,
        scope=scope,
        conditions={},
        evidence=(DraftEvidenceReference(source_id=source_id, excerpt=quote),),
        confidence=confidence,
    )


def _result(*batches: SynthesisBatchResult, candidate: str = "classic-12") -> SynthesisResult:
    claims = tuple(claim for batch in batches for claim in batch.claims)
    return SynthesisResult(
        board_name="12人标准场",
        ruleset_candidate_id=candidate,
        claims=claims,
        batches=batches,
    )


def test_extracts_unverified_claims_and_retains_exact_audit_anchors() -> None:
    evidence = (
        (_evidence("source-a", "女巫首夜不能自救。", HASH_A),),
        (_evidence("source-b", "猎人被投票放逐可以开枪。", HASH_B),),
    )
    synthesis = _result(
        SynthesisBatchResult(
            claims=(_draft("claim-a", "source-a", "首夜不能自救", confidence=None),)
        ),
        SynthesisBatchResult(
            claims=(
                _draft(
                    "claim-b",
                    "source-b",
                    "被投票放逐可以开枪",
                    key="hunter.can_shoot_when_voted_out",
                ),
            ),
        ),
    )

    extracted = ClaimExtractor().extract(evidence, synthesis)

    assert [claim.claim_id for claim in extracted.claims] == ["claim-a", "claim-b"]
    assert all(claim.status is ClaimStatus.UNVERIFIED for claim in extracted.claims)
    assert extracted.claims[0].confidence == 0.0
    assert [
        (anchor.claim_id, anchor.source_id, anchor.quote, anchor.content_sha256, anchor.batch_index)
        for anchor in extracted.anchors
    ] == [
        ("claim-a", "source-a", "首夜不能自救", HASH_A, 0),
        ("claim-b", "source-b", "被投票放逐可以开枪", HASH_B, 1),
    ]

    encoded = render_claim_extraction(extracted)
    assert encoded == extracted.json_bytes()
    assert encoded == render_claim_extraction(extracted)
    payload = json.loads(encoded)
    assert payload["claims"][0]["status"] == "UNVERIFIED"
    assert payload["anchors"][0] == {
        "batch_index": 0,
        "claim_id": "claim-a",
        "content_sha256": HASH_A,
        "quote": "首夜不能自救",
        "source_id": "source-a",
    }


def test_citation_must_belong_to_the_same_batch() -> None:
    evidence = (
        (_evidence("source-a", "甲来源内容"),),
        (_evidence("source-b", "乙来源内容", HASH_B),),
    )
    synthesis = _result(
        SynthesisBatchResult(claims=(_draft("claim-a", "source-b", "来源内容"),)),
        SynthesisBatchResult(),
    )

    with pytest.raises(ClaimCitationError, match="absent from evidence batch 0"):
        extract_claims(evidence, synthesis)


def test_quote_must_be_an_exact_substring() -> None:
    evidence = ((_evidence("source-a", "女巫首夜不能自救。"),),)
    synthesis = _result(
        SynthesisBatchResult(claims=(_draft("claim-a", "source-a", "女巫 首夜不能自救"),)),
    )

    with pytest.raises(ClaimCitationError, match="exact substring"):
        ClaimExtractor().extract(evidence, synthesis)


def test_claim_ids_are_unique_across_batches() -> None:
    evidence = (
        (_evidence("source-a", "甲"),),
        (_evidence("source-b", "乙", HASH_B),),
    )
    synthesis = _result(
        SynthesisBatchResult(claims=(_draft("same-id", "source-a", "甲"),)),
        SynthesisBatchResult(claims=(_draft("same-id", "source-b", "乙"),)),
    )

    with pytest.raises(DuplicateClaimIdError, match="duplicated"):
        ClaimExtractor().extract(evidence, synthesis)


@pytest.mark.parametrize(
    ("field", "value"),
    [("scope", "ROLEPLAY"), ("key", "witch"), ("key", "Witch.can_self_heal")],
)
def test_rule_claim_scope_and_key_are_revalidated(field: str, value: str) -> None:
    evidence = ((_evidence("source-a", "甲"),),)
    draft = _draft("claim-a", "source-a", "甲", **{field: value})
    synthesis = _result(SynthesisBatchResult(claims=(draft,)))

    with pytest.raises(ClaimExtractionError, match="RuleClaim validation"):
        ClaimExtractor().extract(evidence, synthesis)


def test_model_status_injection_is_rejected() -> None:
    evidence = ((_evidence("source-a", "甲"),),)
    synthesis = [
        {
            "claims": [
                {
                    "claim_id": "claim-a",
                    "key": "witch.can_self_heal",
                    "value": False,
                    "scope": "ROLE",
                    "conditions": {},
                    "evidence": [{"source_id": "source-a", "excerpt": "甲"}],
                    "confidence": 0.8,
                    "status": "SUPPORTED",
                },
            ],
        },
    ]

    with pytest.raises(ClaimExtractionError, match="strict validation"):
        ClaimExtractor().extract(
            evidence,
            synthesis,
            ruleset_candidate_id="classic-12",
        )


def test_direct_batch_results_require_matching_candidate_context() -> None:
    evidence = ((_evidence("source-a", "甲"),),)
    batch = SynthesisBatchResult(claims=(_draft("claim-a", "source-a", "甲"),))

    with pytest.raises(ClaimExtractionError, match="required"):
        ClaimExtractor().extract(evidence, (batch,))
    with pytest.raises(ClaimExtractionError, match="lowercase"):
        ClaimExtractor().extract(evidence, (batch,), ruleset_candidate_id="Classic 12")


def test_explicit_candidate_must_match_synthesis_result() -> None:
    evidence = ((_evidence("source-a", "甲"),),)
    synthesis = _result(
        SynthesisBatchResult(claims=(_draft("claim-a", "source-a", "甲"),)),
    )

    with pytest.raises(ClaimExtractionError, match="does not match"):
        ClaimExtractor().extract(
            evidence,
            synthesis,
            ruleset_candidate_id="other-variant",
        )
