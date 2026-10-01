"""Tests for deterministic offline research bundle JSON encoding."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from werewolf.ruleset_workbench import (
    ClaimScope,
    ClaimStatus,
    ResearchBundle,
    SourceClass,
    bundle_sha256,
    decode_bundle,
    encode_bundle,
)


def _source(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"Fictional source {source_id}",
        "publisher": "Fictional Publisher",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": "a" * 64,
        "excerpt": "A wholly fictional excerpt for a codec test.",
        "retrieval_method": "fixture",
    }


def _claim(claim_id: str, source_id: str) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": "fictional-board",
        "key": f"fictional.{claim_id.replace('-', '_')}",
        "value": {"enabled": True, "threshold": 2},
        "scope": ClaimScope.MECHANIC,
        "conditions": {"round": 1},
        "evidence_ids": [source_id],
        "confidence": 0.25,
        "extraction_note": "This is fabricated test data.",
        "status": ClaimStatus.UNVERIFIED,
    }


def _bundle(*, reverse: bool = False) -> ResearchBundle:
    source_ids = ["fictional-source-zeta", "fictional-source-alpha"]
    claim_pairs = [
        ("fictional-claim-zeta", "fictional-source-zeta"),
        ("fictional-claim-alpha", "fictional-source-alpha"),
    ]
    if reverse:
        source_ids.reverse()
        claim_pairs.reverse()
    return ResearchBundle.model_validate(
        {
            "board_name": "虚构测试板子",
            "locale": "zh-CN",
            "platform": "fictional-platform",
            "region": "fictional-region",
            "constraints": ["offline", "fixture"],
            "sources": [_source(source_id) for source_id in source_ids],
            "claims": [_claim(claim_id, source_id) for claim_id, source_id in claim_pairs],
        },
    )


def test_source_and_claim_order_does_not_change_canonical_bytes_or_hash() -> None:
    first = _bundle()
    second = _bundle(reverse=True)

    assert encode_bundle(first) == encode_bundle(second)
    assert bundle_sha256(first) == bundle_sha256(second)

    document = json.loads(encode_bundle(first))
    assert [source["source_id"] for source in document["sources"]] == [
        "fictional-source-alpha",
        "fictional-source-zeta",
    ]
    assert [claim["claim_id"] for claim in document["claims"]] == [
        "fictional-claim-alpha",
        "fictional-claim-zeta",
    ]
    assert "\r" not in encode_bundle(first).decode("utf-8")


def test_nested_reference_mutation_is_revalidated_before_encoding() -> None:
    bundle = _bundle()
    claim = bundle.claim_by_id("fictional-claim-alpha")
    assert claim is not None
    claim.evidence_ids.append("fictional-source-missing")

    with pytest.raises(ValidationError, match="absent from sources"):
        encode_bundle(bundle)


def test_round_trip_uses_strict_json_mode() -> None:
    bundle = _bundle()

    decoded = decode_bundle(encode_bundle(bundle))

    assert decoded == decode_bundle(encode_bundle(decoded))
    assert decoded.board_name == "虚构测试板子"
    assert decoded.source_by_id("fictional-source-alpha") is not None


def test_duplicate_object_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        decode_bundle(b'{"schema_version":1,"schema_version":1}')


@pytest.mark.parametrize("number", [b"NaN", b"Infinity", b"-Infinity", b"1e400"])
def test_non_finite_json_number_is_rejected(number: bytes) -> None:
    with pytest.raises(ValueError, match="invalid|non-finite"):
        decode_bundle(b'{"value":' + number + b"}")


def test_invalid_utf8_is_rejected() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        decode_bundle(b"\xff")


def test_input_size_limit_is_enforced() -> None:
    data = encode_bundle(_bundle())

    with pytest.raises(ValueError, match="max_bytes"):
        decode_bundle(data, max_bytes=len(data) - 1)
