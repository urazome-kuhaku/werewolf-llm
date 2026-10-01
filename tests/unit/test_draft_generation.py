"""Tests for deterministic claim-to-schema draft generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import werewolf.ruleset_workbench.draft_generation as draft_generation
from werewolf.domain.enums import GamePhase
from werewolf.ruleset_workbench import (
    ClaimScope,
    ClaimStatus,
    DraftDocumentTemplate,
    DraftGenerationBlockedError,
    DraftGenerationContext,
    DraftOutputExistsError,
    ResearchBundle,
    generate_draft_package,
    materialize_draft_package,
)
from werewolf.ruleset_workbench.coverage import CoverageRequirement
from werewolf.ruleset_workbench.evidence import SourceClass

BOARD_ID = "fictional-board"
BOARD_REF = f"{BOARD_ID}@1.0.0"


def _board_frontmatter() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "board",
        "id": BOARD_ID,
        "version": "1.0.0",
        "name": "虚构四人测试板",
        "aliases": ["四人测试局"],
        "locale": "zh-CN",
        "summary": "用于验证草稿转换的虚构测试板。",
        "seat_count": 4,
        "factions": {"town": 2, "wolf": 2},
        "roles": [
            {
                "role_ref": {"id": "wolf", "version": "1.0.0"},
                "count": 2,
                "effective_rules": {},
                "override_claim_refs": [],
            },
            {
                "role_ref": {"id": "villager", "version": "1.0.0"},
                "count": 2,
                "effective_rules": {},
                "override_claim_refs": [],
            },
        ],
        "victory": {
            "mode": "eliminate_side",
            "winning_sides": ["town", "wolf"],
            "check_phases": ["VICTORY_CHECK"],
            "draw_policy": "no_winner",
        },
        "wolf_team_visibility": {
            "members_know_each_other": True,
            "discussion_enabled": True,
            "identity_visibility": "members",
        },
        "knife_rule": {
            "selection_mode": "consensus",
            "target_visibility": "wolf_team",
            "available_after_window": "wolf_team_chat",
        },
        "night_windows": [
            "wolf_team_chat",
            {"window_id": "night_resolve", "phase": "NIGHT_RESOLVE"},
        ],
        "day_flow": {
            "vote": {
                "visibility_during_collection": "secret",
                "reveal_after_close": "ballots_and_totals",
                "tie_policy": "pk_then_no_exile_on_retie",
            },
            "pk": {"enabled": True, "candidate_count": 2},
        },
        "mechanics": [],
        "interactions": [],
        "reading_plan": {
            "board_ref": {"id": BOARD_ID, "version": "1.0.0"},
            "bootstrap_topics": ["board:overview"],
            "role_required_topics": {
                "wolf": ["role:wolf"],
                "villager": ["role:villager"],
            },
            "phase_topics": {GamePhase.NIGHT_RESOLVE.value: ["board:overview"]},
            "high_risk_topics": ["board:overview"],
            "suggested_queries": ["胜负条件"],
        },
    }


def _role_frontmatter(role_id: str, name: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "role",
        "id": role_id,
        "version": "1.0.0",
        "name": name,
        "status": "published",
        "faction": "WEREWOLF" if role_id == "wolf" else "GOOD",
        "team": "wolf" if role_id == "wolf" else "town",
        "victory_goal": "eliminate_side",
        "public_summary": f"{name}的测试角色说明。",
        "private_identity_card": f"你是{name}。",
        "abilities": [],
        "knowledge_at_start": [],
        "team_visibility": {
            "channel": "TEAM" if role_id == "wolf" else "PRIVATE",
            "share_identity": role_id == "wolf",
            "shared_knowledge": [],
        },
        "death_behavior": {
            "active_abilities_allowed": False,
            "passive_abilities_continue": False,
            "death_trigger_fires": False,
            "description": "死亡后不再行动。",
        },
        "board_compatibility": [BOARD_REF],
        "common_mistakes": [],
    }


def _bundle(*, role_status: ClaimStatus = ClaimStatus.SUPPORTED) -> ResearchBundle:
    source = {
        "source_id": "source-handbook",
        "url": "https://example.com/handbook",
        "title": "测试规则来源",
        "publisher": "Fixture",
        "source_class": SourceClass.PLATFORM_RULES,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 28, tzinfo=UTC),
        "content_sha256": "a" * 64,
        "excerpt": "测试规则摘录。",
        "retrieval_method": "fixture",
    }
    claims = [
        {
            "claim_id": "claim-board-core",
            "ruleset_candidate_id": BOARD_ID,
            "key": "board.seat_count",
            "value": 4,
            "scope": ClaimScope.BOARD,
            "conditions": {},
            "evidence_ids": ["source-handbook"],
            "confidence": 0.9,
            "status": ClaimStatus.SUPPORTED,
        },
        {
            "claim_id": "claim-wolf-core",
            "ruleset_candidate_id": BOARD_ID,
            "key": "wolf.team_visibility",
            "value": "members",
            "scope": ClaimScope.ROLE,
            "conditions": {},
            "evidence_ids": ["source-handbook"],
            "confidence": 0.9,
            "status": role_status,
        },
        {
            "claim_id": "claim-villager-core",
            "ruleset_candidate_id": BOARD_ID,
            "key": "villager.team_visibility",
            "value": "none",
            "scope": ClaimScope.ROLE,
            "conditions": {},
            "evidence_ids": ["source-handbook"],
            "confidence": 0.9,
            "status": ClaimStatus.SUPPORTED,
        },
    ]
    return ResearchBundle.model_validate(
        {"board_name": "虚构测试板", "locale": "zh-CN", "sources": [source], "claims": claims}
    )


def _templates() -> tuple[DraftDocumentTemplate, ...]:
    return (
        DraftDocumentTemplate(
            kind="board",
            document_id=BOARD_ID,
            version="1.0.0",
            name="虚构四人测试板",
            frontmatter=_board_frontmatter(),
            body="# 虚构四人测试板\n\n## 概览 {#overview}\n\n测试板。",
            claim_ids=("claim-board-core",),
        ),
        DraftDocumentTemplate(
            kind="role",
            document_id="wolf",
            version="1.0.0",
            name="狼人",
            frontmatter=_role_frontmatter("wolf", "狼人"),
            body="# 狼人\n\n## 角色规则 {#role_rules}\n\n测试角色。",
            claim_ids=("claim-wolf-core",),
        ),
        DraftDocumentTemplate(
            kind="role",
            document_id="villager",
            version="1.0.0",
            name="平民",
            frontmatter=_role_frontmatter("villager", "平民"),
            body="# 平民\n\n## 角色规则 {#role_rules}\n\n测试角色。",
            claim_ids=("claim-villager-core",),
        ),
    )


def _requirements() -> tuple[CoverageRequirement, ...]:
    return tuple(
        CoverageRequirement(
            requirement_id=claim_id.removeprefix("claim-") or "claim",
            scope=scope,
            key=key,
            min_independent_evidence_count=1,
        )
        for claim_id, scope, key in (
            ("claim-board-core", ClaimScope.BOARD, "board.seat_count"),
            ("claim-wolf-core", ClaimScope.ROLE, "wolf.team_visibility"),
            ("claim-villager-core", ClaimScope.ROLE, "villager.team_visibility"),
        )
    )


def _context() -> DraftGenerationContext:
    return DraftGenerationContext(
        candidate_id=BOARD_ID,
        board_ref=BOARD_REF,
        reviewed_at=date(2026, 9, 28),
    )


def test_generation_is_schema_valid_closed_and_deterministic() -> None:
    first = generate_draft_package(
        _bundle(), _templates(), context=_context(), requirements=_requirements()
    )
    second = generate_draft_package(
        _bundle(), tuple(reversed(_templates())), context=_context(), requirements=_requirements()
    )

    assert first.coverage.passed is True
    assert first.publish_ready is False
    assert first.coverage_json == second.coverage_json
    assert first.publish_manifest_json == second.publish_manifest_json
    assert [path for path, _ in first.files] == [path for path, _ in second.files]
    board = dict(first.files)["draft/boards/fictional-board/1.0.0/board.md"]
    assert b"claim-board-core" in board
    assert b"source-handbook" in board
    assert b"{#provenance}" in board
    payload = json.loads(first.publish_manifest_json)
    assert payload["status"] == "CANDIDATE_PENDING_HUMAN_REVIEW"
    assert (
        payload["files"][0]["sha256"]
        == hashlib.sha256(
            dict(first.files)["draft/boards/fictional-board/1.0.0/board.md"]
        ).hexdigest()
    )


@pytest.mark.parametrize("blocked_status", [ClaimStatus.UNVERIFIED, ClaimStatus.CONFLICTING])
def test_unverified_or_conflicting_claim_is_a_hard_document_gate(
    blocked_status: ClaimStatus,
) -> None:
    with pytest.raises(DraftGenerationBlockedError) as error:
        generate_draft_package(
            _bundle(role_status=blocked_status),
            _templates(),
            context=_context(),
            requirements=_requirements(),
        )

    assert error.value.claim_ids == ("claim-wolf-core",)
    assert error.value.coverage is not None
    assert error.value.coverage.passed is False


@pytest.mark.parametrize("identity", ["kind", "id", "version", "name"])
def test_template_identity_must_match_frontmatter(identity: str) -> None:
    frontmatter = _board_frontmatter()
    frontmatter[identity] = {
        "kind": "role",
        "id": "other-board",
        "version": "2.0.0",
        "name": "另一个板子",
    }[identity]
    template = DraftDocumentTemplate(
        kind="board",
        document_id=BOARD_ID,
        version="1.0.0",
        name="虚构四人测试板",
        frontmatter=frontmatter,
        body="# 板子",
        claim_ids=("claim-board-core",),
    )

    with pytest.raises(ValueError, match="identity"):
        generate_draft_package(
            _bundle(),
            (*(_templates()[1:]), template),
            context=_context(),
            requirements=_requirements(),
        )


def test_document_claim_scope_must_match_document_kind() -> None:
    board = _templates()[0]
    wrong_scope = DraftDocumentTemplate(
        kind=board.kind,
        document_id=board.document_id,
        version=board.version,
        name=board.name,
        frontmatter=board.frontmatter,
        body=board.body,
        claim_ids=("claim-wolf-core",),
    )

    with pytest.raises(ValueError, match="scope"):
        generate_draft_package(
            _bundle(),
            (wrong_scope, *_templates()[1:]),
            context=_context(),
            requirements=_requirements(),
        )


def test_source_refs_must_be_closed_by_selected_claims() -> None:
    board = _templates()[0]
    frontmatter = dict(board.frontmatter)
    frontmatter["source_refs"] = ["source-handbook", "unselected-source"]
    unclosed = DraftDocumentTemplate(
        kind=board.kind,
        document_id=board.document_id,
        version=board.version,
        name=board.name,
        frontmatter=frontmatter,
        body=board.body,
        claim_ids=board.claim_ids,
    )

    with pytest.raises(ValueError, match="not closed"):
        generate_draft_package(
            _bundle(),
            (unclosed, *_templates()[1:]),
            context=_context(),
            requirements=_requirements(),
        )


@pytest.mark.asyncio
async def test_materialization_refuses_to_overwrite_different_content(tmp_path: Path) -> None:
    package = generate_draft_package(
        _bundle(), _templates(), context=_context(), requirements=_requirements()
    )
    await materialize_draft_package(package, tmp_path)

    first_document = package.documents[0]
    changed_document = replace(first_document, content=first_document.content + b"changed\n")
    package = replace(package, documents=(changed_document, *package.documents[1:]))
    with pytest.raises(DraftOutputExistsError):
        await materialize_draft_package(package, tmp_path)


@pytest.mark.asyncio
async def test_materialization_retries_idempotently_for_identical_package(
    tmp_path: Path,
) -> None:
    package = generate_draft_package(
        _bundle(), _templates(), context=_context(), requirements=_requirements()
    )

    await materialize_draft_package(package, tmp_path)
    await materialize_draft_package(package, tmp_path)

    assert sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()
    ) == sorted(relative for relative, _ in package.files)


@pytest.mark.asyncio
async def test_materialization_still_rejects_different_existing_package(
    tmp_path: Path,
) -> None:
    package = generate_draft_package(
        _bundle(), _templates(), context=_context(), requirements=_requirements()
    )
    await materialize_draft_package(package, tmp_path)

    first_document = package.documents[0]
    changed_document = replace(first_document, content=first_document.content + b"changed\n")
    changed_package = replace(
        package,
        documents=(changed_document, *package.documents[1:]),
    )

    with pytest.raises(DraftOutputExistsError):
        await materialize_draft_package(changed_package, tmp_path)


@pytest.mark.asyncio
async def test_materialization_failure_cleans_partial_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = generate_draft_package(
        _bundle(), _templates(), context=_context(), requirements=_requirements()
    )
    original = draft_generation.atomic_write_bytes
    calls = 0

    async def fail_after_first(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk failure")
        await original(path, content)

    monkeypatch.setattr(draft_generation, "atomic_write_bytes", fail_after_first)
    with pytest.raises(OSError, match="simulated disk failure"):
        await materialize_draft_package(package, tmp_path)

    assert not list(tmp_path.rglob("*.md"))
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.glob(".draft-package-*"))
