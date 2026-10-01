from datetime import UTC, datetime

import pytest

from werewolf.game.state import GameState, PlayerState
from werewolf.game.victory import (
    InvalidVictoryDefinitionError,
    RulesetMismatchError,
    UnpublishedBoardError,
    UnreviewedBoardError,
    evaluate_victory,
)
from werewolf.knowledge.board import BoardDefinition, VictoryDefinition
from werewolf.knowledge.preview import experimental_preview

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _board(*conditions: str, status: str = "published") -> BoardDefinition:
    return BoardDefinition.model_construct(
        board_id="classic-12",
        version="1.0.0",
        status=status,
        reviewed_by="human-reviewer",
        factions={"wolf": 2, "good": 4},
        victory=VictoryDefinition(
            mode="eliminate_side",
            winning_sides=["good", "wolf"],
            special_conditions=list(conditions),
        ),
    )


def _state(*players: PlayerState) -> GameState:
    return GameState(
        game_id="victory-test",
        created_at=NOW,
        updated_at=NOW,
        ruleset={
            "board_id": "classic-12",
            "version": "1.0.0",
            "snapshot_id": "ruleset-" + "a" * 64,
            "manifest_sha256": "b" * 64,
        },
        players={player.seat: player for player in players},
    )


def _player(
    seat: int,
    role_id: str,
    faction_id: str,
    *,
    alive: bool = True,
) -> PlayerState:
    return PlayerState(
        seat=seat,
        role_id=role_id,
        faction_id=faction_id,
        alive=alive,
    )


def test_all_wolves_dead_is_a_good_winner_without_count_constants() -> None:
    state = _state(
        _player(1, "wolf-a", "wolf", alive=False),
        _player(2, "wolf-b", "wolf", alive=False),
        _player(3, "citizen-a", "good"),
        _player(4, "citizen-b", "good"),
        _player(5, "seer", "good"),
    )

    result = evaluate_victory(
        state,
        _board("good_wins_when_all_wolves_are_dead"),
    )

    assert result.status == "WINNER"
    assert result.winner == "good"
    assert result.candidates[0].condition == "all_wolves_dead"
    assert result.requires_moderator is False


@pytest.mark.parametrize(
    ("condition", "dead_role", "role_groups"),
    [
        (
            "wolves_win_when_all_gods_are_dead",
            "seer",
            {"seer": "god", "villager-a": "villager"},
        ),
        (
            "wolves_win_when_all_villagers_are_dead",
            "villager-a",
            {"seer": "god", "villager-a": "villager"},
        ),
    ],
)
def test_wolf_side_requires_a_living_wolf_and_uses_board_role_groups(
    condition: str,
    dead_role: str,
    role_groups: dict[str, str],
) -> None:
    state = _state(
        _player(1, "wolf-a", "wolf"),
        _player(2, "wolf-b", "wolf", alive=False),
        _player(3, "seer", "good", alive=dead_role != "seer"),
        _player(4, "villager-a", "good", alive=dead_role != "villager-a"),
    )

    result = evaluate_victory(
        state,
        _board(condition),
        role_groups=role_groups,
    )

    assert result.status == "WINNER"
    assert result.winner == "wolf"


def test_all_wolves_dead_does_not_make_wolf_side_win_when_all_good_is_dead() -> None:
    state = _state(
        _player(1, "wolf", "wolf", alive=False),
        _player(2, "seer", "good", alive=False),
        _player(3, "villager", "good", alive=False),
    )
    result = evaluate_victory(
        state,
        _board(
            "good_wins_when_all_wolves_are_dead",
            "wolves_win_when_all_gods_are_dead",
        ),
        role_groups={"seer": "god"},
    )

    assert result.status == "WINNER"
    assert result.winner == "good"
    assert {candidate.side for candidate in result.candidates} == {"good"}
    assert result.requires_moderator is False


def test_wolf_side_conditions_merge_into_one_candidate() -> None:
    state = _state(
        _player(1, "wolf", "wolf"),
        _player(2, "seer", "good", alive=False),
        _player(3, "villager", "good", alive=False),
    )

    result = evaluate_victory(
        state,
        _board(
            "wolves_win_when_all_gods_are_dead",
            "wolves_win_when_all_villagers_are_dead",
            "wolf_side_requires_surviving_wolf",
        ),
        role_groups={"seer": "god", "villager": "villager"},
    )

    assert result.status == "WINNER"
    assert result.winner == "wolf"
    assert len(result.candidates) == 1
    assert result.candidates[0].condition == "all_gods_dead"


def test_surviving_wolf_requirement_is_machine_readable() -> None:
    state = _state(
        _player(1, "wolf", "wolf", alive=False),
        _player(2, "seer", "good", alive=False),
    )

    result = evaluate_victory(
        state,
        _board(
            "wolves_win_when_all_gods_are_dead",
            "wolf_side_requires_surviving_wolf",
        ),
        role_groups={"seer": "god"},
    )

    assert result.status == "ONGOING"
    assert "UNSUPPORTED_SPECIAL_CONDITIONS" not in result.reasons


def test_live_players_keep_game_ongoing() -> None:
    state = _state(
        _player(1, "wolf", "wolf"),
        _player(2, "villager", "good"),
    )

    result = evaluate_victory(
        state,
        _board("good_wins_when_all_wolves_are_dead"),
    )

    assert result.status == "ONGOING"
    assert result.winner is None


def test_unpublished_board_is_rejected_before_evaluating_state() -> None:
    state = _state(_player(1, "wolf", "wolf", alive=False))

    with pytest.raises(UnpublishedBoardError):
        evaluate_victory(
            state,
            _board("good_wins_when_all_wolves_are_dead", status="draft"),
        )


def test_review_placeholder_is_rejected() -> None:
    state = _state(_player(1, "wolf", "wolf", alive=False))

    with pytest.raises(UnreviewedBoardError):
        evaluate_victory(
            state,
            _board("good_wins_when_all_wolves_are_dead").model_copy(
                update={"reviewed_by": "pending-human-review"}
            ),
        )


def test_preview_scope_allows_pending_review_board_for_experimental_evaluation() -> None:
    state = _state(_player(1, "wolf", "wolf", alive=False))
    board = _board("good_wins_when_all_wolves_are_dead").model_copy(
        update={"reviewed_by": "pending-human-review"}
    )

    with experimental_preview():
        evaluation = evaluate_victory(state, board)

    assert evaluation.winner == "good"


@pytest.mark.parametrize(
    ("board_id", "version"),
    [("other-board", "1.0.0"), ("classic-12", "2.0.0")],
)
def test_board_must_match_frozen_game_ruleset(board_id: str, version: str) -> None:
    state = _state(_player(1, "wolf", "wolf", alive=False))

    with pytest.raises(RulesetMismatchError):
        evaluate_victory(
            state,
            _board("good_wins_when_all_wolves_are_dead").model_copy(
                update={"board_id": board_id, "version": version}
            ),
        )


def test_missing_machine_condition_is_rejected_instead_of_guessed() -> None:
    state = _state(_player(1, "wolf", "wolf", alive=False))

    with pytest.raises(InvalidVictoryDefinitionError):
        evaluate_victory(state, _board())


def test_missing_good_team_split_waits_for_moderator() -> None:
    state = _state(
        _player(1, "wolf", "wolf"),
        _player(2, "unknown-good-role", "good", alive=False),
    )

    result = evaluate_victory(
        state,
        _board("wolves_win_when_all_gods_are_dead"),
    )

    assert result.status == "PENDING_MODERATOR"
    assert result.winner is None
    assert result.requires_moderator is True
    assert "MISSING_GOD_ROLE_GROUP" in result.reasons
