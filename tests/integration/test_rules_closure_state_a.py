"""Restore nested rule JSON through state serialization and phase commits."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game.phase import transition_phase
from werewolf.game.state import GameState, RuleStateValue

NOW = datetime(2026, 10, 4, tzinfo=UTC)
_NESTED_VALUE = {
    "history": [
        {"request": "first", "targets": [1, {"seat": 2}]},
        [3, {"passed": True}],
    ],
    "metadata": {"source": "rule-state", "tags": ["night", "action"]},
}


def _rule_state_value() -> RuleStateValue:
    return RuleStateValue(
        scope="ABILITY",
        scope_id="witch-heal-instance",
        key="history",
        value_type="json",
        value=_NESTED_VALUE,
        source_batch_id="night-batch-1",
    )


def test_rule_state_value_serializes_and_restores_nested_json_without_warnings() -> None:
    value = _rule_state_value()
    assert isinstance(value.value["history"], tuple)

    python_dump = value.model_dump(mode="python")
    assert python_dump["value"] == _NESTED_VALUE
    json_dump = value.model_dump_json()
    assert json.loads(json_dump)["value"] == _NESTED_VALUE

    python_restored = RuleStateValue.model_validate(python_dump)
    json_restored = RuleStateValue.model_validate_json(json_dump)
    assert python_restored.value == value.value
    assert json_restored.value == value.value


def test_game_state_json_restore_and_phase_transition_keep_rule_json_frozen() -> None:
    state = GameState(
        game_id="rule-state-roundtrip",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        round_no=1,
        rule_state=(_rule_state_value(),),
    )

    restored = GameState.model_validate_json(state.model_dump_json())
    assert restored.rule_state[0].value == state.rule_state[0].value
    assert isinstance(restored.rule_state[0].value["history"], tuple)

    transitioned = transition_phase(
        restored,
        GamePhase.NIGHT_ACTION,
        expected_revision=restored.state_revision,
        now=NOW,
    )
    assert transitioned.phase is GamePhase.NIGHT_ACTION
    assert transitioned.state_revision == restored.state_revision + 1
    assert transitioned.rule_state[0].value == restored.rule_state[0].value
    with pytest.raises(TypeError, match="immutable"):
        transitioned.rule_state[0].value["metadata"]["source"] = "changed"  # type: ignore[index]
