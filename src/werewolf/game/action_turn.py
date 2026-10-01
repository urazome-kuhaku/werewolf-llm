"""Runtime scheduling for one authoritative skill action window.

The scheduler owns the orchestration boundary around ``PlayerRuntime``.  It
binds a physical request in ``GameManager`` before calling the untrusted
runtime and submits only a matching ``ActionResponse`` back through the
manager's authoritative action validator.  It never resolves an action or
spends a resource.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.role import TargetKind
from werewolf.runtime.player_runtime import (
    ActionResponse,
    ActionWindowView,
    Deadline,
    Observation,
    ObservationEvent,
    PlayerRuntime,
    ResponseKind,
    RuntimeProtocolError,
    RuntimeRequestMismatchError,
    RuntimeTurnResult,
    TurnRequest,
    build_turn_response_schema,
)

from .actions import Action as GameAction
from .actions import (
    ActionRequest,
    ActionValidationContext,
    ActionValidationError,
    ActionWindow,
    load_action_registry,
)
from .events import GameEvent
from .manager import EventCommitError, GameManager
from .state import GameState, GrantedAbility, GrantedTriggerAbility, PlayerState


class ActionTurnError(RuntimeError):
    """Base error for runtime action scheduling failures."""


class ActionTurnBusyError(ActionTurnError):
    """The seat already has a physical action request in progress."""


class ActionTurnTimeoutError(TimeoutError, ActionTurnError):
    """The runtime exceeded the hard deadline for an action request."""


class StaleActionResponse(ActionTurnError):
    """A response no longer belongs to the active physical request."""


@dataclass(frozen=True, slots=True)
class ActionTurnResult:
    """The accepted action request and the resulting immutable game state."""

    request: TurnRequest
    runtime_result: RuntimeTurnResult
    state: GameState


@dataclass(frozen=True, slots=True)
class _ActionBinding:
    seat: int
    logical_request_id: str
    attempt_no: int
    previous_request_id: str | None = None


class ActionTurnScheduler:
    """Run a player's action turn against an installed frozen window."""

    def __init__(
        self,
        manager: GameManager,
        runtimes: Mapping[int, PlayerRuntime],
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._manager = manager
        self._runtimes = dict(runtimes)
        self._timeout_seconds = timeout_seconds
        self._bindings: dict[str, _ActionBinding] = {}
        self._registry = load_action_registry()

    async def run_turn(
        self,
        window: ActionWindow,
        seat: int,
        context: ActionValidationContext,
        *,
        retry: bool = False,
    ) -> ActionTurnResult:
        """Call the bound runtime and commit its accepted action proposal.

        ``window`` and ``context`` are coordinator supplied snapshots.  The
        manager independently reloads the installed window and authoritative
        player facts before binding and again before accepting the proposal.
        """

        if not isinstance(window, ActionWindow):
            raise TypeError("window must be an ActionWindow")
        if not isinstance(context, ActionValidationContext):
            raise TypeError("context must be an ActionValidationContext")
        authoritative = await self._manager.get_action_window(window.window_id)
        if authoritative != window:
            raise ActionTurnError("WINDOW_STALE: supplied window differs from installed snapshot")

        state = await self._manager.snapshot()
        player = state.players.get(seat)
        if player is None:
            raise ActionTurnError("SEAT_NOT_ASSIGNED: action seat is not in the current game")
        runtime = self._runtimes.get(seat)
        if runtime is None:
            raise ActionTurnError(f"RUNTIME_MISSING: no runtime is registered for seat {seat}")
        try:
            runtime_ref = runtime.get_session_ref()
        except Exception as exc:
            raise ActionTurnError("RUNTIME_NOT_STARTED: start the seat runtime first") from exc
        if (
            runtime_ref.game_id != state.game_id
            or runtime_ref.seat != seat
            or runtime_ref.session_epoch != player.session_epoch
        ):
            raise ActionTurnError("SESSION_MISMATCH: runtime is not bound to the action seat")

        previous_request_id = player.current_request_id if retry else None
        if not retry and previous_request_id is not None:
            raise ActionTurnBusyError("TURN_IN_PROGRESS: use retry for the active action request")
        if retry and previous_request_id is None:
            raise StaleActionResponse("REQUEST_EXPIRED: no active action request can be retried")

        previous_binding = (
            self._bindings.get(previous_request_id) if previous_request_id is not None else None
        )
        logical_request_id = (
            previous_binding.logical_request_id
            if previous_binding is not None
            else self._logical_request_id(state, authoritative.window_id, seat)
        )
        attempt_no = (
            (previous_binding.attempt_no + 1)
            if previous_binding is not None
            else (2 if retry else 1)
        )
        request_id = self._physical_request_id(
            logical_request_id, attempt_no, state.state_revision + 1
        )

        if retry and previous_request_id is not None:
            try:
                await runtime.abort(previous_request_id)
            except RuntimeRequestMismatchError:
                # A timed out/cancelled runtime may already be idle.  The
                # durable manager binding still prevents the old response.
                pass
            except Exception as exc:
                raise ActionTurnError(
                    f"ABORT_FAILED: could not abort action request {previous_request_id}"
                ) from exc

        try:
            await self._manager.begin_action_turn(
                seat,
                player.session_epoch,
                window_id=authoritative.window_id,
                request_id=request_id,
                previous_request_id=previous_request_id,
                retry=retry,
            )
        except EventCommitError as exc:
            if "TURN_IN_PROGRESS" in str(exc):
                raise ActionTurnBusyError(str(exc)) from exc
            raise ActionTurnError(str(exc)) from exc

        visible = await self._manager.peek_delivery(seat, player.session_epoch)
        request = self._make_request(
            state,
            authoritative,
            seat=seat,
            session_epoch=player.session_epoch,
            request_id=request_id,
            logical_request_id=logical_request_id,
            attempt_no=attempt_no,
            context=context,
            events=visible,
        )
        self._bindings[request_id] = _ActionBinding(
            seat=seat,
            logical_request_id=logical_request_id,
            attempt_no=attempt_no,
            previous_request_id=previous_request_id,
        )
        try:
            if self._timeout_seconds is None:
                result = await runtime.run_turn(request)
            else:
                result = await asyncio.wait_for(
                    runtime.run_turn(request), timeout=self._timeout_seconds
                )
        except TimeoutError as exc:
            raise ActionTurnTimeoutError(
                f"runtime timed out for action request {request.request_id}"
            ) from exc
        return await self.commit_response(request, result, context)

    async def commit_response(
        self,
        request: TurnRequest,
        result: RuntimeTurnResult,
        context: ActionValidationContext,
    ) -> ActionTurnResult:
        """Validate one result and submit its action bundle atomically."""

        binding = self._bindings.get(request.request_id)
        if binding is None:
            raise StaleActionResponse("REQUEST_EXPIRED: request is not owned by this scheduler")
        if (
            result.request_id != request.request_id
            or result.logical_request_id != request.logical_request_id
            or result.attempt_no != request.attempt_no
            or binding.seat <= 0
        ):
            raise StaleActionResponse("REQUEST_MISMATCH: response belongs to another request")
        if not isinstance(result.response, ActionResponse):
            raise RuntimeProtocolError("action turn requires an action response")
        active = await self._manager.snapshot()
        player = active.players.get(binding.seat)
        if (
            player is None
            or player.current_request_id != request.request_id
            or player.session_epoch != request.session_epoch
            or active.phase != request.phase
        ):
            raise StaleActionResponse("REQUEST_EXPIRED: request is no longer active")

        try:
            action_request = ActionRequest(
                request_id=request.request_id,
                game_id=request.game_id,
                window_id=request.action_window.window_id if request.action_window else "",
                seat=binding.seat,
                session_epoch=request.session_epoch,
                phase=request.phase,
                actions=tuple(
                    GameAction.model_validate(action.model_dump(mode="python"))
                    for action in result.response.actions
                ),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeProtocolError(
                "action response could not be converted to a game request"
            ) from exc

        try:
            committed = await self._manager.commit_action_request(action_request, context)
        except ActionValidationError:
            # The manager deliberately retains the request binding so a
            # moderator can inspect or retry an invalid runtime proposal.
            raise
        return ActionTurnResult(request=request, runtime_result=result, state=committed)

    @staticmethod
    def _logical_request_id(state: GameState, window_id: str, seat: int) -> str:
        digest = hashlib.sha256(window_id.encode("utf-8")).hexdigest()[:12]
        return f"{state.game_id}-r{state.round_no}-action-{digest}-s{seat}"

    @staticmethod
    def _physical_request_id(logical_request_id: str, attempt_no: int, revision: int) -> str:
        return f"{logical_request_id}-a{attempt_no}-rev{revision}"[:128]

    def _make_request(
        self,
        state: GameState,
        window: ActionWindow,
        *,
        seat: int,
        session_epoch: int,
        request_id: str,
        logical_request_id: str,
        attempt_no: int,
        context: ActionValidationContext,
        events: tuple[GameEvent, ...],
    ) -> TurnRequest:
        now = datetime.now(UTC)
        timeout = self._timeout_seconds or 120.0
        allowed_action_codes, candidate_seats, summary = self._visible_action_window(
            state,
            window,
            seat=seat,
            context=context,
        )
        if not allowed_action_codes:
            raise ActionTurnError(
                f"NO_AUTHORIZED_ACTION: seat {seat} has no usable action in window "
                f"{window.window_id}"
            )
        observation = Observation(
            summary=summary,
            events=[
                ObservationEvent(
                    event_id=event.event_id,
                    event_type=str(event.event_type),
                    payload=event.payload.model_dump(mode="json"),
                )
                for event in events
            ],
            payload={"seat": seat, "phase": state.phase.value},
        )
        action_window_view = ActionWindowView(
            window_id=window.window_id,
            allowed_action_codes=list(allowed_action_codes),
            min_actions=max(1, window.min_actions),
            max_actions=max(1, window.max_actions),
            allow_pass=window.allow_pass,
            candidate_seats=candidate_seats,
        )
        return TurnRequest(
            request_id=request_id,
            logical_request_id=logical_request_id,
            attempt_no=attempt_no,
            game_id=state.game_id,
            session_epoch=session_epoch,
            phase=state.phase,
            expected_kind=ResponseKind.ACTION,
            action_window=action_window_view,
            observation=observation,
            output_schema=build_turn_response_schema(
                ResponseKind.ACTION,
                request_id,
                action_window=action_window_view,
            ),
            deadline=Deadline(
                soft_deadline=now,
                hard_deadline=now + timedelta(seconds=timeout),
            ),
        )

    def _visible_action_window(
        self,
        state: GameState,
        window: ActionWindow,
        *,
        seat: int,
        context: ActionValidationContext,
    ) -> tuple[tuple[int, ...], list[int], str]:
        """Build the least-privileged action view for one seat.

        The installed window is shared by the coordinator, while a runtime
        request is private to one seat.  Active night grants and trigger
        grants are therefore intersected with the seat's immutable state
        before any action code or candidate target reaches the runtime.  The
        manager repeats the same authorization at commit time; this helper is
        only a disclosure boundary.
        """

        player = state.players.get(seat)
        if player is None:
            raise ActionTurnError(f"SEAT_NOT_ASSIGNED: seat {seat} is not in the current game")

        # The sheriff badge is an office capability, not a role trigger.  It
        # may be installed in any of the three daytime boundary phases, so it
        # must be recognized before the phase-specific role branches below.
        if window.visible_context.get("kind") == "sheriff_badge":
            return self._visible_badge_window(window, context)

        if window.phase is GamePhase.TRIGGER_ACTION:
            return self._visible_trigger_window(state, window, player, context)
        if window.phase is GamePhase.NIGHT_ACTION:
            return self._visible_night_window(state, window, player, context)

        # Non-night action windows retain the coordinator's explicit
        # authorization context.  Day vote turns use their own scheduler, but
        # this fallback keeps generic callers compatible without exposing a
        # code that their context did not authorize.
        codes = tuple(
            code for code in window.allowed_action_codes if code in context.authorized_action_codes
        )
        if window.allow_pass and 299 in window.allowed_action_codes and 299 not in codes:
            codes = (*codes, 299)
        return codes, self._candidate_seats(state, window, context), ""

    @staticmethod
    def _visible_badge_window(
        window: ActionWindow,
        context: ActionValidationContext,
    ) -> tuple[tuple[int, ...], list[int], str]:
        """Build the same narrow badge view in every daytime boundary phase.

        A badge transfer has one target while tearing the badge has none.  The
        wire action window has one shared candidate list, therefore the
        per-action target shape is stated explicitly in the private summary;
        the authoritative manager still validates the selected action.
        """

        codes = tuple(
            code
            for code in (201, 202)
            if code in window.allowed_action_codes and code in context.authorized_action_codes
        )
        raw_candidates = window.visible_context.get("candidate_seats")
        supplied = context.eligible_targets_by_action.get(201, ())
        supplied_set = {
            seat for seat in supplied if isinstance(seat, int) and not isinstance(seat, bool)
        }
        candidates = sorted(
            {
                seat
                for seat in raw_candidates
                if isinstance(seat, int) and not isinstance(seat, bool) and seat in supplied_set
            }
            if isinstance(raw_candidates, (list, tuple))
            else set()
        )
        return (
            codes,
            candidates,
            "201→移交，targets=[一个candidate]；202→撕徽，targets=[]；不可PASS。",
        )

    def _visible_night_window(
        self,
        state: GameState,
        window: ActionWindow,
        player: PlayerState,
        context: ActionValidationContext,
    ) -> tuple[tuple[int, ...], list[int], str]:
        usable: list[tuple[GrantedAbility, tuple[int, ...]]] = []
        for ability in player.granted_abilities:
            if not self._usable_night_ability(player, window, ability):
                continue
            legal_targets = self._active_target_seats(state, player, ability, context)
            usable.append((ability, legal_targets))

        codes = tuple(ability.action_code for ability, _targets in usable)
        if window.allow_pass and 299 in window.allowed_action_codes:
            codes = (*codes, 299)
        candidates = sorted({seat for _ability, targets in usable for seat in targets})
        return tuple(dict.fromkeys(codes)), candidates, ""

    def _visible_trigger_window(
        self,
        state: GameState,
        window: ActionWindow,
        player: PlayerState,
        context: ActionValidationContext,
    ) -> tuple[tuple[int, ...], list[int], str]:
        raw_ability_id = window.visible_context.get("ability_id")
        raw_action_code = window.visible_context.get("action_code")
        ability = next(
            (
                item
                for item in player.granted_trigger_abilities
                if item.ability_id == raw_ability_id
                and item.action_code == raw_action_code
                and not item.consumed
            ),
            None,
        )
        if ability is None or ability.action_code not in window.allowed_action_codes:
            return (), [], ""

        codes = [ability.action_code]
        if ability.trigger.allow_pass and window.allow_pass and 299 in window.allowed_action_codes:
            codes.append(299)
        candidates = self._trigger_target_seats(state, window, player, ability, context)
        target_text = ", ".join(str(item) for item in candidates) if candidates else "无"
        if 299 in codes:
            summary = (
                f"触发技能窗口：请选择发动动作码 {ability.action_code} 并选择合法目标座位"
                f"（{target_text}），或提交 PASS 跳过。"
            )
        else:
            summary = (
                f"触发技能窗口：请发动动作码 {ability.action_code}；合法目标座位（{target_text}）。"
            )
        return tuple(codes), candidates, summary

    def _usable_night_ability(
        self,
        player: PlayerState,
        window: ActionWindow,
        ability: GrantedAbility,
    ) -> bool:
        if (
            ability.timing is not window.phase
            or window.phase not in ability.allowed_phases
            or ability.action_code not in window.allowed_action_codes
        ):
            return False
        limit = ability.usage_limit
        if (
            limit is not None
            and limit.max_uses is not None
            and ability.uses_consumed >= limit.max_uses
        ):
            return False
        try:
            definition = self._registry.get(ability.action_code)
        except KeyError:
            return False
        if definition.resource_id is None:
            return ability.resource is None
        if ability.resource is None or ability.resource.resource_id != definition.resource_id:
            return False
        return (
            player.skill_resources.get(ability.resource.resource_id, 0)
            >= ability.resource.cost_per_use
        )

    def _active_target_seats(
        self,
        state: GameState,
        player: PlayerState,
        ability: GrantedAbility,
        context: ActionValidationContext,
    ) -> tuple[int, ...]:
        rule = ability.target_rule
        if rule.kind is TargetKind.NONE:
            return ()
        try:
            definition = self._registry.get(ability.action_code)
        except KeyError:
            return ()
        candidate_seats = (
            (player.seat,) if rule.kind is TargetKind.SELF else tuple(sorted(state.players))
        )
        derived = {
            seat
            for seat in candidate_seats
            if seat in state.players
            and (state.players[seat].alive or rule.allow_dead)
            and (rule.allow_self or seat != player.seat)
            and not (
                definition.target_policy == "alive_non_authorized_wolf"
                and state.players[seat].faction_id == player.faction_id
            )
        }
        supplied = context.eligible_targets_by_action.get(ability.action_code)
        if supplied is not None:
            derived.intersection_update(supplied)
        return tuple(sorted(derived))

    @staticmethod
    def _trigger_target_seats(
        state: GameState,
        window: ActionWindow,
        player: PlayerState,
        ability: GrantedTriggerAbility,
        context: ActionValidationContext,
    ) -> list[int]:
        if ability.target_rule.kind is TargetKind.NONE:
            return []
        raw = window.visible_context.get("candidate_seats")
        candidates = (
            {item for item in raw if isinstance(item, int) and not isinstance(item, bool)}
            if isinstance(raw, (list, tuple))
            else set()
        )
        supplied = context.eligible_targets_by_action.get(ability.action_code)
        if supplied is not None:
            candidates.intersection_update(supplied)
        return sorted(
            seat
            for seat in candidates
            if seat in state.players
            and (state.players[seat].alive or ability.target_rule.allow_dead)
            and (ability.target_rule.allow_self or seat != player.seat)
        )

    @staticmethod
    def _candidate_seats(
        state: GameState,
        window: ActionWindow,
        context: ActionValidationContext,
    ) -> list[int]:
        raw = window.visible_context.get("candidate_seats")
        candidates: set[int] = set()
        if isinstance(raw, (list, tuple)):
            candidates.update(
                item for item in raw if isinstance(item, int) and not isinstance(item, bool)
            )
        if not candidates:
            for targets in context.eligible_targets_by_action.values():
                candidates.update(targets)
        alive = {seat for seat, player in state.players.items() if player.alive}
        return sorted(seat for seat in candidates if seat in alive)


__all__ = [
    "ActionTurnBusyError",
    "ActionTurnError",
    "ActionTurnResult",
    "ActionTurnScheduler",
    "ActionTurnTimeoutError",
    "StaleActionResponse",
]
