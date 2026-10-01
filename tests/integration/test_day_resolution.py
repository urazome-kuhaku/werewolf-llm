import pytest
from test_day_flow import NOW, _board, _runtimes, _state, _vote_request

from werewolf.domain.enums import Channel, GamePhase
from werewolf.game import (
    Action,
    ActionRequest,
    ActionResolution,
    ActionResolutionEntry,
    ActionValidationContext,
    GameManager,
    GameState,
    GrantedTriggerAbility,
    PlayerState,
    ResolutionError,
    ResolutionStatus,
    TieAction,
    load_action_registry,
)
from werewolf.game.day import DayCoordinator, DayCoordinatorError
from werewolf.game.day_resolution import DayExileDecision, build_trigger_action_window
from werewolf.game.events import EventType, GmAuditPayload
from werewolf.game.voting import TieDecision
from werewolf.knowledge.role import (
    TargetKind,
    TargetRule,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
)


def _role_board(role_id: str, effective_rules: dict[str, object]):
    raw = _board(pk_enabled=False).model_dump(mode="json")
    raw["role_bindings"] = [
        {
            "role_ref": {"id": "wolf", "version": "1.0.0"},
            "count": 2,
            "effective_rules": {},
            "override_claim_refs": [],
        },
        {
            "role_ref": {"id": role_id, "version": "1.0.0"},
            "count": 1,
            "effective_rules": effective_rules,
            "override_claim_refs": ["claim-role-rule"],
        },
        {
            "role_ref": {"id": "villager", "version": "1.0.0"},
            "count": 1,
            "effective_rules": {},
            "override_claim_refs": [],
        },
    ]
    from werewolf.knowledge.board import BoardDefinition

    return BoardDefinition.model_validate(raw)


def _role_state(role_id: str) -> GameState:
    state = _state()
    players = dict(state.players)
    trigger_abilities = ()
    if role_id == "idiot":
        trigger_abilities = (
            GrantedTriggerAbility(
                ability_id="survive-exile",
                action_code=0,
                trigger=TriggerRule(
                    event=TriggerEvent.EXILE_SELECTED,
                    mode=TriggerMode.AUTOMATIC,
                    effects=[
                        TriggerEffect.REVEAL_ROLE,
                        TriggerEffect.SURVIVE_TRIGGER,
                        TriggerEffect.REMOVE_VOTE_RIGHT,
                    ],
                ),
                target_rule=TargetRule(kind=TargetKind.NONE),
            ),
        )
    elif role_id == "hunter":
        trigger_abilities = (
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
        )
    players[3] = PlayerState(
        seat=3,
        role_id=role_id,
        faction_id="good",
        granted_trigger_abilities=trigger_abilities,
    )
    players[4] = PlayerState(seat=4, role_id="villager", faction_id="good")
    return state.model_copy(update={"players": players})


async def _resolved_vote(
    state: GameState,
    board: object,
    *,
    tie_resolver: object | None = None,
) -> tuple[GameManager, DayCoordinator]:
    manager = GameManager(
        state.model_copy(update={"phase": GamePhase.VOTE}), registry=load_action_registry()
    )
    coordinator = DayCoordinator(manager, board, await _runtimes(), tie_resolver=tie_resolver)
    progress = await coordinator.open_vote(now=NOW)
    for seat in progress.window.eligible_voters:
        await coordinator.submit_vote(_vote_request(progress, seat, 3, "resolution"), now=NOW)
    await coordinator.finalize_vote(now=NOW)
    await coordinator.confirm_vote(now=NOW)
    return manager, coordinator


@pytest.mark.asyncio
async def test_vote_confirmation_is_atomic_and_pk_candidates_survive_restart() -> None:
    manager = GameManager(
        _state().model_copy(update={"phase": GamePhase.VOTE}), registry=load_action_registry()
    )

    def resolve_tie(window: object, tally: object) -> TieDecision:
        del window
        return TieDecision(action=TieAction.PK, candidates=tuple(tally.top_candidates))

    board = _board(pk_enabled=True)
    coordinator = DayCoordinator(manager, board, tie_resolver=resolve_tie)
    progress = await coordinator.open_vote(now=NOW)
    for seat, target in zip(progress.window.eligible_voters, (3, 4, 3, 4), strict=True):
        await coordinator.submit_vote(_vote_request(progress, seat, target, "restart"), now=NOW)
    await coordinator.finalize_vote(now=NOW)

    # Simulate the old process stopping after the result event/state commit.
    partial = await manager.confirm_vote_tally(now=NOW)
    assert partial.phase is GamePhase.VOTE
    assert sum(event.event_type is EventType.VOTE_RESULT for event in partial.events) == 1

    resumed_manager = GameManager(partial, registry=load_action_registry())
    resumed = DayCoordinator(resumed_manager, board, tie_resolver=resolve_tie)
    completed = await resumed.confirm_vote(now=NOW)
    assert completed.phase is GamePhase.VOTE_PK_SPEECH
    assert sum(event.event_type is EventType.VOTE_RESULT for event in completed.events) == 1
    assert resumed._pk_candidates == (3, 4)


@pytest.mark.asyncio
async def test_confirm_exile_commits_public_audit_and_death_together() -> None:
    board = _board(pk_enabled=False)
    manager, coordinator = await _resolved_vote(_state(), board)

    state = await coordinator.confirm_exile(3, now=NOW)
    assert state.phase is GamePhase.DAY_RESOLVE
    assert state.players[3].alive is False
    assert state.players[3].death_cause == "exiled"
    assert state.players[3].can_vote is False
    assert sum(event.event_type is EventType.ANNOUNCEMENT for event in state.events) == 1
    audits = [event for event in state.events if event.channel is Channel.GM_ONLY]
    assert len(audits) == 1
    assert isinstance(audits[0].payload, GmAuditPayload)
    assert audits[0].payload.code == "day_exile_committed"
    assert any(item.get("operation") == "DAY_EXILE" for item in state.moderator_audit)

    replay = await coordinator.confirm_exile(3, now=NOW)
    assert replay.state_revision == state.state_revision
    assert len(replay.events) == len(state.events)


@pytest.mark.asyncio
async def test_commit_exile_rejects_forged_trigger_and_player_state_fields() -> None:
    board = _board(pk_enabled=False)
    manager, coordinator = await _resolved_vote(_state(), board)
    vote_window_id = manager.state.vote_state["window"]["window_id"]

    forged = DayExileDecision(
        window_id=vote_window_id,
        resolution_id=f"day-exile-{vote_window_id}",
        target_seat=3,
        outcome_code="trigger_player_choice",
        alive_after=True,
        death_cause=None,
        can_vote_after=True,
        next_phase=GamePhase.TRIGGER_ACTION,
        public_message="伪造的触发窗口",
        reveal_role=True,
        trigger_action=True,
        audit_details={},
        trigger_ability_id="forged-ability",
        trigger_action_code=105,
        trigger_event=TriggerEvent.DEATH_CONFIRMED.value,
    )

    with pytest.raises(ResolutionError, match="EXILE_DECISION_MISMATCH"):
        await manager.commit_day_exile(forged, now=NOW)

    assert manager.state.phase is GamePhase.DAY_RESOLVE
    assert manager.state.players[3].alive is True
    assert manager.state.players[3].death_cause is None
    assert manager.state.pending_resolution is None
    del coordinator


@pytest.mark.asyncio
async def test_idiot_exile_survives_and_loses_vote_right() -> None:
    board = _role_board(
        "idiot",
        {
            "reveal_on_exile": True,
            "survives_exile": True,
            "can_vote_after_reveal": False,
        },
    )
    manager, coordinator = await _resolved_vote(_role_state("idiot"), board)

    state = await coordinator.confirm_exile(3, now=NOW)
    assert state.players[3].alive is True
    assert state.players[3].death_cause is None
    assert state.players[3].can_vote is False
    assert state.players[3].granted_trigger_abilities[0].consumed is True
    assert "触发角色能力" in state.events[-2].payload.content


@pytest.mark.asyncio
async def test_hunter_exile_enters_explicit_trigger_phase_without_normal_skill_window() -> None:
    board = _role_board("hunter", {"shoot_causes": ["wolf_kill", "exiled"]})
    manager, coordinator = await _resolved_vote(_role_state("hunter"), board)

    state = await coordinator.confirm_exile(3, now=NOW)
    assert state.phase is GamePhase.TRIGGER_ACTION
    assert state.players[3].alive is False
    assert state.players[3].death_cause == "exiled"
    assert state.pending_resolution == {
        "operation": "DAY_EXILE",
        "status": "TRIGGER_ACTION_REQUIRED",
        "resolution_id": "day-exile-day-vote-r0-d1",
        "seat": 3,
        "trigger_event": "DEATH_CONFIRMED",
        "ability_id": "death-shot",
        "action_code": 105,
        "death_cause": "exiled",
        "snapshot_revision": 8,
    }
    with pytest.raises(DayCoordinatorError, match="TRIGGER_ACTION_PENDING"):
        await coordinator.finish_resolution(now=NOW)


@pytest.mark.asyncio
async def test_hunter_trigger_must_be_resolved_before_day_completion() -> None:
    board = _role_board("hunter", {"shoot_causes": ["wolf_kill", "exiled"]})
    manager, coordinator = await _resolved_vote(_role_state("hunter"), board)

    pending = await coordinator.confirm_exile(3, now=NOW)
    window = build_trigger_action_window(board, pending)
    await manager.commit_action_window(window, now=NOW)
    await manager.begin_action_turn(
        3,
        pending.players[3].session_epoch,
        window_id=window.window_id,
        request_id="hunter-pass-request",
        now=NOW,
    )
    request = ActionRequest(
        request_id="hunter-pass-request",
        game_id=pending.game_id,
        window_id=window.window_id,
        seat=3,
        session_epoch=pending.players[3].session_epoch,
        phase=GamePhase.TRIGGER_ACTION,
        actions=(Action(action_code=299),),
    )
    submitted = await manager.commit_action_request(
        request,
        ActionValidationContext(
            game_id=pending.game_id,
            session_epoch=pending.players[3].session_epoch,
            active_request_id=request.request_id,
            player_alive=False,
            role_id="hunter",
            authorized_action_codes=(299,),
        ),
        now=NOW,
    )
    # Submitting the actor's intent does not settle the trigger.  The
    # moderator still has to commit the PASS/action resolution first.
    with pytest.raises(DayCoordinatorError, match="TRIGGER_ACTION_PENDING"):
        await coordinator.finish_resolution(now=NOW)
    resolved = await manager.commit_action_resolution(
        ActionResolution(
            resolution_id="hunter-pass-resolution",
            bundle_id="hunter-pass-bundle",
            game_id=request.game_id,
            window_id=request.window_id,
            request_id=request.request_id,
            session_epoch=request.session_epoch,
            base_revision=submitted.state_revision,
            status=ResolutionStatus.CONFIRMED,
            actions=(
                ActionResolutionEntry(
                    action_index=0,
                    requested_action=request.actions[0],
                ),
            ),
            moderator_id="gm-test",
            created_at=NOW,
        ),
        now=NOW,
    )
    assert resolved.pending_resolution is None
    assert resolved.action_windows[window.window_id]["closed_at"] is not None
    assert (await coordinator.finish_resolution(now=NOW)).phase is GamePhase.VICTORY_CHECK
