"""Integration checks for resumable B-flow boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from test_day_flow import NOW, _board, _runtimes, _state
from test_rules_scripted_runtime import (
    GAME_NOW,
    _novel_execution,
    _ready_request,
)
from test_rules_scripted_runtime import (
    _compiled_classic as _compiled_executable_board,
)
from test_rules_scripted_runtime import (
    _registry as _novel_registry,
)
from test_rules_scripted_runtime import (
    _start_runtime as _start_gateway_runtime,
)
from test_rules_scripted_runtime import (
    _state as _novel_state,
)

from werewolf.domain.enums import Channel, GamePhase
from werewolf.game.actions import ActionDefinition, ActionRegistry, load_action_registry
from werewolf.game.day import DayCoordinator, DayCoordinatorError
from werewolf.game.manager import EventCommitError, GameManager, ResolutionError
from werewolf.game.state import GameState
from werewolf.knowledge.board import BoardDefinition
from werewolf.moderator.night_flow import ModeratorNightFlow
from werewolf.moderator.trigger_flow import ModeratorTriggerError, ModeratorTriggerFlow
from werewolf.rules.compiler import execution_windows_from_board, validate_execution_package
from werewolf.rules.models import BoundaryPolicy, ExecutionPackage
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.player_runtime import (
    InitialContext,
    Observation,
    ReadyResponse,
    RuntimeConfig,
)


def _boundary_policy(board: BoardDefinition) -> BoundaryPolicy:
    return BoundaryPolicy(
        last_words_enabled=board.day_flow.last_words.enabled,
        eligible_death_causes=tuple(board.day_flow.last_words.eligible_death_causes),
        before_reveal=board.day_flow.last_words.before_reveal,
        night_death_policy=board.day_flow.last_words.night_death_policy,
        day_death_policy=board.day_flow.last_words.day_death_policy,
        sheriff_enabled=board.day_flow.sheriff.enabled,
        badge_transfer_enabled=board.day_flow.sheriff.transfer_enabled,
        badge_transfer_on_death=board.day_flow.sheriff.transfer_on_death,
    )


def _report_count(state: GameState) -> int:
    return sum(
        (
            event.get("event_type")
            if isinstance(event, Mapping)
            else getattr(event, "event_type", None)
        )
        == "quasar_skill_report"
        for event in state.events
    )


def _event_field(event: object, name: str) -> object:
    return event.get(name) if isinstance(event, Mapping) else getattr(event, name, None)


def _events_of_type(state: GameState, event_type: str) -> list[object]:
    result: list[object] = []
    for event in state.events:
        value = _event_field(event, "event_type")
        if value == event_type or getattr(value, "value", None) == event_type:
            result.append(event)
    return result


def _event_content(event: object) -> object:
    payload = _event_field(event, "payload")
    return (
        payload.get("content")
        if isinstance(payload, Mapping)
        else getattr(payload, "content", None)
    )


def _hook_execution(
    base_execution: ExecutionPackage,
    board: BoardDefinition,
    *,
    flow_choice: str,
):
    """Build an unfamiliar AFTER-hook package with a conditional FLOW result."""

    from test_rules_scripted_runtime import _compare, _literal, _ref

    raw = json.loads(base_execution.model_dump_json())
    skill = raw["skills"][0]
    skill["timing"] = ["DAY_SPEECH"]
    skill["hook_ids"] = ["DAY_SPEECH_AFTER"]
    skill["disclosures"] = []
    skill["grants"] = [
        {
            "grant_id": "quasar_echo_grant",
            "actor_selector": {
                "op": "select",
                "source": "players",
                "where": _compare("eq", _ref("item", "seat"), _literal(2)),
                "map": _ref("item", "seat"),
            },
        }
    ]
    skill["effects"] = [
        {
            "effect_id": "resume-after-speech",
            "effect_type": "FLOW",
            "flow_action": "RESUME_HOOK",
            "condition": _compare("eq", _ref("request", "parameter:flow"), _literal("resume")),
        },
        {
            "effect_id": "advance-to-victory-check",
            "effect_type": "FLOW",
            "flow_action": "ADVANCE_TO_NIGHT",
            "condition": _compare("eq", _ref("request", "parameter:flow"), _literal("advance")),
        },
    ]
    alternate_choice = "advance" if flow_choice == "resume" else "resume"
    skill["parameters"] = [
        {
            "name": "flow",
            "value_type": "str",
            "required": True,
            "choices": [flow_choice, alternate_choice],
        }
    ]
    raw["window_metadata"] = [
        item.model_dump(mode="json") for item in execution_windows_from_board(board)
    ]
    raw["window_settlement_groups"] = {}
    raw["boundary_policy"] = _boundary_policy(board).model_dump(mode="json")
    return ExecutionPackage.model_validate_json(json.dumps(raw))


@pytest.mark.asyncio
async def test_restarted_day_flow_does_not_reopen_exhausted_speech_queue() -> None:
    """An AFTER-hook resume must not schedule a second ordinary speech queue."""

    manager = GameManager(_state(), registry=load_action_registry())
    runtimes = await _runtimes()
    board = _board(pk_enabled=False)
    try:
        flow = DayCoordinator(manager, board, runtimes)
        await flow.announce(now=NOW)
        await flow.open_speech()
        for _ in range(4):
            await flow.run_next_speech()

        exhausted = await manager.snapshot()
        assert exhausted.phase is GamePhase.DAY_SPEECH
        assert exhausted.current_queue == ()
        assert any(
            getattr(event, "phase", None) is GamePhase.DAY_SPEECH
            and getattr(getattr(event, "event_type", None), "value", None) == "speech"
            for event in exhausted.events
        )

        restored_state = GameState.model_validate_json(exhausted.model_dump_json())
        restored_manager = GameManager(restored_state, registry=load_action_registry())
        restarted = DayCoordinator(restored_manager, board, runtimes)
        with pytest.raises(DayCoordinatorError, match="SPEECH_COMPLETE"):
            await restarted.open_speech()
        assert restored_manager.state.current_queue == ()
    finally:
        for runtime in runtimes.values():
            await runtime.close("B flow test complete")


@pytest.mark.asyncio
async def test_disclosures_use_phase_and_window_hooks_after_restore() -> None:
    """Later windows read committed state; phase and window hooks publish separately."""

    compiled = await _compiled_executable_board()
    assert compiled.execution is not None
    board_payload = json.loads(json.dumps(compiled.package_payload["board_definition"]))
    board_payload["night_windows"] = [
        {"window_id": "quasar_mark", "order": 1, "phase": "NIGHT_ACTION"},
        {
            "window_id": "quasar_inspect",
            "order": 2,
            "phase": "NIGHT_ACTION",
            "depends_on": ["quasar_mark"],
        },
        {
            "window_id": "night_resolve",
            "order": 3,
            "phase": "NIGHT_RESOLVE",
            "depends_on": ["quasar_inspect"],
        },
    ]
    board_payload["knife_rule"]["available_after_window"] = "quasar_inspect"
    board_payload["wolf_team_visibility"]["discussion_enabled"] = False
    board = BoardDefinition.model_validate(board_payload)
    windows = execution_windows_from_board(board)
    settlement_groups = {
        "quasar_mark": "early-inspection",
        "quasar_inspect": "late-inspection",
        "night_resolve": "resolution",
    }
    raw_execution = json.loads(_novel_execution(target_count=1, modes=["scan"]).model_dump_json())
    skill = raw_execution["skills"][0]
    skill["window_ids"] = ["quasar_mark", "quasar_inspect"]
    skill["usage"] = {
        "max_uses": 2,
        "scope": "GAME",
        "pass_records": True,
        "pass_updates_history": False,
    }
    skill["disclosures"] = [
        {
            "disclosure_id": "quasar-self-report",
            "audience": "SELF",
            "fields": [],
            "values": {"marker": {"op": "ref", "source": "skill_state", "name": "marker"}},
            "hook": "night_resolve",
            "event_type": "quasar_skill_report",
        }
    ]
    skill["disclosures"].append(
        {
            "disclosure_id": "quasar-phase-report",
            "audience": "SELF",
            "fields": [],
            "values": {"marker": {"op": "ref", "source": "skill_state", "name": "marker"}},
            "hook": "NIGHT_RESOLVE",
            "event_type": "quasar_phase_report",
        }
    )
    skill["effects"] = [
        {
            "effect_id": "mark-window-state",
            "effect_type": "STATE_SET",
            "state_key": "marker",
            "value": {"op": "literal", "value": "fresh"},
        },
        {
            "effect_id": "inspect-current-state",
            "effect_type": "FACT",
            "target": {"op": "ref", "source": "target", "name": "seat"},
            "fact_type": "quasar_inspected_fresh_state",
            "condition": {
                "op": "eq",
                "left": {"op": "ref", "source": "skill_state", "name": "marker"},
                "right": {"op": "literal", "value": "fresh"},
            },
        },
    ]
    raw_execution["state_declarations"] = [
        {
            "skill_id": "quasar_echo_unfamiliar",
            "key": "marker",
            "value_type": "str",
            "initial": "old",
            "scope": "GAME",
            "expiry_policy": "NEVER",
        }
    ]
    raw_execution["window_metadata"] = [item.model_dump(mode="json") for item in windows]
    raw_execution["window_settlement_groups"] = settlement_groups
    raw_execution["boundary_policy"] = _boundary_policy(board).model_dump(mode="json")
    execution = ExecutionPackage.model_validate_json(json.dumps(raw_execution))
    registry = _novel_registry(target_count=1)
    validate_execution_package(
        execution,
        registry,
        available_windows=windows,
        expected_boundary_policy=_boundary_policy(board),
    )
    game_id = "quasar-deferred-game"
    snapshot_id = "quasar-deferred-snapshot"
    seed_manager = _novel_state(
        compiled,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
        target_count=1,
    )
    players = dict(seed_manager.state.players)
    player_one = players[1]
    players[1] = player_one.model_copy(
        update={
            "granted_abilities": tuple(
                ability.model_copy(
                    update={
                        "usage_limit": ability.usage_limit.model_copy(
                            update={"max_uses": 2, "uses_per_round": 2}
                        )
                    }
                )
                if ability.action_code == 987 and ability.usage_limit is not None
                else ability
                for ability in player_one.granted_abilities
            )
        }
    )
    manager = GameManager(
        seed_manager.state.model_copy(update={"action_windows": {}, "players": players}),
        registry=registry,
        execution_package=execution,
    )
    gateway, server, runtime = await _start_gateway_runtime(
        compiled,
        manager,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
    )
    try:
        first = await runtime._read_skill_status()
        assert any(item["skill_id"] == "quasar_echo_unfamiliar" for item in first["abilities"])
        flow = ModeratorNightFlow(manager, board, {1: runtime}, clock=lambda: GAME_NOW)
        first_window = await flow.open()
        assert first_window.action_window is not None
        assert first_window.action_window.logical_window_id == "quasar_mark"
        result = await flow.action_next(1)
        assert result["status"] == "accepted"
        submitted_action_requests = tuple(
            payload
            for payload in manager.state.action_requests.values()
            if isinstance(payload, dict)
            and payload.get("window_id") == "quasar_mark"
            and payload.get("status") == "PENDING"
        )
        assert len(submitted_action_requests) == 1
        assert (
            submitted_action_requests[0]["actions"][0]["action_code"]
            == execution.skills[0].action_code
        )
        assert not any(
            getattr(event, "event_type", None) == "quasar_skill_report"
            for event in manager.state.events
        )

        await flow.advance()
        state_after_first_group = manager.state
        assert any(
            item.skill_id == "quasar_echo_unfamiliar"
            and item.key == "marker"
            and item.value == "fresh"
            for item in state_after_first_group.rule_state
        )
        assert len(state_after_first_group.rule_deferred_disclosures) == 2
        assert not any(
            getattr(event, "event_type", None) in {"quasar_skill_report", "quasar_phase_report"}
            for event in state_after_first_group.events
        )
        second = await runtime._read_skill_status()
        ability = next(
            item for item in second["abilities"] if item["skill_id"] == "quasar_echo_unfamiliar"
        )
        assert ability["uses_consumed"] == 1

        second_window = await flow.open()
        assert second_window.action_window is not None
        assert second_window.action_window.logical_window_id == "quasar_inspect"
        result = await flow.action_next(1)
        assert result["status"] == "accepted"
        await flow.advance()
        state_after_second_group = manager.state
        assert state_after_second_group.phase is GamePhase.NIGHT_RESOLVE
        assert any(
            item.fact_type == "quasar_inspected_fresh_state"
            for entry in state_after_second_group.rule_ledger
            for item in entry.facts
        )
        assert len(state_after_second_group.rule_deferred_disclosures) == 2
        phase_reports = _events_of_type(state_after_second_group, "quasar_phase_report")
        assert len(phase_reports) == 2
        assert all(
            _event_field(event, "channel") in {Channel.PRIVATE, Channel.PRIVATE.value}
            and tuple(_event_field(event, "audience") or ()) == (1,)
            for event in phase_reports
        )
        assert {_event_content(event) for event in phase_reports} == {
            '{"marker":"old"}',
            '{"marker":"fresh"}',
        }
        assert not any(
            getattr(event, "event_type", None) == "quasar_skill_report"
            for event in state_after_second_group.events
        )

        await flow.open()
        reports = [
            event
            for event in manager.state.events
            if getattr(event, "event_type", None) == "quasar_skill_report"
        ]
        phase_reports = _events_of_type(manager.state, "quasar_phase_report")
        assert len(reports) == 2
        assert len(phase_reports) == 2
        assert all(event.channel is Channel.PRIVATE and event.audience == (1,) for event in reports)
        assert all(
            _event_field(event, "channel") in {Channel.PRIVATE, Channel.PRIVATE.value}
            and tuple(_event_field(event, "audience") or ()) == (1,)
            for event in phase_reports
        )
        assert {event.payload.content for event in reports} == {
            '{"marker":"old"}',
            '{"marker":"fresh"}',
        }
        assert {_event_content(event) for event in phase_reports} == {
            '{"marker":"old"}',
            '{"marker":"fresh"}',
        }
        assert len(manager.state.rule_deferred_disclosures) == 0

        restored = GameState.model_validate_json(manager.state.model_dump_json())
        restored_manager = GameManager(
            restored,
            registry=registry,
            execution_package=execution,
        )
        restored_flow = ModeratorNightFlow(restored_manager, board, {}, clock=lambda: GAME_NOW)
        await restored_flow.open()
        assert _report_count(restored_manager.state) == 2
        restored_phase_reports = _events_of_type(restored_manager.state, "quasar_phase_report")
        assert len(restored_phase_reports) == 2
        assert all(
            _event_field(event, "channel") in {Channel.PRIVATE, Channel.PRIVATE.value}
            and tuple(_event_field(event, "audience") or ()) == (1,)
            for event in restored_phase_reports
        )
        assert {_event_content(event) for event in restored_phase_reports} == {
            '{"marker":"old"}',
            '{"marker":"fresh"}',
        }
    finally:
        await runtime.close("B flow test complete")
        await gateway.close()
        await server.close()


@pytest.mark.parametrize(
    ("flow_choice", "expected_phase"),
    [
        ("resume", GamePhase.DAY_SPEECH),
        ("advance", GamePhase.VICTORY_CHECK),
    ],
)
@pytest.mark.asyncio
async def test_real_after_speech_hook_preserves_source_and_resume_queue(
    flow_choice: str,
    expected_phase: GamePhase,
) -> None:
    """A real Gateway choice interrupts and resumes the exact speech boundary."""

    compiled = await _compiled_executable_board()
    assert compiled.execution is not None
    board = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    registry = _novel_registry(target_count=1)
    seed_execution = _novel_execution(target_count=1, modes=["scan"])
    execution = _hook_execution(seed_execution, board, flow_choice=flow_choice)
    windows = execution_windows_from_board(board)
    validate_execution_package(
        execution,
        registry,
        available_windows=windows,
        expected_boundary_policy=_boundary_policy(board),
    )
    game_id = f"quasar-after-{flow_choice}-game"
    snapshot_id = f"quasar-after-{flow_choice}-snapshot"

    # Rebuild from a schema-one seed without its seat-1 legacy grant. The
    # frozen selector in this package grants the hook ability only to seat 2.
    seed_manager = _novel_state(
        compiled,
        seed_execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
        target_count=1,
    )
    players = dict(seed_manager.state.players)
    players[1] = players[1].model_copy(update={"granted_abilities": ()})
    unbound_state = seed_manager.state.model_copy(
        update={
            "execution_identity": None,
            "ability_instances": (),
            "rule_state": (),
            "rule_ledger": (),
            "phase": GamePhase.DAY_ANNOUNCE,
            "day_no": 1,
            "current_queue": None,
            "players": players,
            "action_windows": {},
            "action_requests": {},
        }
    )
    manager = GameManager(
        unbound_state,
        registry=registry,
        execution_package=execution,
    )

    gateway, server, runtime1 = await _start_gateway_runtime(
        compiled,
        manager,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
    )
    token = gateway.issue_token(
        game_id=game_id,
        snapshot_id=snapshot_id,
        seat=2,
        session_epoch=0,
    )
    runtime2 = DemoRuntime(str(server.make_url("")), token)
    try:
        await runtime2.start(
            RuntimeConfig(session_id=f"session-{game_id}-seat-2"),
            InitialContext(game_id=game_id, seat=2, session_epoch=0, role_id="villager"),
        )
        ready2 = await runtime2.run_turn(
            _ready_request(game_id).model_copy(
                update={
                    "request_id": f"ready-{game_id}-seat-2",
                    "logical_request_id": f"ready-{game_id}-seat-2",
                    "observation": Observation(payload={"seat": 2}),
                }
            )
        )
        assert isinstance(ready2.response, ReadyResponse)
        own_status = await runtime2._read_skill_status()
        assert [item["skill_id"] for item in own_status["abilities"]] == ["quasar_echo_unfamiliar"]

        day = DayCoordinator(manager, board, {1: runtime1, 2: runtime2})
        triggers = ModeratorTriggerFlow(manager, board, {2: runtime2}, clock=lambda: GAME_NOW)
        await day.announce(now=GAME_NOW)
        await day.open_speech((1, 2, 3, 4), now=GAME_NOW)
        with pytest.raises(ModeratorTriggerError, match="RULE_HOOK_NOT_ALLOWED"):
            await triggers.poll_hook("DAY_SPEECH_AFTER")

        spoken = await day.run_next_speech()
        assert spoken.event.payload.speaker_seat == 1
        queue_after_speech = manager.state.current_queue
        assert queue_after_speech == (2, 3, 4)
        completed_turn = manager.state.last_serial_turn
        assert completed_turn is not None
        assert completed_turn.seat == 1
        assert completed_turn.request_id == spoken.request.request_id
        assert spoken.event.event_id in completed_turn.event_ids

        hook = await triggers.poll_hook("DAY_SPEECH_AFTER")
        assert hook["status"] == "choice_pending"
        assert manager.state.phase is GamePhase.TRIGGER_ACTION
        cursor = manager.state.rule_workflow_cursor
        assert cursor is not None and cursor.return_point is not None
        return_point = cursor.return_point
        assert return_point.phase is GamePhase.DAY_SPEECH
        assert return_point.hook_id == "DAY_SPEECH_AFTER"
        assert return_point.speaker_seat == 1
        assert return_point.serial_turn_id == completed_turn.request_id
        assert return_point.event_ids == completed_turn.event_ids
        assert manager.state.current_queue == queue_after_speech
        occurrence_id = hook["occurrence_id"]

        progress = await triggers.open()
        assert progress.action_window is not None
        assert progress.action_window.allowed_seats == (2,)
        chosen = await triggers.next(2)
        assert chosen["status"] == "accepted"
        assert manager.state.phase is expected_phase
        assert manager.state.rule_workflow_cursor is not None
        assert manager.state.rule_workflow_cursor.return_point == return_point
        assert manager.state.last_serial_turn == completed_turn

        if flow_choice == "resume":
            repeated_hook = await triggers.poll_hook("DAY_SPEECH_AFTER")
            assert repeated_hook["status"] == "idle"
            assert (
                sum(
                    occurrence.occurrence_id == occurrence_id
                    for occurrence in manager.state.rule_trigger_queue
                )
                == 1
            )
            restored_day = DayCoordinator(manager, board, {1: runtime1, 2: runtime2})
            resumed = await restored_day.run_next_speech()
            assert resumed.event.payload.speaker_seat == 2
            assert manager.state.current_queue == (3, 4)
        else:
            assert manager.state.phase is GamePhase.VICTORY_CHECK
            assert manager.state.phase is not GamePhase.NIGHT_TEAM_CHAT
            assert manager.state.current_queue == ()
    finally:
        await runtime1.close("B flow test complete")
        await runtime2.close("B flow test complete")
        await gateway.close()
        await server.close()


@pytest.mark.asyncio
async def test_shared_attack_and_guard_group_waits_for_both_real_action_windows() -> None:
    """An attack stays provisional until the guard has submitted in-group."""

    from test_rules_scripted_runtime import _compare, _literal, _ref

    compiled = await _compiled_executable_board()
    assert compiled.execution is not None
    board_payload = json.loads(json.dumps(compiled.package_payload["board_definition"]))
    board_payload["night_windows"] = [
        {"window_id": "quasar_wolf", "order": 1, "phase": "NIGHT_ACTION"},
        {
            "window_id": "quasar_guard",
            "order": 2,
            "phase": "NIGHT_ACTION",
            "depends_on": ["quasar_wolf"],
        },
        {
            "window_id": "night_resolve",
            "order": 3,
            "phase": "NIGHT_RESOLVE",
            "depends_on": ["quasar_guard"],
        },
    ]
    board_payload["knife_rule"]["available_after_window"] = "quasar_wolf"
    board_payload["wolf_team_visibility"]["discussion_enabled"] = False
    board = BoardDefinition.model_validate(board_payload)
    windows = execution_windows_from_board(board)

    base_registry = _novel_registry(target_count=1)
    registry = ActionRegistry(
        actions=(
            *base_registry.actions,
            ActionDefinition(
                action_code=988,
                action_name="QUASAR_GUARD",
                target_policy="other_alive",
                target_count=1,
            ),
        )
    )
    raw_execution = json.loads(_novel_execution(target_count=1, modes=["scan"]).model_dump_json())
    raw_execution["actions"].append(
        {"action_code": 988, "action_id": "QUASAR_GUARD", "allow_pass": False}
    )
    target_seat_three = {
        "op": "select",
        "source": "players",
        "where": {
            "op": "and",
            "values": [
                _compare("eq", _ref("item", "seat"), _literal(3)),
                _compare("eq", _ref("item", "alive"), _literal(True)),
            ],
        },
        "map": _ref("item", "seat"),
    }
    attack = raw_execution["skills"][0]
    attack["window_ids"] = ["quasar_wolf"]
    attack["parameters"] = []
    attack["targets"]["selector"] = target_seat_three
    attack["disclosures"] = []
    attack["effects"] = [
        {
            "effect_id": "quasar-wolf-damage",
            "effect_type": "DAMAGE",
            "target": _ref("target", "seat"),
            "tags": ["quasar_wolf_attack"],
        }
    ]
    guard = json.loads(json.dumps(attack))
    guard.update(
        {
            "skill_id": "quasar_guard_unfamiliar",
            "action_code": 988,
            "window_ids": ["quasar_guard"],
            "grants": [
                {
                    "grant_id": "quasar_guard_grant",
                    "actor_selector": {
                        "op": "select",
                        "source": "players",
                        "where": _compare("eq", _ref("item", "seat"), _literal(2)),
                        "map": _ref("item", "seat"),
                    },
                }
            ],
            "usage": {
                "max_uses": 1,
                "scope": "ROUND",
                "pass_records": False,
                "pass_updates_history": False,
            },
            "effects": [
                {
                    "effect_id": "quasar-guard-prevent-death",
                    "effect_type": "PREVENT_DEATH",
                    "target": _ref("target", "seat"),
                }
            ],
        }
    )
    raw_execution["skills"] = [attack, guard]
    raw_execution["interactions"] = [
        {
            "interaction_id": "confirm-quasar-wolf-damage",
            "rule_type": "CONFIRM_DEATH",
            "damage_tags": ["quasar_wolf_attack"],
            "death_cause": "wolf_attack",
        }
    ]
    settlement_groups = {
        "quasar_wolf": "wolf-guard",
        "quasar_guard": "wolf-guard",
        "night_resolve": "resolution",
    }
    raw_execution["window_metadata"] = [item.model_dump(mode="json") for item in windows]
    raw_execution["window_settlement_groups"] = settlement_groups
    raw_execution["boundary_policy"] = _boundary_policy(board).model_dump(mode="json")
    execution = ExecutionPackage.model_validate_json(json.dumps(raw_execution))
    validate_execution_package(
        execution,
        registry,
        available_windows=windows,
        expected_boundary_policy=_boundary_policy(board),
    )

    game_id = "quasar-wolf-guard-game"
    snapshot_id = "quasar-wolf-guard-snapshot"
    seed_manager = _novel_state(
        compiled,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
        target_count=1,
    )
    manager = GameManager(
        seed_manager.state.model_copy(update={"action_windows": {}}),
        registry=registry,
        execution_package=execution,
    )
    gateway, server, runtime1 = await _start_gateway_runtime(
        compiled,
        manager,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
    )
    token = gateway.issue_token(
        game_id=game_id,
        snapshot_id=snapshot_id,
        seat=2,
        session_epoch=0,
    )
    runtime2 = DemoRuntime(str(server.make_url("")), token)
    try:
        await runtime2.start(
            RuntimeConfig(session_id=f"session-{game_id}-seat-2"),
            InitialContext(game_id=game_id, seat=2, session_epoch=0, role_id="villager"),
        )
        ready2 = await runtime2.run_turn(
            _ready_request(game_id).model_copy(
                update={
                    "request_id": f"ready-{game_id}-seat-2",
                    "logical_request_id": f"ready-{game_id}-seat-2",
                    "observation": Observation(payload={"seat": 2}),
                }
            )
        )
        assert isinstance(ready2.response, ReadyResponse)
        status2 = await runtime2._read_skill_status()
        assert [item["skill_id"] for item in status2["abilities"]] == ["quasar_guard_unfamiliar"]

        flow = ModeratorNightFlow(
            manager,
            board,
            {1: runtime1, 2: runtime2},
            clock=lambda: GAME_NOW,
        )
        first_window = await flow.open()
        assert first_window.action_window is not None
        assert first_window.action_window.allowed_seats == (1,)
        assert first_window.action_window.settlement_group_id == "night:0:wolf-guard"
        attack_result = await flow.action_next(1)
        assert attack_result["status"] == "accepted"
        await flow.advance()

        after_attack_collection = manager.state
        assert after_attack_collection.phase is GamePhase.NIGHT_ACTION
        assert after_attack_collection.players[3].alive is True
        assert not any(
            item.group_id == "night:0:wolf-guard" for item in after_attack_collection.rule_receipts
        )
        assert after_attack_collection.rule_workflow_cursor is not None
        assert after_attack_collection.rule_workflow_cursor.status == "COLLECTING"
        blocked_step = await manager.advance_rule_workflow(
            expected_revision=after_attack_collection.state_revision,
            now=GAME_NOW,
        )
        assert blocked_step.kind == "IDLE"
        assert blocked_step.queue_pending is True
        with pytest.raises((EventCommitError, ResolutionError), match="WINDOW_GROUP_INCOMPLETE"):
            await manager.commit_rule_group(
                "night:0:wolf-guard",
                expected_revision=manager.state.state_revision,
                now=GAME_NOW,
            )

        guard_window = await flow.open()
        assert guard_window.action_window is not None
        assert guard_window.action_window.logical_window_id == "quasar_guard"
        assert guard_window.action_window.allowed_seats == (2,)
        assert guard_window.action_window.settlement_group_id == "night:0:wolf-guard"
        guard_result = await flow.action_next(2)
        assert guard_result["status"] == "accepted"
        guard_request = next(
            payload
            for payload in manager.state.action_requests.values()
            if isinstance(payload, dict)
            and payload.get("window_id") == "quasar_guard"
            and payload.get("status") == "PENDING"
        )
        assert guard_request["actions"][0]["targets"] == (3,)
        assert manager.state.players[3].alive is True
        with pytest.raises((EventCommitError, ResolutionError), match="WINDOW_GROUP_INCOMPLETE"):
            await manager.commit_rule_group(
                "night:0:wolf-guard",
                expected_revision=manager.state.state_revision,
                now=GAME_NOW,
            )

        await flow.advance()
        settled = manager.state
        assert settled.players[3].alive is True
        assert settled.players[3].death_cause is None
        assert any(item.group_id == "night:0:wolf-guard" for item in settled.rule_receipts)
    finally:
        await runtime1.close("B flow test complete")
        await runtime2.close("B flow test complete")
        await gateway.close()
        await server.close()
