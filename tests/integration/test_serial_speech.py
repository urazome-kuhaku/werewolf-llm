import asyncio
import json
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionWindow,
    GameManager,
    GameState,
    PlayerState,
    RandomStateRef,
    RulesetRef,
    SerialTurnScheduler,
    SheriffElectionState,
    load_action_registry,
)
from werewolf.game.serial_turn import SerialTurnError, SerialTurnTimeoutError, StaleTurnResponse
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import (
    InitialContext,
    RuntimeConfig,
    RuntimeLifecycleError,
    RuntimeRef,
    RuntimeRequestMismatchError,
    RuntimeTurnResult,
    Speech,
    SpeechResponse,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _state() -> GameState:
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.DAY_SPEECH,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="a" * 64,
        ),
        rng=RandomStateRef(seed=7),
        players={
            1: PlayerState(seat=1, role_id="villager", faction_id="village"),
            2: PlayerState(seat=2, role_id="seer", faction_id="village"),
        },
    )


def _team_chat_state() -> GameState:
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="a" * 64,
        ),
        rng=RandomStateRef(seed=7),
        players={
            1: PlayerState(seat=1, role_id="wolf", faction_id="werewolf"),
            2: PlayerState(seat=2, role_id="wolf", faction_id="werewolf"),
            3: PlayerState(seat=3, role_id="villager", faction_id="village"),
        },
        action_windows={
            "wolf-chat": ActionWindow(
                window_id="wolf-chat",
                game_id="game-1",
                session_epoch=0,
                phase=GamePhase.NIGHT_TEAM_CHAT,
                allowed_seats=(1, 2),
                allowed_role_ids=("wolf",),
                allowed_action_codes=(299,),
                min_actions=0,
                max_actions=0,
                allow_pass=True,
                opened_at=NOW,
            ).model_dump(mode="json")
        },
    )


def _sheriff_speech_state() -> GameState:
    election = SheriffElectionState.start(
        game_id="game-1",
        day_no=1,
        candidates=(1, 2),
        eligible_voters=(1, 2),
        speech_order=(1, 2),
    )
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.SHERIFF_ELECTION_SPEECH,
        day_no=1,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="a" * 64,
        ),
        rng=RandomStateRef(seed=7),
        players={
            seat: PlayerState(seat=seat, role_id="villager", faction_id="village")
            for seat in (1, 2, 3)
        },
        sheriff_election=election.persisted_payload(),
    )


async def _runtime(seat: int, script: list[object] | None = None) -> ScriptedRuntime:
    runtime = ScriptedRuntime(script or [])
    await runtime.start(
        RuntimeConfig(session_id=f"session-{seat}"),
        InitialContext(game_id="game-1", seat=seat, session_epoch=0),
    )
    return runtime


class _BlockingRuntime:
    """A one-session runtime that stays active after host cancellation."""

    def __init__(self, *, abort_fails: bool = False) -> None:
        self._session_ref: RuntimeRef | None = None
        self._active_request_id: str | None = None
        self._requests: list[object] = []
        self._aborts: list[str] = []
        self._abort_fails = abort_fails

    async def start(self, config: RuntimeConfig, context: InitialContext) -> RuntimeRef:
        self._session_ref = RuntimeRef(
            session_id=config.session_id,
            game_id=context.game_id,
            seat=context.seat,
            session_epoch=context.session_epoch,
        )
        return self._session_ref

    async def run_turn(self, request: object) -> RuntimeTurnResult:
        if self._session_ref is None:
            raise RuntimeLifecycleError("runtime has not been started")
        if self._active_request_id is not None:
            raise RuntimeLifecycleError("runtime already has an active turn")
        self._active_request_id = request.request_id
        self._requests.append(request)
        if len(self._requests) == 1:
            # There is intentionally no finally block.  A host timeout
            # cancels this coroutine while the runtime still owns the active
            # Pi turn, matching the Pi adapter's cancellation contract.
            await asyncio.Future()
        self._active_request_id = None
        response = SpeechResponse(
            request_id=request.request_id,
            speech=Speech(text="重试发言"),
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
        if self._abort_fails:
            raise RuntimeLifecycleError("abort failed")
        if self._active_request_id != request_id:
            raise RuntimeRequestMismatchError("request_id is not the active turn")
        self._aborts.append(request_id)
        self._active_request_id = None

    async def close(self, reason: str) -> None:
        del reason

    def get_session_ref(self) -> RuntimeRef:
        if self._session_ref is None:
            raise RuntimeLifecycleError("runtime has not been started")
        return self._session_ref

    @property
    def requests(self) -> tuple[object, ...]:
        return tuple(self._requests)

    @property
    def aborts(self) -> tuple[str, ...]:
        return tuple(self._aborts)


async def _blocking_runtime(*, abort_fails: bool = False) -> _BlockingRuntime:
    runtime = _BlockingRuntime(abort_fails=abort_fails)
    await runtime.start(
        RuntimeConfig(session_id="session-1"),
        InitialContext(game_id="game-1", seat=1, session_epoch=0),
    )
    return runtime


@pytest.mark.asyncio
async def test_public_speech_is_serial_and_second_seat_sees_first_event() -> None:
    first = await _runtime(1)
    second = await _runtime(2)
    manager = GameManager(_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(manager, {1: first, 2: second})

    await scheduler.start((1, 2))
    first_result = await scheduler.run_next()

    prompt = json.loads(PiRuntime()._build_prompt_message(first_result.request))
    schema = prompt["output_schema"]
    assert schema["properties"]["request_id"]["const"] == first_result.request.request_id
    assert schema["properties"]["kind"]["const"] == "speech"
    assert set(schema["required"]) >= {"schema_version", "request_id", "kind", "speech"}

    assert manager.state.current_queue == (2,)
    assert first_result.event.channel.value == "PUBLIC"
    assert first_result.event.payload.kind == "speech"
    assert first_result.event.payload.speaker_seat == 1
    assert manager.state.serial_turn is None
    assert len(second.requests) == 0

    second_result = await scheduler.run_next()

    assert second_result.event.payload.speaker_seat == 2
    assert second.requests[0].observation.events[0].event_id == first_result.event.event_id
    assert manager.state.current_queue == ()
    assert all(event.channel.value == "PUBLIC" for event in manager.state.events)


@pytest.mark.asyncio
async def test_sheriff_campaign_speech_is_serial_and_updates_private_election() -> None:
    first = await _runtime(1)
    second = await _runtime(2)
    manager = GameManager(_sheriff_speech_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(
        manager,
        {1: first, 2: second},
        phase=GamePhase.SHERIFF_ELECTION_SPEECH,
    )

    await scheduler.start()
    assert manager.state.current_queue == (1, 2)
    first_result = await scheduler.run_next()

    assert manager.state.current_queue == (2,)
    assert first_result.event.payload.speaker_seat == 1
    assert manager.state.sheriff_election is not None
    assert manager.state.sheriff_election["speeches"]["1"]["seat"] == 1

    second_result = await scheduler.run_next()
    assert second_result.event.payload.speaker_seat == 2
    assert second.requests[0].observation.events[0].event_id == first_result.event.event_id
    assert manager.state.current_queue == ()
    assert manager.state.serial_turn is None


@pytest.mark.asyncio
async def test_sheriff_serial_queue_rejects_non_candidate_seats() -> None:
    manager = GameManager(_sheriff_speech_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(
        manager,
        {},
        phase=GamePhase.SHERIFF_ELECTION_SPEECH,
    )

    with pytest.raises(Exception, match="SHERIFF_QUEUE_INVALID"):
        await scheduler.start((1, 3))
    assert manager.state.current_queue is None


@pytest.mark.asyncio
async def test_team_speech_is_serial_and_visible_only_to_authorized_wolves() -> None:
    first = await _runtime(1)
    second = await _runtime(2)
    manager = GameManager(_team_chat_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(
        manager,
        {1: first, 2: second},
        phase=GamePhase.NIGHT_TEAM_CHAT,
    )

    await scheduler.start()
    first_result = await scheduler.run_next()

    assert first_result.event.channel.value == "TEAM"
    assert first_result.event.event_type.value == "team_speech"
    assert first_result.event.audience == (1, 2)
    assert manager.state.current_queue == (2,)
    assert await manager.peek_delivery(3, 0) == ()

    second_result = await scheduler.run_next()

    assert second_result.event.channel.value == "TEAM"
    assert second.requests[0].observation.events[0].event_id == first_result.event.event_id
    assert manager.state.current_queue == ()
    assert all(event.channel.value == "TEAM" for event in manager.state.events)


@pytest.mark.asyncio
async def test_team_queue_rejects_seat_outside_installed_window() -> None:
    manager = GameManager(_team_chat_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(
        manager,
        {},
        phase=GamePhase.NIGHT_TEAM_CHAT,
    )

    with pytest.raises(Exception, match="TEAM_SEAT_NOT_AUTHORIZED"):
        await scheduler.start((1, 3))
    assert manager.state.current_queue is None


@pytest.mark.asyncio
async def test_team_bind_rejects_unauthorized_head_even_if_queue_was_forged() -> None:
    manager = GameManager(_team_chat_state(), registry=load_action_registry())
    # Simulate a pre-existing forged queue from an old snapshot.  The bind
    # path must repeat the team-window check instead of trusting that state.
    forged = manager.state.model_copy(update={"current_queue": (3,)})
    manager = GameManager(forged, registry=load_action_registry())

    with pytest.raises(Exception, match="TEAM_SEAT_NOT_AUTHORIZED"):
        await manager.begin_serial_speech_turn(
            3,
            0,
            request_id="team-request",
            logical_request_id="team-logical",
            phase=GamePhase.NIGHT_TEAM_CHAT,
        )


@pytest.mark.asyncio
async def test_late_team_response_after_retry_does_not_append_event() -> None:
    manager = GameManager(_team_chat_state(), registry=load_action_registry())
    await manager.set_serial_turn_queue((1,), phase=GamePhase.NIGHT_TEAM_CHAT)
    await manager.begin_serial_speech_turn(
        1,
        0,
        request_id="team-old",
        logical_request_id="team-logical",
        phase=GamePhase.NIGHT_TEAM_CHAT,
    )
    await manager.begin_serial_speech_turn(
        1,
        0,
        request_id="team-new",
        logical_request_id="team-logical",
        attempt_no=2,
        retry=True,
        phase=GamePhase.NIGHT_TEAM_CHAT,
    )

    with pytest.raises(Exception, match="REQUEST_EXPIRED"):
        await manager.commit_serial_speech(
            1,
            0,
            request_id="team-old",
            logical_request_id="team-logical",
            attempt_no=1,
            text="迟到的狼队发言",
            phase=GamePhase.NIGHT_TEAM_CHAT,
        )

    assert manager.state.events == ()
    assert manager.state.current_queue == (1,)


@pytest.mark.asyncio
async def test_timeout_keeps_head_and_retry_aborts_before_new_request() -> None:
    first = await _blocking_runtime()
    manager = GameManager(_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(manager, {1: first}, timeout_seconds=0.01)
    await scheduler.start((1,))

    with pytest.raises(SerialTurnTimeoutError):
        await scheduler.run_next()
    assert manager.state.current_queue == (1,)
    assert manager.state.serial_turn is not None
    assert manager.state.serial_turn.attempt_no == 1
    old_request = first.requests[0]
    old_event_ids = manager.state.serial_turn.event_ids

    first_retry = await scheduler.retry()

    assert first.aborts == (old_request.request_id,)
    assert first_retry.request.attempt_no == 2
    assert first_retry.request.logical_request_id.endswith("speech-s1")
    assert first_retry.request.request_id != old_request.request_id
    assert first_retry.request.observation.events == []
    assert manager.state.current_queue == ()
    assert old_event_ids == ()


@pytest.mark.asyncio
async def test_abort_failure_keeps_old_binding_and_does_not_start_retry() -> None:
    first = await _blocking_runtime(abort_fails=True)
    manager = GameManager(_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(manager, {1: first}, timeout_seconds=0.01)
    await scheduler.start((1,))

    with pytest.raises(SerialTurnTimeoutError):
        await scheduler.run_next()
    old_binding = manager.state.serial_turn
    assert old_binding is not None

    with pytest.raises(SerialTurnError, match="ABORT_FAILED"):
        await scheduler.retry()

    assert manager.state.serial_turn == old_binding
    assert len(first.requests) == 1
    assert first.aborts == ()


@pytest.mark.asyncio
async def test_late_response_from_aborted_request_is_rejected() -> None:
    first = await _blocking_runtime()
    manager = GameManager(_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(manager, {1: first}, timeout_seconds=0.01)
    await scheduler.start((1,))

    with pytest.raises(SerialTurnTimeoutError):
        await scheduler.run_next()
    old_request = first.requests[0]
    await scheduler.retry()

    late = RuntimeTurnResult(
        request_id=old_request.request_id,
        logical_request_id=old_request.logical_request_id,
        attempt_no=old_request.attempt_no,
        response=SpeechResponse(
            request_id=old_request.request_id,
            speech=Speech(text="迟到回包"),
        ),
    )
    with pytest.raises(StaleTurnResponse, match="REQUEST_EXPIRED"):
        await scheduler.commit_response(old_request, late)


@pytest.mark.asyncio
async def test_late_response_from_superseded_request_is_rejected() -> None:
    first = await _runtime(1)
    manager = GameManager(_state(), registry=load_action_registry())
    scheduler = SerialTurnScheduler(manager, {1: first})
    await scheduler.start((1,))

    await manager.begin_serial_speech_turn(
        1,
        0,
        request_id="old-request",
        logical_request_id="speech-round-1-seat-1",
    )
    await manager.begin_serial_speech_turn(
        1,
        0,
        request_id="new-request",
        logical_request_id="speech-round-1-seat-1",
        attempt_no=2,
        retry=True,
    )
    with pytest.raises(Exception, match="REQUEST_EXPIRED"):
        await manager.commit_serial_speech(
            1,
            0,
            request_id="old-request",
            logical_request_id="speech-round-1-seat-1",
            attempt_no=1,
            text="迟到回包",
        )
    assert manager.state.current_queue == (1,)
