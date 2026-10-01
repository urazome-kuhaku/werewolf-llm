from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionRequest,
    ActionResolution,
    ActionResolutionEntry,
    ActionValidationContext,
    ActionWindow,
    EventCommitError,
    GameManager,
    GameState,
    GrantedTriggerAbility,
    PlayerState,
    ResolutionEffect,
    ResolutionStatus,
    RulesetRef,
    load_action_registry,
)
from werewolf.knowledge.role import (
    TargetKind,
    TargetRule,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)
REGISTRY = load_action_registry()


def _state(*, death_cause: str = "exiled", role_id: str = "hunter") -> GameState:
    return GameState(
        game_id="hunter-game",
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
                role_id=role_id,
                faction_id="good",
                alive=False,
                death_cause=death_cause,
                session_epoch=2,
                granted_trigger_abilities=(
                    GrantedTriggerAbility(
                        ability_id="death-shot",
                        action_code=105,
                        trigger=TriggerRule(
                            event=TriggerEvent.DEATH_CONFIRMED,
                            allowed_death_causes=["wolf_kill", "exiled"],
                            mode=TriggerMode.PLAYER_CHOICE,
                            effects=[TriggerEffect.OPEN_PLAYER_ACTION],
                            allow_pass=True,
                        ),
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
                faction_id="good",
                session_epoch=2,
            ),
            3: PlayerState(
                seat=3,
                role_id="wolf",
                faction_id="wolf",
                session_epoch=2,
            ),
        },
        pending_resolution={
            "operation": "DAY_EXILE",
            "status": "TRIGGER_ACTION_REQUIRED",
            "resolution_id": "exile-1",
            "seat": 1,
            "trigger_event": "DEATH_CONFIRMED",
            "ability_id": "death-shot",
            "action_code": 105,
            "death_cause": death_cause,
            "snapshot_revision": None,
        },
    )


def _window(*, death_cause: str = "exiled") -> ActionWindow:
    return ActionWindow(
        window_id="exile-1-trigger-death-shot",
        game_id="hunter-game",
        session_epoch=2,
        phase=GamePhase.TRIGGER_ACTION,
        allowed_seats=(1,),
        allowed_role_ids=(),
        allowed_action_codes=(105, 299),
        min_actions=1,
        max_actions=1,
        allow_pass=True,
        opened_at=NOW,
        visible_context={
            "trigger_event": "DEATH_CONFIRMED",
            "ability_id": "death-shot",
            "action_code": 105,
            "resolution_id": "exile-1",
            "death_cause": death_cause,
            "snapshot_revision": None,
            "candidate_seats": [2, 3],
        },
    )


def _request() -> tuple[ActionRequest, ActionValidationContext]:
    request = ActionRequest(
        request_id="hunter-request",
        game_id="hunter-game",
        window_id="exile-1-trigger-death-shot",
        seat=1,
        session_epoch=2,
        phase=GamePhase.TRIGGER_ACTION,
        actions=(Action(action_code=105, targets=(2,)),),
    )
    context = ActionValidationContext(
        game_id="hunter-game",
        session_epoch=2,
        active_request_id="hunter-request",
        role_id="hunter",
        authorized_action_codes=(105,),
        alive_seats=(2, 3),
        eligible_targets_by_action={105: (2, 3)},
    )
    return request, context


def _pass_request() -> tuple[ActionRequest, ActionValidationContext]:
    request = ActionRequest(
        request_id="hunter-pass-request",
        game_id="hunter-game",
        window_id="exile-1-trigger-death-shot",
        seat=1,
        session_epoch=2,
        phase=GamePhase.TRIGGER_ACTION,
        actions=(Action(action_code=299),),
    )
    context = ActionValidationContext(
        game_id="hunter-game",
        session_epoch=2,
        active_request_id="hunter-pass-request",
        role_id="unrelated-role",
        authorized_action_codes=(299,),
    )
    return request, context


def _resolution(base_revision: int, request: ActionRequest) -> ActionResolution:
    action = request.actions[0]
    return ActionResolution(
        resolution_id="hunter-resolution-1",
        bundle_id="hunter-bundle-1",
        game_id=request.game_id,
        window_id=request.window_id,
        request_id=request.request_id,
        session_epoch=request.session_epoch,
        base_revision=base_revision,
        status=ResolutionStatus.CONFIRMED,
        actions=(
            ActionResolutionEntry(
                action_index=0,
                requested_action=action,
                resource_cost=0,
                effects=(
                    ResolutionEffect(
                        effect_id="hunter-kill",
                        action_index=0,
                        effect_type="SET_ALIVE",
                        target_seat=2,
                        value=False,
                    ),
                    ResolutionEffect(
                        effect_id="hunter-cause",
                        action_index=0,
                        effect_type="SET_DEATH_CAUSE",
                        target_seat=2,
                        value="hunter_shot",
                    ),
                ),
            ),
        ),
        moderator_id="gm-1",
        created_at=NOW,
    )


def _pass_resolution(base_revision: int, request: ActionRequest) -> ActionResolution:
    return ActionResolution(
        resolution_id="hunter-pass-resolution",
        bundle_id="hunter-pass-bundle",
        game_id=request.game_id,
        window_id=request.window_id,
        request_id=request.request_id,
        session_epoch=request.session_epoch,
        base_revision=base_revision,
        status=ResolutionStatus.CONFIRMED,
        actions=(
            ActionResolutionEntry(
                action_index=0,
                requested_action=request.actions[0],
                resource_cost=0,
                effects=(),
            ),
        ),
        moderator_id="gm-1",
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_bound_dead_hunter_action_request_and_resolution_are_atomic() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    window = _window()

    installed = await manager.commit_action_window(window, now=NOW)
    assert installed.pending_resolution is not None
    assert installed.pending_resolution["window_id"] == window.window_id
    assert installed.players[1].alive is False

    started = await manager.begin_action_turn(
        1,
        2,
        window_id=window.window_id,
        request_id="hunter-request",
        now=NOW,
    )
    assert started.players[1].current_request_id == "hunter-request"

    request, context = _request()
    submitted = await manager.commit_action_request(request, context, now=NOW)
    assert submitted.action_requests[request.request_id]["status"] == "PENDING"
    assert submitted.players[1].current_request_id == request.request_id

    resolution = _resolution(submitted.state_revision, request)
    resolved = await manager.commit_action_resolution(resolution, now=NOW)
    assert resolved.players[1].alive is False
    assert resolved.players[2].alive is False
    assert resolved.players[2].death_cause == "hunter_shot"
    assert resolved.players[1].granted_trigger_abilities[0].consumed is True
    assert resolved.pending_resolution is None
    assert resolved.action_windows[window.window_id]["closed_at"] is not None

    # A resolution retry is idempotent, while a fresh window cannot re-enter
    # the one-shot trigger after the pending marker has been consumed.
    replay = await manager.commit_action_resolution(resolution, now=NOW)
    assert replay is resolved
    with pytest.raises(EventCommitError, match="TRIGGER_ACTION_INVALID"):
        await manager.commit_action_window(window, now=NOW)


@pytest.mark.asyncio
async def test_poisoned_hunter_cannot_install_a_trigger_window() -> None:
    manager = GameManager(_state(death_cause="witch_poison"), registry=REGISTRY)

    with pytest.raises(EventCommitError, match="TRIGGER_ACTION_INVALID"):
        await manager.commit_action_window(_window(death_cause="witch_poison"), now=NOW)

    assert manager.state.state_revision == 0
    assert manager.state.action_windows == {}
    assert manager.state.pending_resolution is not None
    assert "window_id" not in manager.state.pending_resolution


@pytest.mark.asyncio
async def test_trigger_authorization_uses_granted_ability_not_role_id_and_allows_pass() -> None:
    manager = GameManager(_state(role_id="marksman"), registry=REGISTRY)
    window = _window()
    await manager.commit_action_window(window, now=NOW)
    await manager.begin_action_turn(
        1,
        2,
        window_id=window.window_id,
        request_id="hunter-pass-request",
        now=NOW,
    )
    request, context = _pass_request()
    submitted = await manager.commit_action_request(request, context, now=NOW)
    resolved = await manager.commit_action_resolution(
        _pass_resolution(submitted.state_revision, request), now=NOW
    )
    assert resolved.pending_resolution is None
    assert resolved.players[1].granted_trigger_abilities[0].consumed is True
