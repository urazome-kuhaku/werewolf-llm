import asyncio
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.game import (
    Action,
    ActionRequest,
    ActionValidationContext,
    ActionValidationError,
    ActionWindow,
    DeliveryAck,
    EventCommitError,
    EventType,
    GameEvent,
    GameManager,
    GameState,
    PlayerState,
    PrivateRolePayload,
    PublicAnnouncementPayload,
    RevisionConflict,
    RulesetRef,
    StatePatch,
    StatePatchError,
    load_action_registry,
    validate_action_request,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)
REGISTRY = load_action_registry()


def _state(phase: GamePhase = GamePhase.CREATED) -> GameState:
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="a" * 64,
        ),
    )


def _action_state() -> tuple[GameState, ActionRequest, ActionValidationContext]:
    window = ActionWindow(
        window_id="window-1",
        game_id="game-1",
        session_epoch=2,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1, 2),
        allowed_action_codes=(299,),
        min_actions=1,
        max_actions=1,
        allow_pass=True,
        opened_at=NOW,
    )
    state = _state(GamePhase.NIGHT_ACTION).model_copy(
        update={
            "players": {
                1: PlayerState(
                    seat=1,
                    role_id="villager",
                    faction_id="village",
                    session_epoch=2,
                    current_request_id="request-1",
                ),
                2: PlayerState(
                    seat=2,
                    role_id="villager",
                    faction_id="village",
                    session_epoch=2,
                    current_request_id="request-2",
                ),
            },
            "action_windows": {"window-1": window.model_dump(mode="json")},
        }
    )
    request = ActionRequest(
        request_id="request-1",
        game_id="game-1",
        window_id="window-1",
        seat=1,
        session_epoch=2,
        actions=(Action(action_code=299),),
        phase=GamePhase.NIGHT_ACTION,
    )
    context = ActionValidationContext(
        game_id="game-1",
        session_epoch=2,
        active_request_id="request-1",
        role_id="villager",
        authorized_action_codes=(299,),
        alive_seats=(1, 2),
    )
    return state, request, context


def _delivery_state() -> GameState:
    return _state().model_copy(
        update={
            "players": {
                1: PlayerState(
                    seat=1,
                    role_id="villager",
                    faction_id="village",
                    session_epoch=3,
                ),
                2: PlayerState(
                    seat=2,
                    role_id="seer",
                    faction_id="village",
                    session_epoch=3,
                ),
            }
        }
    )


def _public_event(event_id: int, state_revision: int) -> GameEvent:
    return GameEvent.public(
        event_id=event_id,
        game_id="game-1",
        state_revision=state_revision,
        round_no=1,
        phase=GamePhase.DAY_SPEECH,
        created_at=NOW,
        event_type=EventType.ANNOUNCEMENT,
        eligible_seats=(1, 2),
        payload=PublicAnnouncementPayload(content=f"公告 {event_id}"),
    )


@pytest.mark.asyncio
async def test_phase_commits_are_serialized_by_expected_revision() -> None:
    manager = GameManager(_state(), registry=REGISTRY)

    first, second = await asyncio.gather(
        manager.commit_phase_transition(GamePhase.RULESET_READY, expected_revision=0, now=NOW),
        manager.commit_phase_transition(GamePhase.RULESET_READY, expected_revision=0, now=NOW),
        return_exceptions=True,
    )

    assert first.state_revision == 1
    assert isinstance(second, RevisionConflict)
    assert manager.state.state_revision == 1


@pytest.mark.asyncio
async def test_moderator_phase_commit_is_atomic_and_rejects_stale_revision() -> None:
    manager = GameManager(_state(), registry=REGISTRY)

    first, second = await asyncio.gather(
        manager.commit_moderator_operation(
            operation="NEXT",
            command="next",
            expected_revision=0,
            target_phase=GamePhase.RULESET_READY,
            now=NOW,
        ),
        manager.commit_moderator_operation(
            operation="NEXT",
            command="next",
            expected_revision=0,
            target_phase=GamePhase.RULESET_READY,
            now=NOW,
        ),
        return_exceptions=True,
    )

    assert first.state_revision == 1
    assert first.moderator_audit[-1]["operation"] == "NEXT"
    assert isinstance(second, RevisionConflict)
    assert manager.state.state_revision == 1


@pytest.mark.asyncio
async def test_moderator_phase_commit_rejects_pending_trigger_resolution() -> None:
    state = _state(GamePhase.TRIGGER_ACTION).model_copy(
        update={
            "pending_resolution": {
                "status": "TRIGGER_ACTION_REQUIRED",
                "trigger": "hunter_shoot",
            }
        }
    )
    manager = GameManager(state, registry=REGISTRY)

    with pytest.raises(EventCommitError, match="unconfirmed resolution"):
        await manager.commit_moderator_operation(
            operation="NEXT",
            command="next",
            expected_revision=0,
            target_phase=GamePhase.VICTORY_CHECK,
            now=NOW,
        )

    assert manager.state is state


@pytest.mark.asyncio
async def test_moderator_closed_status_requires_finished_phase() -> None:
    manager = GameManager(_state(), registry=REGISTRY)

    with pytest.raises(EventCommitError, match="FINISHED phase"):
        await manager.commit_moderator_operation(
            operation="FINISH",
            command="finish",
            expected_revision=0,
            run_status=RunStatus.CLOSED,
            now=NOW,
        )

    assert manager.state.run_status is RunStatus.READY


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_status", [RunStatus.READY, RunStatus.RUNNING])
async def test_finish_rollback_reopens_only_finished_closed_game(
    rollback_status: RunStatus,
) -> None:
    state = _state(GamePhase.FINISHED).model_copy(update={"run_status": RunStatus.CLOSED})
    manager = GameManager(state, registry=REGISTRY)

    committed = await manager.commit_moderator_operation(
        operation="FINISH_ROLLBACK",
        command="finish",
        expected_revision=0,
        run_status=rollback_status,
        now=NOW,
    )

    assert committed.run_status is rollback_status
    assert committed.phase is GamePhase.FINISHED


@pytest.mark.asyncio
async def test_closed_game_rejects_ordinary_resume_and_imprecise_finish_rollback() -> None:
    state = _state(GamePhase.FINISHED).model_copy(update={"run_status": RunStatus.CLOSED})
    manager = GameManager(state, registry=REGISTRY)

    with pytest.raises(EventCommitError, match="cannot be reopened"):
        await manager.commit_moderator_operation(
            operation="RESUME",
            command="resume",
            expected_revision=0,
            run_status=RunStatus.READY,
            now=NOW,
        )
    with pytest.raises(EventCommitError, match="cannot be reopened"):
        await manager.commit_moderator_operation(
            operation="FINISH_ROLLBACK",
            command="resume",
            expected_revision=0,
            run_status=RunStatus.RUNNING,
            now=NOW,
        )

    assert manager.state.run_status is RunStatus.CLOSED


@pytest.mark.asyncio
async def test_action_request_is_pending_without_executing_skill() -> None:
    state, request, context = _action_state()
    manager = GameManager(state, registry=REGISTRY)

    committed = await manager.commit_action_request(request, context, expected_revision=0, now=NOW)

    assert committed.state_revision == 1
    assert committed.phase is GamePhase.NIGHT_ACTION
    assert committed.players[1].alive is True
    assert committed.action_requests["request-1"]["status"] == "PENDING"
    assert committed.action_windows["window-1"]["submitted_request_ids"] == ("request-1",)
    assert state.state_revision == 0
    assert state.action_requests == {}


@pytest.mark.asyncio
async def test_identical_action_replay_is_read_only() -> None:
    state, request, context = _action_state()
    manager = GameManager(state, registry=REGISTRY)

    committed = await manager.commit_action_request(request, context, now=NOW)
    replay = await manager.commit_action_request(
        request,
        context,
        expected_revision=committed.state_revision,
        now=NOW,
    )

    assert replay is committed
    assert replay.state_revision == 1
    assert len(replay.action_requests) == 1


@pytest.mark.asyncio
async def test_stale_action_commit_cannot_overwrite_first_submission() -> None:
    state, request, context = _action_state()
    other = request.model_copy(update={"request_id": "request-2", "seat": 2})
    other_context = context.model_copy(
        update={"active_request_id": "request-2", "role_id": "villager"}
    )
    manager = GameManager(state, registry=REGISTRY)

    results = await asyncio.gather(
        manager.commit_action_request(request, context, expected_revision=0, now=NOW),
        manager.commit_action_request(other, other_context, expected_revision=0, now=NOW),
        return_exceptions=True,
    )

    assert sum(isinstance(result, GameState) for result in results) == 1
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert len(manager.state.action_requests) == 1
    assert manager.state.state_revision == 1


@pytest.mark.asyncio
async def test_invalid_request_leaves_state_unchanged() -> None:
    state, request, context = _action_state()
    manager = GameManager(state, registry=REGISTRY)
    invalid = request.model_copy(update={"request_id": "other-request"})
    invalid_context = context

    with pytest.raises(Exception, match="REQUEST_MISMATCH"):
        await manager.commit_action_request(invalid, invalid_context, expected_revision=0)

    assert manager.state is state
    assert manager.state.state_revision == 0
    assert manager.state.action_requests == {}


@pytest.mark.asyncio
async def test_public_commit_rejects_action_patch_without_revalidation() -> None:
    state, request, context = _action_state()
    validated = validate_action_request(
        request,
        ActionWindow.model_validate(
            {
                **state.action_windows["window-1"],
                "phase": GamePhase.NIGHT_ACTION,
            }
        ),
        context,
        registry=REGISTRY,
        now=NOW,
    )
    manager = GameManager(state, registry=REGISTRY)

    with pytest.raises(StatePatchError, match="commit_action_request"):
        await manager.commit(
            StatePatch.action_request(validated, expected_revision=state.state_revision, now=NOW)
        )

    assert manager.state is state
    assert manager.state.state_revision == 0
    assert manager.state.action_requests == {}


@pytest.mark.asyncio
async def test_action_request_requires_seat_in_authoritative_state() -> None:
    state, request, context = _action_state()
    state_without_actor = state.model_copy(update={"players": {2: state.players[2]}})
    manager = GameManager(state_without_actor, registry=REGISTRY)

    with pytest.raises(Exception, match="SEAT_NOT_ASSIGNED"):
        await manager.commit_action_request(request, context, expected_revision=0, now=NOW)

    assert manager.state is state_without_actor
    assert manager.state.state_revision == 0
    assert manager.state.action_requests == {}


@pytest.mark.asyncio
async def test_action_request_requires_authoritative_current_request_id() -> None:
    state, request, context = _action_state()
    actor_without_request = state.players[1].model_copy(update={"current_request_id": None})
    state_without_request = state.model_copy(
        update={"players": {1: actor_without_request, 2: state.players[2]}}
    )
    manager = GameManager(state_without_request, registry=REGISTRY)

    with pytest.raises(Exception, match="REQUEST_MISMATCH"):
        await manager.commit_action_request(request, context, expected_revision=0, now=NOW)

    assert manager.state is state_without_request
    assert manager.state.state_revision == 0
    assert manager.state.action_requests == {}


@pytest.mark.asyncio
async def test_action_request_uses_authoritative_player_facts() -> None:
    state, request, context = _action_state()
    dead_actor = state.players[1].model_copy(update={"alive": False})
    dead_state = state.model_copy(update={"players": {1: dead_actor, 2: state.players[2]}})
    forged_context = context.model_copy(
        update={
            "active_request_id": "attacker-request",
            "player_alive": True,
            "role_id": "attacker-role",
            "skill_resources": {"forged_resource": 99},
        }
    )
    manager = GameManager(dead_state, registry=REGISTRY)

    with pytest.raises(Exception, match="PLAYER_DEAD"):
        await manager.commit_action_request(request, forged_context, expected_revision=0, now=NOW)

    assert manager.state is dead_state
    assert manager.state.state_revision == 0
    assert manager.state.action_requests == {}


@pytest.mark.asyncio
async def test_night_action_with_no_grant_rejects_forged_context_authorization() -> None:
    state = _state(GamePhase.NIGHT_ACTION).model_copy(
        update={
            "players": {
                1: PlayerState(
                    seat=1,
                    role_id="villager",
                    faction_id="village",
                    session_epoch=2,
                    current_request_id="request-1",
                )
            },
            "action_windows": {
                "window-1": ActionWindow(
                    window_id="window-1",
                    game_id="game-1",
                    session_epoch=2,
                    phase=GamePhase.NIGHT_ACTION,
                    allowed_seats=(1,),
                    allowed_action_codes=(102,),
                    opened_at=NOW,
                ).model_dump(mode="json")
            },
        }
    )
    manager = GameManager(state, registry=REGISTRY)

    with pytest.raises(ActionValidationError, match="ACTION_UNAUTHORIZED"):
        await manager.commit_action_request(
            ActionRequest(
                request_id="request-1",
                game_id="game-1",
                window_id="window-1",
                seat=1,
                session_epoch=2,
                phase=GamePhase.NIGHT_ACTION,
                actions=(Action(action_code=102, targets=(1,)),),
            ),
            ActionValidationContext(
                game_id="game-1",
                session_epoch=2,
                active_request_id="request-1",
                authorized_action_codes=(102,),
                alive_seats=(1,),
                eligible_targets_by_action={102: (1,)},
            ),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_event_append_and_ack_are_one_revision_and_hide_private_events() -> None:
    manager = GameManager(_delivery_state(), registry=REGISTRY)
    public = _public_event(1, 1)
    private = GameEvent.private(
        event_id=2,
        game_id="game-1",
        state_revision=1,
        round_no=1,
        phase=GamePhase.DAY_SPEECH,
        created_at=NOW,
        event_type=EventType.ROLE_ASSIGNMENT,
        seat=2,
        payload=PrivateRolePayload(role_id="seer", faction_id="village"),
    )

    committed = await manager.commit_events_and_ack(
        (public, private),
        DeliveryAck(1, 3, "turn-1", (1,)),
        expected_revision=0,
        now=NOW,
    )

    assert committed.state_revision == 1
    assert committed.events == (public, private)
    assert committed.delivery_cursors[1].committed_event_id == 1
    assert committed.delivery_cursors[1].in_flight_event_ids == ()
    assert await manager.peek_delivery(1, 3) == ()
    assert tuple(event.event_id for event in await manager.peek_delivery(2, 3)) == (1, 2)
    assert all(event.event_id != 2 for event in await manager.peek_delivery(1, 3))


@pytest.mark.asyncio
async def test_peek_then_begin_freezes_retry_batch_until_successful_ack() -> None:
    manager = GameManager(_delivery_state(), registry=REGISTRY)
    await manager.commit_events((_public_event(1, 1),), now=NOW)

    before = manager.state
    assert tuple(event.event_id for event in await manager.peek_delivery(1, 3)) == (1,)
    assert manager.state is before

    started = await manager.begin_delivery(1, 3, request_id="turn-1", now=NOW)
    assert started.state_revision == 2
    assert started.delivery_cursors[1].committed_event_id == 0
    assert started.delivery_cursors[1].in_flight_event_ids == (1,)

    await manager.commit_events((_public_event(2, 3),), now=NOW)
    assert tuple(event.event_id for event in await manager.peek_delivery(1, 3)) == (1,)

    finished = await manager.commit_delivery_ack(
        1,
        3,
        request_id="turn-1",
        expected_revision=3,
        now=NOW,
    )
    assert finished.state_revision == 4
    assert finished.delivery_cursors[1].committed_event_id == 1
    assert tuple(event.event_id for event in await manager.peek_delivery(1, 3)) == (2,)


@pytest.mark.asyncio
async def test_delivery_rejects_wrong_seat_session_request_and_revision() -> None:
    manager = GameManager(_delivery_state(), registry=REGISTRY)
    await manager.commit_events((_public_event(1, 1),), now=NOW)

    with pytest.raises(EventCommitError, match="SESSION_MISMATCH"):
        await manager.commit_delivery_ack(
            1,
            4,
            request_id="turn-1",
            event_ids=(1,),
            expected_revision=1,
        )
    with pytest.raises(EventCommitError, match="SEAT_NOT_ASSIGNED"):
        await manager.commit_delivery_ack(
            9,
            3,
            request_id="turn-1",
            event_ids=(1,),
            expected_revision=1,
        )
    with pytest.raises(RevisionConflict):
        await manager.commit_delivery_ack(
            1,
            3,
            request_id="turn-1",
            event_ids=(1,),
            expected_revision=0,
        )
    assert manager.state.state_revision == 1
    assert manager.state.delivery_cursors == {}


@pytest.mark.asyncio
async def test_expired_delivery_request_cannot_ack_another_batch() -> None:
    manager = GameManager(_delivery_state(), registry=REGISTRY)
    await manager.commit_events((_public_event(1, 1),), now=NOW)
    await manager.begin_delivery(1, 3, request_id="turn-1", now=NOW)

    with pytest.raises(EventCommitError, match="REQUEST_EXPIRED"):
        await manager.commit_delivery_ack(
            1,
            3,
            request_id="late-turn",
            event_ids=(1,),
            expected_revision=2,
        )
    assert manager.state.delivery_cursors[1].in_flight_event_ids == (1,)


@pytest.mark.asyncio
async def test_public_commit_cannot_bypass_event_authorization() -> None:
    state = _delivery_state()
    manager = GameManager(state, registry=REGISTRY)
    event = _public_event(1, 1)

    with pytest.raises(StatePatchError, match="event patches"):
        await manager.commit(StatePatch.append_events((event,), expected_revision=0, now=NOW))
    assert manager.state is state
    assert manager.state.events == ()


@pytest.mark.asyncio
async def test_concurrent_delivery_commits_serialize_on_revision() -> None:
    state = _delivery_state()
    manager = GameManager(state, registry=REGISTRY)
    results = await asyncio.gather(
        manager.commit_events_and_ack(
            (_public_event(1, 1),),
            DeliveryAck(1, 3, "turn-1", (1,)),
            expected_revision=0,
            now=NOW,
        ),
        manager.commit_events_and_ack(
            (_public_event(1, 1),),
            DeliveryAck(1, 3, "turn-1", (1,)),
            expected_revision=0,
            now=NOW,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, GameState) for result in results) == 1
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert manager.state.state_revision == 1
    assert manager.state.delivery_cursors[1].committed_event_id == 1
