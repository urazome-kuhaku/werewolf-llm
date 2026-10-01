from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.game import (
    ActionWindow,
    DeliveryCursor,
    EventCommitError,
    GameManager,
    load_action_registry,
)
from werewolf.game.state import GameState, PlayerState
from werewolf.game.victory import VictoryCandidate, VictoryEvaluation
from werewolf.knowledge.board import BoardDefinition, VictoryDefinition

NOW = datetime(2026, 9, 29, tzinfo=UTC)
REGISTRY = load_action_registry()


def _board(*conditions: str) -> BoardDefinition:
    return BoardDefinition.model_construct(
        board_id="classic-12",
        version="1.0.0",
        status="published",
        reviewed_by="human-reviewer",
        factions={"wolf": 1, "good": 3},
        victory=VictoryDefinition(
            mode="eliminate_side",
            winning_sides=["good", "wolf"],
            special_conditions=list(conditions),
        ),
    )


def _state(*players: PlayerState, **updates: object) -> GameState:
    return GameState(
        game_id="victory-manager-test",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.VICTORY_CHECK,
        state_revision=0,
        ruleset={
            "board_id": "classic-12",
            "version": "1.0.0",
            "snapshot_id": "ruleset-" + "a" * 64,
            "manifest_sha256": "b" * 64,
        },
        players={player.seat: player for player in players},
        **updates,
    )


def _player(
    seat: int,
    role_id: str,
    faction_id: str,
    *,
    alive: bool = True,
) -> PlayerState:
    return PlayerState(seat=seat, role_id=role_id, faction_id=faction_id, alive=alive)


@pytest.mark.asyncio
async def test_ongoing_victory_check_advances_to_next_night_atomically() -> None:
    manager = GameManager(
        _state(_player(1, "wolf", "wolf"), _player(2, "villager", "good")),
        registry=REGISTRY,
    )

    committed = await manager.commit_victory_check(
        _board("good_wins_when_all_wolves_are_dead"), now=NOW
    )

    assert committed.phase is GamePhase.NIGHT_TEAM_CHAT
    assert committed.state_revision == 1
    assert committed.winner is None
    assert committed.moderator_audit[-1]["operation"] == "VICTORY_CHECK"
    assert committed.moderator_audit[-1]["status"] == "ONGOING"


@pytest.mark.asyncio
async def test_unique_winner_is_recorded_before_finished_phase() -> None:
    manager = GameManager(
        _state(_player(1, "wolf", "wolf", alive=False), _player(2, "villager", "good")),
        registry=REGISTRY,
    )

    committed = await manager.commit_victory_check(
        _board("good_wins_when_all_wolves_are_dead"),
        expected_revision=0,
        now=NOW,
    )

    assert committed.phase is GamePhase.FINISHED
    assert committed.state_revision == 1
    assert committed.winner is not None
    assert committed.winner["side"] == "good"
    assert committed.winner["resolved_by"] == "evaluator"
    assert committed.moderator_audit[-1]["winner"] == "good"


@pytest.mark.asyncio
async def test_pending_result_requires_explicit_candidate_moderator_decision() -> None:
    state = _state(
        _player(1, "wolf", "wolf"),
        _player(2, "seer", "good", alive=False),
        _player(3, "unknown-good", "good"),
    )
    manager = GameManager(state, registry=REGISTRY)
    board = _board("wolves_win_when_all_gods_are_dead")

    with pytest.raises(EventCommitError, match="moderator_winner is required"):
        await manager.commit_victory_check(board, role_groups={"seer": "god"}, now=NOW)

    with pytest.raises(EventCommitError, match="explicit candidates"):
        await manager.commit_victory_check(
            board,
            role_groups={"seer": "god"},
            moderator_winner="good",
            now=NOW,
        )

    committed = await manager.commit_victory_check(
        board,
        role_groups={"seer": "god"},
        moderator_winner="wolf",
        reason="主持人确认未分类好人角色不影响狼队条件",
        now=NOW,
    )
    assert committed.phase is GamePhase.FINISHED
    assert committed.winner is not None
    assert committed.winner["side"] == "wolf"
    assert committed.winner["resolved_by"] == "moderator"


@pytest.mark.asyncio
async def test_pending_result_without_candidates_cannot_be_invented_by_moderator() -> None:
    manager = GameManager(
        _state(_player(1, "wolf", "wolf"), _player(2, "unknown-good", "good", alive=False)),
        registry=REGISTRY,
    )

    with pytest.raises(EventCommitError, match="explicit candidates"):
        await manager.commit_victory_check(
            _board("wolves_win_when_all_gods_are_dead"),
            moderator_winner="wolf",
            now=NOW,
        )
    assert manager.state.phase is GamePhase.VICTORY_CHECK
    assert manager.state.state_revision == 0


@pytest.mark.asyncio
async def test_victory_check_rejects_open_window_and_active_request() -> None:
    window = ActionWindow(
        window_id="open-window",
        game_id="victory-manager-test",
        session_epoch=0,
        phase=GamePhase.VICTORY_CHECK,
        allowed_seats=(1,),
        allowed_action_codes=(299,),
        opened_at=NOW,
    )
    manager = GameManager(
        _state(
            _player(1, "wolf", "wolf"),
            _player(2, "villager", "good"),
            action_windows={"open-window": window.model_dump(mode="json")},
            action_requests={"pending-request": {"status": "PENDING"}},
        ),
        registry=REGISTRY,
    )

    with pytest.raises(EventCommitError, match="action request .* still active"):
        await manager.commit_victory_check(
            _board("good_wins_when_all_wolves_are_dead"),
            now=NOW,
        )

    # The request guard is reached before the open-window guard, and neither
    # mutation may leak out of the failed atomic commit.
    assert manager.state.phase is GamePhase.VICTORY_CHECK
    assert manager.state.state_revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", [RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CLOSED])
async def test_victory_check_rejects_blocked_run_status(run_status: RunStatus) -> None:
    manager = GameManager(
        _state(
            _player(1, "wolf", "wolf"),
            _player(2, "villager", "good"),
            run_status=run_status,
        ),
        registry=REGISTRY,
    )

    with pytest.raises(EventCommitError, match=f"game is {run_status.value}"):
        await manager.commit_victory_check(
            _board("good_wins_when_all_wolves_are_dead"),
            now=NOW,
        )

    assert manager.state.phase is GamePhase.VICTORY_CHECK
    assert manager.state.state_revision == 0


@pytest.mark.asyncio
async def test_victory_check_rejects_player_request_binding_at_cycle_boundary() -> None:
    manager = GameManager(
        _state(
            _player(1, "wolf", "wolf").model_copy(update={"current_request_id": "request-1"}),
            _player(2, "villager", "good"),
        ),
        registry=REGISTRY,
    )

    with pytest.raises(EventCommitError, match="seat 1 still has an active action request binding"):
        await manager.commit_victory_check(
            _board("good_wins_when_all_wolves_are_dead"),
            now=NOW,
        )

    assert manager.state.phase is GamePhase.VICTORY_CHECK
    assert manager.state.state_revision == 0


@pytest.mark.asyncio
async def test_victory_check_rejects_in_flight_delivery_cursor_at_cycle_boundary() -> None:
    cursor = DeliveryCursor(session_epoch=0).with_in_flight("delivery-1", (1,))
    manager = GameManager(
        _state(
            _player(1, "wolf", "wolf"),
            _player(2, "villager", "good"),
            delivery_cursors={1: cursor},
        ),
        registry=REGISTRY,
    )

    with pytest.raises(EventCommitError, match="seat 1 still has an in-flight delivery cursor"):
        await manager.commit_victory_check(
            _board("good_wins_when_all_wolves_are_dead"),
            now=NOW,
        )

    assert manager.state.phase is GamePhase.VICTORY_CHECK
    assert manager.state.state_revision == 0


@pytest.mark.asyncio
async def test_pending_conflicting_candidates_only_accept_a_candidate(monkeypatch) -> None:
    def pending(*args, **kwargs) -> VictoryEvaluation:
        del args, kwargs
        return VictoryEvaluation(
            status="PENDING_MODERATOR",
            candidates=(
                VictoryCandidate("good", "condition-a"),
                VictoryCandidate("wolf", "condition-b"),
            ),
            reasons=("SIMULTANEOUS_WIN_CONDITIONS",),
            requires_moderator=True,
        )

    monkeypatch.setattr("werewolf.game.manager.evaluate_victory", pending)
    manager = GameManager(
        _state(_player(1, "wolf", "wolf"), _player(2, "villager", "good")),
        registry=REGISTRY,
    )

    with pytest.raises(EventCommitError, match="explicit candidates"):
        await manager.commit_victory_check(
            _board("good_wins_when_all_wolves_are_dead"),
            moderator_winner="neutral",
            now=NOW,
        )
    committed = await manager.commit_victory_check(
        _board("good_wins_when_all_wolves_are_dead"),
        moderator_winner="wolf",
        now=NOW,
    )
    assert committed.phase is GamePhase.FINISHED
    assert committed.winner is not None
    assert committed.winner["side"] == "wolf"
