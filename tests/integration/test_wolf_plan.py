from __future__ import annotations

import json

import pytest
from test_moderator_night_flow import _board, _state, _team_runtime
from test_serial_speech import _BlockingRuntime

from werewolf.game import GameManager, GameState, load_action_registry
from werewolf.game.events import EventType, GameEvent
from werewolf.knowledge.board import BoardDefinition
from werewolf.moderator.night_flow import ModeratorNightError, ModeratorNightFlow
from werewolf.runtime.player_runtime import (
    InitialContext,
    RuntimeConfig,
    Speech,
    SpeechResponse,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime


def _speech(text: str):
    return lambda request: SpeechResponse(
        request_id=request.request_id,
        speech=Speech(text=text),
    )


def _typed_snapshot_state(state: GameState) -> GameState:
    """Rehydrate the event union before exercising manager mutations."""

    events = tuple(
        event if isinstance(event, GameEvent) else GameEvent.model_validate_json(json.dumps(event))
        for event in state.events
    )
    return state.model_copy(update={"events": events})


async def _flow(
    *,
    board: BoardDefinition | None = None,
    seat_one: list[object] | None = None,
    seat_two: list[object] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[ModeratorNightFlow, GameManager, ScriptedRuntime, ScriptedRuntime]:
    manager = GameManager(_state(), registry=load_action_registry())
    wolf_one = await _team_runtime(1, seat_one or [_speech("一号狼提案")])
    wolf_two = await _team_runtime(2, seat_two or [_speech("二号狼提案")])
    flow = ModeratorNightFlow(
        manager,
        board if board is not None else _board(),
        {1: wolf_one, 2: wolf_two},
        snapshot_id="ruleset-test",
        timeout_seconds=timeout_seconds,
    )
    await flow.open()
    return flow, manager, wolf_one, wolf_two


@pytest.mark.asyncio
async def test_final_plan_sees_every_discussion_speech_and_stays_team_private() -> None:
    flow, manager, wolf_one, wolf_two = await _flow(
        seat_one=[_speech("一号建议刀三号"), _speech("最终刀三号，分工守边")],
        seat_two=[_speech("二号建议刀四号")],
    )

    await flow.team_next()
    await flow.team_next()
    with pytest.raises(ModeratorNightError, match="WOLF_PLAN_REQUIRED"):
        await flow.advance()

    await flow.plan_next()
    plan_request = wolf_one.requests[-1]
    observed = {
        event.payload["content"]
        for event in plan_request.observation.events
        if event.event_type == EventType.TEAM_SPEECH.value
    }
    assert "一号建议刀三号" in observed
    assert "二号建议刀四号" in observed
    plan_event = manager.state.events[-1]
    assert plan_event.event_type is EventType.TEAM_SPEECH
    assert plan_event.correlation_id.endswith("-wolf-plan-s1")
    assert plan_event.audience == (1, 2)
    assert all(
        event.audience == (1, 2) for event in manager.state.events if event.channel.value == "TEAM"
    )
    assert not any(event.channel.value == "TEAM" for event in await manager.peek_delivery(3, 1))
    assert wolf_two.requests[-1].logical_request_id.endswith("-speech-s2")


@pytest.mark.asyncio
async def test_plan_confirmation_is_independent_of_final_target_requirement() -> None:
    flow, _, _, _ = await _flow(
        board=_board(final_target_required=False, plan_confirmation_required=True)
    )

    await flow.team_next()
    await flow.team_next()
    with pytest.raises(ModeratorNightError, match="WOLF_PLAN_REQUIRED"):
        await flow.advance()
    assert flow.plan_progress()["enabled"] is True
    await flow.plan_next()
    await flow.advance()
    action = await flow.open()
    assert action.action_window.allow_pass is True
    assert 299 in action.action_window.allowed_action_codes


@pytest.mark.asyncio
async def test_final_target_requirement_does_not_imply_plan_confirmation() -> None:
    flow, manager, _, _ = await _flow(
        board=_board(final_target_required=True, plan_confirmation_required=False)
    )

    await flow.team_next()
    await flow.team_next()
    with pytest.raises(ModeratorNightError, match="WOLF_PLAN_UNAVAILABLE"):
        await flow.plan_next()
    assert flow.plan_progress()["enabled"] is False
    advanced = await flow.advance()

    assert advanced.phase.value == "NIGHT_ACTION"
    action = await flow.open()
    assert action.action_window.allow_pass is True
    assert 299 in action.action_window.allowed_action_codes
    assert manager.state.phase.value == "NIGHT_ACTION"


@pytest.mark.asyncio
async def test_empty_drained_queue_without_current_generation_fails_closed() -> None:
    base = _state().model_copy(update={"round_no": 1, "current_queue": ()})
    manager = GameManager(base, registry=load_action_registry())
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {},
        snapshot_id="ruleset-test",
    )
    await flow.open()

    with pytest.raises(ModeratorNightError, match="TEAM_SPEECH_NOT_STARTED"):
        await flow.advance()


@pytest.mark.asyncio
async def test_discussion_marker_queue_gap_blocks_advance_and_plan() -> None:
    flow, manager, _, _ = await _flow()
    window = flow._current_team_window()
    await flow._start_team_discussion(window)
    # The discussion marker commit succeeded, but queue installation did not.
    manager._state = manager.state.model_copy(update={"current_queue": ()})  # type: ignore[attr-defined]

    with pytest.raises(ModeratorNightError, match="TEAM_SPEECH_INCOMPLETE"):
        await flow.advance()
    with pytest.raises(ModeratorNightError, match="WOLF_PLAN_NOT_READY"):
        await flow.plan_next()


@pytest.mark.asyncio
async def test_plan_retry_survives_timeout_and_reconstructed_flow_can_advance() -> None:
    flow, manager, wolf_one, wolf_two = await _flow(
        seat_one=[_speech("一号建议刀三号")],
        seat_two=[_speech("二号建议刀四号")],
        timeout_seconds=0.01,
    )
    await flow.team_next()
    await flow.team_next()

    blocking = _BlockingRuntime()
    await blocking.start(
        RuntimeConfig(session_id="plan-session-1"),
        InitialContext(game_id="game-1", seat=1, session_epoch=1),
    )
    flow.wolf_plan_scheduler._runtimes[1] = blocking  # type: ignore[attr-defined]
    with pytest.raises(ModeratorNightError, match="runtime timed out"):
        await flow.plan_next()
    assert manager.state.serial_turn is not None
    assert manager.state.current_queue == (1,)
    with pytest.raises(ModeratorNightError, match="WOLF_PLAN_INCOMPLETE"):
        await flow.advance()

    raw_state = GameState.model_validate_json(manager.state.model_dump_json())
    raw_manager = GameManager(
        raw_state,
        registry=load_action_registry(),
    )
    raw_reconstructed = ModeratorNightFlow(
        raw_manager,
        _board(),
        {1: blocking, 2: wolf_two},
        snapshot_id="ruleset-test",
        timeout_seconds=0.01,
    )
    assert raw_reconstructed.plan_progress()["status"] == "IN_PROGRESS"

    restored_manager = GameManager(
        _typed_snapshot_state(raw_state),
        registry=load_action_registry(),
    )
    reconstructed = ModeratorNightFlow(
        restored_manager,
        _board(),
        {1: blocking, 2: wolf_two},
        snapshot_id="ruleset-test",
        timeout_seconds=0.01,
    )
    assert reconstructed.plan_progress()["status"] == "IN_PROGRESS"

    retry = await reconstructed.plan_retry()
    assert retry["attempt_no"] == 2
    assert restored_manager.state.serial_turn is None
    assert restored_manager.state.current_queue == ()
    assert reconstructed.plan_progress()["status"] == "COMPLETE"
    await reconstructed.advance()
    assert restored_manager.state.phase.value == "NIGHT_ACTION"


@pytest.mark.asyncio
async def test_completed_plan_survives_json_restore_and_nonwolf_peek_stays_private() -> None:
    flow, manager, wolf_one, wolf_two = await _flow(
        seat_one=[_speech("一号建议刀三号"), _speech("最终刀三号")],
        seat_two=[_speech("二号建议刀四号")],
    )
    await flow.team_next()
    await flow.team_next()
    await flow.plan_next()

    raw_state = GameState.model_validate_json(manager.state.model_dump_json())
    raw_manager = GameManager(raw_state, registry=load_action_registry())
    raw_restored = ModeratorNightFlow(
        raw_manager,
        _board(),
        {1: wolf_one, 2: wolf_two},
        snapshot_id="ruleset-test",
    )
    assert raw_restored.plan_progress()["status"] == "COMPLETE"

    restored_manager = GameManager(
        _typed_snapshot_state(raw_state),
        registry=load_action_registry(),
    )
    restored = ModeratorNightFlow(
        restored_manager,
        _board(),
        {1: wolf_one, 2: wolf_two},
        snapshot_id="ruleset-test",
    )
    assert restored.plan_progress()["status"] == "COMPLETE"
    await restored.advance()
    assert restored_manager.state.phase.value == "NIGHT_ACTION"
    assert not any(
        event.channel.value == "TEAM" for event in await restored_manager.peek_delivery(3, 1)
    )


@pytest.mark.asyncio
async def test_re_discussion_invalidates_the_previous_plan() -> None:
    flow, manager, _, _ = await _flow(
        seat_one=[
            _speech("第一轮一号提案"),
            _speech("第一轮最终方案"),
            _speech("第二轮一号改提案"),
            _speech("第二轮最终方案"),
        ],
        seat_two=[_speech("第一轮二号提案"), _speech("第二轮二号改提案")],
    )
    await flow.team_next()
    await flow.team_next()
    await flow.plan_next()
    assert flow.plan_progress()["status"] == "COMPLETE"

    again = await flow.team_again()
    assert again["generation"] == 2
    await flow.team_next()
    await flow.team_next()
    with pytest.raises(ModeratorNightError, match="WOLF_PLAN_REQUIRED"):
        await flow.advance()
    assert flow.plan_progress()["generation"] == 2
    await flow.plan_next()
    assert flow.plan_progress()["status"] == "COMPLETE"
    assert (
        len(
            [
                event
                for event in manager.state.events
                if event.event_type is EventType.TEAM_SPEECH
                and event.correlation_id.endswith("-wolf-plan-s1")
            ]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_single_surviving_wolf_still_requires_a_private_final_plan() -> None:
    base = _state()
    players = dict(base.players)
    players[2] = players[2].model_copy(update={"alive": False})
    manager = GameManager(
        base.model_copy(update={"players": players}), registry=load_action_registry()
    )
    wolf = await _team_runtime(
        1,
        [_speech("独狼提案刀三号"), _speech("最终刀三号，失败时改守边")],
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: wolf},
        snapshot_id="ruleset-test",
    )
    opened = await flow.open()
    assert opened.action_window.allowed_seats == (1,)
    await flow.team_next()
    await flow.plan_next()
    assert flow.plan_progress()["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_discussion_marker_and_plan_marker_gaps_are_repairable() -> None:
    flow, manager, _, _ = await _flow(
        seat_one=[_speech("一号提案"), _speech("最终计划")],
        seat_two=[_speech("二号提案")],
    )
    window = flow._current_team_window()
    await flow._start_team_discussion(window)
    assert flow.plan_progress()["status"] == "DISCUSSION_PENDING"
    await flow.team_next()
    await flow.team_next()

    await manager.commit_moderator_operation(
        operation="WOLF_PLAN_STARTED",
        command="night plan next",
        expected_revision=manager.state.state_revision,
        reason=(
            f"round={manager.state.round_no};window={window.window_id};generation=1;coordinator=1"
        ),
    )
    await flow.plan_next()
    assert flow.plan_progress()["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_new_physical_night_window_starts_a_fresh_discussion_generation() -> None:
    base = _state().model_copy(update={"round_no": 1, "current_queue": ()})
    manager = GameManager(base, registry=load_action_registry())
    wolf_one = await _team_runtime(1, [_speech("新夜一号"), _speech("新夜最终计划")])
    wolf_two = await _team_runtime(2, [_speech("新夜二号")])
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: wolf_one, 2: wolf_two},
        snapshot_id="ruleset-test",
    )
    opened = await flow.open()
    assert opened.action_window.window_id == "wolf_team_chat-r1"
    await flow.team_next()
    await flow.team_next()
    assert flow.plan_progress()["generation"] == 1
    await flow.plan_next()
    assert flow.plan_progress()["status"] == "COMPLETE"
