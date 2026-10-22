"""Security and matching tests for the closed declarative scenario format."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.domain.scenario import (
    AssertionField,
    HostCommandName,
    HostCommandStep,
    ScenarioDefinition,
    ScenarioError,
    ScenarioMode,
    ScriptAction,
    StateAssertionStep,
    build_host_command,
    load_scenario,
)
from werewolf.runtime.declarative_runtime import ScenarioRunEvidence, ScenarioRuntimeError
from werewolf.runtime.player_runtime import (
    ActionWindowView,
    Deadline,
    Observation,
    ResponseKind,
    TurnRequest,
)


def _request(*, seat: int = 4, round_no: int = 2) -> TurnRequest:
    now = datetime(2026, 10, 22, tzinfo=UTC)
    return TurnRequest(
        request_id="game-r2-action-window-s4-a1",
        logical_request_id="game-r2-action-window-s4",
        attempt_no=1,
        game_id="game-1",
        session_epoch=0,
        phase=GamePhase.NIGHT_ACTION,
        expected_kind=ResponseKind.ACTION,
        action_window=ActionWindowView(
            window_id="window-1",
            allowed_action_codes=[106],
            candidate_seats=[2, 4],
        ),
        observation=Observation(
            payload={"seat": seat, "phase": "NIGHT_ACTION", "round_no": round_no, "day_no": 1}
        ),
        output_schema={"type": "object"},
        deadline=Deadline(soft_deadline=now, hard_deadline=now + timedelta(seconds=30)),
    )


def test_yaml_loader_rejects_duplicate_unknown_and_python_tags(tmp_path) -> None:
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        "schema_version: 1\nschema_version: 1\nscenario_id: gate\n"
        "board_ref: board@1.0.0\nmode: COMMAND\n",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError):
        load_scenario(duplicate)

    python_tag = tmp_path / "python-tag.yaml"
    python_tag.write_text(
        "schema_version: 1\nscenario_id: gate\nboard_ref: board@1.0.0\nmode: COMMAND\n"
        "steps: !!python/object/apply:os.system ['echo unsafe']\n",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError):
        load_scenario(python_tag)

    duplicate_json = tmp_path / "duplicate.json"
    duplicate_json.write_text(
        '{"scenario_id":"gate","scenario_id":"other",'
        '"board_ref":"board@1.0.0","mode":"COMMAND"}',
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError, match="duplicate key"):
        load_scenario(duplicate_json)

    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text(
        '{"scenario_id":"gate","board_ref":"board@1.0.0",'
        '"mode":"COMMAND","budget":{"max_turns":NaN}}',
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError, match="numeric constant"):
        load_scenario(non_finite)

    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(
            {
                "schema_version": 1,
                "scenario_id": "gate",
                "board_ref": "board@1.0.0",
                "mode": "COMMAND",
                "callback": "module.function",
            }
        )


def test_host_command_is_rendered_from_typed_bounded_arguments(tmp_path) -> None:
    step = HostCommandStep.model_validate(
        {
            "kind": "HOST_COMMAND",
            "step_id": "confirm-exile",
            "command": "DAY_CONFIRM_EXILE",
            "target": "NONE",
        },
        strict=False,
    )
    assert build_host_command(step) == "day confirm-exile none"

    with pytest.raises(ValidationError):
        HostCommandStep.model_validate(
            {"kind": "HOST_COMMAND", "step_id": "run", "command": "shell", "content": "rm -rf"}
        )

    for index, invalid in enumerate(
        (
            {
                "kind": "PLAYER_TURN",
                "step_id": "string-seat",
                "match": {"seat": "4"},
            },
            {
                "kind": "PLAYER_TURN",
                "step_id": "bool-seat",
                "match": {"seat": True},
            },
        )
    ):
        invalid_path = tmp_path / f"invalid-seat-{index}.json"
        invalid_path.write_text(
            json.dumps(
                {
                    "scenario_id": "strict-bounds",
                    "board_ref": "test_board@1.0.0",
                    "mode": "COMMAND",
                    "steps": [invalid],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ScenarioError):
            load_scenario(invalid_path)

    strict_budget = tmp_path / "strict-budget.json"
    strict_budget.write_text(
        json.dumps(
            {
                "scenario_id": "strict-budget",
                "board_ref": "test_board@1.0.0",
                "mode": "COMMAND",
                "budget": {"max_turns": True},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError):
        load_scenario(strict_budget)


def test_typed_host_only_assertions_require_narrow_selectors() -> None:
    good = StateAssertionStep.model_validate(
        {
            "kind": "ASSERT",
            "step_id": "guard-alive",
            "field": AssertionField.ALIVE,
            "operator": "EQ",
            "expected": True,
            "seat": 4,
        }
    )
    assert good.seat == 4

    with pytest.raises(ValidationError):
        StateAssertionStep.model_validate(
            {
                "kind": "ASSERT",
                "step_id": "free-path",
                "field": "game_state_value",
                "operator": "EQ",
                "expected": True,
                "state_key": "resolved",
                "seat": 4,
            }
        )
    with pytest.raises(ValidationError):
        HostCommandStep.model_validate(
            {
                "kind": "HOST_COMMAND",
                "step_id": "bad-target",
                "command": "DAY_CONFIRM_EXILE",
                "target": 100,
            }
        )


def test_player_step_matches_only_its_bound_seat_and_private_skill_projection() -> None:
    step = {
        "kind": "PLAYER_TURN",
        "step_id": "guard-r2",
        "match": {
            "seat": 4,
            "role_id": "guard",
            "phase": "NIGHT_ACTION",
            "round_no": 2,
            "day_no": 1,
            "authorized_skill_ids": ["guard_protect"],
        },
        "actions": [{"action_code": 106, "targets": [2]}],
    }
    scenario = ScenarioDefinition.model_validate(
        {
            "scenario_id": "guard-gate",
            "board_ref": "test_board@1.0.0",
            "mode": ScenarioMode.COMMAND.value,
            "steps": [step],
        },
        strict=False,
    )
    evidence = ScenarioRunEvidence(scenario=scenario, turn_budget=5)
    request = _request()

    with pytest.raises(ScenarioRuntimeError):
        evidence.select_player_step(
            request,
            seat=5,
            role_id="guard",
            skill_ids=frozenset({"guard_protect"}),
        )
    assert (
        evidence.select_player_step(
            request,
            seat=4,
            role_id="guard",
            skill_ids=frozenset({"wolf_kill"}),
        )
        is None
    )

    matched = evidence.select_player_step(
        request,
        seat=4,
        role_id="guard",
        skill_ids=frozenset({"guard_protect"}),
    )
    assert matched is not None
    assert isinstance(matched.actions[0], ScriptAction)
    assert matched.actions[0].targets == (2,)
    assert evidence.required_unconsumed() == ["guard-r2"]


def test_host_step_consumption_needs_success_and_post_state_proof() -> None:
    step = HostCommandStep.model_validate(
        {
            "kind": "HOST_COMMAND",
            "step_id": "read-day",
            "command": HostCommandName.DAY_STATUS,
        }
    )
    scenario = ScenarioDefinition.model_validate(
        {
            "scenario_id": "host-proof",
            "board_ref": "test_board@1.0.0",
            "mode": "COMMAND",
            "steps": [step.model_dump(mode="json")],
        },
        strict=False,
    )
    failed = ScenarioRunEvidence(scenario=scenario, turn_budget=1)
    state = {
        "game_id": "game-1",
        "state_revision": 5,
        "phase": "DAY_ANNOUNCE",
        "run_status": "RUNNING",
    }
    failed.observe_host_command(
        "day status",
        {"status": "error"},
        pre_state=state,
        post_state=state,
    )
    assert failed.required_unconsumed() == ["read-day"]

    succeeded = ScenarioRunEvidence(scenario=scenario, turn_budget=1)
    succeeded.observe_host_command(
        "day status",
        {"status": "ok"},
        pre_state=state,
        post_state=state,
    )
    assert succeeded.required_unconsumed() == []

