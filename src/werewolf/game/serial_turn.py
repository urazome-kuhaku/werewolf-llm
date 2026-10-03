"""Serial public and team speech scheduling.

The scheduler owns orchestration only.  ``GameManager`` remains the sole
writer of authoritative state: it binds the physical request and delivery
cursor before the runtime call, then commits the public event, cursor ack, and
queue pop together after a valid response.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from werewolf.domain.enums import GamePhase
from werewolf.runtime.player_runtime import (
    Deadline,
    Observation,
    ObservationEvent,
    PlayerRuntime,
    ResponseKind,
    RuntimeTurnResult,
    SpeechResponse,
    TurnRequest,
    build_turn_response_schema,
)

from .events import GameEvent
from .manager import EventCommitError, GameManager
from .state import GameState


class SerialTurnError(RuntimeError):
    """Base error for queue and serial runtime coordination failures."""


class SerialTurnBusyError(SerialTurnError):
    """The queue head already has a physical request waiting for a response."""


class SerialTurnTimeoutError(TimeoutError, SerialTurnError):
    """The runtime exceeded the configured hard deadline."""


class StaleTurnResponse(SerialTurnError):
    """A response arrived after its physical request was superseded."""


@dataclass(frozen=True, slots=True)
class TurnQueue:
    """An immutable, duplicate-free queue of seats."""

    seats: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.seats:
            raise ValueError("turn queue must contain at least one seat")
        if any(isinstance(seat, bool) or not 1 <= seat <= 64 for seat in self.seats):
            raise ValueError("turn queue seats must be integers between 1 and 64")
        if len(set(self.seats)) != len(self.seats):
            raise ValueError("turn queue seats must be unique")

    @classmethod
    def from_seats(cls, seats: Iterable[int]) -> TurnQueue:
        return cls(tuple(seats))

    @property
    def head(self) -> int:
        return self.seats[0]

    def after_head(self) -> TurnQueue | None:
        remaining = self.seats[1:]
        return None if not remaining else type(self)(remaining)


@dataclass(frozen=True, slots=True)
class SerialSpeechResult:
    """A committed speech and the request that produced it."""

    request: TurnRequest
    runtime_result: RuntimeTurnResult
    event: GameEvent


class SerialTurnScheduler:
    """Run one public or authorized team speech at a time for a frozen queue."""

    def __init__(
        self,
        manager: GameManager,
        runtimes: Mapping[int, PlayerRuntime],
        *,
        queue: TurnQueue | Sequence[int] | None = None,
        timeout_seconds: float | None = None,
        phase: GamePhase = GamePhase.DAY_SPEECH,
        logical_label: str | None = None,
    ) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if phase not in {
            GamePhase.DAY_SPEECH,
            GamePhase.VOTE_PK_SPEECH,
            GamePhase.NIGHT_TEAM_CHAT,
            GamePhase.SHERIFF_ELECTION_SPEECH,
            GamePhase.SHERIFF_ELECTION_PK_SPEECH,
            GamePhase.DAY_ANNOUNCE,
            GamePhase.DAY_RESOLVE,
            GamePhase.TRIGGER_ACTION,
        }:
            raise ValueError(
                "serial speech phase must be DAY_SPEECH, VOTE_PK_SPEECH, "
                "NIGHT_TEAM_CHAT, SHERIFF_ELECTION_SPEECH, or "
                "SHERIFF_ELECTION_PK_SPEECH, DAY_ANNOUNCE, DAY_RESOLVE, "
                "or TRIGGER_ACTION"
            )
        self._manager = manager
        self._runtimes = dict(runtimes)
        self._queue = self._coerce_queue(queue) if queue is not None else None
        self._timeout_seconds = timeout_seconds
        self._phase = phase
        if logical_label is not None and (
            not logical_label
            or len(logical_label) > 32
            or not logical_label.replace("-", "").isalnum()
        ):
            raise ValueError("logical_label must be a short alphanumeric hyphenated label")
        self._logical_label = logical_label
        self._request_seats: dict[str, int] = {}

    @staticmethod
    def _coerce_queue(queue: TurnQueue | Sequence[int]) -> TurnQueue:
        return queue if isinstance(queue, TurnQueue) else TurnQueue(tuple(queue))

    @property
    def queue(self) -> TurnQueue | None:
        current = self._manager.state.current_queue
        return None if current is None else TurnQueue(tuple(current))

    async def start(self, queue: TurnQueue | Sequence[int] | None = None) -> None:
        """Install the immutable queue before the first runtime call."""

        requested = self._coerce_queue(queue) if queue is not None else self._queue
        if requested is None and self._phase is GamePhase.NIGHT_TEAM_CHAT:
            window = await self._manager.get_active_team_chat_window()
            requested = TurnQueue(window.allowed_seats)
        if requested is None and self._phase in {
            GamePhase.SHERIFF_ELECTION_SPEECH,
            GamePhase.SHERIFF_ELECTION_PK_SPEECH,
        }:
            seats = await self._manager.sheriff_speech_queue(self._phase)
            if not seats:
                raise SerialTurnError(
                    "TURN_QUEUE_EMPTY: all authorized sheriff candidates have spoken"
                )
            requested = TurnQueue(seats)
        if requested is None:
            raise ValueError("a turn queue is required")
        await self._manager.set_serial_turn_queue(requested.seats, phase=self._phase)
        self._queue = requested

    async def run_next(self) -> SerialSpeechResult:
        """Run the current queue head and commit it on a valid response."""

        return await self._run(retry=False)

    async def retry(self) -> SerialSpeechResult:
        """Retry the unchanged queue head with a new physical request ID."""

        return await self._run(retry=True)

    async def _run(self, *, retry: bool) -> SerialSpeechResult:
        state = await self._manager.snapshot()
        if state.current_queue is None or not state.current_queue:
            raise SerialTurnError("TURN_QUEUE_EMPTY: no serial speech turn is pending")
        if not retry and state.serial_turn is not None:
            raise SerialTurnBusyError("TURN_IN_PROGRESS: use retry for the active queue head")
        seat = state.current_queue[0]
        runtime = self._runtimes.get(seat)
        if runtime is None:
            raise SerialTurnError(f"RUNTIME_MISSING: no runtime is registered for seat {seat}")
        try:
            runtime_ref = runtime.get_session_ref()
        except Exception as exc:
            raise SerialTurnError("RUNTIME_NOT_STARTED: start the seat runtime first") from exc
        player = state.players.get(seat)
        if player is None:
            raise SerialTurnError("SEAT_NOT_ASSIGNED: queue head is not a current player")
        if (
            runtime_ref.game_id != state.game_id
            or runtime_ref.seat != seat
            or runtime_ref.session_epoch != player.session_epoch
        ):
            raise SerialTurnError("SESSION_MISMATCH: runtime is not bound to the queue head")

        previous = state.serial_turn
        if retry:
            if previous is None:
                raise SerialTurnError("REQUEST_EXPIRED: no active turn can be retried")
            try:
                old_request_id = previous.request_id
                logical_request_id = previous.logical_request_id
                attempt_no = previous.attempt_no + 1
            except (AttributeError, TypeError, ValueError) as exc:
                raise SerialTurnError("REQUEST_INVALID: active turn metadata is malformed") from exc

            # A runtime session accepts at most one active turn.  The durable
            # game binding must remain on the old request until the runtime
            # confirms that request has been aborted and the session is idle.
            # In particular, do not let ``begin_serial_speech_turn`` supersede
            # the binding first: if abort fails, the host still has a precise
            # request to inspect, extend, or recover.
            try:
                await runtime.abort(old_request_id)
            except Exception as exc:
                raise SerialTurnError(
                    f"ABORT_FAILED: could not abort serial request {old_request_id}"
                ) from exc
        else:
            logical_request_id = self._logical_request_id(
                state.game_id,
                state.round_no,
                seat,
                phase=self._phase,
                label=self._logical_label,
            )
            attempt_no = 1
        request_id = self._physical_request_id(
            logical_request_id,
            attempt_no,
            state.state_revision,
        )

        bound = await self._manager.begin_serial_speech_turn(
            seat,
            player.session_epoch,
            request_id=request_id,
            logical_request_id=logical_request_id,
            attempt_no=attempt_no,
            retry=retry,
            phase=self._phase,
        )
        visible = await self._manager.peek_delivery(seat, player.session_epoch)
        request = self._make_request(
            bound,
            seat=seat,
            session_epoch=player.session_epoch,
            request_id=request_id,
            logical_request_id=logical_request_id,
            attempt_no=attempt_no,
            events=visible,
        )
        self._request_seats[request.request_id] = seat
        try:
            if self._timeout_seconds is None:
                result = await runtime.run_turn(request)
            else:
                result = await asyncio.wait_for(
                    runtime.run_turn(request),
                    timeout=self._timeout_seconds,
                )
        except TimeoutError as exc:
            raise SerialTurnTimeoutError(
                f"runtime timed out for serial request {request.request_id}"
            ) from exc
        return await self.commit_response(request, result)

    async def commit_response(
        self,
        request: TurnRequest,
        result: RuntimeTurnResult,
    ) -> SerialSpeechResult:
        """Validate and commit a response, including stale-response checks."""

        if result.request_id != request.request_id:
            raise StaleTurnResponse("REQUEST_MISMATCH: response request_id is stale")
        if result.logical_request_id != request.logical_request_id:
            raise StaleTurnResponse("REQUEST_MISMATCH: response logical_request_id is stale")
        if result.attempt_no != request.attempt_no:
            raise StaleTurnResponse("REQUEST_MISMATCH: response attempt_no is stale")
        if not isinstance(result.response, SpeechResponse):
            raise SerialTurnError("SCHEMA_INVALID: serial speech requires a speech response")
        seat = self._request_seats.get(request.request_id)
        if seat is None:
            raise StaleTurnResponse("REQUEST_EXPIRED: request is not owned by this scheduler")
        active = (await self._manager.snapshot()).serial_turn
        if (
            active is None
            or active.request_id != request.request_id
            or active.logical_request_id != request.logical_request_id
            or active.attempt_no != request.attempt_no
            or active.seat != seat
        ):
            raise StaleTurnResponse("REQUEST_EXPIRED: request is no longer active")
        try:
            commit = (
                self._manager.commit_serial_sheriff_speech
                if self._phase
                in {
                    GamePhase.SHERIFF_ELECTION_SPEECH,
                    GamePhase.SHERIFF_ELECTION_PK_SPEECH,
                }
                else self._manager.commit_serial_speech
            )
            committed = await commit(
                seat,
                request.session_epoch,
                request_id=request.request_id,
                logical_request_id=request.logical_request_id,
                attempt_no=request.attempt_no,
                text=result.response.speech.text,
                phase=self._phase,
            )
        except EventCommitError as exc:
            if "REQUEST_EXPIRED" in str(exc) or "REQUEST_MISMATCH" in str(exc):
                raise StaleTurnResponse(str(exc)) from exc
            raise SerialTurnError(str(exc)) from exc
        if not committed.events or not isinstance(committed.events[-1], GameEvent):
            raise SerialTurnError("COMMIT_INVALID: speech event was not appended")
        return SerialSpeechResult(request, result, committed.events[-1])

    @staticmethod
    def _logical_request_id(
        game_id: str,
        round_no: int,
        seat: int,
        *,
        phase: GamePhase = GamePhase.DAY_SPEECH,
        label: str | None = None,
    ) -> str:
        if label is not None:
            return f"{game_id}-r{round_no}-{label}-s{seat}"
        label = {
            GamePhase.SHERIFF_ELECTION_SPEECH: "sheriff-speech",
            GamePhase.SHERIFF_ELECTION_PK_SPEECH: "sheriff-pk-speech",
        }.get(phase, "speech")
        return f"{game_id}-r{round_no}-{label}-s{seat}"

    @staticmethod
    def _physical_request_id(logical_request_id: str, attempt_no: int, revision: int) -> str:
        return f"{logical_request_id}-a{attempt_no}-rev{revision}"

    def _make_request(
        self,
        state: GameState,
        *,
        seat: int,
        session_epoch: int,
        request_id: str,
        logical_request_id: str,
        attempt_no: int,
        events: tuple[GameEvent, ...],
    ) -> TurnRequest:
        del seat  # The runtime seat is bound by its session, not model text.
        now = datetime.now(UTC)
        timeout = self._timeout_seconds or 120.0
        observation = Observation(
            summary=(
                "你当前获得的是遗言发言机会；请发表本局遗言，不要把它当作普通存活玩家的日常发言。"
                if self._logical_label == "last-words"
                else (
                    "你当前是狼人团队的最终协调员。请综合本夜所有队友提案，明确刀口及理由、"
                    "战术分工与备选方案，并说明如何处理分歧。这里只提交团队最终方案说明；"
                    "实际刀口必须在后续结构化 WOLF_KILL 行动中提交，文字不会改变目标。"
                )
                if self._logical_label == "wolf-plan"
                else ""
            ),
            events=[
                ObservationEvent(
                    event_id=event.event_id,
                    event_type=str(event.event_type),
                    payload=event.payload.model_dump(mode="json"),
                )
                for event in events
            ],
        )
        return TurnRequest(
            request_id=request_id,
            logical_request_id=logical_request_id,
            attempt_no=attempt_no,
            game_id=state.game_id,
            session_epoch=session_epoch,
            phase=self._phase,
            expected_kind=ResponseKind.SPEECH,
            observation=observation,
            output_schema=build_turn_response_schema(
                ResponseKind.SPEECH,
                request_id,
            ),
            deadline=Deadline(
                soft_deadline=now,
                hard_deadline=now + timedelta(seconds=timeout),
            ),
        )


__all__ = [
    "SerialSpeechResult",
    "SerialTurnError",
    "SerialTurnBusyError",
    "SerialTurnScheduler",
    "SerialTurnTimeoutError",
    "StaleTurnResponse",
    "TurnQueue",
]
