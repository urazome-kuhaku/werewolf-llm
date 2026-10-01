"""Boundary tests for atomic ruleset claims."""

import math

import pytest
from pydantic import ValidationError

from werewolf.ruleset_workbench import ClaimScope, ClaimStatus, RuleClaim


def _valid_claim(**overrides: object) -> dict[str, object]:
    claim: dict[str, object] = {
        "claim_id": "claim-witch-self-heal",
        "ruleset_candidate_id": "classic-12",
        "key": "witch.can_self_heal",
        "value": False,
        "scope": ClaimScope.ROLE,
        "conditions": {"night": 1, "alive": True},
        "evidence_ids": ["source-official-rules", "source-platform-rules"],
        "confidence": 0.95,
        "extraction_note": "The two sources state the same restriction.",
        "status": ClaimStatus.SUPPORTED,
    }
    claim.update(overrides)
    return claim


def test_valid_claim_has_stable_schema_and_enum_values() -> None:
    claim = RuleClaim.model_validate(_valid_claim())

    assert claim.schema_version == 1
    assert claim.scope is ClaimScope.ROLE
    assert claim.status is ClaimStatus.SUPPORTED
    assert claim.model_dump()["scope"] == "ROLE"
    assert claim.model_dump()["status"] == "SUPPORTED"
    assert {member.value for member in ClaimScope} == {
        "BOARD",
        "ROLE",
        "MECHANIC",
        "INTERACTION",
    }
    assert {member.value for member in ClaimStatus} == {
        "SUPPORTED",
        "CONFLICTING",
        "UNVERIFIED",
        "REJECTED",
    }


@pytest.mark.parametrize(
    "field",
    ["claim_id", "ruleset_candidate_id"],
)
@pytest.mark.parametrize(
    "value",
    ["../claim", "vault/claims/claim", "https://example.com/claim", "Claim", "claim id"],
)
def test_path_like_or_non_logical_ids_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(**{field: value}))


@pytest.mark.parametrize(
    "key",
    [
        "witch",
        "Witch.can_self_heal",
        "witch.can-self-heal",
        "witch.can__self_heal",
        "witch._can_self_heal",
        "witch.can_self_heal_",
        "witch/can_self_heal",
        "witch..can_self_heal",
        "a" * 129,
    ],
)
def test_key_must_be_bounded_dotted_snake_case(key: str) -> None:
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(key=key))


def test_evidence_ids_must_be_non_empty_distinct_logical_ids() -> None:
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(evidence_ids=[]))
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(
            _valid_claim(evidence_ids=["source-official-rules", "source-official-rules"]),
        )
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(evidence_ids=["vault/sources/rules"]))


@pytest.mark.parametrize("confidence", [-0.01, 1.01, math.nan, math.inf, -math.inf])
def test_confidence_must_be_finite_and_between_zero_and_one(confidence: float) -> None:
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(confidence=confidence))


@pytest.mark.parametrize("value", [{"items": {1, 2}}, ("not", "json"), object()])
def test_value_must_be_a_json_value(value: object) -> None:
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(value=value))


def test_conditions_must_be_a_json_mapping() -> None:
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(conditions={"items": {1, 2}}))
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(conditions={1: "non-string-key"}))


def test_extra_fields_and_invalid_schema_version_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(unexpected=True))
    with pytest.raises(ValidationError):
        RuleClaim.model_validate(_valid_claim(schema_version=2))


def test_extraction_note_is_optional() -> None:
    claim = RuleClaim.model_validate(_valid_claim(extraction_note=None))

    assert claim.extraction_note is None
