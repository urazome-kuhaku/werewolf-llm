"""Moderator adapter for board-defined night windows.

The game coordinator owns the authoritative window lifecycle and the action
turn scheduler owns runtime request binding.  This module only derives the
window union from the frozen per-seat grants and translates those primitives
into a small moderator command surface.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from werewolf.domain.enums import GamePhase
from werewolf.game.action_turn import (
    ActionTurnError,
    ActionTurnResult,
    ActionTurnScheduler,
)
from werewolf.game.actions import (
    ActionDefinition,
    ActionRegistry,
    ActionValidationContext,
    ActionWindow,
    load_action_registry,
)
from werewolf.game.events import (
    EventType,
    GameEvent,
    PrivateNoticePayload,
    PrivateWitchTargetPayload,
    TeamNoticePayload,
)
from werewolf.game.manager import EventCommitError, GameManager
from werewolf.game.night import (
    NightCoordinator,
    NightCoordinatorError,
    NightWindowConfig,
    NightWindowProgress,
)
from werewolf.game.resolution import ActionResolution
from werewolf.game.serial_turn import (
    SerialSpeechResult,
    SerialTurnError,
    SerialTurnScheduler,
)
from werewolf.game.state import GameState, GrantedAbility, PlayerState
from werewolf.knowledge.board import BoardDefinition, NightWindow
from werewolf.moderator.classic_resolution import build_classic_night_resolutions
from werewolf.runtime.player_runtime import PlayerRuntime


class ModeratorNightError(RuntimeError):
    """A safe, user-facing failure at the moderator night boundary."""


def _load_window(raw: object) -> ActionWindow:
    if not isinstance(raw, Mapping):
        raise ModeratorNightError("stored night window is malformed")
    try:
        data = json.loads(json.dumps(raw))
        phase = data.get("phase")
        if isinstance(phase, str):
            data["phase"] = GamePhase(phase)
        return ActionWindow.model_validate(data)
    except ValueError as exc:
        raise ModeratorNightError("stored night window is malformed") from exc


class ModeratorNightFlow:
    """Bind one running moderator game to its night coordinator."""

    def __init__(
        self,
        manager: GameManager,
        board: BoardDefinition,
        runtimes: Mapping[int, PlayerRuntime],
        *,
        snapshot_id: str | None = None,
        timeout_seconds: float | None = None,
        clock: Any | None = None,
    ) -> None:
        self._manager = manager
        self._board = board
        self._clock = clock or (lambda: datetime.now(UTC))
        self._snapshot_id = snapshot_id
        self._wolf_coordinator_seat: int | None = None
        self._window_configs: dict[str, NightWindowConfig] = {}
        self._runtimes = dict(runtimes)
        self.coordinator: NightCoordinator
        self._refresh_configuration(manager.state)
        self.scheduler = ActionTurnScheduler(
            manager,
            runtimes,
            timeout_seconds=timeout_seconds,
        )
        # ``GameState.current_queue`` is shared by all serialized speech
        # phases and is intentionally left as an empty tuple after a queue is
        # drained.  Keep the physical team-window identity alongside it so a
        # newly opened window in the next round can start a fresh queue while
        # a repeated ``team next`` in the same window still fails closed.
        self._team_queue_window_id: str | None = None
        self.team_scheduler = SerialTurnScheduler(
            manager,
            runtimes,
            timeout_seconds=timeout_seconds,
            phase=GamePhase.NIGHT_TEAM_CHAT,
        )

    @property
    def state(self) -> GameState:
        return self._manager.state

    @property
    def wolf_coordinator_seat(self) -> int | None:
        # The coordinator is a projection of the current authoritative player
        # state.  A new night can remove a previously selected wolf (or make a
        # grant unusable), so do not expose the constructor-time projection.
        self._refresh_configuration(self._manager.state)
        return self._wolf_coordinator_seat

    def _refresh_configuration(self, state: GameState) -> None:
        """Rebuild round-sensitive authorization from the current state.

        ``NightCoordinator`` keeps its board and supplied window configs as
        immutable adapter state.  Replacing that adapter at command
        boundaries lets each round reflect deaths, consumed resources, and
        exhausted ability grants while preserving already installed windows:
        the replacement coordinator reads those windows from ``GameState``
        and returns them unchanged.
        """

        self._wolf_coordinator_seat = self._select_wolf_coordinator(state)
        self._window_configs = self._build_window_configs(state)
        self.coordinator = NightCoordinator(
            self._manager,
            self._board,
            self._window_configs,
            snapshot_id=self._snapshot_id,
        )

    @staticmethod
    def _physical_window_id(board_window_id: str, round_no: int) -> str:
        return board_window_id if round_no == 0 else f"{board_window_id}-r{round_no}"

    def _team_role_ids(self) -> frozenset[str]:
        role_ids: set[str] = set()
        for window in self._board.night_windows:
            if window.phase is GamePhase.NIGHT_TEAM_CHAT:
                role_ids.update(window.visible_to)
        return frozenset(role_ids)

    def _wolf_seats(self, state: GameState) -> tuple[int, ...]:
        players = state.players
        role_ids = self._team_role_ids()
        seats: list[int] = []
        for seat, player in sorted(players.items()):
            if not player.alive:
                continue
            if (role_ids and player.role_id in role_ids) or (
                not role_ids and player.faction_id.casefold() in {"wolf", "werewolf"}
            ):
                seats.append(seat)
        return tuple(seats)

    def _select_wolf_coordinator(self, state: GameState) -> int | None:
        """Choose the deterministic final wolf submitter for this round.

        The seat is derived from the frozen team visibility and private player
        state.  It is recorded in moderator audit when the action window is
        first opened; it is never added to the role knowledge document.
        """

        wolf_kill = self._action_definition_by_name("WOLF_KILL")
        if wolf_kill is None:
            return None
        if wolf_kill.target_policy != "alive_non_authorized_wolf":
            return None
        wolves = self._wolf_seats(state)
        for seat in wolves:
            player = state.players[seat]
            if any(
                ability.action_code == wolf_kill.action_code
                and ability.timing is GamePhase.NIGHT_ACTION
                and GamePhase.NIGHT_ACTION in ability.allowed_phases
                and (
                    ability.usage_limit is None
                    or ability.usage_limit.max_uses is None
                    or ability.uses_consumed < ability.usage_limit.max_uses
                )
                and (
                    ability.resource is None
                    or player.skill_resources.get(ability.resource.resource_id, 0)
                    >= ability.resource.cost_per_use
                )
                for ability in player.granted_abilities
            ):
                return seat
        return None

    def _action_definition_by_name(self, name: str) -> ActionDefinition | None:
        # NightCoordinator and GameManager independently load the same
        # registry.  Importing through the manager is intentionally avoided so
        # this adapter cannot mutate or replace its authoritative registry.
        registry = load_action_registry()
        for definition in registry.actions:
            if definition.action_name == name:
                return definition
        return None

    def _build_window_configs(self, state: GameState) -> dict[str, NightWindowConfig]:
        players = state.players
        registry = self._action_registry()
        wolf_kill = self._action_definition_by_name("WOLF_KILL")
        wolf_kill_code = wolf_kill.action_code if wolf_kill is not None else None
        action_configs: dict[str, NightWindowConfig] = {}

        for board_window in self._board.night_windows:
            if board_window.phase is GamePhase.NIGHT_TEAM_CHAT:
                team_seats = self._wolf_seats(state)
                action_configs[board_window.window_id] = NightWindowConfig(
                    allowed_seats=team_seats,
                    allowed_role_ids=tuple(board_window.visible_to),
                    allowed_action_codes=(299,),
                    min_actions=0,
                    max_actions=0,
                    allow_pass=True,
                )
                continue
            if board_window.phase is not GamePhase.NIGHT_ACTION:
                continue

            allowed_seats: list[int] = []
            allowed_codes: set[int] = set()
            for seat, player in sorted(players.items()):
                if not player.alive:
                    continue
                grants = tuple(
                    ability
                    for ability in player.granted_abilities
                    if ability.timing is GamePhase.NIGHT_ACTION
                    and GamePhase.NIGHT_ACTION in ability.allowed_phases
                    and self._grant_is_usable(
                        player,
                        ability,
                        registry.get(ability.action_code),
                    )
                )
                if not grants:
                    continue
                # All wolves receive the team discussion window, but only the
                # selected coordinator receives the final WOLF_KILL turn.
                if wolf_kill_code is not None and any(
                    ability.action_code == wolf_kill_code for ability in grants
                ):
                    if seat != self._wolf_coordinator_seat:
                        grants = tuple(
                            ability for ability in grants if ability.action_code != wolf_kill_code
                        )
                if (
                    grants
                    or any(
                        ability.action_code == wolf_kill_code
                        for ability in player.granted_abilities
                    )
                    and seat == self._wolf_coordinator_seat
                ):
                    allowed_codes.update(ability.action_code for ability in grants)
                    allowed_seats.append(seat)

            # The final wolf target must be collected before a dependent
            # current-kill action such as WITCH_HEAL.  The order is a host
            # scheduling fact derived from the grant and registry policy;
            # it is not inferred from a role name or a client request.
            heal_definition = self._action_definition_by_name("WITCH_HEAL")

            def seat_priority(seat: int) -> tuple[int, int]:
                if seat == self._wolf_coordinator_seat:
                    return (0, seat)
                player = players[seat]
                dependent = any(
                    ability.action_code in allowed_codes
                    and heal_definition is not None
                    and heal_definition.action_code == ability.action_code
                    for ability in player.granted_abilities
                )
                return (1 if dependent else 2, seat)

            allowed_seats.sort(key=seat_priority)

            allow_pass = True
            allowed_codes.add(299)
            action_configs[board_window.window_id] = NightWindowConfig(
                allowed_seats=tuple(allowed_seats),
                allowed_action_codes=tuple(sorted(allowed_codes)),
                min_actions=1,
                max_actions=1,
                allow_pass=allow_pass,
                visible_context={
                    "candidate_seats": [
                        seat for seat, player in sorted(players.items()) if player.alive
                    ]
                },
            )
        return action_configs

    def _current_board_window(self) -> NightWindow:
        state = self.state
        candidates = [item for item in self._board.night_windows if item.phase is state.phase]
        for board_window in sorted(candidates, key=lambda item: item.order):
            physical_id = self._physical_window_id(board_window.window_id, state.round_no)
            raw = state.action_windows.get(physical_id)
            if raw is None or _load_window(raw).closed_at is None:
                return board_window
        raise ModeratorNightError("all night windows for the current phase are closed")

    def _current_action_window(self) -> ActionWindow:
        if self.state.phase is not GamePhase.NIGHT_ACTION:
            raise ModeratorNightError("night action commands require NIGHT_ACTION")
        board_window = self._current_board_window()
        physical_id = self._physical_window_id(board_window.window_id, self.state.round_no)
        raw = self.state.action_windows.get(physical_id)
        if raw is None:
            raise ModeratorNightError("open the current night window first")
        window = _load_window(raw)
        if window.closed_at is not None:
            raise ModeratorNightError("the current night action window is closed")
        return window

    async def _audit_coordinator(self, board_window: NightWindow) -> None:
        seat = self._wolf_coordinator_seat
        if seat is None:
            return
        marker = (
            f"round={self.state.round_no};window={board_window.window_id};"
            f"wolf_final_submitter_seat={seat}"
        )
        if any(
            item.get("operation") == "NIGHT_COORDINATOR_SELECTED" and item.get("reason") == marker
            for item in self.state.moderator_audit
        ):
            return
        try:
            await self._manager.commit_moderator_operation(
                operation="NIGHT_COORDINATOR_SELECTED",
                command="night open",
                expected_revision=self.state.state_revision,
                reason=marker,
                now=self._clock(),
            )
        except (EventCommitError, ValueError, TypeError) as exc:
            raise ModeratorNightError(str(exc)) from exc

    async def open(self) -> NightWindowProgress:
        self._refresh_configuration(self._manager.state)
        board_window = self._current_board_window()
        if board_window.phase is GamePhase.NIGHT_ACTION:
            await self._audit_coordinator(board_window)
        try:
            progress = await self.coordinator.open_next_window(now=self._clock())
            if board_window.phase is GamePhase.NIGHT_TEAM_CHAT:
                await self._publish_team_roster_notice()
            return progress
        except NightCoordinatorError as exc:
            raise ModeratorNightError(str(exc)) from exc

    async def _publish_team_roster_notice(self) -> None:
        """Deliver the board-authorized wolf roster once per night.

        Role assignment events intentionally contain only the receiving
        seat's own identity.  The team window is the separate, frozen board
        boundary where members may learn the other wolf seats.  No roster is
        emitted when the board disables that visibility contract.
        """

        if not self._board.wolf_team_visibility.members_know_each_other:
            return
        if self._board.wolf_team_visibility.identity_visibility != "members":
            return
        state = self.state
        seats = self._wolf_seats(state)
        if not seats:
            return
        correlation = f"night-wolf-roster-r{state.round_no}"
        if any(getattr(event, "correlation_id", None) == correlation for event in state.events):
            return
        events = tuple(event for event in state.events if hasattr(event, "event_id"))
        event_id = max((event.event_id for event in events), default=0) + 1
        event = GameEvent.team(
            event_id=event_id,
            game_id=state.game_id,
            state_revision=state.state_revision + 1,
            round_no=state.round_no,
            phase=GamePhase.NIGHT_TEAM_CHAT,
            created_at=self._clock(),
            event_type=EventType.TEAM_NOTICE,
            authorized_seats=seats,
            correlation_id=correlation,
            payload=TeamNoticePayload(content="狼人队伍成员座位：" + "、".join(map(str, seats))),
        )
        try:
            await self._manager.commit_events((event,), now=self._clock())
        except (EventCommitError, ValueError, TypeError) as exc:
            raise ModeratorNightError(str(exc)) from exc

    async def advance(self) -> GameState:
        self._refresh_configuration(self._manager.state)
        if self.state.phase is GamePhase.NIGHT_RESOLVE:
            raise ModeratorNightError(
                "night advance cannot resolve effects; provide explicit moderator resolutions"
            )
        board_window = self._current_board_window()
        if board_window.phase is GamePhase.NIGHT_TEAM_CHAT:
            # Team speech is a durable queue.  A missing queue means that the
            # moderator has not started the first discussion round; a
            # non-empty queue means one or more authorized wolves still need
            # to speak.  Only an explicitly exhausted queue may be advanced.
            if self.state.serial_turn is not None:
                raise ModeratorNightError(
                    "TEAM_SPEECH_IN_PROGRESS: finish or retry the active team speaker "
                    "before advancing"
                )
            if self.state.current_queue is None:
                raise ModeratorNightError(
                    "TEAM_SPEECH_NOT_STARTED: run night team next before advancing"
                )
            if self.state.current_queue:
                raise ModeratorNightError(
                    "TEAM_SPEECH_INCOMPLETE: every authorized team seat must speak before advancing"
                )
        try:
            return await self.coordinator.advance_from_current_window(
                expected_window_id=board_window.window_id,
                now=self._clock(),
            )
        except NightCoordinatorError as exc:
            raise ModeratorNightError(str(exc)) from exc

    def _current_team_window(self) -> ActionWindow:
        if self.state.phase is not GamePhase.NIGHT_TEAM_CHAT:
            raise ModeratorNightError("night team commands require NIGHT_TEAM_CHAT")
        board_window = self._current_board_window()
        if board_window.phase is not GamePhase.NIGHT_TEAM_CHAT:
            raise ModeratorNightError("the current night window is not team chat")
        physical_id = self._physical_window_id(board_window.window_id, self.state.round_no)
        raw = self.state.action_windows.get(physical_id)
        if raw is None:
            raise ModeratorNightError("open the current night team window first")
        window = _load_window(raw)
        if window.closed_at is not None:
            raise ModeratorNightError("the current night team window is closed")
        return window

    @staticmethod
    def _team_turn_payload(
        result: SerialSpeechResult,
        *,
        window_id: str,
    ) -> dict[str, object]:
        """Return moderator-safe metadata for one committed private speech."""

        return {
            "status": "accepted",
            "seat": result.event.actor_seat,
            "request_id": result.request.request_id,
            "logical_request_id": result.request.logical_request_id,
            "attempt_no": result.request.attempt_no,
            "window_id": window_id,
            "event_id": result.event.event_id,
            "phase": result.request.phase.value,
        }

    async def team_next(self) -> dict[str, object]:
        """Run the next speaker in the frozen wolf-team queue.

        The first call persists the queue from the currently open team
        window.  Later calls consume its head.  Once the queue is empty the
        moderator must use :meth:`team_again` to begin another round.
        """

        self._refresh_configuration(self._manager.state)
        window = self._current_team_window()
        state = self.state
        if state.serial_turn is not None:
            raise ModeratorNightError(
                "TEAM_SPEECH_IN_PROGRESS: use night team retry for the active speaker"
            )
        if state.current_queue is None or (
            not state.current_queue and self._team_queue_window_id != window.window_id
        ):
            try:
                await self.team_scheduler.start(queue=window.allowed_seats)
            except (SerialTurnError, RuntimeError, ValueError) as exc:
                raise ModeratorNightError(str(exc)) from exc
            self._team_queue_window_id = window.window_id
        elif not state.current_queue:
            raise ModeratorNightError(
                "TEAM_SPEECH_COMPLETE: use night team again for another discussion round"
            )
        try:
            result = await self.team_scheduler.run_next()
        except (SerialTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorNightError(str(exc)) from exc
        return self._team_turn_payload(result, window_id=window.window_id)

    async def team_retry(self) -> dict[str, object]:
        """Retry the persisted active team speaker without changing the queue."""

        self._refresh_configuration(self._manager.state)
        window = self._current_team_window()
        state = self.state
        if state.serial_turn is None:
            raise ModeratorNightError(
                "TEAM_SPEECH_NOT_IN_PROGRESS: no active team speaker can be retried"
            )
        if state.current_queue is None or not state.current_queue:
            raise ModeratorNightError("TEAM_SPEECH_QUEUE_INVALID: no pending team seat to retry")
        try:
            result = await self.team_scheduler.retry()
        except (SerialTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorNightError(str(exc)) from exc
        return self._team_turn_payload(result, window_id=window.window_id)

    async def team_again(self) -> dict[str, object]:
        """Explicitly start another discussion round from the frozen window."""

        self._refresh_configuration(self._manager.state)
        window = self._current_team_window()
        state = self.state
        if state.serial_turn is not None:
            raise ModeratorNightError(
                "TEAM_SPEECH_IN_PROGRESS: finish or retry the active team speaker first"
            )
        if state.current_queue is None:
            raise ModeratorNightError(
                "TEAM_SPEECH_NOT_STARTED: complete the first discussion round before again"
            )
        if state.current_queue:
            raise ModeratorNightError(
                "TEAM_SPEECH_INCOMPLETE: finish the current discussion round before again"
            )
        try:
            await self.team_scheduler.start(queue=window.allowed_seats)
        except (SerialTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorNightError(str(exc)) from exc
        return {
            "status": "ready",
            "queue": list(window.allowed_seats),
            "phase": GamePhase.NIGHT_TEAM_CHAT.value,
        }

    @staticmethod
    def _pending_kill_target(
        state: GameState,
        action_window_id: str,
        wolf_kill_code: int | None,
        actor_seat: int | None = None,
    ) -> int | None:
        if wolf_kill_code is None:
            return None
        requests = state.action_requests
        for payload in requests.values():
            if not isinstance(payload, Mapping) or payload.get("window_id") != action_window_id:
                continue
            if payload.get("status") != "PENDING":
                continue
            if actor_seat is not None and payload.get("seat") != actor_seat:
                continue
            raw_actions = payload.get("actions")
            if not isinstance(raw_actions, (list, tuple)):
                continue
            for raw_action in raw_actions:
                if (
                    isinstance(raw_action, Mapping)
                    and raw_action.get("action_code") == wolf_kill_code
                ):
                    targets = raw_action.get("targets", [])
                    if (
                        isinstance(targets, (list, tuple))
                        and targets
                        and isinstance(targets[0], int)
                        and not isinstance(targets[0], bool)
                    ):
                        return targets[0]
        return None

    @staticmethod
    def _grant_is_usable(
        player: PlayerState,
        ability: GrantedAbility,
        definition: ActionDefinition,
    ) -> bool:
        if (
            ability.timing is not GamePhase.NIGHT_ACTION
            or GamePhase.NIGHT_ACTION not in ability.allowed_phases
        ):
            return False
        limit = ability.usage_limit
        if (
            limit is not None
            and limit.max_uses is not None
            and ability.uses_consumed >= limit.max_uses
        ):
            return False
        if definition.resource_id is None:
            if ability.resource is not None:
                return False
        elif ability.resource is None or ability.resource.resource_id != definition.resource_id:
            return False
        return not (
            ability.resource is not None
            and player.skill_resources.get(ability.resource.resource_id, 0)
            < ability.resource.cost_per_use
        )

    def _active_grant(
        self,
        player: PlayerState,
        definition: ActionDefinition,
    ) -> GrantedAbility | None:
        """Return a usable grant for a registry action, if one exists."""

        # The board decides who may receive the information; the registry and
        # the copied grant decide whether that seat still owns a usable heal.
        for ability in player.granted_abilities:
            if ability.action_code != definition.action_code:
                continue
            if not self._grant_is_usable(player, ability, definition):
                continue
            return ability
        return None

    def _witch_target_seats(self, state: GameState) -> tuple[int, ...]:
        """Find board-authorized, currently usable recipients of the knife."""

        heal_definition = self._action_definition_by_name("WITCH_HEAL")
        if heal_definition is None:
            return ()
        seats: list[int] = []
        for seat, player in sorted(state.players.items()):
            if not player.alive:
                continue
            binding = next(
                (item for item in self._board.role_bindings if item.role_ref.id == player.role_id),
                None,
            )
            if binding is None or binding.effective_rules.get("knows_wolf_target") is not True:
                continue
            if self._active_grant(player, heal_definition) is not None:
                seats.append(seat)
        return tuple(seats)

    def _wolf_submission_confirmed(self, state: GameState, window: ActionWindow) -> bool:
        """Return whether the selected final wolf seat accepted one request."""

        coordinator = self._wolf_coordinator_seat
        if coordinator is None:
            return False
        return any(
            isinstance(payload, Mapping)
            and payload.get("window_id") == window.window_id
            and payload.get("status") == "PENDING"
            and payload.get("seat") == coordinator
            for payload in state.action_requests.values()
        )

    async def _publish_witch_target_notice(self, window: ActionWindow) -> None:
        """Commit the board-authorized knife notice before witch runtime input."""

        state = self.state
        recipients = self._witch_target_seats(state)
        if not recipients:
            return
        if not self._wolf_submission_confirmed(state, window):
            return
        wolf_kill = self._action_definition_by_name("WOLF_KILL")
        if wolf_kill is None:
            return
        target = self._pending_kill_target(
            state,
            window.window_id,
            wolf_kill.action_code,
            actor_seat=self._wolf_coordinator_seat,
        )
        correlation_prefix = f"night-witch-target-r{state.round_no}-w{window.window_id}"
        existing = {
            event.correlation_id
            for event in state.events
            if isinstance(event, GameEvent) and event.correlation_id
        }
        timestamp = self._clock()
        events: list[GameEvent] = []
        next_event_id = (
            max(
                (event.event_id for event in state.events if isinstance(event, GameEvent)),
                default=0,
            )
            + 1
        )
        for seat in recipients:
            correlation = f"{correlation_prefix}-s{seat}"
            if correlation in existing:
                continue
            if target is None:
                event_type = EventType.PRIVATE_NOTICE
                payload: PrivateNoticePayload | PrivateWitchTargetPayload = PrivateNoticePayload(
                    content="本夜没有狼人刀口。"
                )
            else:
                event_type = EventType.WITCH_TARGET
                payload = PrivateWitchTargetPayload(target_seat=target)
            events.append(
                GameEvent.private(
                    event_id=next_event_id,
                    game_id=state.game_id,
                    state_revision=state.state_revision + 1,
                    round_no=state.round_no,
                    phase=GamePhase.NIGHT_ACTION,
                    created_at=timestamp,
                    event_type=event_type,
                    seat=seat,
                    actor_seat=seat,
                    correlation_id=correlation,
                    payload=payload,
                )
            )
            next_event_id += 1
        if not events:
            return
        try:
            await self._manager.commit_events(
                tuple(events),
                expected_revision=state.state_revision,
                now=timestamp,
            )
        except (EventCommitError, ValueError, TypeError) as exc:
            raise ModeratorNightError(str(exc)) from exc

    def _context_for(self, seat: int, window: ActionWindow) -> ActionValidationContext:
        state = self.state
        player = state.players.get(seat)
        if player is None:
            raise ModeratorNightError(f"seat {seat} is not assigned")
        registry = self._action_registry()
        wolf_kill = next(
            (item for item in registry.actions if item.action_name == "WOLF_KILL"),
            None,
        )
        active = tuple(
            ability
            for ability in player.granted_abilities
            if ability.timing is GamePhase.NIGHT_ACTION
            and GamePhase.NIGHT_ACTION in ability.allowed_phases
            and ability.action_code in window.allowed_action_codes
            and (
                ability.usage_limit is None
                or ability.usage_limit.max_uses is None
                or ability.uses_consumed < ability.usage_limit.max_uses
            )
            and (
                ability.resource is None
                or state.players[seat].skill_resources.get(ability.resource.resource_id, 0)
                >= ability.resource.cost_per_use
            )
        )
        alive = tuple(sorted(item.seat for item in state.players.values() if item.alive))
        wolf_seats = set(self._wolf_seats(state))
        kill_target = self._pending_kill_target(
            state,
            window.window_id,
            wolf_kill.action_code if wolf_kill is not None else None,
        )
        target_sets: dict[int, tuple[int, ...]] = {}
        for ability in active:
            try:
                definition = registry.get(ability.action_code)
            except KeyError:
                continue
            candidates = set(alive)
            if definition.target_policy in {
                "alive_non_authorized_wolf",
                "other_alive",
                "current_kill_not_self",
            }:
                candidates.discard(seat)
            if definition.target_policy == "alive_non_authorized_wolf":
                candidates.difference_update(wolf_seats)
            elif definition.target_policy == "current_kill_not_self":
                candidates = (
                    {kill_target} if kill_target is not None and kill_target != seat else set()
                )
            elif definition.target_policy == "none":
                candidates = set()
            target_sets[ability.action_code] = tuple(sorted(candidates))
        return ActionValidationContext(
            game_id=state.game_id,
            session_epoch=window.session_epoch,
            active_request_id="moderator-night-request",
            player_alive=player.alive,
            player_qualified=seat in window.allowed_seats,
            role_id=player.role_id,
            authorized_action_codes=tuple(
                sorted(
                    {
                        *{item.action_code for item in active},
                        *({299} if window.allow_pass else set()),
                    }
                )
            ),
            skill_resources=dict(player.skill_resources),
            alive_seats=alive,
            eligible_targets_by_action=target_sets,
            current_kill_target_seat=kill_target,
        )

    @staticmethod
    def _action_registry() -> ActionRegistry:
        return load_action_registry()

    def _submitted_seats(self, window: ActionWindow) -> set[int]:
        submitted: set[int] = set()
        for payload in self.state.action_requests.values():
            if not isinstance(payload, dict) or payload.get("window_id") != window.window_id:
                continue
            seat = payload.get("seat")
            if isinstance(seat, int) and not isinstance(seat, bool):
                submitted.add(seat)
        return submitted

    def _choose_seat(self, window: ActionWindow, seat: int | None) -> int:
        if seat is not None:
            if seat not in window.allowed_seats:
                raise ModeratorNightError(f"seat {seat} is not authorized in this night window")
            return seat
        submitted = self._submitted_seats(window)
        for candidate in window.allowed_seats:
            player = self.state.players[candidate]
            if candidate not in submitted and player.current_request_id is None:
                return candidate
        blocked = [
            candidate
            for candidate in window.allowed_seats
            if self.state.players[candidate].current_request_id is not None
        ]
        if blocked:
            raise ModeratorNightError(
                f"seat {blocked[0]} has an active request; use night action retry {blocked[0]}"
            )
        raise ModeratorNightError("all authorized seats have submitted this night action")

    @staticmethod
    def _turn_payload(result: ActionTurnResult, seat: int) -> dict[str, object]:
        return {
            "status": "accepted",
            "seat": seat,
            "request_id": result.request.request_id,
            "attempt_no": result.request.attempt_no,
            "window_id": result.request.action_window.window_id
            if result.request.action_window is not None
            else None,
            "phase": result.state.phase.value,
        }

    async def action_next(self, seat: int | None = None) -> dict[str, object]:
        self._refresh_configuration(self._manager.state)
        window = self._current_action_window()
        selected = self._choose_seat(window, seat)
        witch_seats = self._witch_target_seats(self.state)
        if selected in witch_seats:
            if self._wolf_coordinator_seat is not None and not self._wolf_submission_confirmed(
                self.state, window
            ):
                raise ModeratorNightError(
                    "WOLF_ACTION_REQUIRED: final wolf action must be accepted before witch input"
                )
            await self._publish_witch_target_notice(window)
        try:
            result = await self.scheduler.run_turn(
                window,
                selected,
                self._context_for(selected, window),
            )
        except (ActionTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorNightError(str(exc)) from exc
        return self._turn_payload(result, selected)

    async def action_retry(self, seat: int | None = None) -> dict[str, object]:
        self._refresh_configuration(self._manager.state)
        window = self._current_action_window()
        selected = self._choose_retry_seat(window, seat)
        try:
            result = await self.scheduler.run_turn(
                window,
                selected,
                self._context_for(selected, window),
                retry=True,
            )
        except (ActionTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorNightError(str(exc)) from exc
        return self._turn_payload(result, selected)

    def _choose_retry_seat(self, window: ActionWindow, seat: int | None) -> int:
        candidates = (seat,) if seat is not None else tuple(window.allowed_seats)
        for candidate in candidates:
            if candidate is None:
                continue
            if candidate not in window.allowed_seats:
                raise ModeratorNightError(
                    f"seat {candidate} is not authorized in this night window"
                )
            if self.state.players[candidate].current_request_id is not None:
                return candidate
        raise ModeratorNightError("no authorized seat has an active request to retry")

    def progress(self) -> dict[str, object]:
        self._refresh_configuration(self._manager.state)
        state = self.state
        payload: dict[str, object] = {
            "phase": state.phase.value,
            "wolf_coordinator_seat": self._wolf_coordinator_seat,
            "window": None,
            "submitted_seats": [],
            "team_queue": (
                list(state.current_queue)
                if state.phase is GamePhase.NIGHT_TEAM_CHAT and state.current_queue is not None
                else None
            ),
            "team_turn": (
                state.serial_turn.model_dump(mode="json")
                if state.phase is GamePhase.NIGHT_TEAM_CHAT and state.serial_turn is not None
                else None
            ),
        }
        candidates = [item for item in self._board.night_windows if item.phase is state.phase]
        for board_window in sorted(candidates, key=lambda item: item.order):
            physical_id = self._physical_window_id(board_window.window_id, state.round_no)
            raw = state.action_windows.get(physical_id)
            if raw is None:
                payload["window"] = {
                    "board_window_id": board_window.window_id,
                    "window_id": physical_id,
                    "status": "NOT_OPEN",
                }
                break
            window = _load_window(raw)
            if window.closed_at is None:
                payload["window"] = window.model_dump(mode="json")
                payload["submitted_seats"] = sorted(self._submitted_seats(window))
                break
        return payload

    def pending(self) -> dict[str, object]:
        """Return the moderator-only requests awaiting night resolution.

        Player action requests are immutable intents.  They are deliberately
        projected here without an outcome, effect, or disposition: those
        values belong to the explicit ``ActionResolution`` supplied by the
        moderator.  The action window is selected from the current board
        snapshot and the current ``NIGHT_RESOLVE`` boundary, so requests from
        an earlier round or another window cannot be mistaken for this
        night's work.
        """

        self._refresh_configuration(self._manager.state)
        state = self.state
        if state.phase is not GamePhase.NIGHT_RESOLVE:
            raise ModeratorNightError("night pending requires NIGHT_RESOLVE")

        action_windows = [
            item for item in self._board.night_windows if item.phase is GamePhase.NIGHT_ACTION
        ]
        resolve_windows = [
            item for item in self._board.night_windows if item.phase is GamePhase.NIGHT_RESOLVE
        ]
        if len(action_windows) != 1:
            raise ModeratorNightError("night pending requires exactly one NIGHT_ACTION window")
        if len(resolve_windows) != 1:
            raise ModeratorNightError("night pending requires exactly one NIGHT_RESOLVE window")

        action_window_id = self._physical_window_id(action_windows[0].window_id, state.round_no)
        resolve_window_id = self._physical_window_id(resolve_windows[0].window_id, state.round_no)
        raw_action_window = state.action_windows.get(action_window_id)
        raw_resolve_window = state.action_windows.get(resolve_window_id)
        if raw_action_window is None:
            raise ModeratorNightError("the current night action window is not open")
        if raw_resolve_window is None:
            raise ModeratorNightError("the current night resolve window is not open")
        try:
            action_window = _load_window(raw_action_window)
            resolve_window = _load_window(raw_resolve_window)
        except ModeratorNightError:
            raise
        if action_window.phase is not GamePhase.NIGHT_ACTION:
            raise ModeratorNightError("the current night action window is malformed")
        if action_window.closed_at is not None:
            raise ModeratorNightError("the current night action window is closed")
        if resolve_window.phase is not GamePhase.NIGHT_RESOLVE:
            raise ModeratorNightError("the current night resolve window is malformed")
        if resolve_window.closed_at is not None:
            raise ModeratorNightError("the current night resolve window is closed")

        pending_requests: list[dict[str, object]] = []
        for request_id, payload in state.action_requests.items():
            if not isinstance(payload, dict):
                continue
            if payload.get("window_id") != action_window_id:
                continue
            if payload.get("status") != "PENDING":
                continue
            stored_request_id = payload.get("request_id")
            if stored_request_id != request_id or not isinstance(stored_request_id, str):
                raise ModeratorNightError("stored night action request is malformed")
            actions = payload.get("actions")
            session_epoch = payload.get("session_epoch")
            game_id = payload.get("game_id")
            if (
                not isinstance(actions, (list, tuple))
                or not actions
                or not isinstance(session_epoch, int)
                or isinstance(session_epoch, bool)
                or session_epoch < 0
                or game_id != state.game_id
            ):
                raise ModeratorNightError("stored night action request is malformed")
            # An ActionRequest is one atomic action bundle in V1.  Reusing its
            # request ID as bundle ID preserves the authoritative identity
            # without fabricating a second ID for the moderator to reconcile.
            pending_requests.append(
                {
                    "bundle_id": stored_request_id,
                    "game_id": state.game_id,
                    "request_id": stored_request_id,
                    "window_id": action_window_id,
                    "session_epoch": session_epoch,
                    "base_revision": state.state_revision,
                    "actions": json.loads(json.dumps(actions)),
                }
            )

        return {
            "phase": state.phase.value,
            "action_window_id": action_window_id,
            "resolve_window_id": resolve_window_id,
            "base_revision": state.state_revision,
            "pending_requests": pending_requests,
            "private": True,
            "sensitive": True,
        }

    @staticmethod
    def _reject_duplicate_json_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        """Reject ambiguous JSON objects before Pydantic sees the payload."""

        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    @classmethod
    def _load_resolutions_file(cls, filename: str) -> tuple[ActionResolution, ...]:
        """Load one complete, strictly typed moderator resolution batch."""

        path = Path(filename).expanduser()
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ModeratorNightError(f"resolution file cannot be opened: {filename}") from exc
        if not path.is_file():
            raise ModeratorNightError(f"resolution file is not a regular file: {filename}")
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ModeratorNightError(f"resolution file cannot be read: {filename}") from exc
        try:
            raw = json.loads(raw_text, object_pairs_hook=cls._reject_duplicate_json_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ModeratorNightError("resolution file must contain valid JSON") from exc
        if not isinstance(raw, list):
            raise ModeratorNightError("resolution file must contain a JSON array")
        try:
            return tuple(ActionResolution.model_validate(item) for item in raw)
        except (TypeError, ValueError) as exc:
            raise ModeratorNightError(
                "resolution file contains an invalid ActionResolution record"
            ) from exc

    async def resolve(self, args: tuple[str, ...] = ()) -> GameState:
        """Confirm one complete moderator batch at the night resolve boundary.

        The file is parsed before the coordinator is called.  Consequently an
        invalid file, an incomplete batch, or a stale ``base_revision`` cannot
        partially mutate the authoritative state.  An omitted file is only a
        recovery shortcut for a night whose action requests are already all
        terminal; it never fills in a pending player's decision.
        """

        self._refresh_configuration(self._manager.state)
        if len(args) > 1:
            raise ModeratorNightError(
                "night resolve syntax: night resolve <json-file>; omit the file only "
                "when no pending request remains"
            )

        if args:
            resolutions = self._load_resolutions_file(args[0])
        else:
            requests = [
                payload
                for payload in self.state.action_requests.values()
                if isinstance(payload, dict)
            ]
            pending = [payload for payload in requests if payload.get("status") == "PENDING"]
            if pending or not requests:
                raise ModeratorNightError(
                    "night resolve requires explicit moderator resolutions: provide "
                    "<json-file> containing one complete ActionResolution for every "
                    "pending request"
                )
            resolutions = ()

        try:
            return await self.coordinator.confirm_night(
                resolutions,
                now=self._clock(),
            )
        except NightCoordinatorError as exc:
            raise ModeratorNightError(str(exc)) from exc

    async def auto_resolve(self) -> GameState:
        """Apply the bounded classic-board night proposal for the current night.

        The proposal builder is pure and only covers the published classic
        twelve-seat board.  The coordinator remains the single serialized
        commit boundary, so this convenience entry point cannot bypass the
        normal pending-request and frozen-window checks.
        """

        self._refresh_configuration(self._manager.state)
        try:
            resolutions = build_classic_night_resolutions(self.state, self._board)
            return await self.coordinator.resolve(resolutions, now=self._clock())
        except (NightCoordinatorError, ValueError, TypeError) as exc:
            raise ModeratorNightError(str(exc)) from exc


__all__ = ["ModeratorNightError", "ModeratorNightFlow"]
