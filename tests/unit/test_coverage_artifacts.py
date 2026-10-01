"""Tests for deterministic coverage artifact rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from werewolf.ruleset_workbench.bundle_codec import bundle_sha256
from werewolf.ruleset_workbench.bundles import ResearchBundle
from werewolf.ruleset_workbench.claims import ClaimScope, ClaimStatus
from werewolf.ruleset_workbench.coverage import CoverageRequirement, analyze_coverage
from werewolf.ruleset_workbench.coverage_artifacts import (
    CoverageArtifactError,
    render_coverage_artifacts,
)
from werewolf.ruleset_workbench.evidence import SourceClass


def _source(source_id: str, digest: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"Fixture source {source_id}",
        "publisher": "fixture",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": digest,
        "excerpt": "不应出现在 coverage.json 的网页摘录。",
        "retrieval_method": "fixture",
    }


def _claim(
    claim_id: str,
    source_id: str,
    *,
    key: str = "board.player_count",
    value: object = 12,
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": "fictional-board",
        "key": key,
        "value": value,
        "scope": ClaimScope.BOARD,
        "conditions": {},
        "evidence_ids": [source_id],
        "confidence": 0.9,
        "extraction_note": "Fixture claim.",
        "status": ClaimStatus.SUPPORTED,
    }


def _bundle(*, reverse: bool = False) -> ResearchBundle:
    sources = [_source("source-a", "a" * 64), _source("source-b", "b" * 64)]
    claims = [_claim("claim-a", "source-a"), _claim("claim-b", "source-b")]
    if reverse:
        sources.reverse()
        claims.reverse()
    return ResearchBundle.model_validate(
        {
            "board_name": "虚构测试板子",
            "locale": "zh-CN",
            "sources": sources,
            "claims": claims,
        },
    )


def _requirement(requirement_id: str, key: str = "board.player_count") -> CoverageRequirement:
    return CoverageRequirement.model_validate(
        {
            "requirement_id": requirement_id,
            "scope": ClaimScope.BOARD,
            "key": key,
            "conditions": {},
            "required": True,
            "min_independent_evidence_count": 1,
        },
    )


def test_complete_report_contains_gate_fields_and_no_source_material() -> None:
    bundle = _bundle()
    report = analyze_coverage(
        bundle,
        "fictional-board",
        [_requirement("player-count")],
    )
    artifacts = render_coverage_artifacts(report, bundle_sha256(bundle))
    payload = json.loads(artifacts.coverage.decode("utf-8"))

    assert payload["schema_version"] == 1
    assert payload["bundle_sha256"] == bundle_sha256(bundle)
    assert payload["ruleset_candidate_id"] == "fictional-board"
    assert payload["required_count"] == 1
    assert payload["satisfied_count"] == 1
    assert payload["coverage_percentage"] == 100.0
    assert payload["required"] == {
        "total": 1,
        "satisfied": 1,
        "ratio": 1.0,
        "percentage": 100.0,
    }
    assert payload["passed"] is True
    item = payload["items"][0]
    assert item["requirement"]["requirement_id"] == "player-count"
    assert item["status"] == "SATISFIED"
    assert item["evidence_count"] == 2
    assert item["blocking"] is False
    assert item["blocking_reason"] is None
    assert "fictional.example" not in artifacts.coverage.decode("utf-8")
    assert "不应出现在" not in artifacts.coverage.decode("utf-8")


def test_missing_report_remains_blocking_and_is_stably_sorted() -> None:
    bundle = _bundle()
    first_report = analyze_coverage(
        bundle,
        "fictional-board",
        [_requirement("z-missing", "board.missing_rule"), _requirement("a-present")],
    )
    second_report = analyze_coverage(
        _bundle(reverse=True),
        "fictional-board",
        [_requirement("a-present"), _requirement("z-missing", "board.missing_rule")],
    )
    digest = bundle_sha256(bundle)
    first = render_coverage_artifacts(first_report, digest)
    second = render_coverage_artifacts(second_report, digest)

    assert first.files == second.files
    payload = json.loads(first.coverage.decode("utf-8"))
    assert payload["required_count"] == 2
    assert payload["satisfied_count"] == 1
    assert payload["required"]["ratio"] == 0.5
    assert payload["passed"] is False
    assert payload["blocking_requirement_ids"] == ["z-missing"]
    missing = next(item for item in payload["items"] if item["requirement_id"] == "z-missing")
    assert missing["status"] == "MISSING"
    assert missing["evidence_count"] == 0
    assert missing["blocking"] is True
    assert missing["blocking_reason"] == "no matching claim"
    assert first.coverage.endswith(b"\n")
    assert b"\r" not in first.coverage


@pytest.mark.parametrize("digest", ["A" * 64, "0" * 63, "not-a-digest"])
def test_digest_must_be_explicit_lowercase_sha256(digest: str) -> None:
    bundle = _bundle()
    report = analyze_coverage(bundle, "fictional-board", [_requirement("player-count")])
    with pytest.raises(CoverageArtifactError, match="bundle_sha256"):
        render_coverage_artifacts(report, digest)


def test_tampered_report_counts_or_non_finite_percentage_are_rejected() -> None:
    bundle = _bundle()
    report = analyze_coverage(bundle, "fictional-board", [_requirement("player-count")])
    object.__setattr__(report, "satisfied_count", 0)
    with pytest.raises(CoverageArtifactError, match="satisfied_count"):
        render_coverage_artifacts(report, bundle_sha256(bundle))

    report = analyze_coverage(bundle, "fictional-board", [_requirement("player-count")])
    object.__setattr__(report, "coverage_percentage", float("nan"))
    with pytest.raises(CoverageArtifactError, match="coverage"):
        render_coverage_artifacts(report, bundle_sha256(bundle))


def test_tampered_satisfied_item_cannot_be_upgraded_to_pass() -> None:
    bundle = _bundle()
    report = analyze_coverage(
        bundle,
        "fictional-board",
        [_requirement("missing", "board.missing_rule")],
    )
    item = report.items[0]
    object.__setattr__(item, "status", "SATISFIED")
    with pytest.raises(CoverageArtifactError):
        render_coverage_artifacts(report, bundle_sha256(bundle))
