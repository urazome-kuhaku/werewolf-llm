"""Integration tests for the immutable published knowledge package loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from werewolf.knowledge.package_loader import (
    KnowledgePackageDocumentError,
    KnowledgePackageLoader,
    KnowledgePackageReferenceError,
)

BOARD = "test-board@1.0.0"


def _role(*, board_refs: list[str] | None = None, role_id: str = "wolf") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "role",
        "id": role_id,
        "name": "狼人",
        "aliases": [],
        "version": "1.0.0",
        "status": "published",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-09-27",
        "faction": "WEREWOLF",
        "team": "wolf",
        "victory_goal": "eliminate_town",
        "public_summary": "测试角色。",
        "private_identity_card": "你属于狼人阵营。",
        "abilities": [],
        "knowledge_at_start": [],
        "team_visibility": {"channel": "TEAM", "share_identity": True},
        "death_behavior": {
            "active_abilities_allowed": False,
            "passive_abilities_continue": False,
            "death_trigger_fires": False,
            "description": "死亡后不再行动。",
        },
        "board_compatibility": board_refs or [BOARD],
        "common_mistakes": [],
        "claim_refs": ["claim-role"],
        "source_refs": ["source-role"],
    }


def _predicate(*, operator: str = "IS_TRUE", value: object | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "subject": "voter",
        "field": "alive",
        "operator": operator,
    }
    if operator != "IS_TRUE":
        result["value"] = value if value is not None else True
    return result


def _mechanic(
    *,
    mechanic_id: str = "voting",
    version: str = "1.0.0",
    board_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "mechanic",
        "id": mechanic_id,
        "version": version,
        "name": "投票",
        "aliases": [],
        "summary": "测试投票机制。",
        "status": "published",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-09-27",
        "board_refs": board_refs or [BOARD],
        "applicable_phases": ["VOTE"],
        "participation": [
            {"participant_id": "voter", "participant_kind": "player", "conditions": []}
        ],
        "inputs": [],
        "outputs": [
            {
                "output_id": "result",
                "value_type": "vote_result",
                "description": "投票结果。",
                "visibility": "PUBLIC",
            }
        ],
        "processing_order": [{"step_id": "collect", "order": 1, "action": "collect_votes"}],
        "exception_branches": [],
        "result_visibility": [
            {
                "notification_id": "public_result",
                "visibility": "PUBLIC",
                "message_code": "vote_result",
            }
        ],
        "examples": [
            {
                "example_id": "vote_example",
                "given": [_predicate()],
                "when": [{"action_id": "submit", "action": "vote"}],
                "then": {
                    "outcome_code": "vote_collected",
                    "status": "NO_EFFECT",
                    "effects": [],
                },
            }
        ],
        "claim_refs": ["claim-mechanic"],
        "source_refs": ["source-mechanic"],
    }


def _interaction(*, board_refs: list[str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "interaction",
        "id": "wolf-voting",
        "version": "1.0.0",
        "name": "狼人投票交互",
        "status": "published",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-09-27",
        "board_refs": board_refs or [BOARD],
        "subjects": ["wolf", "voting"],
        "situation_key": "wolf.voting",
        "preconditions": [_predicate()],
        "ordering": [{"step_id": "resolve", "order": 1, "action": "resolve_vote"}],
        "outcome": {
            "outcome_code": "resolved",
            "status": "APPLIED",
            "effects": [
                {
                    "effect_id": "mark_resolved",
                    "operation": "SET",
                    "subject": "voting",
                    "field": "resolved",
                    "value": True,
                    "visibility": "PUBLIC",
                }
            ],
        },
        "notifications": [
            {
                "notification_id": "public_result",
                "visibility": "PUBLIC",
                "message_code": "vote_resolved",
            }
        ],
        "examples": [
            {
                "example_id": "interaction_example",
                "given": [_predicate()],
                "when": [{"action_id": "resolve", "action": "resolve_vote"}],
                "then": {
                    "outcome_code": "resolved",
                    "status": "APPLIED",
                    "effects": [
                        {
                            "effect_id": "mark_resolved",
                            "operation": "SET",
                            "subject": "voting",
                            "field": "resolved",
                            "value": True,
                            "visibility": "PUBLIC",
                        }
                    ],
                },
            }
        ],
        "claim_refs": ["claim-interaction"],
        "source_refs": ["source-interaction"],
    }


def _board(
    *,
    mechanics: list[str] | None = None,
    interactions: list[str] | None = None,
    reading_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "board",
        "id": "test-board",
        "version": "1.0.0",
        "name": "测试板",
        "aliases": [],
        "locale": "zh-CN",
        "status": "published",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-09-27",
        "summary": "用于包加载器测试的板子。",
        "seat_count": 1,
        "factions": {"wolf": 1},
        "roles": [
            {
                "role_ref": "wolf@1.0.0",
                "count": 1,
                "effective_rules": {},
                "override_claim_refs": [],
            }
        ],
        "victory": {
            "mode": "eliminate_side",
            "winning_sides": ["wolf"],
            "check_phases": ["VICTORY_CHECK"],
        },
        "wolf_team_visibility": {
            "members_know_each_other": True,
            "discussion_enabled": True,
            "identity_visibility": "members",
        },
        "knife_rule": {"selection_mode": "consensus", "target_visibility": "wolf_team"},
        "night_windows": ["wolf_team_chat"],
        "day_flow": {
            "vote": {
                "visibility_during_collection": "secret",
                "reveal_after_close": "totals_only",
                "tie_policy": "no_exile_on_tie",
            }
        },
        "mechanics": mechanics or ["voting@1.0.0"],
        "interactions": interactions or ["wolf-voting@1.0.0"],
        "reading_plan": reading_plan
        or {
            "board_ref": BOARD,
            "bootstrap_topics": ["board:overview", "mechanic:voting"],
            "role_required_topics": {"wolf": ["role:wolf"]},
            "phase_topics": {"VOTE": ["interaction:wolf-voting"]},
            "high_risk_topics": ["interaction:wolf-voting"],
            "suggested_queries": ["投票如何结算"],
        },
        "claim_refs": ["claim-board"],
        "source_refs": ["source-board"],
    }


def _write_document(
    root: Path, kind: str, identifier: str, version: str, values: dict[str, Any]
) -> None:
    path = root / f"{kind}s" / identifier / version / f"{kind}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = yaml.safe_dump(values, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{yaml_text}---\n# {values.get('name', identifier)}\n", encoding="utf-8")


def _write_package(root: Path, **overrides: Any) -> None:
    board = _board(**overrides)
    _write_document(root, "board", "test-board", "1.0.0", board)
    _write_document(root, "role", "wolf", "1.0.0", _role())
    _write_document(root, "mechanic", "voting", "1.0.0", _mechanic())
    _write_document(root, "interaction", "wolf-voting", "1.0.0", _interaction())


@pytest.mark.asyncio
async def test_loads_complete_package_with_body_hash_and_canonical_paths(tmp_path: Path) -> None:
    _write_package(tmp_path)

    package = await KnowledgePackageLoader(tmp_path).load(BOARD)

    assert package.board_ref.format() == BOARD
    assert package.board.relative_path == "boards/test-board/1.0.0/board.md"
    assert package.roles["wolf"].relative_path == "roles/wolf/1.0.0/role.md"
    assert package.mechanics["voting"].relative_path == "mechanics/voting/1.0.0/mechanic.md"
    assert package.interactions["wolf-voting"].relative_path == (
        "interactions/wolf-voting/1.0.0/interaction.md"
    )
    assert package.board.body.startswith("# 测试板")
    assert len(package.board.sha256) == 64
    assert package.get_document("wolf@1.0.0").model.role_id == "wolf"
    assert tuple(document.ref.format() for document in package.iter_dependencies()) == (
        BOARD,
        "wolf@1.0.0",
        "voting@1.0.0",
        "wolf-voting@1.0.0",
    )


@pytest.mark.asyncio
async def test_missing_dependency_is_rejected_without_path_from_frontmatter(tmp_path: Path) -> None:
    _write_package(tmp_path, mechanics=["missing@1.0.0"])

    with pytest.raises(KnowledgePackageDocumentError, match="missing published mechanic"):
        await KnowledgePackageLoader(tmp_path).load(BOARD)


@pytest.mark.asyncio
async def test_document_identity_and_status_must_match_requested_ref(tmp_path: Path) -> None:
    _write_package(tmp_path)
    role_path = tmp_path / "roles" / "wolf" / "1.0.0" / "role.md"
    values = _role()
    values["id"] = "villager"
    values["status"] = "draft"
    yaml_text = yaml.safe_dump(values, allow_unicode=True, sort_keys=False)
    role_path.write_text(f"---\n{yaml_text}---\n# 狼人\n", encoding="utf-8")

    with pytest.raises(KnowledgePackageDocumentError, match="identity|status"):
        await KnowledgePackageLoader(tmp_path).load(BOARD)


@pytest.mark.asyncio
async def test_duplicate_logical_dependency_ids_across_versions_are_rejected(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, mechanics=["voting@1.0.0", "voting@2.0.0"])

    with pytest.raises(KnowledgePackageReferenceError, match="duplicate logical dependency ID"):
        await KnowledgePackageLoader(tmp_path).load(BOARD)


@pytest.mark.asyncio
async def test_board_affinity_accepts_shared_board_references(tmp_path: Path) -> None:
    _write_package(tmp_path)
    shared_refs = [BOARD, "other-board@1.0.0"]
    _write_document(
        tmp_path,
        "role",
        "wolf",
        "1.0.0",
        _role(board_refs=shared_refs),
    )
    _write_document(
        tmp_path,
        "mechanic",
        "voting",
        "1.0.0",
        _mechanic(board_refs=shared_refs),
    )
    _write_document(
        tmp_path,
        "interaction",
        "wolf-voting",
        "1.0.0",
        _interaction(board_refs=shared_refs),
    )

    package = await KnowledgePackageLoader(tmp_path).load(BOARD)

    assert [ref.format() for ref in package.roles["wolf"].model.board_compatibility] == shared_refs
    assert [ref.format() for ref in package.mechanics["voting"].model.board_refs] == shared_refs
    assert [ref.format() for ref in package.interactions["wolf-voting"].model.board_refs] == (
        shared_refs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["role", "mechanic", "interaction"])
async def test_board_affinity_rejects_other_board_only(tmp_path: Path, kind: str) -> None:
    _write_package(tmp_path)
    other_only = ["other-board@1.0.0"]
    values_by_kind = {
        "role": ("wolf", _role(board_refs=other_only)),
        "mechanic": ("voting", _mechanic(board_refs=other_only)),
        "interaction": ("wolf-voting", _interaction(board_refs=other_only)),
    }
    identifier, values = values_by_kind[kind]
    _write_document(tmp_path, kind, identifier, "1.0.0", values)

    with pytest.raises(KnowledgePackageReferenceError, match="requested board"):
        await KnowledgePackageLoader(tmp_path).load(BOARD)


@pytest.mark.asyncio
async def test_reading_plan_requires_loaded_roles_and_documents(tmp_path: Path) -> None:
    plan = _board()["reading_plan"]
    assert isinstance(plan, dict)
    plan["role_required_topics"] = {"seer": ["role:seer"]}
    _write_package(tmp_path, reading_plan=plan)

    with pytest.raises(KnowledgePackageReferenceError, match="unknown role"):
        await KnowledgePackageLoader(tmp_path).load(BOARD)


@pytest.mark.asyncio
async def test_reading_plan_section_refs_are_exposed_for_compiler_validation(
    tmp_path: Path,
) -> None:
    plan = _board()["reading_plan"]
    assert isinstance(plan, dict)
    plan["bootstrap_topics"] = ["board:unknown-topic"]
    _write_package(tmp_path, reading_plan=plan)

    package = await KnowledgePackageLoader(tmp_path).load(BOARD)

    assert [reference.format() for reference in package.unresolved_reading_refs] == [
        "board:unknown-topic"
    ]


@pytest.mark.asyncio
async def test_reading_plan_document_refs_must_resolve(tmp_path: Path) -> None:
    plan = _board()["reading_plan"]
    assert isinstance(plan, dict)
    plan["bootstrap_topics"] = ["mechanic:unknown-mechanic"]
    _write_package(tmp_path, reading_plan=plan)

    with pytest.raises(KnowledgePackageReferenceError, match="unresolved reference"):
        await KnowledgePackageLoader(tmp_path).load(BOARD)
