"""Host controlled last words for board-defined daytime boundaries.

Last words are a small serial speech section, but their eligibility is a
state fact.  This adapter only accepts seats whose latest committed death
provenance matches the frozen board policy; it never lets a host choose an
arbitrary dead seat.  Delivery and completion both leave durable evidence in
the manager so a restarted moderator can resume the same queue safely.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from werewolf.domain.enums import GamePhase
from werewolf.game.day import _night_death_seats
from werewolf.game.manager import EventCommitError, GameManager
from werewolf.game.serial_turn import (
    SerialSpeechResult,
    SerialTurnError,
    SerialTurnScheduler,
    TurnQueue,
)
from werewolf.game.state import GameState
from werewolf.knowledge.board import BoardDefinition
from werewolf.runtime.player_runtime import PlayerRuntime


class LastWordsError(RuntimeError):
    """A safe moderator error from the last words boundary."""


@dataclass(frozen=True, slots=True)
class _LastWordsSource:
    source_id: str
    seats: tuple[int, ...]
    phase: GamePhase


_COMPLETION_RE = re.compile(r"^source=([^;]+);seat=([1-9][0-9]*)$")


def _exile_source(state: GameState, board: BoardDefinition) -> _LastWordsSource | None:
    if board.day_flow.last_words.day_death_policy == "none":
        return None
    eligible = set(board.day_flow.last_words.eligible_death_causes)
    vote_state = state.vote_state if isinstance(state.vote_state, dict) else {}
    raw_window = vote_state.get("window")
    current_window_id = raw_window.get("window_id") if isinstance(raw_window, dict) else None
    if not isinstance(current_window_id, str) or not current_window_id:
        return None
    for audit in reversed(state.moderator_audit):
        if not isinstance(audit, dict) or audit.get("operation") != "DAY_EXILE":
            continue
        if current_window_id is not None and audit.get("vote_window_id") != current_window_id:
            continue
        seat = audit.get("target_seat")
        if type(seat) is not int:
            continue
        player = state.players.get(seat)
        if player is None or player.alive or player.death_cause not in eligible:
            continue
        resolution_id = audit.get("resolution_id")
        marker = (
            resolution_id
            if isinstance(resolution_id, str)
            else str(audit.get("committed_revision", ""))
        )
        return _LastWordsSource(
            source_id=f"day-exile-d{state.day_no}-{marker}",
            seats=(seat,),
            phase=(
                GamePhase.TRIGGER_ACTION
                if state.phase is GamePhase.TRIGGER_ACTION
                else GamePhase.DAY_RESOLVE
            ),
        )
    return None


def _source_for(state: GameState, board: BoardDefinition) -> _LastWordsSource | None:
    policy = board.day_flow.last_words
    if not policy.enabled:
        return None
    if state.phase in {GamePhase.DAY_RESOLVE, GamePhase.TRIGGER_ACTION}:
        pending = state.pending_resolution
        if state.phase is GamePhase.TRIGGER_ACTION and isinstance(pending, dict):
            if pending.get("operation") == "DAY_EXILE":
                return _exile_source(state, board)
            if pending.get("operation") == "NIGHT_RESOLUTION":
                seats = _night_death_seats(state, board)
                return (
                    _LastWordsSource(f"night-r{state.round_no}", seats, state.phase)
                    if seats
                    else None
                )
        source = _exile_source(state, board)
        if source is not None:
            return source
    if state.phase in {
        GamePhase.DAY_ANNOUNCE,
        GamePhase.DAY_SPEECH,
        GamePhase.SHERIFF_ELECTION_SPEECH,
        GamePhase.SHERIFF_ELECTION,
        GamePhase.SHERIFF_ELECTION_PK_SPEECH,
        GamePhase.SHERIFF_ELECTION_PK,
        GamePhase.SHERIFF_TRANSFER,
    }:
        seats = _night_death_seats(state, board)
        if seats:
            return _LastWordsSource(f"night-r{state.round_no}", seats, state.phase)
    return None


class LastWordsFlow:
    """Drive one board-authorized last words queue."""

    def __init__(
        self,
        manager: GameManager,
        board: BoardDefinition,
        runtimes: Mapping[int, PlayerRuntime],
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(manager, GameManager):
            raise TypeError("manager must be a GameManager")
        if not isinstance(board, BoardDefinition):
            raise TypeError("board must be a BoardDefinition")
        self._manager = manager
        self._board = board
        self._runtimes = dict(runtimes)
        self._timeout_seconds = timeout_seconds
        self._scheduler: SerialTurnScheduler | None = None
        self._source_id: str | None = None

    @property
    def state(self) -> GameState:
        return self._manager.state

    def _completed(self, source_id: str) -> set[int]:
        completed: set[int] = set()
        for audit in self.state.moderator_audit:
            if not isinstance(audit, dict) or audit.get("operation") != "LAST_WORDS_COMPLETE":
                continue
            reason = audit.get("reason")
            if not isinstance(reason, str):
                continue
            match = _COMPLETION_RE.fullmatch(reason)
            if match is not None and match.group(1) == source_id:
                completed.add(int(match.group(2)))
        # The speech commit and the completion audit are separate serialized
        # commits.  A crash between them must still be read as completed from
        # the durable public speech correlation, otherwise a restart would
        # ask the dead seat to repeat its last words.
        marker = "-last-words-s"
        for event in self.state.events:
            if getattr(event, "round_no", None) != self.state.round_no:
                continue
            correlation = getattr(event, "correlation_id", None)
            if not isinstance(correlation, str) or marker not in correlation:
                continue
            suffix = correlation.rsplit(marker, 1)[-1]
            if suffix.isdigit():
                completed.add(int(suffix))
        return completed

    def _current(self) -> tuple[_LastWordsSource | None, tuple[int, ...]]:
        source = _source_for(self.state, self._board)
        if source is None:
            return None, ()
        return source, tuple(
            seat for seat in source.seats if seat not in self._completed(source.source_id)
        )

    def status(self) -> dict[str, object]:
        source, pending = self._current()
        state = self.state
        queue = (
            list(state.current_queue)
            if self._source_id is not None and state.current_queue is not None
            else []
        )
        return {
            "phase": state.phase.value,
            "enabled": self._board.day_flow.last_words.enabled,
            "source": None if source is None else source.source_id,
            "pending_seats": list(pending),
            "queue": queue,
            "turn": None
            if state.serial_turn is None
            else state.serial_turn.model_dump(mode="json"),
        }

    def require_complete(self) -> None:
        source, pending = self._current()
        if pending:
            raise LastWordsError(
                "LAST_WORDS_PENDING: complete last words for seats "
                + ", ".join(str(seat) for seat in pending)
            )

    async def _ensure_scheduler(self, source: _LastWordsSource, pending: tuple[int, ...]) -> None:
        state = await self._manager.snapshot()
        if state.serial_turn is not None:
            active = state.serial_turn
            if (
                state.phase is not source.phase
                or "-last-words-s" not in active.logical_request_id
                or active.seat not in pending
            ):
                raise LastWordsError("LAST_WORDS_TURN_CONFLICT: another serial turn is active")
            self._source_id = source.source_id
            if self._scheduler is None:
                self._scheduler = SerialTurnScheduler(
                    self._manager,
                    self._runtimes,
                    timeout_seconds=self._timeout_seconds,
                    phase=state.phase,
                    logical_label="last-words",
                )
            return
        current_queue = state.current_queue
        if current_queue is not None and current_queue != ():
            if tuple(current_queue) != pending:
                raise LastWordsError("LAST_WORDS_QUEUE_CONFLICT: another serial queue is active")
        self._scheduler = SerialTurnScheduler(
            self._manager,
            self._runtimes,
            timeout_seconds=self._timeout_seconds,
            phase=source.phase,
            logical_label="last-words",
        )
        current_queue = state.current_queue
        if current_queue in (None, ()):
            await self._scheduler.start(TurnQueue(pending))
        self._source_id = source.source_id

    async def next(self, seat: int | None = None) -> SerialSpeechResult:
        source, pending = self._current()
        if source is None or not pending:
            raise LastWordsError("LAST_WORDS_COMPLETE: no eligible last words are pending")
        await self._ensure_scheduler(source, pending)
        assert self._scheduler is not None
        current = await self._manager.snapshot()
        if not current.current_queue:
            raise LastWordsError("LAST_WORDS_COMPLETE: the last words queue is exhausted")
        if seat is not None and seat != current.current_queue[0]:
            raise LastWordsError(
                f"SEAT_NOT_AT_HEAD: last words seat {current.current_queue[0]} is next"
            )
        try:
            result = await self._scheduler.run_next()
        except SerialTurnError as exc:
            raise LastWordsError(str(exc)) from exc
        source_id = source.source_id
        await self._commit_completion(source_id, result)
        return result

    async def _commit_completion(self, source_id: str, result: SerialSpeechResult) -> None:
        try:
            await self._manager.commit_moderator_operation(
                operation="LAST_WORDS_COMPLETE",
                command="last-words next",
                expected_revision=self.state.state_revision,
                reason=(
                    f"source={source_id};"
                    f"seat={result.request.logical_request_id.rsplit('-s', 1)[-1]}"
                ),
            )
        except (EventCommitError, TypeError, ValueError) as exc:
            raise LastWordsError(str(exc)) from exc

    async def retry(self, seat: int | None = None) -> SerialSpeechResult:
        state = await self._manager.snapshot()
        if state.serial_turn is None:
            raise LastWordsError("LAST_WORDS_RETRY_NOT_AVAILABLE: no active last words request")
        if seat is not None and seat != state.serial_turn.seat:
            raise LastWordsError(
                f"SEAT_NOT_ALLOWED: active last words seat is {state.serial_turn.seat}"
            )
        source, pending = self._current()
        if source is None or not pending:
            raise LastWordsError("LAST_WORDS_RETRY_NOT_AVAILABLE: no eligible last words source")
        await self._ensure_scheduler(source, pending)
        assert self._scheduler is not None
        try:
            result = await self._scheduler.retry()
        except SerialTurnError as exc:
            raise LastWordsError(str(exc)) from exc
        await self._commit_completion(source.source_id, result)
        return result


__all__ = ["LastWordsError", "LastWordsFlow"]
