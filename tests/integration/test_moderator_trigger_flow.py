"""Integration coverage for the generic moderator trigger boundary."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_hunter_trigger import _state as hunter_state
from test_moderator_night_flow import _board

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionResolution,
    ActionResolutionEntry,
    GameManager,
    ResolutionEffect,
    ResolutionStatus,
    load_action_registry,
)
from werewolf.moderator.trigger_flow import ModeratorTriggerError, ModeratorTriggerFlow
from werewolf.runtime.player_runtime import ActionResponse, InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 29, tzinfo=UTC)
REGISTRY = load_action_registry()


async def _runtime(*actions: object) -> ScriptedRuntime:
    runtime = ScriptedRuntime(actions)
    await runtime.start(
        RuntimeConfig(session_id="trigger-session"),
        InitialContext(game_id="hunter-game", seat=1, session_epoch=2),
    )
    return runtime


def _resolution(flow: ModeratorTriggerFlow, *, target_seat: int | None = 2) -> ActionResolution:
    record = flow.pending()["pending_requests"]
    assert isinstance(record, list) and len(record) == 1
    raw = record[0]
    assert isinstance(raw, dict)
    actions = raw["actions"]
    assert isinstance(actions, list) and len(actions) == 1
    requested = Action.model_validate(actions[0])
    effects = ()
    if target_seat is not None:
        effects = (
            ResolutionEffect(
                effect_id="trigger-death",
                action_index=0,
                effect_type="SET_ALIVE",
                target_seat=target_seat,
                value=False,
            ),
            ResolutionEffect(
                effect_id="trigger-cause",
                action_index=0,
                effect_type="SET_DEATH_CAUSE",
                target_seat=target_seat,
                value="hunter_shot",
            ),
        )
    return ActionResolution(
        resolution_id="trigger-ruling",
        bundle_id="trigger-bundle",
        game_id=flow.state.game_id,
        window_id=raw["window_id"],
        request_id=raw["request_id"],
        session_epoch=raw["session_epoch"],
        base_revision=flow.state.state_revision,
        status=ResolutionStatus.CONFIRMED,
        actions=(
            ActionResolutionEntry(
                action_index=0,
                requested_action=requested,
                resource_cost=0,
                effects=effects,
            ),
        ),
        moderator_id="moderator-test",
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_trigger_flow_runs_day_exile_choice_and_requires_explicit_ruling() -> None:
    manager = GameManager(hunter_state(), registry=REGISTRY)
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 105, "targets": [2]}],
        )
    )
    flow = ModeratorTriggerFlow(manager, _board(), {1: runtime}, clock=lambda: NOW)

    opened = await flow.open(now=NOW)
    assert opened.operation == "DAY_EXILE"
    assert opened.action_window.allowed_seats == (1,)
    assert (await flow.open(now=NOW)).action_window == opened.action_window

    result = await flow.next()
    assert result["attempt_no"] == 1
    pending = flow.pending()
    assert pending["private"] is True
    assert pending["sensitive"] is True
    assert len(pending["pending_requests"]) == 1  # type: ignore[arg-type]

    committed = await flow.resolve(_resolution(flow), now=NOW)
    assert committed.pending_resolution is None
    assert committed.action_windows[opened.action_window.window_id]["closed_at"] is not None
    assert flow.completion_origin() == "DAY_EXILE"
    assert flow.finish_target() is GamePhase.VICTORY_CHECK

    finished = await flow.finish(now=NOW)
    assert finished.phase is GamePhase.VICTORY_CHECK


@pytest.mark.asyncio
async def test_trigger_flow_uses_night_provenance_and_explicit_pass() -> None:
    state = hunter_state().model_copy(
        update={
            "pending_resolution": {
                **hunter_state().pending_resolution,  # type: ignore[misc]
                "operation": "NIGHT_RESOLUTION",
            },
            "moderator_audit": (
                {
                    "operation": "ACTION_RESOLUTION",
                    "resolution_id": "exile-1",
                },
            ),
        }
    )
    manager = GameManager(state, registry=REGISTRY)
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 299}],
        )
    )
    flow = ModeratorTriggerFlow(manager, _board(), {1: runtime}, clock=lambda: NOW)

    await flow.open(now=NOW)
    await flow.next()
    resolved = await flow.resolve(_resolution(flow, target_seat=None), now=NOW)
    assert resolved.pending_resolution is None
    assert flow.completion_origin() == "NIGHT_RESOLUTION"
    assert flow.finish_target() is GamePhase.DAY_ANNOUNCE
    finished = await flow.finish(now=NOW)
    assert finished.phase is GamePhase.DAY_ANNOUNCE


@pytest.mark.asyncio
async def test_trigger_completion_origin_rejects_pending_open_and_unknown_boundaries() -> None:
    manager = GameManager(hunter_state(), registry=REGISTRY)
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 299}],
        )
    )
    flow = ModeratorTriggerFlow(manager, _board(), {1: runtime}, clock=lambda: NOW)

    with pytest.raises(ModeratorTriggerError, match="TRIGGER_ACTION_PENDING"):
        flow.completion_origin()
    opened = await flow.open(now=NOW)
    with pytest.raises(ModeratorTriggerError, match="TRIGGER_ACTION_PENDING"):
        flow.finish_target()

    # A closed window without the pending marker is still not a valid finish
    # boundary while its trigger request remains open.
    manager._state = manager.state.model_copy(update={"pending_resolution": None})
    with pytest.raises(ModeratorTriggerError, match="trigger request must be resolved"):
        flow.completion_origin()

    raw_window = dict(manager.state.action_windows[opened.action_window.window_id])
    visible_context = dict(raw_window["visible_context"])  # type: ignore[arg-type]
    visible_context.pop("operation", None)
    visible_context["resolution_id"] = "unknown-resolution"
    raw_window["visible_context"] = visible_context
    raw_window["closed_at"] = NOW.isoformat()
    manager._state = manager.state.model_copy(
        update={
            "action_windows": {opened.action_window.window_id: raw_window},
        }
    )
    with pytest.raises(ModeratorTriggerError, match="trigger provenance is unknown"):
        flow.completion_origin()


@pytest.mark.asyncio
async def test_trigger_flow_retry_keeps_actor_request_when_runtime_proposal_is_rejected() -> None:
    manager = GameManager(hunter_state(), registry=REGISTRY)
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 105, "targets": [1]}],
        ),
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 105, "targets": [2]}],
        ),
    )
    flow = ModeratorTriggerFlow(manager, _board(), {1: runtime}, clock=lambda: NOW)
    await flow.open(now=NOW)

    with pytest.raises(ModeratorTriggerError, match="TARGET_NOT_ALLOWED"):
        await flow.next()
    retried = await flow.retry()
    assert retried["attempt_no"] == 2
