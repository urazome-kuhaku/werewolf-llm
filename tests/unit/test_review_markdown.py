"""Deterministic workbench review Markdown tests."""

from datetime import UTC, datetime

import pytest

from werewolf.ruleset_workbench import (
    ClaimScope,
    ClaimStatus,
    ResearchBundle,
    SourceClass,
    render_review_markdown,
)
from werewolf.ruleset_workbench.analysis import analyze_bundle


def _source(source_id: str, excerpt: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://example.com/{source_id}",
        "title": f"Source {source_id}",
        "publisher": "Fixture",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 28, tzinfo=UTC),
        "content_sha256": (source_id[-1] if source_id[-1] in "0123456789abcdef" else "a") * 64,
        "excerpt": excerpt,
        "retrieval_method": "fixture",
    }


def _claim(
    claim_id: str,
    *,
    scope: ClaimScope,
    key: str,
    value: object,
    source_id: str,
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": "classic-12",
        "key": key,
        "value": value,
        "scope": scope,
        "conditions": {},
        "evidence_ids": [source_id],
        "confidence": 0.8,
        "extraction_note": "Fixture extraction.",
        "status": ClaimStatus.UNVERIFIED,
    }


def _bundle() -> ResearchBundle:
    return ResearchBundle.model_validate(
        {
            "board_name": "Classic 12",
            "locale": "zh-CN",
            "sources": [
                _source("source-a", "女巫不可自救。"),
                _source("source-b", "女巫可以自救。"),
                _source("source-c", "板子有十二名玩家。"),
            ],
            "claims": [
                _claim(
                    "claim-role-false",
                    scope=ClaimScope.ROLE,
                    key="witch.can_self_heal",
                    value=False,
                    source_id="source-a",
                ),
                _claim(
                    "claim-role-true",
                    scope=ClaimScope.ROLE,
                    key="witch.can_self_heal",
                    value=True,
                    source_id="source-b",
                ),
                _claim(
                    "claim-board-count",
                    scope=ClaimScope.BOARD,
                    key="board.player_count",
                    value=12,
                    source_id="source-c",
                ),
            ],
        },
    )


def test_review_markdown_is_closed_grouped_and_deterministic() -> None:
    bundle = _bundle()
    first = render_review_markdown(bundle, analyze_bundle(bundle))
    reordered = bundle.model_copy(
        update={
            "sources": tuple(reversed(bundle.sources)),
            "claims": tuple(reversed(bundle.claims)),
        },
    )
    second = render_review_markdown(reordered, analyze_bundle(reordered))

    assert first == second
    assert first.endswith(b"\n")
    assert b"\r" not in first
    assert b"## UNVERIFIED claims by scope" in first
    assert b"### BOARD" in first
    assert b"### ROLE" in first
    assert b"https://example.com/source-a" in first
    assert "女巫不可自救。".encode() in first
    assert "女巫可以自救。".encode() in first
    assert b"NEEDS_DECISION" in first
    assert b"NOT_EVALUATED" in first
    assert b"published" not in first.lower()
    assert b"compiled" not in first.lower()


def test_review_markdown_rejects_unclosed_citation() -> None:
    bundle = _bundle()
    bundle.claims[0].evidence_ids.append("source-missing")

    with pytest.raises(ValueError, match="absent from sources"):
        render_review_markdown(bundle, analyze_bundle(_bundle()))
