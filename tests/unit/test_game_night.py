"""Tests for board driven night window coordination and atomic resolve."""

import json
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionDisposition,
    ActionRequest,
    ActionResolution,
    ActionResolutionEntry,
    ActionValidationContext,
    ActionValidationError,
    ActionWindow,
    GameManager,
    GameState,
    GrantedAbility,
    GrantedTriggerAbility,
    NightCoordinator,
    NightCoordinatorError,
    NightWindowConfig,
    PlayerState,
    ResolutionEffect,
    ResolutionError,
    ResolutionStatus,
    RulesetRef,
    load_action_registry,
)
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.role import (
    TargetKind,
    TargetRule,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
    UsageLimit,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)
REGISTRY = load_action_registry()


def _board() -> BoardDefinition:
    return BoardDefinition.model_validate(
        {
            "schema_version": 1,
            "kind": "board",
            "id": "fictional-board",
            "version": "1.0.0",
            "name": "夜间协调测试板",
            "aliases": [],
            "locale": "zh-CN",
            "status": "published",
            "reviewed_by": "GM",
            "reviewed_at": "2026-09-27",
            "summary": "用于验证夜间窗口的测试板。",
            "seat_count": 4,
            "factions": {"town": 2, "wolf": 2},
            "roles": [
                {
                    "role_ref": {"id": "wolf", "version": "1.0.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
                {
                    "role_ref": {"id": "villager", "version": "1.0.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
            ],
            "victory": {
                "mode": "eliminate_side",
                "winning_sides": ["town", "wolf"],
                "check_phases": ["VICTORY_CHECK"],
                "draw_policy": "no_winner",
            },
            "wolf_team_visibility": {
                "members_know_each_other": True,
                "discussion_enabled": True,
                "identity_visibility": "members",
            },
            "knife_rule": {"selection_mode": "consensus", "target_visibility": "wolf_team"},
            "night_windows": [
                {
                    "window_id": "wolf_team_chat",
                    "visible_to": ["wolf"],
                },
                {"window_id": "wolf_kill", "depends_on": ["wolf_team_chat"]},
                {
                    "window_id": "night_resolve",
                    "phase": "NIGHT_RESOLVE",
                    "depends_on": ["wolf_kill"],
                },
            ],
            "day_flow": {
                "announce_deaths": True,
                "vote": {
                    "visibility_during_collection": "secret",
                    "reveal_after_close": "ballots_and_totals",
                    "tie_policy": "pk_then_no_exile_on_retie",
                },
                "pk": {"enabled": True, "candidate_count": 2},
                "last_words": {
                    "enabled": True,
                    "eligible_death_causes": ["night_kill", "day_exile"],
                },
            },
            "mechanics": ["night-resolution@1.0.0"],
            "interactions": ["night-edge@1.0.0"],
            "reading_plan": {
                "board_ref": {"id": "fictional-board", "version": "1.0.0"},
                "bootstrap_topics": ["board:overview"],
                "role_required_topics": {"wolf": ["role:wolf"]},
                "phase_topics": {"NIGHT_ACTION": ["mechanic:night-resolution"]},
                "high_risk_topics": ["mechanic:night-resolution"],
                "suggested_queries": [],
            },
            "claim_refs": ["claim-board"],
            "source_refs": ["source-board"],
        }
    )


def _state(
    *, phase: GamePhase = GamePhase.NIGHT_TEAM_CHAT, max_uses: int | None = None
) -> GameState:
    wolf_ability = GrantedAbility(
        ability_id="kill",
        action_code=101,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=(GamePhase.NIGHT_ACTION,),
        target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
        usage_limit=UsageLimit(max_uses=max_uses) if max_uses is not None else None,
    )
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        ruleset=RulesetRef(
            board_id="fictional-board",
            version="1.0.0",
            snapshot_id="ruleset-test",
            manifest_sha256="a" * 64,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id="wolf",
                faction_id="wolf",
                granted_abilities=(wolf_ability,),
                session_epoch=1,
            ),
            2: PlayerState(
                seat=2,
                role_id="wolf",
                faction_id="wolf",
                granted_abilities=(wolf_ability,),
                session_epoch=1,
            ),
            3: PlayerState(
                seat=3,
                role_id="villager",
                faction_id="town",
                session_epoch=1,
            ),
            4: PlayerState(
                seat=4,
                role_id="villager",
                faction_id="town",
                session_epoch=1,
            ),
        },
    )


def _coordinator(
    manager: GameManager,
    *,
    allowed_seats: tuple[int, ...] = (1,),
    allowed_role_ids: tuple[str, ...] = ("wolf",),
    allowed_action_codes: tuple[int, ...] = (101,),
) -> NightCoordinator:
    return NightCoordinator(
        manager,
        _board(),
        {
            "wolf_kill": NightWindowConfig(
                allowed_seats=allowed_seats,
                allowed_role_ids=allowed_role_ids,
                allowed_action_codes=allowed_action_codes,
            )
        },
        snapshot_id="ruleset-test",
    )


async def _submit_kill(
    manager: GameManager,
    *,
    seat: int,
    request_id: str,
    target: int,
    window_id: str = "wolf_kill",
) -> None:
    await manager.begin_action_turn(
        seat,
        1,
        window_id=window_id,
        request_id=request_id,
    )
    await manager.commit_action_request(
        ActionRequest(
            request_id=request_id,
            game_id="game-1",
            window_id=window_id,
            seat=seat,
            session_epoch=1,
            phase=GamePhase.NIGHT_ACTION,
            actions=(Action(action_code=101, targets=(target,)),),
        ),
        ActionValidationContext(
            game_id="game-1",
            session_epoch=1,
            active_request_id=request_id,
            authorized_action_codes=(101,),
            alive_seats=(1, 2, 3, 4),
            eligible_targets_by_action={101: (3, 4)},
        ),
    )


def _resolution(
    state: GameState,
    request_id: str,
    target: int,
    *,
    window_id: str = "wolf_kill",
) -> ActionResolution:
    request = state.action_requests[request_id]
    requested = Action.model_validate(request["actions"][0])
    return ActionResolution(
        resolution_id=f"resolution-{request_id}",
        bundle_id=f"bundle-{request_id}",
        game_id="game-1",
        window_id=window_id,
        request_id=request_id,
        session_epoch=1,
        base_revision=state.state_revision,
        status=ResolutionStatus.CONFIRMED,
        actions=(
            ActionResolutionEntry(
                action_index=0,
                requested_action=requested,
                effects=(
                    ResolutionEffect(
                        effect_id=f"death-{request_id}",
                        action_index=0,
                        effect_type="SET_ALIVE",
                        target_seat=target,
                        value=False,
                    ),
                    ResolutionEffect(
                        effect_id=f"cause-{request_id}",
                        action_index=0,
                        effect_type="SET_DEATH_CAUSE",
                        target_seat=target,
                        value="wolf_kill",
                    ),
                ),
            ),
        ),
        moderator_id="gm",
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_wolf_kill_rejects_same_faction_target_even_when_context_forges_it() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    coordinator = _coordinator(manager)
    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    action = await coordinator.open_next_window(now=NOW)
    await manager.begin_action_turn(
        1,
        1,
        window_id=action.action_window.window_id,
        request_id="forged-team-kill",
    )

    with pytest.raises(ActionValidationError, match="TARGET_NOT_ALLOWED"):
        await manager.commit_action_request(
            ActionRequest(
                request_id="forged-team-kill",
                game_id="game-1",
                window_id=action.action_window.window_id,
                seat=1,
                session_epoch=1,
                phase=GamePhase.NIGHT_ACTION,
                actions=(Action(action_code=101, targets=(2,)),),
            ),
            ActionValidationContext(
                game_id="game-1",
                session_epoch=1,
                active_request_id="forged-team-kill",
                authorized_action_codes=(101,),
                alive_seats=(1, 2, 3, 4),
                eligible_targets_by_action={101: (2,)},
            ),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_night_windows_follow_board_order_and_effects_wait_for_resolve() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    coordinator = _coordinator(manager)

    team = await coordinator.open_next_window(now=NOW)
    assert team.phase is GamePhase.NIGHT_TEAM_CHAT
    assert team.action_window.allowed_seats == (1, 2)

    await coordinator.advance_from_current_window(expected_window_id="wolf_team_chat", now=NOW)
    action = await coordinator.open_next_window(now=NOW)
    assert action.phase is GamePhase.NIGHT_ACTION

    await _submit_kill(manager, seat=1, request_id="kill-1", target=3)
    assert (await manager.snapshot()).players[3].alive is True
    await coordinator.advance_from_current_window(expected_window_id="wolf_kill", now=NOW)
    assert (await manager.snapshot()).phase is GamePhase.NIGHT_RESOLVE

    await coordinator.open_next_window(now=NOW)
    before = await manager.snapshot()
    resolution = _resolution(before, "kill-1", 3)
    after = await coordinator.confirm_night((resolution,), now=NOW)
    assert after.phase is GamePhase.DAY_ANNOUNCE
    assert after.players[3].alive is False
    assert after.players[3].death_cause == "wolf_kill"


@pytest.mark.asyncio
async def test_night_death_trigger_opens_and_must_finish_before_day_announce() -> None:
    base = _state()
    players = dict(base.players)
    players[3] = players[3].model_copy(
        update={
            "role_id": "hunter",
            "granted_trigger_abilities": (
                GrantedTriggerAbility(
                    ability_id="death-shot",
                    action_code=105,
                    trigger=TriggerRule(
                        event=TriggerEvent.DEATH_CONFIRMED,
                        allowed_death_causes=["wolf_kill"],
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
        }
    )
    manager = GameManager(base.model_copy(update={"players": players}), registry=REGISTRY)
    coordinator = _coordinator(manager)

    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)
    await _submit_kill(manager, seat=1, request_id="kill-trigger", target=3)
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)
    before = await manager.snapshot()
    committed = await coordinator.confirm_night((_resolution(before, "kill-trigger", 3),), now=NOW)

    assert committed.phase is GamePhase.TRIGGER_ACTION
    assert committed.day_no == 0
    assert committed.pending_resolution is not None
    trigger_window_id = committed.pending_resolution["window_id"]
    trigger_window = ActionWindow.model_validate_json(
        json.dumps(committed.action_windows[trigger_window_id])
    )
    assert trigger_window.allowed_seats == (3,)
    assert trigger_window.allowed_action_codes == (105, 299)

    with pytest.raises(NightCoordinatorError, match="TRIGGER_ACTION_PENDING"):
        await coordinator.finish_trigger_action(now=NOW)

    await manager.begin_action_turn(
        3,
        1,
        window_id=trigger_window_id,
        request_id="hunter-shot",
        now=NOW,
    )
    request = ActionRequest(
        request_id="hunter-shot",
        game_id="game-1",
        window_id=trigger_window_id,
        seat=3,
        session_epoch=1,
        phase=GamePhase.TRIGGER_ACTION,
        actions=(Action(action_code=105, targets=(4,)),),
    )
    await manager.commit_action_request(
        request,
        ActionValidationContext(
            game_id="game-1",
            session_epoch=1,
            active_request_id="hunter-shot",
            authorized_action_codes=(105,),
            alive_seats=(1, 2, 4),
            eligible_targets_by_action={105: (4,)},
            player_alive=False,
            player_qualified=True,
            role_id="hunter",
        ),
        now=NOW,
    )
    after_request = await manager.snapshot()
    trigger_resolution = ActionResolution(
        resolution_id="resolution-hunter-shot",
        bundle_id="bundle-hunter-shot",
        game_id="game-1",
        window_id=trigger_window_id,
        request_id="hunter-shot",
        session_epoch=1,
        base_revision=after_request.state_revision,
        status=ResolutionStatus.CONFIRMED,
        actions=(
            ActionResolutionEntry(
                action_index=0,
                requested_action=request.actions[0],
                effects=(
                    ResolutionEffect(
                        effect_id="shot-death",
                        action_index=0,
                        effect_type="SET_ALIVE",
                        target_seat=4,
                        value=False,
                    ),
                    ResolutionEffect(
                        effect_id="shot-cause",
                        action_index=0,
                        effect_type="SET_DEATH_CAUSE",
                        target_seat=4,
                        value="hunter_shot",
                    ),
                ),
            ),
        ),
        moderator_id="gm",
        created_at=NOW,
    )
    resolved = await manager.commit_action_resolution(trigger_resolution, now=NOW)
    assert resolved.pending_resolution is None
    assert resolved.phase is GamePhase.TRIGGER_ACTION
    assert resolved.players[3].granted_trigger_abilities[0].consumed is True

    finished = await coordinator.finish_trigger_action(now=NOW)
    assert finished.phase is GamePhase.DAY_ANNOUNCE
    assert finished.day_no == 1


@pytest.mark.asyncio
async def test_active_ability_use_is_exhausted_before_the_next_night() -> None:
    manager = GameManager(_state(max_uses=1), registry=REGISTRY)
    coordinator = _coordinator(manager)

    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    action = await coordinator.open_next_window(now=NOW)
    await _submit_kill(
        manager,
        seat=1,
        request_id="limited-kill-r0",
        target=3,
        window_id=action.action_window.window_id,
    )
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)
    before = await manager.snapshot()
    after = await coordinator.confirm_night(
        (_resolution(before, "limited-kill-r0", 3, window_id=action.action_window.window_id),),
        now=NOW,
    )
    assert after.players[1].granted_abilities[0].uses_consumed == 1

    for target_phase in (
        GamePhase.DAY_SPEECH,
        GamePhase.VOTE,
        GamePhase.DAY_RESOLVE,
        GamePhase.VICTORY_CHECK,
        GamePhase.NIGHT_TEAM_CHAT,
    ):
        await manager.commit_phase_transition(target_phase, now=NOW)

    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    with pytest.raises(NightCoordinatorError, match="ACTION_NOT_ALLOWED"):
        await coordinator.open_next_window(now=NOW)


@pytest.mark.asyncio
async def test_moderator_cannot_override_wolf_kill_to_a_teammate() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    coordinator = _coordinator(manager)
    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    action = await coordinator.open_next_window(now=NOW)
    await _submit_kill(
        manager,
        seat=1,
        request_id="override-team-kill",
        target=3,
        window_id=action.action_window.window_id,
    )
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)
    before = await manager.snapshot()
    valid = _resolution(before, "override-team-kill", 3, window_id=action.action_window.window_id)
    entry = valid.actions[0].model_copy(
        update={
            "disposition": ActionDisposition.OVERRIDDEN,
            "resolved_action": Action(action_code=101, targets=(2,)),
            "effects": tuple(
                effect.model_copy(update={"target_seat": 2}) for effect in valid.actions[0].effects
            ),
        }
    )
    forged = valid.model_copy(update={"status": "OVERRIDDEN", "actions": (entry,)})

    with pytest.raises(ResolutionError, match="TARGET_NOT_ALLOWED"):
        await manager.commit_action_resolution(forged, now=NOW)

    assert (await manager.snapshot()).players[2].alive is True


@pytest.mark.asyncio
async def test_second_night_uses_round_qualified_windows_and_ignores_old_requests() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    coordinator = _coordinator(manager)

    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    first_action = await coordinator.open_next_window(now=NOW)
    await _submit_kill(
        manager,
        seat=1,
        request_id="kill-r0",
        target=3,
        window_id=first_action.action_window.window_id,
    )
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)
    before_first_resolution = await manager.snapshot()
    await coordinator.confirm_night(
        (
            _resolution(
                before_first_resolution,
                "kill-r0",
                3,
                window_id=first_action.action_window.window_id,
            ),
        ),
        now=NOW,
    )

    for target_phase in (
        GamePhase.DAY_SPEECH,
        GamePhase.VOTE,
        GamePhase.DAY_RESOLVE,
        GamePhase.VICTORY_CHECK,
        GamePhase.NIGHT_TEAM_CHAT,
    ):
        await manager.commit_phase_transition(target_phase, now=NOW)

    state = await manager.snapshot()
    assert state.round_no == 1
    assert state.day_no == 1
    second_team = await coordinator.open_next_window(now=NOW)
    assert second_team.action_window.window_id == "wolf_team_chat-r1"
    assert "wolf_team_chat" in state.action_windows
    await coordinator.advance_from_current_window(now=NOW)
    second_action = await coordinator.open_next_window(now=NOW)
    assert second_action.action_window.window_id == "wolf_kill-r1"

    # The r0 request remains in the authoritative history, but cannot satisfy
    # the r1 action window because request ownership is physical and round
    # qualified.
    with pytest.raises(NightCoordinatorError, match="SUBMISSIONS_INCOMPLETE"):
        await coordinator.advance_from_current_window(now=NOW)

    await _submit_kill(
        manager,
        seat=1,
        request_id="kill-r1",
        target=4,
        window_id=second_action.action_window.window_id,
    )
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)
    before_second_resolution = await manager.snapshot()
    after = await coordinator.confirm_night(
        (
            _resolution(
                before_second_resolution,
                "kill-r1",
                4,
                window_id=second_action.action_window.window_id,
            ),
        ),
        now=NOW,
    )
    assert after.phase is GamePhase.DAY_ANNOUNCE
    assert after.players[4].alive is False


@pytest.mark.asyncio
async def test_confirm_night_recovers_after_effects_commit_before_lifecycle_step() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    coordinator = _coordinator(manager)
    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    action = await coordinator.open_next_window(now=NOW)
    await _submit_kill(
        manager,
        seat=1,
        request_id="kill-partial",
        target=3,
        window_id=action.action_window.window_id,
    )
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)

    partial = await manager.snapshot()
    resolution = _resolution(
        partial,
        "kill-partial",
        3,
        window_id=action.action_window.window_id,
    )
    await manager.commit_action_resolutions((resolution,), now=NOW)
    partial = await manager.snapshot()
    assert partial.phase is GamePhase.NIGHT_RESOLVE
    assert partial.action_windows[action.action_window.window_id]["closed_at"] is not None

    recovered = await coordinator.confirm_night((), now=NOW)
    assert recovered.phase is GamePhase.DAY_ANNOUNCE
    assert recovered.day_no == 1
    resolve_id = next(
        window_id
        for window_id, payload in recovered.action_windows.items()
        if isinstance(payload, dict)
        and payload.get("visible_context", {}).get("night_window_id") == "night_resolve"
    )
    assert recovered.action_windows[resolve_id]["closed_at"] is not None


@pytest.mark.asyncio
async def test_night_confirmation_is_all_or_nothing_across_requests() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    coordinator = _coordinator(manager, allowed_seats=(1, 2))
    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)
    await _submit_kill(manager, seat=1, request_id="kill-1", target=3)
    await _submit_kill(manager, seat=2, request_id="kill-2", target=4)
    await coordinator.advance_from_current_window(now=NOW)
    await coordinator.open_next_window(now=NOW)
    before = await manager.snapshot()
    invalid = _resolution(before, "kill-2", 3)

    with pytest.raises(NightCoordinatorError, match="RESOLUTION_REJECTED"):
        await coordinator.confirm_night((_resolution(before, "kill-1", 3), invalid), now=NOW)

    after = await manager.snapshot()
    assert after.state_revision == before.state_revision
    assert after.players[3].alive is True
    assert after.players[4].alive is True
    assert after.action_requests["kill-1"]["status"] == "PENDING"
    assert after.action_requests["kill-2"]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_night_window_rejects_role_unauthorized_seat() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    coordinator = _coordinator(manager, allowed_seats=(3,))
    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)

    with pytest.raises(NightCoordinatorError, match="ROLE_NOT_ALLOWED"):
        await coordinator.open_next_window(now=NOW)


@pytest.mark.asyncio
async def test_night_window_rejects_action_without_a_matching_grant() -> None:
    manager = GameManager(_state(), registry=REGISTRY)
    coordinator = _coordinator(manager, allowed_action_codes=(102,))

    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)

    with pytest.raises(NightCoordinatorError, match="ACTION_NOT_ALLOWED"):
        await coordinator.open_next_window(now=NOW)


@pytest.mark.asyncio
async def test_night_action_union_cannot_cross_authorize_a_seat() -> None:
    seer_ability = GrantedAbility(
        ability_id="inspect",
        action_code=102,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=(GamePhase.NIGHT_ACTION,),
        target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
    )
    base = _state()
    players = dict(base.players)
    players[2] = players[2].model_copy(
        update={
            "role_id": "seer",
            "granted_abilities": (seer_ability,),
        }
    )
    manager = GameManager(base.model_copy(update={"players": players}), registry=REGISTRY)
    coordinator = _coordinator(
        manager,
        allowed_seats=(1, 2),
        allowed_role_ids=("wolf", "seer"),
        allowed_action_codes=(101, 102),
    )

    await coordinator.open_next_window(now=NOW)
    await coordinator.advance_from_current_window(now=NOW)
    action = await coordinator.open_next_window(now=NOW)
    await manager.begin_action_turn(
        1,
        1,
        window_id=action.action_window.window_id,
        request_id="wolf-forged-seer",
    )

    with pytest.raises(ActionValidationError, match="ACTION_UNAUTHORIZED"):
        await manager.commit_action_request(
            ActionRequest(
                request_id="wolf-forged-seer",
                game_id="game-1",
                window_id=action.action_window.window_id,
                seat=1,
                session_epoch=1,
                phase=GamePhase.NIGHT_ACTION,
                actions=(Action(action_code=102, targets=(3,)),),
            ),
            ActionValidationContext(
                game_id="game-1",
                session_epoch=1,
                active_request_id="wolf-forged-seer",
                authorized_action_codes=(102,),
                alive_seats=(1, 2, 3, 4),
                eligible_targets_by_action={102: (3,)},
            ),
            now=NOW,
        )
