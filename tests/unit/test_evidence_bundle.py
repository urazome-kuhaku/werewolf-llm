"""Boundary tests for imported offline research bundles."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from werewolf.ruleset_workbench import (
    ClaimScope,
    ClaimStatus,
    ResearchBundle,
    SourceClass,
)


def _source(source_id: str = "fictional-source-alpha") -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": "Fictional rules excerpt",
        "publisher": "Fictional Publisher",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": "a" * 64,
        "excerpt": "A wholly fictional excerpt for a structural test.",
        "retrieval_method": "fixture",
    }


def _claim(
    claim_id: str = "fictional-claim-alpha",
    *,
    evidence_ids: list[str] | None = None,
    status: ClaimStatus = ClaimStatus.UNVERIFIED,
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": "fictional-board",
        "key": "fictional.rule_enabled",
        "value": True,
        "scope": ClaimScope.MECHANIC,
        "conditions": {"round": 1},
        "evidence_ids": evidence_ids or ["fictional-source-alpha"],
        "confidence": 0.25,
        "extraction_note": "This is fabricated test data.",
        "status": status,
    }


def _valid_bundle(**overrides: object) -> dict[str, object]:
    bundle: dict[str, object] = {
        "board_name": "Fictional Board",
        "locale": "zh-CN",
        "sources": [_source()],
        "claims": [_claim()],
    }
    bundle.update(overrides)
    return bundle


def test_valid_bundle_is_closed_and_lookup_is_deterministic() -> None:
    bundle = ResearchBundle.model_validate(_valid_bundle())

    assert bundle.schema_version == 1
    assert isinstance(bundle.sources, tuple)
    assert isinstance(bundle.claims, tuple)
    assert bundle.source_by_id("fictional-source-alpha") is not None
    assert bundle.source_by_id("missing-source") is None
    assert bundle.claim_by_id("fictional-claim-alpha") is not None
    assert bundle.claim_by_id("missing-claim") is None


def test_claim_status_is_preserved_without_automatic_support() -> None:
    bundle = ResearchBundle.model_validate(
        _valid_bundle(claims=[_claim(status=ClaimStatus.CONFLICTING)]),
    )

    claim = bundle.claim_by_id("fictional-claim-alpha")
    assert claim is not None
    assert claim.status is ClaimStatus.CONFLICTING


@pytest.mark.parametrize(
    "field",
    ["sources", "claims"],
)
def test_empty_collections_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        ResearchBundle.model_validate(_valid_bundle(**{field: []}))


def test_duplicate_source_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate source_id"):
        ResearchBundle.model_validate(
            _valid_bundle(sources=[_source(), _source()]),
        )


def test_duplicate_claim_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate claim_id"):
        ResearchBundle.model_validate(
            _valid_bundle(claims=[_claim(), _claim()]),
        )


def test_missing_evidence_references_are_rejected() -> None:
    with pytest.raises(ValidationError, match="absent from sources"):
        ResearchBundle.model_validate(
            _valid_bundle(claims=[_claim(evidence_ids=["fictional-source-missing"])]),
        )


def test_schema_metadata_and_unknown_fields_are_strict() -> None:
    with pytest.raises(ValidationError):
        ResearchBundle.model_validate(_valid_bundle(schema_version=2))
    with pytest.raises(ValidationError):
        ResearchBundle.model_validate(_valid_bundle(locale="not a locale"))
    with pytest.raises(ValidationError):
        ResearchBundle.model_validate(_valid_bundle(unexpected=True))
