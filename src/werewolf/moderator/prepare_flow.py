"""Moderator-owned player knowledge preparation flow.

The first runtime turn is deliberately a normal ``PlayerRuntime`` request.
The model may claim that it has read the board and its role, but the claim is
accepted only when the loopback gateway has captured the corresponding
seat-bound successful receipts.  A verified claim is then committed through
``GameManager`` so the phase transition cannot race a stale session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from werewolf.domain.enums import GamePhase
from werewolf.game.manager import EventCommitError, GameManager
from werewolf.game.state import GameState
from werewolf.knowledge.service import QueryContext
from werewolf.moderator.sessions import PlayerSessionError, PlayerSessionService
from werewolf.runtime.knowledge_bootstrap import KnowledgeNotReadyError, KnowledgeReadyGate
from werewolf.runtime.player_runtime import (
    Deadline,
    Observation,
    ReadyResponse,
    ResponseKind,
    RuntimeRequestMismatchError,
    RuntimeTurnResult,
    TurnRequest,
    build_turn_response_schema,
)


class PlayerPrepareError(RuntimeError):
    """Raised when a player cannot complete the knowledge preparation turn."""


Clock = Callable[[], datetime]


class PlayerPrepareFlow:
    """Run and commit the seat-scoped READY turn for the current game."""

    def __init__(
        self,
        manager: GameManager,
        sessions: PlayerSessionService,
        *,
        clock: Clock,
        turn_timeout: timedelta = timedelta(seconds=120),
    ) -> None:
        if not isinstance(manager, GameManager):
            raise TypeError("manager must be a GameManager")
        if not isinstance(sessions, PlayerSessionService):
            raise TypeError("sessions must be a PlayerSessionService")
        if turn_timeout <= timedelta(0):
            raise ValueError("turn_timeout must be positive")
        self.manager = manager
        self.sessions = sessions
        self.clock = clock
        self.turn_timeout = turn_timeout
        self._attempts: dict[int, int] = {}
        # Keep the last physical request for every seat until its READY
        # response has been committed.  A timeout or a rejected response can
        # leave a real Pi turn active, so retry must address this exact
        # request before issuing a replacement.
        self._requests: dict[int, TurnRequest] = {}
        self._lock = asyncio.Lock()

    async def status(self) -> dict[str, object]:
        """Return moderator-safe preparation status for every assigned seat."""

        state = await self.manager.snapshot()
        if state.phase is not GamePhase.PLAYER_PREPARE:
            raise PlayerPrepareError(
                f"prepare status requires PLAYER_PREPARE, current phase is {state.phase.value}"
            )
        try:
            records = self.sessions.records
        except PlayerSessionError as exc:
            raise PlayerPrepareError(str(exc)) from exc
        seats: list[dict[str, object]] = []
        for seat in sorted(state.players):
            player = state.players[seat]
            receipts = self.sessions.receipts_for_seat(seat)
            seats.append(
                {
                    "seat": seat,
                    "ready": bool(player.knowledge_receipt_ids),
                    "receipt_count": len(receipts),
                    "attempts": self._attempts.get(seat, 0),
                    "runtime_started": seat in records,
                }
            )
        return {
            "status": "ok",
            "phase": state.phase.value,
            "ready": all(bool(player.knowledge_receipt_ids) for player in state.players.values()),
            "players": seats,
        }

    async def next(self, seat: int | None = None) -> dict[str, object]:
        """Run the next pending seat's READY turn."""

        return await self._run(seat, command="next")

    async def retry(self, seat: int | None = None) -> dict[str, object]:
        """Retry a failed or timed-out READY turn for one seat."""

        return await self._run(seat, command="retry")

    async def _run(self, seat: int | None, *, command: str) -> dict[str, object]:
        async with self._lock:
            state = await self.manager.snapshot()
            if state.phase is not GamePhase.PLAYER_PREPARE:
                raise PlayerPrepareError(
                    f"prepare {command} requires PLAYER_PREPARE, current phase is "
                    f"{state.phase.value}"
                )
            chosen = self._choose_seat(state, seat)
            player = state.players[chosen]
            try:
                record = self.sessions.records[chosen]
            except (KeyError, PlayerSessionError) as exc:
                raise PlayerPrepareError(
                    f"player session for seat {chosen} is unavailable"
                ) from exc

            previous = self._requests.get(chosen)
            if command == "next" and previous is not None:
                raise PlayerPrepareError(
                    f"TURN_IN_PROGRESS: seat {chosen} has an active READY request; "
                    "use prepare retry"
                )
            if command == "retry":
                if previous is None:
                    raise PlayerPrepareError(
                        f"REQUEST_EXPIRED: no active READY request can be retried for seat {chosen}"
                    )
                logical_request_id = previous.logical_request_id
                attempt_no = previous.attempt_no + 1
                # A runtime session accepts only one physical turn.  Abort the
                # old request before replacing its host-side binding.  A
                # malformed response can already have returned the runtime to
                # idle; that is still a safe replacement and is represented by
                # RuntimeRequestMismatchError.
                try:
                    await record.runtime.abort(previous.request_id)
                except RuntimeRequestMismatchError:
                    pass
                except Exception as exc:
                    raise PlayerPrepareError(
                        f"ABORT_FAILED: could not abort READY request {previous.request_id}"
                    ) from exc
                self._requests.pop(chosen, None)
            else:
                logical_request_id = f"prepare-seat-{chosen}"
                attempt_no = self._attempts.get(chosen, 0) + 1
            self._attempts[chosen] = attempt_no
            request_id = f"prepare-seat-{chosen}-attempt-{attempt_no}"
            started_at = self._aware_now()
            request = TurnRequest(
                request_id=request_id,
                logical_request_id=logical_request_id,
                attempt_no=attempt_no,
                game_id=state.game_id,
                session_epoch=player.session_epoch,
                phase=GamePhase.PLAYER_PREPARE,
                expected_kind=ResponseKind.READY,
                observation=Observation(
                    summary=(
                        "阅读当前板子与本人角色知识，并在完成 get_board 与 get_role "
                        "查询后提交 READY。"
                    ),
                    payload={
                        "required_tools": ["get_board", "get_role"],
                        "seat": chosen,
                    },
                ),
                output_schema=build_turn_response_schema(
                    ResponseKind.READY,
                    request_id,
                ),
                deadline=Deadline(
                    soft_deadline=started_at + self.turn_timeout / 2,
                    hard_deadline=started_at + self.turn_timeout,
                ),
            )
            self._requests[chosen] = request
            try:
                result = await asyncio.wait_for(
                    record.runtime.run_turn(request),
                    timeout=self.turn_timeout.total_seconds(),
                )
            except TimeoutError as exc:
                raise PlayerPrepareError(
                    f"seat {chosen} READY turn timed out; use prepare retry"
                ) from exc
            except Exception as exc:
                raise PlayerPrepareError(f"seat {chosen} READY turn failed: {exc}") from exc
            if not isinstance(result, RuntimeTurnResult):
                raise PlayerPrepareError(
                    f"seat {chosen} READY turn returned an invalid runtime result"
                )
            if result.request_id != request.request_id:
                raise PlayerPrepareError(
                    f"REQUEST_MISMATCH: seat {chosen} READY result request_id is stale"
                )
            if result.logical_request_id != request.logical_request_id:
                raise PlayerPrepareError(
                    f"REQUEST_MISMATCH: seat {chosen} READY result logical_request_id is stale"
                )
            if result.attempt_no != request.attempt_no:
                raise PlayerPrepareError(
                    f"REQUEST_MISMATCH: seat {chosen} READY result attempt_no is stale"
                )
            response = result.response
            if not isinstance(response, ReadyResponse):
                raise PlayerPrepareError(
                    f"seat {chosen} READY turn returned {response.kind!r}, expected 'ready'"
                )
            if response.request_id != request.request_id:
                raise PlayerPrepareError(
                    f"REQUEST_MISMATCH: seat {chosen} READY response request_id is stale"
                )

            claimed_ids = tuple(response.ready.knowledge_receipts)
            actual_receipts = self.sessions.receipts_for_seat(chosen)
            by_id = {receipt.receipt_id: receipt for receipt in actual_receipts}
            missing = tuple(receipt_id for receipt_id in claimed_ids if receipt_id not in by_id)
            if missing:
                raise PlayerPrepareError(
                    f"KNOWLEDGE_NOT_READY: seat {chosen} claimed unknown or foreign receipt IDs"
                )
            receipts = tuple(by_id[receipt_id] for receipt_id in claimed_ids)
            context = QueryContext(
                game_id=record.context.game_id,
                snapshot_id=record.bootstrap_card.snapshot_id,
                seat=record.context.seat,
                session_epoch=record.context.session_epoch,
            )
            gate = KnowledgeReadyGate(
                self.sessions.gateway.service,
                context,
                record.context.role_id or record.bootstrap_card.your_role.id,
            )
            try:
                gate.require_ready(receipts)
            except KnowledgeNotReadyError as exc:
                raise PlayerPrepareError(str(exc)) from exc

            try:
                committed = await self.manager.commit_player_ready(
                    seat=chosen,
                    session_epoch=player.session_epoch,
                    receipt_ids=claimed_ids,
                    expected_revision=state.state_revision,
                    now=self._aware_now(),
                )
            except (EventCommitError, ValueError, TypeError) as exc:
                raise PlayerPrepareError(str(exc)) from exc
            self._requests.pop(chosen, None)
            return {
                "status": "ready",
                "phase": committed.phase.value,
                "seat": chosen,
                "attempt_no": attempt_no,
                "receipt_ids": list(claimed_ids),
                "all_ready": all(
                    bool(item.knowledge_receipt_ids) for item in committed.players.values()
                ),
            }

    @staticmethod
    def _choose_seat(state: GameState, seat: int | None) -> int:
        players = state.players
        if seat is not None:
            if type(seat) is not int or not 1 <= seat <= 64:
                raise PlayerPrepareError("prepare seat must be between 1 and 64")
            player = players.get(seat)
            if player is None:
                raise PlayerPrepareError(f"seat {seat} is not a current player")
            if player.knowledge_receipt_ids:
                raise PlayerPrepareError(f"seat {seat} is already ready")
            return seat
        pending = [
            item_seat
            for item_seat, player in sorted(players.items())
            if not player.knowledge_receipt_ids
        ]
        if not pending:
            raise PlayerPrepareError("all players are already ready")
        return pending[0]

    def _aware_now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise PlayerPrepareError("prepare clock must return an aware datetime")
        return value.astimezone(UTC)


__all__ = ["PlayerPrepareError", "PlayerPrepareFlow"]
