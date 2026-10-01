"""Moderator orchestration for a board-defined sheriff badge window.

The badge decision is a player action with a deliberately small contract:
201 transfers to one frozen eligible seat and 202 tears the badge.  This flow
only opens the window, asks the current office holder, and forwards the
accepted request to the manager's serialized badge reducer.  It never chooses
a successor on behalf of the player.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from werewolf.domain.enums import GamePhase
from werewolf.game.action_turn import ActionTurnError, ActionTurnScheduler
from werewolf.game.actions import ActionValidationContext, ActionWindow
from werewolf.game.manager import (
    EventCommitError,
    GameManager,
    _sheriff_badge_allowed_phases,
)
from werewolf.game.state import GameState, PlayerState
from werewolf.knowledge.board import BoardDefinition
from werewolf.runtime.player_runtime import PlayerRuntime


class ModeratorBadgeError(RuntimeError):
    """A safe host-facing failure at the sheriff badge boundary."""


def _load_window(raw: object) -> ActionWindow:
    if not isinstance(raw, Mapping):
        raise ModeratorBadgeError("BADGE_WINDOW_INVALID: stored window is malformed")
    try:
        data = json.loads(json.dumps(raw))
        phase = data.get("phase")
        if isinstance(phase, str):
            data["phase"] = GamePhase(phase)
        return ActionWindow.model_validate(data)
    except (TypeError, ValueError) as exc:
        raise ModeratorBadgeError("BADGE_WINDOW_INVALID: stored window is malformed") from exc


def _trigger_origin(state: GameState) -> tuple[str, str] | None:
    """Recover the latest closed trigger operation from durable state."""

    for raw_window in reversed(tuple(state.action_windows.values())):
        try:
            window = _load_window(raw_window)
        except ModeratorBadgeError:
            continue
        visible = window.visible_context
        operation = visible.get("operation")
        resolution_id = visible.get("resolution_id")
        if (
            window.phase is GamePhase.TRIGGER_ACTION
            and window.closed_at is not None
            and operation in {"DAY_EXILE", "NIGHT_RESOLUTION"}
            and isinstance(resolution_id, str)
        ):
            return str(operation), resolution_id
    return None


class ModeratorSheriffBadgeFlow:
    """Drive one death or voting-rights invalidated sheriff decision."""

    def __init__(
        self,
        manager: GameManager,
        board: BoardDefinition,
        runtimes: Mapping[int, PlayerRuntime],
        *,
        timeout_seconds: float | None = None,
        clock: object | None = None,
    ) -> None:
        if not isinstance(manager, GameManager):
            raise TypeError("manager must be a GameManager")
        if not isinstance(board, BoardDefinition):
            raise TypeError("board must be a BoardDefinition")
        self._manager = manager
        self._board = board
        self._runtimes = dict(runtimes)
        self._timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._scheduler = ActionTurnScheduler(
            manager,
            self._runtimes,
            timeout_seconds=timeout_seconds,
        )

    @property
    def state(self) -> GameState:
        return self._manager.state

    @property
    def board(self) -> BoardDefinition:
        return self._board

    def _source(self) -> tuple[int, PlayerState]:
        state = self.state
        if state.phase not in _sheriff_badge_allowed_phases(state):
            raise ModeratorBadgeError(
                "BADGE_PHASE_REQUIRED: badge commands require a daytime boundary"
            )
        if state.phase is GamePhase.TRIGGER_ACTION:
            if state.pending_resolution is not None:
                raise ModeratorBadgeError(
                    "BADGE_PENDING: resolve the trigger action before opening the badge window"
                )
            origin = _trigger_origin(state)
            if origin is None or origin[0] != "DAY_EXILE":
                raise ModeratorBadgeError(
                    "BADGE_ORIGIN_INVALID: trigger badge requires a closed DAY_EXILE source"
                )
        source = state.sheriff_seat
        if type(source) is not int:
            raise ModeratorBadgeError("BADGE_SOURCE_INVALID: there is no current sheriff")
        player = state.players.get(source)
        if player is None:
            raise ModeratorBadgeError("BADGE_SOURCE_INVALID: current sheriff seat is unknown")
        policy = self._board.day_flow.sheriff
        if player.alive and player.can_vote:
            raise ModeratorBadgeError("BADGE_NOT_TRIGGERED: the sheriff is still eligible")
        if policy.transfer_enabled is not True:
            raise ModeratorBadgeError("BADGE_TRANSFER_DISABLED: badge transfer is disabled")
        if player.alive and policy.transfer_on_resignation is not True:
            raise ModeratorBadgeError("BADGE_TRANSFER_DISABLED: resignation transfer is disabled")
        if not player.alive and policy.transfer_on_death is not True:
            raise ModeratorBadgeError("BADGE_TRANSFER_DISABLED: death transfer is disabled")
        return source, player

    def _window(self) -> ActionWindow:
        marker = self.state.sheriff_badge
        if not isinstance(marker, dict) or marker.get("status") != "OPEN":
            raise ModeratorBadgeError("BADGE_NOT_OPEN: open the badge window first")
        window_id = marker.get("window_id")
        if not isinstance(window_id, str):
            raise ModeratorBadgeError("BADGE_STATE_INVALID: badge window ID is missing")
        raw = self.state.action_windows.get(window_id)
        if raw is None:
            raise ModeratorBadgeError("BADGE_WINDOW_NOT_FOUND: badge window is missing")
        return _load_window(raw)

    async def open(self, *, now: datetime | None = None) -> dict[str, object]:
        """Install or recover the current sheriff's exact badge contract."""

        source, player = self._source()
        marker = self.state.sheriff_badge
        if isinstance(marker, dict) and marker.get("status") == "OPEN":
            window = self._window()
            if marker.get("source_seat") != source:
                raise ModeratorBadgeError("BADGE_PENDING: another badge decision is active")
            return self.progress(window=window)
        candidates = tuple(
            sorted(
                seat
                for seat, candidate in self.state.players.items()
                if seat != source and candidate.alive and candidate.can_vote
            )
        )
        window_id = f"sheriff-badge-r{self.state.round_no}-d{self.state.day_no}-s{source}"
        window = ActionWindow(
            window_id=window_id,
            game_id=self.state.game_id,
            session_epoch=player.session_epoch,
            phase=self.state.phase,
            allowed_seats=(source,),
            allowed_action_codes=(201, 202),
            min_actions=1,
            max_actions=1,
            allow_pass=False,
            opened_at=self.state.updated_at,
            visible_context={
                "kind": "sheriff_badge",
                "source_seat": source,
                "candidate_seats": list(candidates),
                "transfer_action_code": 201,
                "tear_action_code": 202,
                "trigger": "death" if not player.alive else "resignation",
            },
        )
        try:
            committed = await self._manager.open_sheriff_badge_window(
                self._board,
                window,
                source_seat=source,
                expected_revision=self.state.state_revision,
                now=now or self._now(),
            )
        except (EventCommitError, RuntimeError, TypeError, ValueError) as exc:
            raise ModeratorBadgeError(str(exc)) from exc
        return self.progress(
            state=committed, window=_load_window(committed.action_windows[window_id])
        )

    def _context(self, window: ActionWindow, source: int) -> ActionValidationContext:
        state = self.state
        player = state.players[source]
        candidates = window.visible_context.get("candidate_seats")
        candidate_seats = (
            tuple(item for item in candidates if type(item) is int)
            if isinstance(candidates, (list, tuple))
            else ()
        )
        return ActionValidationContext(
            game_id=state.game_id,
            session_epoch=player.session_epoch,
            active_request_id="moderator-sheriff-badge-request",
            player_alive=True,
            player_qualified=True,
            role_id=player.role_id,
            authorized_action_codes=(201, 202),
            alive_seats=tuple(sorted(seat for seat, item in state.players.items() if item.alive)),
            eligible_targets_by_action={201: candidate_seats, 202: ()},
        )

    async def next(self) -> dict[str, object]:
        opened = await self.open()
        window = self._window()
        source = window.allowed_seats[0]
        state = self.state
        if state.players[source].current_request_id is not None:
            raise ModeratorBadgeError("BADGE_TURN_IN_PROGRESS: use badge retry")
        try:
            result = await self._scheduler.run_turn(window, source, self._context(window, source))
        except (ActionTurnError, RuntimeError, TypeError, ValueError) as exc:
            raise ModeratorBadgeError(str(exc)) from exc
        return {
            **opened,
            "action": {
                "status": "accepted",
                "seat": source,
                "request_id": result.request.request_id,
                "logical_request_id": result.request.logical_request_id,
                "attempt_no": result.request.attempt_no,
            },
        }

    async def retry(self) -> dict[str, object]:
        opened = await self.open()
        window = self._window()
        source = window.allowed_seats[0]
        if self.state.players[source].current_request_id is None:
            raise ModeratorBadgeError("BADGE_RETRY_NOT_AVAILABLE: no active badge request")
        try:
            result = await self._scheduler.run_turn(
                window,
                source,
                self._context(window, source),
                retry=True,
            )
        except (ActionTurnError, RuntimeError, TypeError, ValueError) as exc:
            raise ModeratorBadgeError(str(exc)) from exc
        return {
            **opened,
            "action": {
                "status": "accepted",
                "seat": source,
                "request_id": result.request.request_id,
                "logical_request_id": result.request.logical_request_id,
                "attempt_no": result.request.attempt_no,
            },
        }

    async def resolve(self, *, request_id: str | None = None) -> GameState:
        window = self._window()
        source = window.allowed_seats[0]
        candidates = [
            (request, payload)
            for request, payload in self.state.action_requests.items()
            if isinstance(payload, dict)
            and payload.get("window_id") == window.window_id
            and payload.get("seat") == source
            and payload.get("status") == "PENDING"
        ]
        if request_id is None:
            if len(candidates) != 1:
                raise ModeratorBadgeError(
                    "BADGE_REQUEST_PENDING: exactly one accepted badge request is required"
                )
            request_id = candidates[0][0]
        if not any(item[0] == request_id for item in candidates):
            raise ModeratorBadgeError(
                "BADGE_REQUEST_INVALID: request is not pending for badge window"
            )
        try:
            return await self._manager.commit_sheriff_badge_decision(
                self._board,
                request_id=request_id,
                expected_revision=self.state.state_revision,
                now=self._now(),
            )
        except (EventCommitError, RuntimeError, TypeError, ValueError) as exc:
            raise ModeratorBadgeError(str(exc)) from exc

    async def finish(self) -> GameState:
        if self.state.phase not in _sheriff_badge_allowed_phases(self.state):
            raise ModeratorBadgeError(
                "BADGE_PHASE_REQUIRED: badge completion requires a daytime boundary"
            )
        if (
            not isinstance(self.state.sheriff_badge, dict)
            or self.state.sheriff_badge.get("status") != "COMPLETE"
        ):
            raise ModeratorBadgeError("BADGE_PENDING: resolve the badge action first")
        if self.state.phase is GamePhase.TRIGGER_ACTION:
            marker = self.state.sheriff_badge
            origin = _trigger_origin(self.state)
            window_id = marker.get("window_id")
            raw_window = (
                self.state.action_windows.get(window_id) if isinstance(window_id, str) else None
            )
            if (
                origin is None
                or origin[0] != "DAY_EXILE"
                or not isinstance(raw_window, Mapping)
                or _load_window(raw_window).visible_context.get("origin") != "DAY_EXILE"
                or _load_window(raw_window).visible_context.get("origin_resolution_id") != origin[1]
            ):
                raise ModeratorBadgeError(
                    "BADGE_ORIGIN_INVALID: trigger badge requires a closed DAY_EXILE source"
                )
        try:
            return await self._manager.complete_sheriff_badge(
                expected_revision=self.state.state_revision,
                now=self._now(),
            )
        except (EventCommitError, RuntimeError, TypeError, ValueError) as exc:
            raise ModeratorBadgeError(str(exc)) from exc

    def progress(
        self, *, state: GameState | None = None, window: ActionWindow | None = None
    ) -> dict[str, object]:
        current = self.state if state is None else state
        marker = current.sheriff_badge
        marker_candidates = marker.get("candidate_seats") if isinstance(marker, dict) else None
        candidate_values = (
            list(marker_candidates) if isinstance(marker_candidates, (list, tuple)) else []
        )
        marker_window_id = marker.get("window_id") if isinstance(marker, dict) else None
        payload: dict[str, object] = {
            "phase": current.phase.value,
            "status": marker.get("status") if isinstance(marker, dict) else "NONE",
            "source_seat": marker.get("source_seat")
            if isinstance(marker, dict)
            else current.sheriff_seat,
            "target_seat": marker.get("target_seat") if isinstance(marker, dict) else None,
            "window_id": marker.get("window_id") if isinstance(marker, dict) else None,
            "candidate_seats": candidate_values,
            "pending_request": None,
        }
        active_window = window
        if active_window is None and isinstance(marker, dict) and isinstance(marker_window_id, str):
            raw = current.action_windows.get(marker_window_id)
            if raw is not None:
                try:
                    active_window = _load_window(raw)
                except ModeratorBadgeError:
                    active_window = None
        if active_window is not None:
            payload["window"] = active_window.model_dump(mode="json")
            pending = [
                request_id
                for request_id, raw in current.action_requests.items()
                if isinstance(raw, dict)
                and raw.get("window_id") == active_window.window_id
                and raw.get("status") == "PENDING"
            ]
            payload["pending_request"] = pending[0] if len(pending) == 1 else None
        return payload

    def _now(self) -> datetime:
        value = self._clock() if callable(self._clock) else datetime.now(UTC)
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ModeratorBadgeError("BADGE_TIME_INVALID: clock must return an aware datetime")
        return value.astimezone(UTC)


__all__ = ["ModeratorBadgeError", "ModeratorSheriffBadgeFlow"]
