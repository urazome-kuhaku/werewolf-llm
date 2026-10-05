"""Moderator orchestration for board-defined trigger action windows.

The game manager owns trigger authorization, request validation, effect
application, and phase transitions.  This adapter only connects those
authoritative boundaries to one player runtime and exposes the pending
requests that a moderator must resolve explicitly.

Trigger abilities are selected from the immutable grants copied to the seat
at setup.  No role name is interpreted here.  The same flow therefore serves
death triggers discovered after a daytime exile and after a night resolution.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from werewolf.domain.enums import GamePhase
from werewolf.game.action_turn import (
    ActionTurnError,
    ActionTurnResult,
    ActionTurnScheduler,
)
from werewolf.game.actions import ActionRequest, ActionValidationContext, ActionWindow
from werewolf.game.day_resolution import build_trigger_action_window
from werewolf.game.manager import EventCommitError, GameManager, ResolutionError
from werewolf.game.resolution import (
    ActionDisposition,
    ActionResolution,
    ActionResolutionEntry,
    ResolutionStatus,
)
from werewolf.game.state import GameState, RuleHook, RuleWorkflowStep
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.role import TargetKind
from werewolf.runtime.player_runtime import PlayerRuntime


class ModeratorTriggerError(RuntimeError):
    """A safe, host-facing failure at the trigger action boundary."""


@dataclass(frozen=True, slots=True)
class TriggerActionProgress:
    """The installed generic trigger window and its durable provenance."""

    operation: str
    phase: GamePhase
    action_window: ActionWindow | None

    @property
    def window(self) -> ActionWindow:
        """Compatibility alias used by other moderator flow progress values."""

        if self.action_window is None:
            raise ModeratorTriggerError("TRIGGER_WINDOW_NOT_OPEN: no player choice is pending")
        return self.action_window

    @property
    def board_window_id(self) -> str:
        """Return the stable adapter-level window name."""

        return "trigger_action"


def _load_window(raw: object) -> ActionWindow:
    if not isinstance(raw, Mapping):
        raise ModeratorTriggerError("TRIGGER_WINDOW_INVALID: stored window is malformed")
    try:
        # State fields are recursively frozen containers.  A JSON round trip
        # gives Pydantic the ordinary list/dict wire representation expected
        # by the ActionWindow validators.
        data = json.loads(json.dumps(raw))
        phase = data.get("phase")
        if isinstance(phase, str):
            data["phase"] = GamePhase(phase)
        return ActionWindow.model_validate(data)
    except (TypeError, ValueError) as exc:
        raise ModeratorTriggerError("TRIGGER_WINDOW_INVALID: stored window is malformed") from exc


class ModeratorTriggerFlow:
    """Drive one pending, ability-defined trigger action boundary.

    ``next`` and ``retry`` call the actor's runtime.  A successful response is
    still only a validated request; ``resolve`` always requires an explicit
    :class:`ActionResolution` from the moderator.  ``finish`` advances to the
    phase dictated by the durable trigger provenance after that resolution.
    """

    _OPERATIONS = frozenset({"DAY_EXILE", "NIGHT_RESOLUTION"})

    def __init__(
        self,
        manager: GameManager,
        board: BoardDefinition,
        runtimes: Mapping[int, PlayerRuntime],
        *,
        timeout_seconds: float | None = None,
        clock: Any | None = None,
    ) -> None:
        if not isinstance(manager, GameManager):
            raise TypeError("manager must be a GameManager")
        if not isinstance(board, BoardDefinition):
            raise TypeError("board must be a BoardDefinition")
        self._manager = manager
        self._board = board
        self._runtimes = dict(runtimes)
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

    @property
    def scheduler(self) -> ActionTurnScheduler:
        return self._scheduler

    @property
    def _uses_rule_workflow(self) -> bool:
        if self._manager.execution_package is None:
            return False
        state = self.state
        cursor = state.rule_workflow_cursor
        if cursor is None:
            return False
        return bool(
            cursor.status != "IDLE"
            or cursor.active_occurrence_id is not None
            or cursor.pending_boundary_id is not None
            or cursor.pending_flow_action is not None
            or state.rule_trigger_queue
            or any(item.is_pending for item in state.rule_boundaries)
        )

    async def _advance_rule(
        self,
        *,
        hook_id: RuleHook | None = None,
        now: datetime | None = None,
    ) -> RuleWorkflowStep:
        state = await self._manager.snapshot()
        return await self._manager.advance_rule_workflow(
            expected_revision=state.state_revision,
            now=now or self._clock(),
            hook_id=hook_id,
        )

    async def _commit_rule_occurrence(self, step: RuleWorkflowStep) -> GameState:
        if step.kind not in {"AUTOMATIC", "PLAYER_CHOICE"} or step.occurrence_id is None:
            raise ModeratorTriggerError("RULE_OCCURRENCE_INVALID: no active occurrence to commit")
        state = await self._manager.snapshot()
        window = step.action_window
        group_id = (
            window.settlement_group_id
            if window is not None
            else state.rule_workflow_cursor.settlement_group_id
            if state.rule_workflow_cursor is not None
            else None
        )
        if not isinstance(group_id, str) or not group_id:
            raise ModeratorTriggerError("RULE_GROUP_MISSING: occurrence has no settlement group")
        try:
            return await self._manager.commit_rule_group(
                group_id,
                request_ids=None,
                occurrence_ids=(step.occurrence_id,),
                expected_revision=state.state_revision,
                now=self._clock(),
            )
        except (EventCommitError, ResolutionError, TypeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc

    async def _rule_choice(self, step: RuleWorkflowStep) -> ActionWindow:
        if step.kind != "PLAYER_CHOICE" or step.action_window is None:
            raise ModeratorTriggerError("RULE_CHOICE_MISSING: no player choice is ready")
        installed = await self._manager.get_action_window(step.action_window.window_id)
        if installed != step.action_window:
            raise ModeratorTriggerError("RULE_CHOICE_STALE: installed choice window changed")
        return installed

    def _rule_choice_context(self, seat: int, window: ActionWindow) -> ActionValidationContext:
        state = self.state
        player = state.players.get(seat)
        if player is None or seat not in window.allowed_seats:
            raise ModeratorTriggerError(
                "RULE_CHOICE_ACTOR_INVALID: actor is outside the choice window"
            )
        raw_candidates = window.visible_context.get("candidate_seats")
        candidates = tuple(
            sorted({item for item in raw_candidates if type(item) is int and item in state.players})
            if isinstance(raw_candidates, (list, tuple))
            else ()
        )
        actor_code = window.visible_context.get("rule_action_code")
        if type(actor_code) is not int or actor_code not in window.allowed_action_codes:
            raise ModeratorTriggerError(
                "RULE_CHOICE_INVALID: action is outside the installed window"
            )
        return ActionValidationContext(
            game_id=state.game_id,
            session_epoch=window.session_epoch,
            active_request_id="moderator-rule-choice-request",
            player_alive=player.alive,
            player_qualified=True,
            role_id=player.role_id,
            authorized_action_codes=(actor_code,),
            skill_resources=dict(player.skill_resources),
            alive_seats=tuple(
                sorted(seat_no for seat_no, item in state.players.items() if item.alive)
            ),
            eligible_targets_by_action={actor_code: candidates},
        )

    async def _run_rule_choice(
        self,
        step: RuleWorkflowStep,
        *,
        retry: bool = False,
        seat: int | None = None,
        now: datetime | None = None,
    ) -> tuple[ActionTurnResult, GameState]:
        window = await self._rule_choice(step)
        if len(window.allowed_seats) != 1:
            raise ModeratorTriggerError("RULE_CHOICE_INVALID: choice must authorize one actor")
        actor = window.allowed_seats[0]
        if seat is not None and seat != actor:
            raise ModeratorTriggerError(f"SEAT_NOT_ALLOWED: choice belongs to seat {actor}")
        context = self._rule_choice_context(actor, window)
        try:
            result = await self._scheduler.run_turn(
                window,
                actor,
                context,
                retry=retry,
            )
        except (ActionTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc
        request_id = result.request.request_id
        current = await self._manager.snapshot()
        try:
            current = await self._manager.complete_rule_window(
                window.window_id,
                expected_revision=current.state_revision,
                now=now or self._clock(),
            )
            group_id = window.settlement_group_id
            if group_id is None and current.rule_workflow_cursor is not None:
                group_id = current.rule_workflow_cursor.settlement_group_id
            if not group_id:
                raise ModeratorTriggerError("RULE_GROUP_MISSING: choice has no settlement group")
            current = await self._manager.commit_rule_group(
                group_id,
                request_ids=(request_id,),
                occurrence_ids=(step.occurrence_id,) if step.occurrence_id is not None else (),
                expected_revision=current.state_revision,
                now=now or self._clock(),
            )
        except (EventCommitError, ResolutionError, TypeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc
        return result, current

    async def _continue_rule_workflow(
        self,
        *,
        hook_id: RuleHook | None = None,
        execute_choices: bool = False,
        now: datetime | None = None,
    ) -> tuple[RuleWorkflowStep, ActionTurnResult | None]:
        """Drain manager-owned automatic occurrences and optionally choices."""

        step_limit = 512
        first = True
        last_action: ActionTurnResult | None = None
        for _ in range(step_limit):
            before_advance = await self._manager.snapshot()
            revision_before_advance = before_advance.state_revision
            step = await self._manager.advance_rule_workflow(
                expected_revision=revision_before_advance,
                now=now or self._clock(),
                hook_id=hook_id if first else None,
            )
            first = False
            if step.kind == "AUTOMATIC":
                await self._commit_rule_occurrence(step)
                continue
            if step.kind == "PLAYER_CHOICE":
                if not execute_choices:
                    return step, last_action
                existing = next(
                    (
                        (request_id, payload)
                        for request_id, payload in self.state.action_requests.items()
                        if isinstance(payload, dict)
                        and payload.get("window_id") == step.window_id
                        and payload.get("status") == "PENDING"
                    ),
                    None,
                )
                if existing is not None:
                    state = await self._manager.snapshot()
                    window = await self._rule_choice(step)
                    try:
                        state = await self._manager.complete_rule_window(
                            window.window_id,
                            expected_revision=state.state_revision,
                            now=now or self._clock(),
                        )
                        group_id = window.settlement_group_id
                        if group_id is None and state.rule_workflow_cursor is not None:
                            group_id = state.rule_workflow_cursor.settlement_group_id
                        if not group_id:
                            raise ModeratorTriggerError(
                                "RULE_GROUP_MISSING: choice has no settlement group"
                            )
                        state = await self._manager.commit_rule_group(
                            group_id,
                            request_ids=(existing[0],),
                            occurrence_ids=(step.occurrence_id,)
                            if step.occurrence_id is not None
                            else (),
                            expected_revision=state.state_revision,
                            now=now or self._clock(),
                        )
                    except (EventCommitError, ResolutionError, TypeError, ValueError) as exc:
                        raise ModeratorTriggerError(str(exc)) from exc
                else:
                    action, _state = await self._run_rule_choice(step, now=now)
                    last_action = action
                continue
            if step.kind == "IDLE" and step.queue_pending:
                state = await self._manager.snapshot()
                cursor = state.rule_workflow_cursor
                if cursor is not None and cursor.status == "DRAINING":
                    # A completed boundary may leave a later confirmed
                    # occurrence or boundary in the same durable workflow.
                    # Continue only when the manager says the cursor is
                    # actively draining; COLLECTING, WAITING_BOUNDARY, and
                    # ERROR are intentional pause points.
                    continue
            if step.kind == "IDLE":
                state = await self._manager.snapshot()
                cursor = state.rule_workflow_cursor
                if (
                    cursor is not None
                    and cursor.status == "RETURN_READY"
                    and state.state_revision > revision_before_advance
                    and (cursor.return_point is not None or cursor.pending_flow_action is not None)
                ):
                    # Closing the last typed boundary is a durable step of its
                    # own. Make one more manager call to execute the already
                    # proven return edge. Requiring a revision advance here
                    # keeps empty hook polls and stalled cursors from spinning.
                    continue
            return step, last_action
        raise ModeratorTriggerError("RULE_WORKFLOW_BUDGET_EXCEEDED: workflow did not drain")

    async def poll_hook(self, hook_id: RuleHook) -> dict[str, object]:
        """Poll an ordinary DAY_SPEECH hook and pause if a choice is due."""

        if self._manager.execution_package is None:
            return {"status": "idle", "hook_id": hook_id}
        if hook_id not in {"DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"}:
            raise ModeratorTriggerError("RULE_HOOK_INVALID: unsupported speech hook")
        state = self.state
        if state.phase is not GamePhase.DAY_SPEECH:
            raise ModeratorTriggerError(
                "RULE_HOOK_NOT_ALLOWED: speech hooks require ordinary DAY_SPEECH"
            )
        if any(boundary.is_pending for boundary in state.rule_boundaries):
            raise ModeratorTriggerError(
                "RULE_HOOK_NOT_ALLOWED: speech hooks are closed during a death boundary"
            )
        if hook_id == "DAY_SPEECH_BEFORE" and (
            not state.current_queue or state.serial_turn is not None
        ):
            raise ModeratorTriggerError(
                "RULE_HOOK_NOT_ALLOWED: BEFORE requires an unstarted ordinary speech turn"
            )
        if hook_id == "DAY_SPEECH_AFTER" and state.serial_turn is not None:
            raise ModeratorTriggerError(
                "RULE_HOOK_NOT_ALLOWED: AFTER requires a committed ordinary speech turn"
            )
        try:
            await self._manager.publish_due_rule_disclosures(
                hook_id,
                expected_revision=state.state_revision,
                now=self._clock(),
            )
        except (EventCommitError, ResolutionError, TypeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc
        try:
            step, _action = await self._continue_rule_workflow(hook_id=hook_id)
        except (EventCommitError, ResolutionError, TypeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc
        if step.kind == "PLAYER_CHOICE":
            return {
                "status": "choice_pending",
                "hook_id": hook_id,
                "occurrence_id": step.occurrence_id,
                "window_id": step.window_id,
                "phase": self.state.phase.value,
            }
        return {"status": "idle", "hook_id": hook_id, "phase": self.state.phase.value}

    def _pending(self) -> dict[str, object]:
        state = self.state
        if self._uses_rule_workflow:
            cursor = state.rule_workflow_cursor
            if cursor is None or cursor.active_occurrence_id is None:
                raise ModeratorTriggerError(
                    "TRIGGER_ACTION_MISSING: no manager-owned rule occurrence is active"
                )
            occurrence = next(
                (
                    item
                    for item in state.rule_trigger_queue
                    if item.occurrence_id == cursor.active_occurrence_id
                ),
                None,
            )
            if occurrence is None:
                raise ModeratorTriggerError("RULE_OCCURRENCE_INVALID: active occurrence is missing")
            window_id = occurrence.window_id
            if not isinstance(window_id, str) or not window_id:
                window_id = next(iter(cursor.active_window_ids), None)
            return {
                "status": "RULE_OCCURRENCE_REQUIRED",
                "operation": "RULE_HOOK" if occurrence.kind == "HOOK" else "RULE_TRIGGER",
                "occurrence_id": occurrence.occurrence_id,
                "window_id": window_id,
                "actor_seat": occurrence.actor_seat,
            }
        if state.phase is not GamePhase.TRIGGER_ACTION:
            raise ModeratorTriggerError(
                "TRIGGER_PHASE_REQUIRED: trigger commands require TRIGGER_ACTION"
            )
        pending = state.pending_resolution
        if not isinstance(pending, dict) or pending.get("status") != "TRIGGER_ACTION_REQUIRED":
            raise ModeratorTriggerError(
                "TRIGGER_ACTION_MISSING: no pending trigger action is available"
            )
        operation = pending.get("operation")
        if operation not in self._OPERATIONS:
            raise ModeratorTriggerError(
                "TRIGGER_OPERATION_INVALID: pending trigger has an unsupported operation"
            )
        return cast(dict[str, object], pending)

    def _require_phase(self) -> None:
        if self.state.phase is not GamePhase.TRIGGER_ACTION:
            raise ModeratorTriggerError(
                "TRIGGER_PHASE_REQUIRED: trigger commands require TRIGGER_ACTION"
            )

    def _current_window(self, *, require_open: bool = True) -> ActionWindow:
        pending = self._pending()
        raw_window_id = pending.get("window_id")
        if not isinstance(raw_window_id, str) or not raw_window_id:
            raise ModeratorTriggerError(
                "TRIGGER_WINDOW_NOT_OPEN: install the trigger action window first"
            )
        raw = self.state.action_windows.get(raw_window_id)
        if raw is None:
            raise ModeratorTriggerError(
                "TRIGGER_WINDOW_NOT_OPEN: install the trigger action window first"
            )
        window = _load_window(raw)
        if self._uses_rule_workflow:
            occurrence_id = pending.get("occurrence_id")
            cursor = self.state.rule_workflow_cursor
            if (
                window.phase is not GamePhase.TRIGGER_ACTION
                or window.visible_context.get("rule_occurrence_id") != occurrence_id
                or cursor is None
                or window.window_id not in cursor.active_window_ids
            ):
                raise ModeratorTriggerError(
                    "RULE_CHOICE_INVALID: window is outside the active occurrence"
                )
        elif window.phase is not GamePhase.TRIGGER_ACTION:
            raise ModeratorTriggerError("TRIGGER_WINDOW_INVALID: stored window has wrong phase")
        if require_open and (
            window.closed_at is not None or window.collection_complete_at is not None
        ):
            raise ModeratorTriggerError("TRIGGER_WINDOW_CLOSED: trigger action window is closed")
        return window

    def _submitted_seats(self, window: ActionWindow) -> set[int]:
        """Return actors with a durable pending request in this window."""

        return {
            seat
            for payload in self.state.action_requests.values()
            if isinstance(payload, Mapping)
            and payload.get("window_id") == window.window_id
            and payload.get("status") == "PENDING"
            and type(seat := payload.get("seat")) is int
        }

    async def open(self, *, now: datetime | None = None) -> TriggerActionProgress:
        """Install or return the marker-bound trigger window idempotently."""

        if self._uses_rule_workflow:
            step, _action = await self._continue_rule_workflow(now=now)
            if step.kind == "PLAYER_CHOICE":
                window = await self._rule_choice(step)
                operation = "RULE_HOOK" if step.hook_id is not None else "RULE_TRIGGER"
                return TriggerActionProgress(operation, GamePhase.TRIGGER_ACTION, window)
            return TriggerActionProgress("RULE_WORKFLOW", self.state.phase, None)

        pending = self._pending()
        legacy_operation = pending["operation"]
        if not isinstance(legacy_operation, str):  # pragma: no cover - _pending guards this
            raise ModeratorTriggerError("TRIGGER_OPERATION_INVALID: operation is not a string")
        operation = legacy_operation

        # The marker receives its window_id in the same manager commit as the
        # window.  If that binding already exists, simply recover it after a
        # process restart or repeated command.
        bound_window_id = pending.get("window_id")
        if isinstance(bound_window_id, str) and bound_window_id:
            window = self._current_window()
            return TriggerActionProgress(operation, GamePhase.TRIGGER_ACTION, window)

        state = self.state
        try:
            window = build_trigger_action_window(self._board, state)
            visible_context = dict(window.visible_context)
            # Keep provenance on the frozen window as well as in the pending
            # marker.  The marker is cleared when the action is resolved, so
            # this gives a restarted moderator enough information to select
            # the correct post-trigger phase.
            visible_context["operation"] = operation
            window = window.model_copy(update={"visible_context": visible_context})
            committed = await self._manager.commit_action_window(
                window,
                expected_revision=state.state_revision,
                now=now or self._clock(),
            )
        except (EventCommitError, TypeError, ValueError) as exc:
            # A concurrent opener may have installed the same marker-bound
            # window while this call was building its snapshot.  Re-read the
            # authoritative state once and recover that idempotent result.
            current = self.state
            current_pending = current.pending_resolution
            if (
                isinstance(current_pending, dict)
                and current_pending.get("status") == "TRIGGER_ACTION_REQUIRED"
                and current_pending.get("operation") == operation
                and isinstance(current_pending.get("window_id"), str)
            ):
                try:
                    recovered = self._current_window()
                except ModeratorTriggerError:
                    pass
                else:
                    return TriggerActionProgress(operation, GamePhase.TRIGGER_ACTION, recovered)
            raise ModeratorTriggerError(str(exc)) from exc

        installed = _load_window(committed.action_windows[window.window_id])
        return TriggerActionProgress(operation, GamePhase.TRIGGER_ACTION, installed)

    def _context_for(self, seat: int, window: ActionWindow) -> ActionValidationContext:
        state = self.state
        player = state.players.get(seat)
        if player is None:
            raise ModeratorTriggerError(f"SEAT_NOT_ASSIGNED: trigger seat {seat} is unknown")
        if self._uses_rule_workflow:
            return self._rule_choice_context(seat, window)
        ability_id = window.visible_context.get("ability_id")
        action_code = window.visible_context.get("action_code")
        ability = next(
            (
                item
                for item in player.granted_trigger_abilities
                if item.ability_id == ability_id
                and item.action_code == action_code
                and not item.consumed
            ),
            None,
        )
        if ability is None:
            raise ModeratorTriggerError(
                "TRIGGER_ABILITY_INVALID: the pending ability is not granted or is consumed"
            )
        candidates = window.visible_context.get("candidate_seats")
        candidate_seats = (
            tuple(
                item for item in candidates if isinstance(item, int) and not isinstance(item, bool)
            )
            if isinstance(candidates, (list, tuple))
            else ()
        )
        authorized = (
            (ability.action_code, 299) if ability.trigger.allow_pass else (ability.action_code,)
        )
        targets = () if ability.target_rule.kind is TargetKind.NONE else candidate_seats
        return ActionValidationContext(
            game_id=state.game_id,
            session_epoch=window.session_epoch,
            active_request_id="moderator-trigger-request",
            player_alive=player.alive,
            player_qualified=True,
            role_id=player.role_id,
            authorized_action_codes=authorized,
            skill_resources=dict(player.skill_resources),
            alive_seats=tuple(
                sorted(seat_no for seat_no, item in state.players.items() if item.alive)
            ),
            eligible_targets_by_action={ability.action_code: targets},
        )

    def _choose_seat(self, window: ActionWindow, seat: int | None) -> int:
        if len(window.allowed_seats) != 1:
            raise ModeratorTriggerError(
                "TRIGGER_WINDOW_INVALID: a trigger window must authorize exactly one seat"
            )
        selected = window.allowed_seats[0] if seat is None else seat
        if selected != window.allowed_seats[0]:
            raise ModeratorTriggerError(
                f"SEAT_NOT_ALLOWED: seat {selected} is not in trigger window"
            )
        player = self.state.players[selected]
        if player.current_request_id is not None:
            raise ModeratorTriggerError(
                f"TRIGGER_TURN_IN_PROGRESS: seat {selected} has an active request; use retry"
            )
        if self._submitted_requests(window, selected):
            raise ModeratorTriggerError(
                "TRIGGER_REQUEST_SUBMITTED: resolve the existing trigger request before continuing"
            )
        return selected

    def _choose_retry_seat(self, window: ActionWindow, seat: int | None) -> int:
        selected = window.allowed_seats[0] if seat is None else seat
        if selected != window.allowed_seats[0]:
            raise ModeratorTriggerError(
                f"SEAT_NOT_ALLOWED: seat {selected} is not in trigger window"
            )
        if self.state.players[selected].current_request_id is None:
            raise ModeratorTriggerError(
                "TRIGGER_RETRY_NOT_AVAILABLE: no active trigger request can be retried"
            )
        return selected

    def _submitted_requests(self, window: ActionWindow, seat: int) -> tuple[dict[str, object], ...]:
        records: list[dict[str, object]] = []
        for payload in self.state.action_requests.values():
            if not isinstance(payload, dict):
                continue
            if payload.get("window_id") == window.window_id and payload.get("seat") == seat:
                records.append(cast(dict[str, object], payload))
        return tuple(records)

    def _stored_request(
        self,
        request_id: str,
        payload: object,
        window: ActionWindow,
    ) -> ActionRequest:
        """Validate a trigger acknowledgement against its exact stored request."""

        if not isinstance(payload, Mapping):
            raise ModeratorTriggerError("TRIGGER_REQUEST_INVALID: stored request is malformed")
        if (
            payload.get("request_id") != request_id
            or payload.get("status") != "PENDING"
            or payload.get("window_id") != window.window_id
            or payload.get("seat") != window.allowed_seats[0]
        ):
            raise ModeratorTriggerError(
                "TRIGGER_REQUEST_INVALID: stored request is outside the bound trigger window"
            )
        data = {key: payload[key] for key in ActionRequest.model_fields if key in payload}
        request_data = json.loads(json.dumps(data))
        if isinstance(request_data.get("phase"), str):
            request_data["phase"] = GamePhase(request_data["phase"])
        try:
            request = ActionRequest.model_validate(request_data)
        except (TypeError, ValueError) as exc:
            raise ModeratorTriggerError(
                "TRIGGER_REQUEST_INVALID: stored request is malformed"
            ) from exc
        actor = self.state.players.get(request.seat)
        if (
            actor is None
            or request.game_id != self.state.game_id
            or request.window_id != window.window_id
            or request.phase is not GamePhase.TRIGGER_ACTION
            or request.session_epoch != window.session_epoch
            or request.session_epoch != actor.session_epoch
            or actor.current_request_id != request.request_id
            or request.seat != window.allowed_seats[0]
            or len(request.actions) != 1
        ):
            raise ModeratorTriggerError(
                "TRIGGER_REQUEST_INVALID: request does not match its actor, session, and window"
            )
        action = request.actions[0]
        if action.action_code not in window.allowed_action_codes:
            raise ModeratorTriggerError(
                "TRIGGER_REQUEST_INVALID: action is outside the frozen trigger window"
            )
        return request

    def _rule_acknowledgement(
        self,
        window: ActionWindow,
        request_id: str,
        payload: object,
    ) -> ActionResolution:
        """Build a neutral receipt; only the pinned interpreter decides effects."""

        request = self._stored_request(request_id, payload, window)
        digest = hashlib.sha256(
            f"{self.state.game_id}:{window.window_id}:{request.request_id}".encode()
        ).hexdigest()[:32]
        return ActionResolution(
            resolution_id=f"rule-ack-{digest}",
            bundle_id=f"rule-bundle-{digest}",
            game_id=request.game_id,
            window_id=window.window_id,
            request_id=request.request_id,
            session_epoch=request.session_epoch,
            base_revision=self.state.state_revision,
            status=ResolutionStatus.CONFIRMED,
            actions=tuple(
                ActionResolutionEntry(
                    action_index=index,
                    requested_action=action,
                    disposition=ActionDisposition.CONFIRMED,
                )
                for index, action in enumerate(request.actions)
            ),
            moderator_id="rule-interpreter",
            reason="neutral trigger receipt; frozen package interpreter decides effects",
            # The accepted request updated the authoritative state timestamp.
            # Rebuilding an acknowledgement after a transport retry therefore
            # produces the same immutable receipt.
            created_at=self.state.updated_at,
        )

    @staticmethod
    def _turn_payload(result: ActionTurnResult, seat: int, operation: str) -> dict[str, object]:
        return {
            "status": "accepted",
            "operation": operation,
            "seat": seat,
            "request_id": result.request.request_id,
            "logical_request_id": result.request.logical_request_id,
            "attempt_no": result.request.attempt_no,
            "window_id": result.request.action_window.window_id
            if result.request.action_window is not None
            else None,
            "phase": result.state.phase.value,
        }

    async def next(self, seat: int | None = None) -> dict[str, object]:
        """Ask the trigger actor for a skill choice or an explicit PASS."""

        if self._uses_rule_workflow:
            step, _action = await self._continue_rule_workflow()
            if step.kind != "PLAYER_CHOICE":
                raise ModeratorTriggerError("RULE_CHOICE_MISSING: no player choice is pending")
            result, _state = await self._run_rule_choice(step, seat=seat)
            next_step, _next_action = await self._continue_rule_workflow(hook_id=step.hook_id)
            return {
                "status": "accepted",
                "operation": "RULE_HOOK" if step.hook_id is not None else "RULE_TRIGGER",
                "seat": step.actor_seat,
                "request_id": result.request.request_id,
                "window_id": result.request.action_window.window_id
                if result.request.action_window is not None
                else step.window_id,
                "phase": self.state.phase.value,
                "next_step": next_step.kind,
            }

        progress = await self.open()
        window = progress.action_window
        if window is None:
            raise ModeratorTriggerError("TRIGGER_WINDOW_MISSING: no action window is installed")
        selected = self._choose_seat(window, seat)
        try:
            result = await self._scheduler.run_turn(
                window,
                selected,
                self._context_for(selected, window),
            )
        except (ActionTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc
        return self._turn_payload(result, selected, progress.operation)

    async def retry(self, seat: int | None = None) -> dict[str, object]:
        """Retry the active physical trigger request without changing its seat."""

        if self._uses_rule_workflow:
            step, _action = await self._continue_rule_workflow()
            if step.kind != "PLAYER_CHOICE":
                raise ModeratorTriggerError("RULE_CHOICE_MISSING: no player choice is pending")
            result, _state = await self._run_rule_choice(step, retry=True, seat=seat)
            next_step, _next_action = await self._continue_rule_workflow(hook_id=step.hook_id)
            return {
                "status": "accepted",
                "operation": "RULE_HOOK" if step.hook_id is not None else "RULE_TRIGGER",
                "seat": step.actor_seat,
                "request_id": result.request.request_id,
                "window_id": step.window_id,
                "phase": self.state.phase.value,
                "next_step": next_step.kind,
            }

        progress = await self.open()
        window = progress.action_window
        if window is None:
            raise ModeratorTriggerError("TRIGGER_WINDOW_MISSING: no action window is installed")
        selected = self._choose_retry_seat(window, seat)
        try:
            result = await self._scheduler.run_turn(
                window,
                selected,
                self._context_for(selected, window),
                retry=True,
            )
        except (ActionTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc
        return self._turn_payload(result, selected, progress.operation)

    def pending(self) -> dict[str, object]:
        """Return moderator-only, unresolved trigger requests."""

        if self._uses_rule_workflow:
            pending = self._pending()
            window = self._current_window()
            rule_requests: list[dict[str, object]] = []
            for request_id, payload in self.state.action_requests.items():
                if (
                    isinstance(payload, dict)
                    and payload.get("window_id") == window.window_id
                    and payload.get("status") == "PENDING"
                ):
                    rule_requests.append({"request_id": request_id, "seat": payload.get("seat")})
            return {
                "operation": pending["operation"],
                "occurrence_id": pending["occurrence_id"],
                "window": window.model_dump(mode="json"),
                "pending_requests": rule_requests,
            }

        pending = self._pending()
        window = self._current_window()
        requests: list[dict[str, object]] = []
        for request_id, payload in self.state.action_requests.items():
            if not isinstance(payload, dict):
                continue
            if payload.get("window_id") != window.window_id or payload.get("status") != "PENDING":
                continue
            if payload.get("request_id") != request_id:
                raise ModeratorTriggerError(
                    "TRIGGER_REQUEST_INVALID: stored request ID is inconsistent"
                )
            actions = payload.get("actions")
            if not isinstance(actions, (list, tuple)) or not actions:
                raise ModeratorTriggerError("TRIGGER_REQUEST_INVALID: stored actions are malformed")
            requests.append(
                {
                    "request_id": request_id,
                    "bundle_id": request_id,
                    "game_id": payload.get("game_id"),
                    "window_id": payload.get("window_id"),
                    "seat": payload.get("seat"),
                    "session_epoch": payload.get("session_epoch"),
                    "base_revision": self.state.state_revision,
                    "actions": json.loads(json.dumps(actions)),
                }
            )
        return {
            "phase": self.state.phase.value,
            "operation": pending.get("operation"),
            "window_id": window.window_id,
            "base_revision": self.state.state_revision,
            "pending_requests": requests,
            "private": True,
            "sensitive": True,
        }

    @staticmethod
    def _normalize_resolutions(
        resolutions: ActionResolution | Sequence[ActionResolution],
    ) -> tuple[ActionResolution, ...]:
        if isinstance(resolutions, ActionResolution):
            return (resolutions,)
        if isinstance(resolutions, (str, bytes)):
            raise ModeratorTriggerError(
                "TRIGGER_RESOLUTION_INVALID: expected ActionResolution values"
            )
        try:
            normalized = tuple(resolutions)
        except TypeError as exc:
            raise ModeratorTriggerError(
                "TRIGGER_RESOLUTION_INVALID: expected ActionResolution values"
            ) from exc
        if not normalized or any(not isinstance(item, ActionResolution) for item in normalized):
            raise ModeratorTriggerError(
                "TRIGGER_RESOLUTION_INVALID: expected ActionResolution values"
            )
        return normalized

    async def resolve(
        self,
        resolutions: ActionResolution | Sequence[ActionResolution],
        *,
        now: datetime | None = None,
    ) -> GameState:
        """Commit explicit moderator resolutions for current trigger requests."""

        if self._uses_rule_workflow:
            del resolutions, now
            raise ModeratorTriggerError(
                "RULE_RESOLUTION_UNAVAILABLE: executable occurrences must run through "
                "ActionTurnScheduler and manager rule-group commit"
            )

        self._pending()
        window = self._current_window()
        normalized = self._normalize_resolutions(resolutions)
        if any(item.window_id != window.window_id for item in normalized):
            raise ModeratorTriggerError("WINDOW_MISMATCH: trigger resolution uses another window")
        try:
            return (
                await self._manager.commit_action_resolutions(
                    normalized,
                    expected_revision=self.state.state_revision,
                    now=now or self._clock(),
                )
                if len(normalized) > 1
                else await self._manager.commit_action_resolution(
                    normalized[0],
                    expected_revision=self.state.state_revision,
                    now=now or self._clock(),
                )
            )
        except (EventCommitError, ResolutionError, TypeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc

    async def auto_resolve(self) -> dict[str, object]:
        """Run the trigger actor and commit through the frozen execution package."""

        if self._uses_rule_workflow:
            step, action = await self._continue_rule_workflow(execute_choices=True)
            return {
                "status": "resolved" if action is not None else "idle",
                "operation": "RULE_WORKFLOW",
                "next_step": step.kind,
                "boundary_id": step.boundary.boundary_id if step.boundary is not None else None,
                "phase": self.state.phase.value,
            }

        if self._manager.execution_package is None:
            state = self.state
            if (
                state.execution_identity is not None
                or state.ability_instances
                or state.rule_state
                or state.rule_ledger
                or state.rule_receipts
            ):
                raise ModeratorTriggerError(
                    "TRIGGER_AUTO_RESOLVE_UNAVAILABLE: executable history requires "
                    "its pinned package"
                )
            from werewolf.moderator.classic_resolution import CLASSIC_BOARD_ID

            if self._board.board_id != CLASSIC_BOARD_ID:
                raise ModeratorTriggerError(
                    "TRIGGER_AUTO_RESOLVE_UNAVAILABLE: board is unsupported"
                )

        progress = await self.open()
        action_window = progress.action_window
        if action_window is None:
            raise ModeratorTriggerError("TRIGGER_WINDOW_NOT_OPEN: action window is missing")
        pending = self.pending()
        raw_requests = pending.get("pending_requests")
        if not isinstance(raw_requests, list) or not raw_requests:
            await self.next()
            pending = self.pending()
            raw_requests = pending.get("pending_requests")
        if not isinstance(raw_requests, list) or len(raw_requests) != 1:
            raise ModeratorTriggerError(
                "TRIGGER_REQUEST_INVALID: exactly one actor request is required"
            )
        request_envelope = raw_requests[0]
        if not isinstance(request_envelope, Mapping):
            raise ModeratorTriggerError("TRIGGER_REQUEST_INVALID: request envelope is malformed")
        request_id = request_envelope.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ModeratorTriggerError("TRIGGER_REQUEST_INVALID: request ID is missing")

        if self._manager.execution_package is not None:
            raw = self.state.action_requests.get(request_id)
            receipt = self._rule_acknowledgement(
                action_window,
                request_id,
                raw,
            )
            try:
                await self._manager.commit_action_resolutions(
                    (receipt,),
                    use_rules_engine=True,
                    expected_revision=self.state.state_revision,
                    now=self._clock(),
                )
            except (EventCommitError, ResolutionError, TypeError, ValueError) as exc:
                raise ModeratorTriggerError(str(exc)) from exc
            resolution_id = receipt.resolution_id
        else:
            from werewolf.moderator.classic_resolution import build_classic_trigger_resolution

            try:
                stored = self.state.action_requests.get(request_id)
                request = self._stored_request(request_id, stored, action_window)
                resolution = build_classic_trigger_resolution(self.state, request)
            except (TypeError, ValueError) as exc:
                raise ModeratorTriggerError(str(exc)) from exc
            await self.resolve(resolution)
            resolution_id = resolution.resolution_id
        return {
            "status": "resolved",
            "operation": progress.operation,
            "window_id": action_window.window_id,
            "request_id": request_id,
            "resolution_id": resolution_id,
            "phase": self.state.phase.value,
        }

    def _origin(self, window: ActionWindow) -> str:
        window_operation = window.visible_context.get("operation")
        if isinstance(window_operation, str) and window_operation in self._OPERATIONS:
            return window_operation
        raw_operation = self.state.pending_resolution
        if (
            isinstance(raw_operation, dict)
            and isinstance(raw_operation.get("operation"), str)
            and raw_operation["operation"] in self._OPERATIONS
        ):
            return str(raw_operation["operation"])
        resolution_id = window.visible_context.get("resolution_id")
        if not isinstance(resolution_id, str):
            raise ModeratorTriggerError("TRIGGER_OPERATION_INVALID: trigger provenance is missing")
        for audit in reversed(self.state.moderator_audit):
            if not isinstance(audit, dict) or audit.get("resolution_id") != resolution_id:
                continue
            operation = audit.get("operation")
            if operation == "DAY_EXILE":
                return "DAY_EXILE"
            if operation == "ACTION_RESOLUTION":
                return "NIGHT_RESOLUTION"
        raise ModeratorTriggerError("TRIGGER_OPERATION_INVALID: trigger provenance is unknown")

    def _completion_window(self) -> tuple[ActionWindow, str]:
        """Validate and return the closed trigger boundary and its source.

        The pending resolution marker is intentionally checked before loading
        the window.  A successful resolution clears that marker, so the
        durable action window and its provenance are the source of truth for
        the completion edge and for callers that need to order adjacent day
        work.
        """

        self._require_phase()
        if self.state.pending_resolution is not None:
            raise ModeratorTriggerError(
                "TRIGGER_ACTION_PENDING: resolve the pending trigger request first"
            )
        trigger_windows = [
            _load_window(raw)
            for raw in self.state.action_windows.values()
            if isinstance(raw, Mapping) and raw.get("phase") == GamePhase.TRIGGER_ACTION.value
        ]
        if not trigger_windows:
            raise ModeratorTriggerError(
                "TRIGGER_WINDOW_NOT_OPEN: no trigger action window has been installed"
            )
        if len(trigger_windows) != 1:
            raise ModeratorTriggerError(
                "TRIGGER_WINDOW_INVALID: multiple trigger action windows are installed"
            )
        window = trigger_windows[0]
        if window.closed_at is None:
            raise ModeratorTriggerError(
                "TRIGGER_ACTION_PENDING: the trigger request must be resolved, including PASS"
            )
        return window, self._origin(window)

    def completion_origin(self) -> str:
        """Return the validated source operation for the closed trigger edge."""

        if self._uses_rule_workflow:
            return "RULE_WORKFLOW"

        _window, origin = self._completion_window()
        return origin

    def finish_target(self) -> GamePhase:
        """Return the validated phase target for the closed trigger edge."""

        if self._uses_rule_workflow:
            return self.state.phase

        _window, origin = self._completion_window()
        return GamePhase.VICTORY_CHECK if origin == "DAY_EXILE" else GamePhase.DAY_ANNOUNCE

    async def finish(self, *, now: datetime | None = None) -> GameState:
        """Advance after every trigger request has an explicit resolution."""

        if self._uses_rule_workflow:
            step, _action = await self._continue_rule_workflow(now=now)
            if step.kind == "PLAYER_CHOICE":
                raise ModeratorTriggerError(
                    "RULE_CHOICE_PENDING: run trigger next or auto-resolve before finish"
                )
            state = await self._manager.snapshot()
            cursor = state.rule_workflow_cursor
            if step.kind == "RETURN" and step.boundary is not None and step.boundary.is_pending:
                raise ModeratorTriggerError(
                    "RULE_BOUNDARY_PENDING: complete the death boundary before finish"
                )
            if cursor is not None and cursor.status in {
                "COLLECTING",
                "ERROR",
                "WAITING_BOUNDARY",
                "RETURN_READY",
            }:
                code = cursor.error_code or cursor.status
                raise ModeratorTriggerError(
                    f"RULE_WORKFLOW_PENDING: cannot finish while workflow is {code}"
                )
            return state

        target = self.finish_target()
        try:
            return await self._manager.commit_phase_transition(
                target,
                expected_revision=self.state.state_revision,
                now=now or self._clock(),
            )
        except (EventCommitError, TypeError, ValueError) as exc:
            raise ModeratorTriggerError(str(exc)) from exc

    def progress(self) -> dict[str, object]:
        """Return moderator-safe progress, omitting action effects."""

        if self._uses_rule_workflow:
            state = self.state
            cursor = state.rule_workflow_cursor
            occurrence = None
            window: ActionWindow | None = None
            if cursor is not None and cursor.active_occurrence_id is not None:
                occurrence = next(
                    (
                        item
                        for item in state.rule_trigger_queue
                        if item.occurrence_id == cursor.active_occurrence_id
                    ),
                    None,
                )
                if occurrence is not None and occurrence.window_id is not None:
                    raw_window = state.action_windows.get(occurrence.window_id)
                    if raw_window is not None:
                        try:
                            window = _load_window(raw_window)
                        except ModeratorTriggerError:
                            window = None
            return {
                "phase": state.phase.value,
                "operation": None
                if occurrence is None
                else "RULE_HOOK"
                if occurrence.kind == "HOOK"
                else "RULE_TRIGGER",
                "occurrence_id": None if occurrence is None else occurrence.occurrence_id,
                "queue_pending": bool(
                    cursor is not None and cursor.status in {"DRAINING", "WAITING_CHOICE"}
                ),
                "window": None if window is None else window.model_dump(mode="json"),
                "submitted_seats": [] if window is None else sorted(self._submitted_seats(window)),
                "boundary_id": None
                if cursor is None or cursor.pending_boundary_id is None
                else cursor.pending_boundary_id,
            }

        payload: dict[str, object] = {
            "phase": self.state.phase.value,
            "operation": None,
            "window": None,
            "submitted_seats": [],
        }
        if self.state.phase is not GamePhase.TRIGGER_ACTION:
            return payload
        pending = self.state.pending_resolution
        if isinstance(pending, dict):
            payload["operation"] = pending.get("operation")
            window_id = pending.get("window_id")
            if isinstance(window_id, str) and window_id:
                raw = self.state.action_windows.get(window_id)
                if raw is not None:
                    try:
                        window = _load_window(raw)
                    except ModeratorTriggerError:
                        payload["window"] = {"window_id": window_id, "status": "INVALID"}
                    else:
                        payload["window"] = window.model_dump(mode="json")
                        submitted_seats: set[int] = set()
                        for record in self.state.action_requests.values():
                            if not isinstance(record, dict):
                                continue
                            if record.get("window_id") != window.window_id:
                                continue
                            seat_value = record.get("seat")
                            if isinstance(seat_value, int) and not isinstance(seat_value, bool):
                                submitted_seats.add(seat_value)
                        payload["submitted_seats"] = sorted(submitted_seats)
        return payload


__all__ = ["ModeratorTriggerError", "ModeratorTriggerFlow", "TriggerActionProgress"]
