"""Schema and evaluator coverage for board-owned victory role groups."""

from datetime import UTC, datetime

import pytest

from werewolf.game.state import GameState, PlayerState
from werewolf.game.victory import evaluate_victory
from werewolf.knowledge.board import BoardDefinition, VictoryDefinition

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _board_data(*, role_groups: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "board",
        "id": "role-group-board",
        "version": "1.0.0",
        "name": "胜负分组测试板",
        "aliases": [],
        "locale": "zh-CN",
        "status": "published",
        "reviewed_by": "human-reviewer",
        "reviewed_at": "2026-10-01",
        "summary": "验证板子胜负角色分组。",
        "seat_count": 4,
        "factions": {"wolf": 1, "good": 3},
        "roles": [
            {
                "role_ref": {"id": "wolf", "version": "1.0.0"},
                "count": 1,
                "effective_rules": {},
                "override_claim_refs": [],
            },
            {
                "role_ref": {"id": "seer", "version": "1.0.0"},
                "count": 1,
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
            "winning_sides": ["good", "wolf"],
            "check_phases": ["VICTORY_CHECK"],
            "special_conditions": ["wolves_win_when_all_gods_are_dead"],
            **({"role_groups": role_groups} if role_groups is not None else {}),
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
        "night_windows": [{"window_id": "wolf_team_chat", "phase": "NIGHT_TEAM_CHAT"}],
        "day_flow": {
            "vote": {
                "visibility_during_collection": "secret",
                "reveal_after_close": "totals_only",
                "tie_policy": "no_exile_on_tie",
            }
        },
        "mechanics": [],
        "interactions": [],
        "reading_plan": {
            "board_ref": {"id": "role-group-board", "version": "1.0.0"},
            "bootstrap_topics": ["board:overview"],
            "role_required_topics": {},
            "phase_topics": {},
            "high_risk_topics": ["board:overview"],
            "suggested_queries": [],
        },
        "claim_refs": ["claim-board"],
        "source_refs": ["source-board"],
    }


def _state(*players: PlayerState) -> GameState:
    return GameState(
        game_id="role-group-game",
        created_at=NOW,
        updated_at=NOW,
        ruleset={
            "board_id": "role-group-board",
            "version": "1.0.0",
            "snapshot_id": "ruleset-" + "a" * 64,
            "manifest_sha256": "b" * 64,
        },
        players={player.seat: player for player in players},
    )


def _player(seat: int, role_id: str, faction_id: str, *, alive: bool = True) -> PlayerState:
    return PlayerState(seat=seat, role_id=role_id, faction_id=faction_id, alive=alive)


def test_non_empty_role_groups_must_cover_exactly_the_bound_roles() -> None:
    board = BoardDefinition.model_validate(
        _board_data(role_groups={"wolf": "wolf", "seer": "god", "villager": "villager"})
    )

    assert board.victory.role_groups == {
        "wolf": "wolf",
        "seer": "god",
        "villager": "villager",
    }


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        (
            {"wolf": "wolf", "seer": "god"},
            "missing role IDs: villager",
        ),
        (
            {"wolf": "wolf", "seer": "god", "villager": "villager", "guard": "god"},
            "unknown role IDs: guard",
        ),
    ],
)
def test_non_empty_role_groups_reject_missing_or_unknown_roles(
    mapping: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        BoardDefinition.model_validate(_board_data(role_groups=mapping))


def test_board_role_groups_are_authoritative_over_legacy_explicit_mapping() -> None:
    board = BoardDefinition.model_construct(
        board_id="role-group-board",
        version="1.0.0",
        status="published",
        reviewed_by="human-reviewer",
        factions={"wolf": 1, "good": 3},
        victory=VictoryDefinition(
            mode="eliminate_side",
            winning_sides=["good", "wolf"],
            special_conditions=["wolves_win_when_all_gods_are_dead"],
            role_groups={"wolf": "wolf", "seer": "god", "villager": "villager"},
        ),
    )
    state = _state(
        _player(1, "wolf", "wolf"),
        _player(2, "seer", "good", alive=False),
        _player(3, "villager", "good"),
    )

    result = evaluate_victory(
        state,
        board,
        role_groups={"seer": "villager"},
    )

    assert result.status == "WINNER"
    assert result.winner == "wolf"
