import asyncio
import json
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionTurnError,
    ActionTurnScheduler,
    ActionTurnTimeoutError,
    ActionValidationContext,
    ActionWindow,
    GameManager,
    GameState,
    GrantedAbility,
    GrantedTriggerAbility,
    PlayerState,
    RulesetRef,
    load_action_registry,
)
from werewolf.game.events import (
    EventType,
    GameEvent,
    PrivateRolePayload,
    PublicAnnouncementPayload,
)
from werewolf.knowledge.role import (
    TargetKind,
    TargetRule,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
)
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import (
    ActionResponse,
    InitialContext,
    RuntimeConfig,
    RuntimeRef,
    RuntimeRequestMismatchError,
    RuntimeTurnResult,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
REGISTRY = load_action_registry()


def _setup(
    *,
    action_code: int = 102,
    target: int = 2,
    role_id: str = "seer",
    allowed_role_ids: tuple[str, ...] = (),
    window_action_codes: tuple[int, ...] | None = None,
    allow_pass: bool = False,
) -> tuple[GameManager, ActionWindow, ActionValidationContext, ScriptedRuntime]:
    window = ActionWindow(
        window_id="night-window",
        game_id="game-1",
        session_epoch=2,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_role_ids=allowed_role_ids,
        allowed_action_codes=window_action_codes or (action_code,),
        allow_pass=allow_pass,
        opened_at=NOW,
        visible_context={"candidate_seats": [2]},
    )
    state = GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_ACTION,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="a" * 64,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id=role_id,
                faction_id="village",
                session_epoch=2,
                granted_abilities=(
                    GrantedAbility(
                        ability_id="inspect",
                        action_code=action_code,
                        timing=GamePhase.NIGHT_ACTION,
                        allowed_phases=(GamePhase.NIGHT_ACTION,),
                        target_rule=TargetRule(
                            kind=TargetKind.PLAYER,
                            min_targets=1,
                            max_targets=1,
                        ),
                    ),
                ),
            ),
            2: PlayerState(
                seat=2,
                role_id="villager",
                faction_id="village",
                session_epoch=2,
            ),
        },
        action_windows={"night-window": window.model_dump(mode="json")},
    )
    context = ActionValidationContext(
        game_id="game-1",
        session_epoch=2,
        active_request_id="coordinator-placeholder",
        role_id=role_id,
        authorized_action_codes=(action_code,),
        alive_seats=(1, 2),
        eligible_targets_by_action={action_code: (target,)},
    )
    runtime = ScriptedRuntime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": action_code, "targets": [target]}],
            )
        ]
    )
    return GameManager(state, registry=REGISTRY), window, context, runtime


def _trigger_setup() -> tuple[GameManager, ActionWindow, ActionValidationContext, ScriptedRuntime]:
    trigger = TriggerRule(
        event=TriggerEvent.DEATH_CONFIRMED,
        allowed_death_causes=["exiled"],
        mode=TriggerMode.PLAYER_CHOICE,
        effects=[TriggerEffect.OPEN_PLAYER_ACTION],
        allow_pass=True,
    )
    window = ActionWindow(
        window_id="exile-1-trigger-death-shot",
        game_id="trigger-game",
        session_epoch=2,
        phase=GamePhase.TRIGGER_ACTION,
        allowed_seats=(1,),
        allowed_action_codes=(105, 299),
        allow_pass=True,
        opened_at=NOW,
        visible_context={
            "trigger_event": "DEATH_CONFIRMED",
            "ability_id": "death-shot",
            "action_code": 105,
            "resolution_id": "exile-1",
            "death_cause": "exiled",
            "snapshot_revision": None,
            "candidate_seats": [2, 3],
        },
    )
    state = GameState(
        game_id="trigger-game",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.TRIGGER_ACTION,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="a" * 64,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id="hunter",
                faction_id="village",
                alive=False,
                death_cause="exiled",
                session_epoch=2,
                granted_trigger_abilities=(
                    GrantedTriggerAbility(
                        ability_id="death-shot",
                        action_code=105,
                        trigger=trigger,
                        target_rule=TargetRule(
                            kind=TargetKind.PLAYER,
                            min_targets=1,
                            max_targets=1,
                        ),
                    ),
                ),
            ),
            2: PlayerState(
                seat=2,
                role_id="villager",
                faction_id="village",
                session_epoch=2,
            ),
            3: PlayerState(
                seat=3,
                role_id="wolf",
                faction_id="wolf",
                session_epoch=2,
            ),
        },
        action_windows={},
        pending_resolution={
            "status": "TRIGGER_ACTION_REQUIRED",
            "resolution_id": "exile-1",
            "seat": 1,
            "trigger_event": "DEATH_CONFIRMED",
            "ability_id": "death-shot",
            "action_code": 105,
            "death_cause": "exiled",
            "snapshot_revision": None,
        },
    )
    context = ActionValidationContext(
        game_id="trigger-game",
        session_epoch=2,
        active_request_id="coordinator-placeholder",
        authorized_action_codes=(105,),
        alive_seats=(2, 3),
        eligible_targets_by_action={105: (2, 3)},
    )
    runtime = ScriptedRuntime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 105, "targets": [2]}],
            )
        ]
    )
    return GameManager(state, registry=REGISTRY), window, context, runtime


def _announcement(event_id: int = 1) -> GameEvent:
    return GameEvent.public(
        event_id=event_id,
        game_id="game-1",
        state_revision=1,
        round_no=0,
        phase=GamePhase.NIGHT_ACTION,
        created_at=NOW,
        event_type=EventType.ANNOUNCEMENT,
        eligible_seats=(1, 2),
        payload=PublicAnnouncementPayload(content="the night begins"),
    )


def _private_role(event_id: int, seat: int) -> GameEvent:
    return GameEvent.private(
        event_id=event_id,
        game_id="game-1",
        state_revision=1,
        round_no=0,
        phase=GamePhase.NIGHT_ACTION,
        created_at=NOW,
        event_type=EventType.ROLE_ASSIGNMENT,
        seat=seat,
        payload=PrivateRolePayload(role_id="seer", faction_id="village"),
    )


class _TimeoutThenActionRuntime:
    def __init__(self) -> None:
        self._session_ref: RuntimeRef | None = None
        self._attempts = 0
        self.requests = []

    async def start(self, config: RuntimeConfig, context: InitialContext) -> RuntimeRef:
        self._session_ref = RuntimeRef(
            session_id=config.session_id,
            game_id=context.game_id,
            seat=context.seat,
            session_epoch=context.session_epoch,
        )
        return self._session_ref

    async def run_turn(self, request):
        self.requests.append(request)
        self._attempts += 1
        if self._attempts == 1:
            await asyncio.sleep(1)
        response = ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 102, "targets": [2]}],
        )
        return RuntimeTurnResult(
            request_id=request.request_id,
            logical_request_id=request.logical_request_id,
            attempt_no=request.attempt_no,
            response=response,
        )

    async def steer(self, request_id: str, message: str) -> None:
        del request_id, message

    async def abort(self, request_id: str) -> None:
        del request_id

    async def close(self, reason: str) -> None:
        del reason

    def get_session_ref(self) -> RuntimeRef:
        assert self._session_ref is not None
        return self._session_ref


async def _start(
    runtime: ScriptedRuntime,
    *,
    seat: int = 1,
    epoch: int = 2,
    game_id: str = "game-1",
) -> None:
    await runtime.start(
        RuntimeConfig(session_id="session-1"),
        InitialContext(game_id=game_id, seat=seat, session_epoch=epoch),
    )


@pytest.mark.asyncio
async def test_action_turn_binds_runtime_and_commits_pending_request() -> None:
    manager, window, context, runtime = _setup()
    await _start(runtime)

    result = await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)

    prompt = json.loads(PiRuntime()._build_prompt_message(result.request))
    schema = prompt["output_schema"]
    assert schema["properties"]["request_id"]["const"] == result.request.request_id
    assert schema["properties"]["kind"]["const"] == "action"
    assert set(schema["required"]) >= {"schema_version", "request_id", "kind", "actions"}
    assert schema["$defs"]["Action"]["properties"]["action_code"]["enum"] == [102]
    assert schema["properties"]["actions"]["minItems"] == 1

    assert result.state.action_requests
    request_id = next(iter(result.state.action_requests))
    assert result.request.request_id == request_id
    assert result.state.players[1].current_request_id == request_id
    assert result.state.players[1].alive is True


@pytest.mark.asyncio
async def test_shared_night_window_exposes_only_current_seat_grants_and_targets() -> None:
    manager, window, context, runtime = _setup(
        window_action_codes=(101, 102, 299),
        allow_pass=True,
    )
    await _start(runtime)

    await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)

    view = runtime.requests[0].action_window
    assert view is not None
    assert view.allowed_action_codes == [102, 299]
    assert view.candidate_seats == [2]


@pytest.mark.asyncio
async def test_trigger_window_exposes_bound_ability_pass_and_reminder() -> None:
    manager, window, context, runtime = _trigger_setup()
    await manager.commit_action_window(window, now=NOW)
    await _start(runtime, seat=1, game_id="trigger-game")

    await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)

    view = runtime.requests[0].action_window
    assert view is not None
    assert view.allowed_action_codes == [105, 299]
    assert view.candidate_seats == [2, 3]
    assert "105" in runtime.requests[0].observation.summary
    assert "PASS" in runtime.requests[0].observation.summary
    assert "2, 3" in runtime.requests[0].observation.summary


@pytest.mark.asyncio
async def test_successful_action_confirms_the_same_request_delivery_cursor() -> None:
    manager, window, context, runtime = _setup()
    await manager.commit_events((_announcement(),), now=NOW)
    await _start(runtime)

    result = await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)

    cursor = result.state.delivery_cursors[1]
    assert cursor.session_epoch == 2
    assert cursor.committed_event_id == 1
    assert cursor.in_flight_request_id is None
    assert cursor.in_flight_event_ids == ()


@pytest.mark.asyncio
async def test_role_mismatch_rejects_before_runtime_receives_visible_context() -> None:
    manager, window, context, runtime = _setup(allowed_role_ids=("witch",))
    await _start(runtime)

    with pytest.raises(ActionTurnError, match="ROLE_NOT_ALLOWED"):
        await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)

    state = await manager.snapshot()
    assert state.players[1].current_request_id is None
    assert state.delivery_cursors == {}
    assert runtime.requests == ()


@pytest.mark.asyncio
async def test_invalid_target_leaves_only_recoverable_binding() -> None:
    manager, window, context, runtime = _setup(target=2)
    runtime = ScriptedRuntime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 102, "targets": [1]}],
            )
        ]
    )
    await _start(runtime)
    scheduler = ActionTurnScheduler(manager, {1: runtime})

    with pytest.raises(ValueError, match="TARGET_NOT_ALLOWED|SELF_TARGET"):
        await scheduler.run_turn(window, 1, context)

    state = await manager.snapshot()
    assert state.action_requests == {}
    assert state.players[1].current_request_id is not None


@pytest.mark.asyncio
async def test_invalid_action_preserves_the_frozen_batch_for_retry() -> None:
    manager, window, context, runtime = _setup(target=2)
    await manager.commit_events((_announcement(),), now=NOW)
    runtime = ScriptedRuntime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 102, "targets": [1]}],
            ),
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 102, "targets": [2]}],
            ),
        ]
    )
    await _start(runtime)
    scheduler = ActionTurnScheduler(manager, {1: runtime})

    with pytest.raises(ValueError, match="TARGET_NOT_ALLOWED|SELF_TARGET"):
        await scheduler.run_turn(window, 1, context)

    failed = await manager.snapshot()
    old_request_id = failed.players[1].current_request_id
    assert old_request_id is not None
    assert failed.delivery_cursors[1].in_flight_request_id == old_request_id
    assert failed.delivery_cursors[1].in_flight_event_ids == (1,)

    retried = await scheduler.run_turn(window, 1, context, retry=True)
    assert retried.state.action_requests
    assert retried.state.delivery_cursors[1].committed_event_id == 1
    assert retried.state.delivery_cursors[1].in_flight_event_ids == ()
    assert retried.request.request_id != old_request_id
    assert tuple(request.observation.events[0].event_id for request in runtime.requests) == (1, 1)


@pytest.mark.asyncio
async def test_runtime_timeout_preserves_in_flight_batch_for_retry() -> None:
    manager, window, context, _ = _setup(target=2)
    await manager.commit_events((_announcement(),), now=NOW)
    runtime = _TimeoutThenActionRuntime()
    await _start(runtime)
    scheduler = ActionTurnScheduler(manager, {1: runtime}, timeout_seconds=0.01)

    with pytest.raises(ActionTurnTimeoutError):
        await scheduler.run_turn(window, 1, context)

    timed_out = await manager.snapshot()
    old_request_id = timed_out.players[1].current_request_id
    assert old_request_id is not None
    assert timed_out.action_requests == {}
    assert timed_out.delivery_cursors[1].committed_event_id == 0
    assert timed_out.delivery_cursors[1].in_flight_request_id == old_request_id
    assert timed_out.delivery_cursors[1].in_flight_event_ids == (1,)

    retried = await scheduler.run_turn(window, 1, context, retry=True)
    assert retried.request.request_id != old_request_id
    assert retried.state.delivery_cursors[1].committed_event_id == 1
    assert retried.state.delivery_cursors[1].in_flight_event_ids == ()
    assert tuple(event.event_id for event in runtime.requests[0].observation.events) == (1,)
    assert tuple(event.event_id for event in runtime.requests[1].observation.events) == (1,)


@pytest.mark.asyncio
async def test_action_observation_and_in_flight_cursor_exclude_other_seat_private_event() -> None:
    manager, window, context, runtime = _setup(target=2)
    await manager.commit_events(
        (_announcement(), _private_role(2, 2), _private_role(3, 1)),
        now=NOW,
    )
    runtime = ScriptedRuntime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 102, "targets": [1]}],
            )
        ]
    )
    await _start(runtime)
    scheduler = ActionTurnScheduler(manager, {1: runtime})

    with pytest.raises(ValueError, match="TARGET_NOT_ALLOWED|SELF_TARGET"):
        await scheduler.run_turn(window, 1, context)

    state = await manager.snapshot()
    cursor = state.delivery_cursors[1]
    assert cursor.in_flight_event_ids == (1, 3)
    assert all(event_id != 2 for event_id in cursor.in_flight_event_ids)
    assert tuple(event.event_id for event in runtime.requests[0].observation.events) == (1, 3)


@pytest.mark.asyncio
async def test_wrong_runtime_session_is_rejected_before_binding() -> None:
    manager, window, context, runtime = _setup()
    await _start(runtime, seat=2)

    with pytest.raises(ActionTurnError, match="SESSION_MISMATCH"):
        await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)

    state = await manager.snapshot()
    assert state.action_requests == {}
    assert state.players[1].current_request_id is None


@pytest.mark.asyncio
async def test_old_request_response_does_not_create_action_request() -> None:
    manager, window, context, runtime = _setup()
    runtime = ScriptedRuntime(
        [
            lambda request: {
                "schema_version": 1,
                "request_id": "old-request",
                "kind": "action",
                "actions": [{"action_code": 102, "targets": [2]}],
            }
        ]
    )
    await _start(runtime)

    with pytest.raises(RuntimeRequestMismatchError):
        await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)

    state = await manager.snapshot()
    assert state.action_requests == {}
    assert state.players[1].current_request_id is not None


@pytest.mark.asyncio
async def test_second_turn_cannot_replace_active_request_without_retry() -> None:
    manager, window, context, runtime = _setup()
    await _start(runtime)
    scheduler = ActionTurnScheduler(manager, {1: runtime})

    with pytest.raises(ActionTurnError):
        # The first call commits, so the same seat is still bound to its
        # submitted request until the moderator resolves the action.
        await scheduler.run_turn(window, 1, context)
        await scheduler.run_turn(window, 1, context)

    assert len((await manager.snapshot()).action_requests) == 1
