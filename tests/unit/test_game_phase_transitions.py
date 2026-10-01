from datetime import UTC, datetime

import pytest

from werewolf.domain import GamePhase
from werewolf.game import (
    GameState,
    InvalidTransition,
    RulesetRef,
    can_transition,
    transition_phase,
)
from werewolf.knowledge.snapshot import _snapshot_id


def _state(phase: GamePhase = GamePhase.CREATED) -> GameState:
    timestamp = datetime(2026, 9, 28, tzinfo=UTC)
    return GameState(
        game_id="game-1",
        created_at=timestamp,
        updated_at=timestamp,
        phase=phase,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="b" * 64,
        ),
    )


def test_transition_returns_new_state_and_increments_once() -> None:
    old = _state()
    new = transition_phase(
        old,
        GamePhase.RULESET_READY,
        now=datetime(2026, 9, 28, 1, tzinfo=UTC),
    )
    assert new is not old
    assert old.phase is GamePhase.CREATED
    assert old.state_revision == 0
    assert new.phase is GamePhase.RULESET_READY
    assert new.state_revision == 1
    assert new.updated_at == datetime(2026, 9, 28, 1, tzinfo=UTC)


def test_invalid_transition_does_not_change_old_state() -> None:
    old = _state()
    with pytest.raises(InvalidTransition) as error:
        transition_phase(old, GamePhase.NIGHT_ACTION)
    assert error.value.source is GamePhase.CREATED
    assert error.value.target is GamePhase.NIGHT_ACTION
    assert old.phase is GamePhase.CREATED
    assert old.state_revision == 0


def test_unknown_phase_value_is_rejected_without_mutation() -> None:
    old = _state()
    with pytest.raises(InvalidTransition, match="NOT_A_PHASE"):
        transition_phase(old, "NOT_A_PHASE")  # type: ignore[arg-type]
    assert old.phase is GamePhase.CREATED
    assert old.state_revision == 0


def test_sheriff_branch_is_explicit() -> None:
    assert can_transition(GamePhase.DAY_ANNOUNCE, GamePhase.SHERIFF_ELECTION_SPEECH)
    assert can_transition(GamePhase.SHERIFF_ELECTION, GamePhase.SHERIFF_TRANSFER)
    assert can_transition(GamePhase.SHERIFF_ELECTION_PK, GamePhase.SHERIFF_TRANSFER)
    assert can_transition(GamePhase.SHERIFF_TRANSFER, GamePhase.DAY_SPEECH)
    assert not can_transition(GamePhase.SHERIFF_ELECTION, GamePhase.DAY_SPEECH)


def test_vote_tie_branch_and_next_night_are_explicit() -> None:
    assert can_transition(GamePhase.VOTE, GamePhase.VOTE_PK_SPEECH)
    assert can_transition(GamePhase.VOTE_PK, GamePhase.DAY_RESOLVE)
    assert can_transition(GamePhase.VICTORY_CHECK, GamePhase.NIGHT_TEAM_CHAT)


def test_expected_revision_prevents_stale_commit() -> None:
    old = _state()
    with pytest.raises(ValueError, match="revision mismatch"):
        transition_phase(old, GamePhase.RULESET_READY, expected_revision=1)
    assert old.state_revision == 0


def test_transition_preserves_builder_snapshot_reference() -> None:
    snapshot_id = _snapshot_id(
        {
            "board_ref": "classic-12@1.0.0",
            "compiler_version": "knowledge-compiler/1",
            "files": [{"path": "board.md", "sha256": "a" * 64}],
            "game_id": "game-1",
            "package_id": "classic-12@1.0.0",
            "package_identity": "b" * 64,
            "schema_version": 1,
        }
    )
    state = GameState(
        game_id="game-1",
        created_at=datetime(2026, 9, 28, tzinfo=UTC),
        updated_at=datetime(2026, 9, 28, tzinfo=UTC),
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id=snapshot_id,
            manifest_sha256="a" * 64,
        ),
    )

    transitioned = transition_phase(state, GamePhase.RULESET_READY)
    assert transitioned.ruleset is not None
    assert transitioned.ruleset.snapshot_id == snapshot_id


def test_day_and_round_counters_advance_at_cycle_boundaries() -> None:
    night = _state(GamePhase.NIGHT_RESOLVE)
    day = transition_phase(night, GamePhase.DAY_ANNOUNCE)
    assert day.day_no == 1
    assert day.round_no == 0

    day_resolve = day.model_copy(update={"phase": GamePhase.DAY_RESOLVE})
    victory = transition_phase(day_resolve, GamePhase.VICTORY_CHECK)
    assert victory.round_no == 1
    next_night = transition_phase(victory, GamePhase.NIGHT_TEAM_CHAT)
    assert next_night.round_no == 1
