"""Unit tests for source evidence validation."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from werewolf.ruleset_workbench import SourceClass, SourceEvidence


def _valid_evidence(**overrides: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "source_id": "source-official-rules",
        "url": "https://example.com/rules",
        "title": "Official rules",
        "publisher": "Example Tournament",
        "source_class": SourceClass.OFFICIAL_EVENT,
        "published_at": datetime(2026, 9, 1, 12, tzinfo=UTC),
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": "a" * 64,
        "excerpt": "The source states the relevant rule.",
        "retrieval_method": "https",
    }
    evidence.update(overrides)
    return evidence


def test_valid_source_evidence_parses_with_stable_values() -> None:
    evidence = SourceEvidence.model_validate(_valid_evidence())

    assert evidence.schema_version == 1
    assert evidence.source_class is SourceClass.OFFICIAL_EVENT
    assert evidence.url.scheme == "https"
    assert evidence.fetched_at.tzinfo is not None
    assert evidence.model_dump()["content_sha256"] == "a" * 64
    assert {member.value for member in SourceClass} == {
        "OFFICIAL_EVENT",
        "PLATFORM_RULES",
        "MATURE_RULE_GUIDE",
        "COMMUNITY",
        "STRATEGY_GUIDE",
        "OTHER",
    }


@pytest.mark.parametrize(
    ("url", "secret_fragments"),
    [
        ("ftp://example.com/rules", ()),
        ("file:///rules", ()),
        ("/rules", ()),
        ("https://evidence-user@example.com/rules", ("evidence-user",)),
        ("https://:evidence-secret@example.com/rules", ("evidence-secret",)),
        (
            "https://evidence-user:evidence-secret@example.com/rules",
            ("evidence-user", "evidence-secret"),
        ),
    ],
)
def test_invalid_url_is_rejected_without_echoing_credentials(
    url: str,
    secret_fragments: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError) as error:
        SourceEvidence.model_validate(_valid_evidence(url=url))
    assert all(fragment not in str(error.value) for fragment in secret_fragments)


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 9, 27, 12),
        datetime(2026, 9, 27, 12, tzinfo=UTC).replace(tzinfo=None),
        "2026-09-27T20:00:00+08:00",
    ],
)
def test_non_utc_or_timezone_free_timestamp_is_rejected(timestamp: object) -> None:
    with pytest.raises(ValidationError):
        SourceEvidence.model_validate(_valid_evidence(fetched_at=timestamp))


@pytest.mark.parametrize(
    "content_sha256",
    ["a" * 63, "a" * 65, "g" * 64, "A" * 64, " "],
)
def test_invalid_sha256_is_rejected(content_sha256: str) -> None:
    with pytest.raises(ValidationError):
        SourceEvidence.model_validate(_valid_evidence(content_sha256=content_sha256))


def test_extra_fields_and_path_like_source_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceEvidence.model_validate(_valid_evidence(unexpected=True))
    with pytest.raises(ValidationError):
        SourceEvidence.model_validate(_valid_evidence(source_id="https://example.com/source"))
    with pytest.raises(ValidationError):
        SourceEvidence.model_validate(_valid_evidence(source_id="vault/sources/source"))


def test_publisher_and_published_at_are_optional() -> None:
    evidence = SourceEvidence.model_validate(
        _valid_evidence(publisher=None, published_at=None),
    )

    assert evidence.publisher is None
    assert evidence.published_at is None
