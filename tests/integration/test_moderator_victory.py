"""Integration coverage for the moderator's explicit victory commands."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.game import GameManager, load_action_registry
from werewolf.game.state import GameState, PlayerState
from werewolf.knowledge.board import BoardDefinition, VictoryDefinition
from werewolf.moderator import ModeratorError, ModeratorShell

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _board(*conditions: str) -> BoardDefinition:
    return BoardDefinition.model_construct(
        board_id="classic-12",
        version="1.0.0",
        status="published",
        reviewed_by="human-reviewer",
        factions={"wolf": 1, "good": 1},
        victory=VictoryDefinition(
            mode="eliminate_side",
            winning_sides=["good", "wolf"],
            special_conditions=list(conditions),
        ),
    )


def _player(seat: int, faction: str, *, alive: bool = True) -> PlayerState:
    return PlayerState(
        seat=seat,
        role_id=faction,
        faction_id=faction,
        alive=alive,
    )


def _shell(
    board: BoardDefinition,
    *players: PlayerState,
    phase: GamePhase = GamePhase.VICTORY_CHECK,
    run_status: RunStatus = RunStatus.READY,
) -> ModeratorShell:
    state = GameState(
        game_id="moderator-victory-test",
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        run_status=run_status,
        state_revision=0,
        ruleset={
            "board_id": "classic-12",
            "version": "1.0.0",
            "snapshot_id": "ruleset-" + "a" * 64,
            "manifest_sha256": "b" * 64,
        },
        players={player.seat: player for player in players},
    )
    shell = ModeratorShell("unused-game-config.yaml", clock=lambda: NOW)
    shell.manager = GameManager(state, registry=load_action_registry())
    # The shell deliberately reads this frozen runtime object; it never falls
    # back to a live workbench or an unbound board argument.
    shell.runtime_bundle = SimpleNamespace(board=board)
    return shell


@pytest.mark.asyncio
async def test_victory_status_is_public_and_check_advances_an_ongoing_game() -> None:
    shell = _shell(
        _board("good_wins_when_all_wolves_are_dead"),
        _player(1, "wolf"),
        _player(2, "good"),
    )

    with pytest.raises(ModeratorError, match="explicit victory check"):
        await shell.execute("next")

    status = await shell.execute("victory status")
    assert status is not None
    assert status["phase"] == GamePhase.VICTORY_CHECK.value
    assert status["victory"]["status"] == "ONGOING"  # type: ignore[index]
    assert "players" not in status
    assert "role_id" not in str(status)
    assert "faction_id" not in str(status)

    checked = await shell.execute("victory check")
    assert checked is not None
    assert checked["phase"] == GamePhase.NIGHT_TEAM_CHAT.value
    assert checked["victory"]["status"] == "ONGOING"  # type: ignore[index]


@pytest.mark.asyncio
async def test_victory_check_records_an_unambiguous_winner() -> None:
    shell = _shell(
        _board("good_wins_when_all_wolves_are_dead"),
        _player(1, "wolf", alive=False),
        _player(2, "good"),
    )

    checked = await shell.execute("victory check")
    assert checked is not None
    assert checked["phase"] == GamePhase.FINISHED.value
    assert checked["victory"]["winner"] == "good"  # type: ignore[index]
    assert shell.state.winner is not None
    assert shell.state.winner["side"] == "good"

    with pytest.raises(ModeratorError, match="requires the VICTORY_CHECK phase"):
        await shell.execute("victory check")


@pytest.mark.asyncio
async def test_victory_check_rejects_a_wrong_candidate_atomically() -> None:
    shell = _shell(
        _board("good_wins_when_all_wolves_are_dead"),
        _player(1, "wolf", alive=False),
        _player(2, "good"),
    )

    with pytest.raises(ModeratorError, match="does not match the evaluator"):
        await shell.execute("victory check wolf")
    assert shell.state.phase is GamePhase.VICTORY_CHECK
    assert shell.state.state_revision == 0
    assert shell.state.winner is None


@pytest.mark.asyncio
async def test_victory_check_without_candidate_preserves_pending_moderator_decision() -> None:
    shell = _shell(
        _board("wolves_win_when_all_gods_are_dead"),
        _player(1, "wolf"),
        _player(2, "good", alive=False),
    )

    with pytest.raises(ModeratorError, match="moderator_winner is required"):
        await shell.execute("victory check")
    assert shell.state.phase is GamePhase.VICTORY_CHECK
    assert shell.state.state_revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", [RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CLOSED])
async def test_victory_check_rejects_blocked_run_status(run_status: RunStatus) -> None:
    shell = _shell(
        _board("good_wins_when_all_wolves_are_dead"),
        _player(1, "wolf"),
        _player(2, "good"),
        run_status=run_status,
    )

    with pytest.raises(ModeratorError, match="paused|failed|closed"):
        await shell.execute("victory check")
    assert shell.state.phase is GamePhase.VICTORY_CHECK
    assert shell.state.state_revision == 0


@pytest.mark.asyncio
async def test_victory_check_rejects_early_phase_and_help_lists_explicit_commands() -> None:
    shell = _shell(
        _board("good_wins_when_all_wolves_are_dead"),
        _player(1, "wolf"),
        _player(2, "good"),
        phase=GamePhase.DAY_RESOLVE,
    )

    with pytest.raises(ModeratorError, match="requires the VICTORY_CHECK phase"):
        await shell.execute("victory check")
    help_result = await shell.execute("help")
    assert help_result is not None
    assert "victory status" in help_result["commands"]
    assert "victory check [candidate-side]" in help_result["commands"]
