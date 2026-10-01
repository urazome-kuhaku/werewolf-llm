"""Unit tests for deterministic workbench artifact rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

import werewolf.ruleset_workbench.artifacts as artifacts_module
from werewolf.ruleset_workbench import (
    ClaimScope,
    ClaimStatus,
    ResearchBundle,
    SourceClass,
    WorkbenchArtifactError,
    render_workbench_artifacts,
)
from werewolf.ruleset_workbench.bundle_codec import encode_bundle as original_encode_bundle


def _source(source_id: str, *, excerpt: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"虚构来源 {source_id}",
        "publisher": "虚构出版者",
        "source_class": SourceClass.OTHER,
        "published_at": datetime(2026, 9, 1, 12, tzinfo=UTC),
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": "a" * 64,
        "excerpt": excerpt,
        "retrieval_method": "fixture",
    }


def _claim(claim_id: str, source_id: str) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": "fictional-board",
        "key": f"fictional.{claim_id.replace('-', '_')}",
        "value": {"enabled": True, "说明": "中文值"},
        "scope": ClaimScope.MECHANIC,
        "conditions": {"阶段": "夜晚"},
        "evidence_ids": [source_id],
        "confidence": 0.75,
        "extraction_note": "仅用于测试。",
        "status": ClaimStatus.SUPPORTED,
    }


def _bundle(*, reverse: bool = False, source_id: str = "fictional-source-alpha") -> ResearchBundle:
    sources = [
        _source("fictional-source-zeta", excerpt="甲行\r\n乙行\r丙行"),
        _source(source_id, excerpt="只保留必要摘录，不包含网页全文。"),
    ]
    claims = [
        _claim("fictional-claim-zeta", "fictional-source-zeta"),
        _claim("fictional-claim-alpha", source_id),
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


def test_rendered_files_keep_metadata_excerpt_paths_and_complete_claims() -> None:
    artifacts = render_workbench_artifacts(_bundle())

    index = json.loads(artifacts.source_index)
    assert index["schema_version"] == 1
    assert [source["source_id"] for source in index["sources"]] == [
        "fictional-source-alpha",
        "fictional-source-zeta",
    ]
    first = index["sources"][0]
    assert first["url"] == "https://fictional.example/fictional-source-alpha"
    assert first["published_at"] == "2026-09-01T12:00:00Z"
    assert first["fetched_at"] == "2026-09-27T12:00:00Z"
    assert first["source_class"] == "OTHER"
    assert first["content_sha256"] == "a" * 64
    assert first["excerpt_path"] == "sources/fictional-source-alpha.md"
    assert "excerpt" not in first

    excerpt_by_id = {item.source_id: item for item in artifacts.source_excerpts}
    assert excerpt_by_id["fictional-source-zeta"].content == "甲行\n乙行\n丙行\n".encode()
    assert excerpt_by_id["fictional-source-alpha"].content == (
        "只保留必要摘录，不包含网页全文。\n".encode()
    )
    assert all(b"\r" not in item.content for item in artifacts.source_excerpts)

    lines = artifacts.claims.decode("utf-8").splitlines()
    assert len(lines) == 2
    claims = [json.loads(line) for line in lines]
    assert [claim["claim_id"] for claim in claims] == [
        "fictional-claim-alpha",
        "fictional-claim-zeta",
    ]
    assert claims[0]["evidence_ids"] == ["fictional-source-alpha"]
    assert claims[0]["value"] == {"enabled": True, "说明": "中文值"}
    assert claims[0]["conditions"] == {"阶段": "夜晚"}
    assert b"\r" not in artifacts.source_index
    assert b"\r" not in artifacts.claims


def test_rendering_is_stable_for_reordered_bundle_collections() -> None:
    first = render_workbench_artifacts(_bundle())
    second = render_workbench_artifacts(_bundle(reverse=True))

    assert first.source_index == second.source_index
    assert first.source_excerpts == second.source_excerpts
    assert first.claims == second.claims
    assert first.files == second.files


def test_rendering_uses_one_canonical_bundle_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def counting_encode(bundle: ResearchBundle) -> bytes:
        nonlocal calls
        calls += 1
        return original_encode_bundle(bundle)

    monkeypatch.setattr(artifacts_module, "encode_bundle", counting_encode)

    render_workbench_artifacts(_bundle())

    assert calls == 1


def test_nested_mutation_is_rejected_before_any_artifact_is_returned() -> None:
    bundle = _bundle()
    claim = bundle.claim_by_id("fictional-claim-alpha")
    assert claim is not None
    claim.evidence_ids.append("fictional-source-missing")

    with pytest.raises(ValidationError, match="absent from sources"):
        render_workbench_artifacts(bundle)


def test_reserved_source_id_cannot_become_a_windows_path() -> None:
    bundle = _bundle(source_id="con")

    with pytest.raises(WorkbenchArtifactError, match="reserved Windows"):
        render_workbench_artifacts(bundle)


def test_artifact_bytes_are_strict_utf8_json_with_one_claim_per_line() -> None:
    artifacts = render_workbench_artifacts(_bundle())

    assert artifacts.source_index.endswith(b"\n")
    assert artifacts.claims.endswith(b"\n")
    assert artifacts.claims.count(b"\n") == 2
    # JSON output deliberately keeps Chinese characters readable and does not
    # add a platform-specific carriage return.
    assert "中文" in artifacts.claims.decode("utf-8")
    json.loads(artifacts.source_index.decode("utf-8"))
    for line in artifacts.claims.decode("utf-8").splitlines():
        json.loads(line)
