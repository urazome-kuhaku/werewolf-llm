"""Tests for deterministic analysis artifact rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from werewolf.ruleset_workbench.analysis import analyze_bundle
from werewolf.ruleset_workbench.analysis_artifacts import (
    AnalysisArtifactError,
    render_analysis_artifacts,
)
from werewolf.ruleset_workbench.bundle_codec import bundle_sha256
from werewolf.ruleset_workbench.bundles import ResearchBundle
from werewolf.ruleset_workbench.claims import ClaimScope, ClaimStatus
from werewolf.ruleset_workbench.evidence import SourceClass


def _source(source_id: str, digest: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"虚构来源 {source_id}",
        "publisher": "fixture",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": digest,
        "excerpt": "仅供测试的必要摘录，不含网页全文。",
        "retrieval_method": "fixture",
    }


def _claim(
    claim_id: str,
    value: object,
    source_id: str,
    *,
    candidate: str = "classic-12",
    conditions: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": candidate,
        "key": "witch.can_self_heal",
        "value": value,
        "scope": ClaimScope.ROLE,
        "conditions": conditions or {"night": 1},
        "evidence_ids": [source_id],
        "confidence": 0.9,
        "extraction_note": "Fixture claim.",
        "status": ClaimStatus.SUPPORTED,
    }


def _bundle(*, reverse: bool = False) -> ResearchBundle:
    sources = [
        _source("source-a", "a" * 64),
        _source("source-b", "b" * 64),
    ]
    claims = [
        _claim("claim-false", False, "source-a"),
        _claim("claim-true", True, "source-b"),
    ]
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


def test_rendering_keeps_context_values_claims_evidence_and_decision_markers() -> None:
    bundle = _bundle()
    report = analyze_bundle(bundle)
    artifacts = render_analysis_artifacts(report, bundle_sha256(bundle))

    variants = json.loads(artifacts.variants.decode("utf-8"))
    conflicts = json.loads(artifacts.conflicts.decode("utf-8"))

    assert variants["schema_version"] == 1
    assert variants["bundle_sha256"] == bundle_sha256(bundle)
    assert variants["variants"][0]["ruleset_candidate_id"] == "classic-12"
    context = variants["variants"][0]["contexts"][0]["context"]
    assert context["key"] == "witch.can_self_heal"
    assert context["conditions"] == {"night": 1}
    assert variants["variants"][0]["claim_ids"] == ["claim-false", "claim-true"]

    assert conflicts["schema_version"] == 1
    assert conflicts["bundle_sha256"] == bundle_sha256(bundle)
    assert conflicts["needs_human_decision"] is True
    conflict = conflicts["conflicts"][0]
    assert conflict["unresolved"] is True
    assert conflict["needs_human_decision"] is True
    assert conflict["resolution"] == "NEEDS_DECISION"
    assert conflict["claim_ids"] == ["claim-false", "claim-true"]
    assert conflict["evidence_ids"] == ["source-a", "source-b"]
    assert [item["value"] for item in conflict["values"]] == [False, True]
    assert [item["evidence_ids"] for item in conflict["values"]] == [
        ["source-a"],
        ["source-b"],
    ]
    assert conflicts["keys_needing_decision"] == ["witch.can_self_heal"]
    assert "仅供测试的必要摘录" not in artifacts.variants.decode("utf-8")
    assert "仅供测试的必要摘录" not in artifacts.conflicts.decode("utf-8")


def test_rendering_is_stable_for_reordered_bundle_and_uses_lf_utf8_json() -> None:
    first_bundle = _bundle()
    second_bundle = _bundle(reverse=True)
    first = render_analysis_artifacts(analyze_bundle(first_bundle), bundle_sha256(first_bundle))
    second = render_analysis_artifacts(
        analyze_bundle(second_bundle),
        bundle_sha256(second_bundle),
    )

    assert first.files == second.files
    for _, content in first.files:
        assert content.endswith(b"\n")
        assert b"\r" not in content
        json.loads(content.decode("utf-8"))


def test_renderer_does_not_auto_resolve_or_write_files(tmp_path) -> None:
    bundle = _bundle()
    artifacts = render_analysis_artifacts(analyze_bundle(bundle), bundle_sha256(bundle))

    assert not tuple(tmp_path.iterdir())
    assert b"NEEDS_DECISION" in artifacts.conflicts


@pytest.mark.parametrize("digest", ["A" * 64, "0" * 63, "not-a-digest"])
def test_bundle_digest_must_be_explicit_canonical_sha256(digest: str) -> None:
    bundle = _bundle()
    with pytest.raises(AnalysisArtifactError, match="bundle_sha256"):
        render_analysis_artifacts(analyze_bundle(bundle), digest)
