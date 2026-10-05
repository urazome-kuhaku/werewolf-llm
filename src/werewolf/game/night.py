"""Board driven coordination for the three night phases.

The coordinator owns window ordering and the hand-off between
``NIGHT_TEAM_CHAT``, ``NIGHT_ACTION`` and ``NIGHT_RESOLVE``.  It does not
decide what a role means.  Action codes, authorized seats and role IDs are
supplied by the frozen board/role snapshot through :class:`NightWindowConfig`.
Player requests remain pending until the moderator confirms a complete bundle
at the ``NIGHT_RESOLVE`` boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import JsonValue

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.board import BoardDefinition, NightWindow
from werewolf.knowledge.preview import experimental_preview_enabled

from .actions import ActionWindow
from .manager import EventCommitError, GameManager, ResolutionError
from .resolution import ActionResolution
from .state import GameState


class NightCoordinatorError(ValueError):
    """Raised when a board driven night operation cannot proceed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class NightWindowConfig:
    """Runtime authorization facts derived from the frozen role documents.

    A board declares order and dependencies, while role documents and the
    action registry provide the per-seat action authorization.  Keeping those
    inputs separate prevents this coordinator from embedding a particular
    wolf/seer/witch implementation.
    """

    allowed_seats: tuple[int, ...] = ()
    allowed_role_ids: tuple[str, ...] = ()
    allowed_action_codes: tuple[int, ...] = ()
    min_actions: int = 1
    max_actions: int = 1
    allow_pass: bool = False
    visible_context: Mapping[str, JsonValue] = field(default_factory=dict)
    collection_only: bool = False

    def __post_init__(self) -> None:
        if len(set(self.allowed_seats)) != len(self.allowed_seats):
            raise ValueError("allowed_seats must not contain duplicates")
        if any(isinstance(seat, bool) or not 1 <= seat <= 64 for seat in self.allowed_seats):
            raise ValueError("allowed_seats must contain seats between 1 and 64")
        if len(set(self.allowed_role_ids)) != len(self.allowed_role_ids):
            raise ValueError("allowed_role_ids must not contain duplicates")
        if len(set(self.allowed_action_codes)) != len(self.allowed_action_codes):
            raise ValueError("allowed_action_codes must not contain duplicates")
        if self.collection_only:
            if self.allowed_seats or self.allowed_action_codes or self.allow_pass:
                raise ValueError("collection-only windows cannot authorize seats, actions, or PASS")
            if self.min_actions != 0 or self.max_actions != 0:
                raise ValueError("collection-only windows require zero actions")
        elif not self.allowed_action_codes:
            raise ValueError("allowed_action_codes must not be empty")
        if not 0 <= self.min_actions <= self.max_actions <= 32:
            raise ValueError("min_actions/max_actions must be between 0 and 32")
        if not self.collection_only and self.max_actions == 0 and not self.allow_pass:
            raise ValueError("zero-action windows must allow_pass")


@dataclass(frozen=True, slots=True)
class NightWindowProgress:
    """The installed board window and its current phase."""

    board_window_id: str
    phase: GamePhase
    order: int
    dependencies: tuple[str, ...]
    action_window: ActionWindow


def _load_window(raw: object) -> ActionWindow:
    if not isinstance(raw, Mapping):
        raise NightCoordinatorError("WINDOW_INVALID", "stored night window is not a mapping")
    data = json.loads(json.dumps(raw))
    phase = data.get("phase")
    if isinstance(phase, str):
        try:
            data["phase"] = GamePhase(phase)
        except ValueError as exc:
            raise NightCoordinatorError(
                "WINDOW_INVALID", "stored window has an invalid phase"
            ) from exc
    try:
        return ActionWindow.model_validate(data)
    except ValueError as exc:
        raise NightCoordinatorError("WINDOW_INVALID", "stored night window is malformed") from exc


class NightCoordinator:
    """Coordinate a single immutable board's night windows."""

    def __init__(
        self,
        manager: GameManager,
        board: BoardDefinition,
        window_configs: Mapping[str, NightWindowConfig],
        *,
        snapshot_id: str | None = None,
    ) -> None:
        if not isinstance(manager, GameManager):
            raise TypeError("manager must be a GameManager")
        if not isinstance(board, BoardDefinition):
            raise TypeError("board must be a BoardDefinition")
        if board.status != "published":
            raise NightCoordinatorError(
                "BOARD_NOT_PUBLISHED", "night coordination requires a published board"
            )
        if board.reviewed_by == "pending-human-review" and not experimental_preview_enabled():
            raise NightCoordinatorError(
                "BOARD_NOT_REVIEWED", "night coordination requires a human-reviewed board"
            )
        if not window_configs:
            raise ValueError("window_configs must not be empty")
        self._manager = manager
        self._board = board
        self._window_configs = dict(window_configs)
        state = manager.state
        if state.ruleset is None:
            raise NightCoordinatorError("RULESET_MISSING", "game has no frozen ruleset reference")
        if state.ruleset.board_id != board.board_id or state.ruleset.version != board.version:
            raise NightCoordinatorError("RULESET_MISMATCH", "board does not match the game ruleset")
        if snapshot_id is not None and state.ruleset.snapshot_id != snapshot_id:
            raise NightCoordinatorError(
                "SNAPSHOT_MISMATCH", "board is not bound to the game snapshot"
            )
        self._snapshot_id = state.ruleset.snapshot_id

    @property
    def board(self) -> BoardDefinition:
        return self._board

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    def _ordered(self) -> tuple[NightWindow, ...]:
        return tuple(sorted(self._board.night_windows, key=lambda window: window.order))

    @staticmethod
    def _physical_window_id(board_window_id: str, state: GameState) -> str:
        """Return the state key for one logical board window in this round.

        The first round keeps the historical unqualified IDs for compatibility
        with V1 callers. Later rounds are physically distinct while retaining
        the logical board ID in the window context.
        """

        if state.round_no == 0:
            return board_window_id
        return f"{board_window_id}-r{state.round_no}"

    def _physical_window_id_for(self, board_window: NightWindow, state: GameState) -> str:
        return self._physical_window_id(board_window.window_id, state)

    def _settlement_group_id_for(
        self,
        board_window: NightWindow,
        state: GameState,
    ) -> str | None:
        execution = self._manager.execution_package
        if execution is None:
            return None
        groups = execution.window_settlement_groups
        if not groups:
            return f"night:{state.round_no}"
        static_group = groups.get(board_window.window_id)
        if not isinstance(static_group, str) or not static_group:
            raise NightCoordinatorError(
                "WINDOW_GROUP_MISSING",
                f"frozen package has no settlement group for {board_window.window_id!r}",
            )
        return f"night:{state.round_no}:{static_group}"

    def _static_settlement_group_for(self, board_window: NightWindow) -> str | None:
        execution = self._manager.execution_package
        if execution is None:
            return None
        groups = execution.window_settlement_groups
        return groups.get(board_window.window_id) if groups else "night"

    def _group_members(self, board_window: NightWindow) -> tuple[NightWindow, ...]:
        group_id = self._static_settlement_group_for(board_window)
        if group_id is None:
            return ()
        return tuple(
            item for item in self._ordered() if self._static_settlement_group_for(item) == group_id
        )

    def _current_board_window(self, state: GameState) -> NightWindow:
        for board_window in self._ordered():
            # The action collection window intentionally remains open while
            # the phase advances to NIGHT_RESOLVE.  Select by current phase
            # first so that it does not block the resolve boundary.
            if board_window.phase is not state.phase:
                continue
            raw = state.action_windows.get(self._physical_window_id_for(board_window, state))
            if raw is None:
                return board_window
            stored = _load_window(raw)
            # Executable windows separate collection from final settlement.
            # Once a window has stopped accepting requests, the next window in
            # the same night can open while the shared settlement group stays
            # provisional.  Legacy/manual windows still advance only after
            # their ordinary close boundary.
            collected = stored.collection_complete_at is not None
            if stored.closed_at is None and not collected:
                return board_window
        raise NightCoordinatorError("NIGHT_COMPLETE", "all board night windows are closed")

    def _config_for(self, board_window: NightWindow, state: GameState) -> NightWindowConfig:
        supplied = self._window_configs.get(board_window.window_id)
        if supplied is not None:
            return supplied
        if board_window.phase is GamePhase.NIGHT_ACTION:
            raise NightCoordinatorError(
                "WINDOW_CONFIG_MISSING",
                f"action window {board_window.window_id!r} has no role authorization config",
            )
        if board_window.phase is GamePhase.NIGHT_RESOLVE:
            return NightWindowConfig(
                allowed_seats=(),
                allowed_action_codes=(),
                min_actions=0,
                max_actions=0,
                collection_only=True,
            )
        # Team chat visibility comes from the board's frozen visible_to role
        # IDs.  Resolve is a moderator boundary and has no player action.
        if board_window.phase is GamePhase.NIGHT_TEAM_CHAT and not board_window.visible_to:
            raise NightCoordinatorError(
                "WINDOW_CONFIG_MISSING",
                f"team window {board_window.window_id!r} has no frozen visibility rule",
            )
        if board_window.visible_to:
            seats = tuple(
                seat
                for seat, player in sorted(state.players.items())
                if player.alive and player.role_id in board_window.visible_to
            )
        else:
            seats = tuple(seat for seat, player in sorted(state.players.items()) if player.alive)
        if not seats:
            if self._manager.execution_package is not None:
                return NightWindowConfig(
                    allowed_seats=(),
                    allowed_action_codes=(),
                    min_actions=0,
                    max_actions=0,
                    collection_only=True,
                )
            raise NightCoordinatorError(
                "NO_ELIGIBLE_SEATS", "night window has no eligible live seats"
            )
        return NightWindowConfig(
            allowed_seats=seats,
            allowed_role_ids=tuple(board_window.visible_to),
            allowed_action_codes=(299,),
            min_actions=0,
            max_actions=0,
            allow_pass=True,
        )

    @staticmethod
    def _session_epoch(state: GameState, seats: Sequence[int]) -> int:
        epochs = {state.players[seat].session_epoch for seat in seats}
        if len(epochs) != 1:
            raise NightCoordinatorError(
                "SESSION_MISMATCH", "all seats in one action window must share a session epoch"
            )
        return next(iter(epochs))

    def _build_action_window(
        self,
        state: GameState,
        board_window: NightWindow,
        *,
        now: datetime | None = None,
    ) -> ActionWindow:
        config = self._config_for(board_window, state)
        if not config.allowed_seats and not config.collection_only:
            raise NightCoordinatorError("NO_ELIGIBLE_SEATS", "night window has no authorized seats")
        unknown = tuple(seat for seat in config.allowed_seats if seat not in state.players)
        if unknown:
            raise NightCoordinatorError("SEAT_NOT_ASSIGNED", f"unknown night seats: {unknown}")
        if config.allowed_role_ids:
            unauthorized = tuple(
                seat
                for seat in config.allowed_seats
                if state.players[seat].role_id not in config.allowed_role_ids
            )
            if unauthorized:
                raise NightCoordinatorError(
                    "ROLE_NOT_ALLOWED", f"night seats are not authorized: {unauthorized}"
                )
        if board_window.phase is GamePhase.NIGHT_ACTION and not config.collection_only:
            self._validate_action_config(
                state,
                config,
                logical_window_id=board_window.window_id,
            )
        epoch = self._session_epoch(state, config.allowed_seats) if config.allowed_seats else 0
        visible = dict(config.visible_context)
        visible.update(
            {
                "night_window_id": board_window.window_id,
                "physical_window_id": self._physical_window_id_for(board_window, state),
                "night_round": state.round_no,
                "night_order": board_window.order,
                "depends_on": list(board_window.depends_on),
            }
        )
        physical_window_id = self._physical_window_id_for(board_window, state)
        ordered = self._ordered()
        position = next(
            index for index, item in enumerate(ordered) if item.window_id == board_window.window_id
        )
        next_window_id = ordered[position + 1].window_id if position + 1 < len(ordered) else None
        return ActionWindow(
            window_id=physical_window_id,
            game_id=state.game_id,
            session_epoch=epoch,
            phase=board_window.phase,
            collection_only=config.collection_only,
            logical_window_id=board_window.window_id,
            settlement_group_id=self._settlement_group_id_for(board_window, state),
            next_window_id=next_window_id,
            allowed_seats=tuple(config.allowed_seats),
            allowed_role_ids=tuple(config.allowed_role_ids),
            allowed_action_codes=tuple(config.allowed_action_codes),
            min_actions=config.min_actions,
            max_actions=config.max_actions,
            allow_pass=config.allow_pass,
            opened_at=now or datetime.now(UTC),
            dependency_receipt_ids=(),
            dependencies_satisfied=True,
            visible_context=visible,
            allow_concurrent=board_window.parallel,
        )

    def _validate_action_config(
        self,
        state: GameState,
        config: NightWindowConfig,
        *,
        logical_window_id: str,
    ) -> None:
        """Validate an action window against per-seat setup grants.

        ``allowed_action_codes`` is a window union, while authorization is
        still checked per request seat by ``GameManager``.  This validation
        catches an unusable or cross-role union at installation time without
        removing the coordinator's ability to narrow a wolf team to one final
        submitting seat.
        """

        if config.collection_only:
            return
        if 299 in config.allowed_action_codes and not config.allow_pass:
            raise NightCoordinatorError(
                "PASS_NOT_ALLOWED", "PASS may only appear in a window that allows pass"
            )
        if config.allow_pass and 299 not in config.allowed_action_codes:
            raise NightCoordinatorError(
                "PASS_NOT_ALLOWED", "a pass-enabled window must include the PASS action code"
            )
        action_codes = set(config.allowed_action_codes)
        grant_union: set[int] = set()
        if self._manager.execution_package is not None:
            for seat in config.allowed_seats:
                player = state.players[seat]
                if not player.alive:
                    raise NightCoordinatorError(
                        "PLAYER_DEAD", f"dead seat {seat} cannot receive a night action"
                    )
                active = self._manager._rule_skill_instances(
                    state,
                    seat,
                    GamePhase.NIGHT_ACTION.value,
                    allowed_codes=action_codes,
                    logical_window_id=logical_window_id,
                )
                active_codes = {skill.action_code for _instance, skill in active}
                grant_union.update(active_codes)
                if not active_codes and not config.allow_pass:
                    raise NightCoordinatorError(
                        "ACTION_NOT_ALLOWED",
                        f"seat {seat} has no active package skill in this night window",
                    )
            unsupported = action_codes - grant_union - ({299} if config.allow_pass else set())
            if unsupported:
                raise NightCoordinatorError(
                    "ACTION_NOT_ALLOWED",
                    "night window actions have no matching active package skills: "
                    f"{tuple(sorted(unsupported))}",
                )
            return
        for seat in config.allowed_seats:
            player = state.players[seat]
            if not player.alive:
                raise NightCoordinatorError(
                    "PLAYER_DEAD", f"dead seat {seat} cannot receive a night action"
                )
            active_codes = {
                ability.action_code
                for ability in player.granted_abilities
                if ability.timing is GamePhase.NIGHT_ACTION
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
            }
            grant_union.update(active_codes)
            if not (active_codes & action_codes) and not config.allow_pass:
                raise NightCoordinatorError(
                    "ACTION_NOT_ALLOWED",
                    f"seat {seat} has no active ability in this night window",
                )
        unsupported = action_codes - grant_union - ({299} if config.allow_pass else set())
        if unsupported:
            raise NightCoordinatorError(
                "ACTION_NOT_ALLOWED",
                "night window actions have no matching active grants: "
                f"{tuple(sorted(unsupported))}",
            )

    async def open_next_window(self, *, now: datetime | None = None) -> NightWindowProgress:
        """Install and return the next board window for the current phase."""

        state = await self._manager.snapshot()
        if state.phase not in {
            GamePhase.NIGHT_TEAM_CHAT,
            GamePhase.NIGHT_ACTION,
            GamePhase.NIGHT_RESOLVE,
        }:
            raise NightCoordinatorError(
                "PHASE_NOT_ALLOWED", "current phase is outside the night cycle"
            )
        board_window = self._current_board_window(state)
        if board_window.phase is not state.phase:
            raise NightCoordinatorError(
                "PHASE_MISMATCH",
                f"board window {board_window.window_id!r} expects {board_window.phase.value}",
            )
        physical_window_id = self._physical_window_id_for(board_window, state)
        existing_raw = state.action_windows.get(physical_window_id)
        if existing_raw is not None:
            existing = _load_window(existing_raw)
            if existing.closed_at is None:
                await self._publish_due_window_disclosures(board_window, existing, now=now)
                return NightWindowProgress(
                    board_window_id=board_window.window_id,
                    phase=board_window.phase,
                    order=board_window.order,
                    dependencies=tuple(board_window.depends_on),
                    action_window=existing,
                )
        for dependency in board_window.depends_on:
            dependency_id = self._physical_window_id(dependency, state)
            dependency_raw = state.action_windows.get(dependency_id)
            if dependency_raw is None:
                raise NightCoordinatorError(
                    "DEPENDENCY_UNSATISFIED",
                    f"night window {board_window.window_id!r} depends on {dependency!r}",
                )
            dependency_window = _load_window(dependency_raw)
            dependency_complete = (
                dependency_window.closed_at is not None
                or dependency_window.collection_complete_at is not None
            )
            if not dependency_complete and dependency_window.phase is GamePhase.NIGHT_ACTION:
                dependency_complete = self._action_submissions_complete(state, dependency_window)
            if not dependency_complete:
                raise NightCoordinatorError(
                    "DEPENDENCY_UNSATISFIED",
                    f"night window {board_window.window_id!r} depends on {dependency!r}",
                )
        window = self._build_action_window(state, board_window, now=now)
        committed = await self._manager.commit_action_window(window, now=now)
        installed = _load_window(committed.action_windows[physical_window_id])
        await self._publish_due_window_disclosures(board_window, installed, now=now)
        return NightWindowProgress(
            board_window_id=board_window.window_id,
            phase=board_window.phase,
            order=board_window.order,
            dependencies=tuple(board_window.depends_on),
            action_window=installed,
        )

    async def _publish_due_window_disclosures(
        self,
        board_window: NightWindow,
        action_window: ActionWindow,
        *,
        now: datetime | None,
    ) -> None:
        """Deliver declarations due as this frozen phase/window becomes active."""

        if self._manager.execution_package is None:
            return
        try:
            state = await self._manager.snapshot()
            await self._manager.publish_due_rule_disclosures(
                board_window.phase.value,
                expected_revision=state.state_revision,
                now=now,
            )
            state = await self._manager.snapshot()
            await self._manager.publish_due_rule_disclosures(
                action_window.logical_window_id or board_window.window_id,
                logical_window_id=action_window.logical_window_id or board_window.window_id,
                expected_revision=state.state_revision,
                now=now,
            )
        except (EventCommitError, ResolutionError, TypeError, ValueError) as exc:
            raise NightCoordinatorError("DISCLOSURE_PUBLISH_FAILED", str(exc)) from exc

    @staticmethod
    def _window_requests(state: GameState, window_id: str) -> dict[str, dict[str, JsonValue]]:
        return {
            request_id: payload
            for request_id, payload in state.action_requests.items()
            if isinstance(payload, dict) and payload.get("window_id") == window_id
        }

    def _action_submissions_complete(self, state: GameState, window: ActionWindow) -> bool:
        requests = self._window_requests(state, window.window_id)
        seats = {
            payload.get("seat")
            for payload in requests.values()
            if isinstance(payload.get("seat"), int)
        }
        return set(window.allowed_seats).issubset(seats)

    async def advance_from_current_window(
        self,
        *,
        expected_window_id: str | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Move from chat/action collection to the next night phase.

        ``NIGHT_ACTION`` advances after every authorized seat has submitted an
        intent.  The action window stays open while the game enters
        ``NIGHT_RESOLVE`` so the moderator can commit effects there.
        """

        state = await self._manager.snapshot()
        try:
            board_window = self._current_board_window(state)
        except NightCoordinatorError as exc:
            # Recover a crash after the final current-phase window was marked
            # collection-complete but before the phase edge was persisted.
            if exc.code != "NIGHT_COMPLETE" or self._manager.execution_package is None:
                raise
            current_phase_windows = tuple(
                item for item in self._ordered() if item.phase is state.phase
            )
            if not current_phase_windows or any(
                not self._collection_complete_for(
                    state,
                    self._physical_window_id_for(item, state),
                )
                for item in current_phase_windows
            ):
                raise
            last = max(current_phase_windows, key=lambda item: item.order)
            ordered = self._ordered()
            position = next(
                index for index, item in enumerate(ordered) if item.window_id == last.window_id
            )
            group_members = self._group_members(last)
            group_complete = bool(group_members) and group_members[-1].window_id == last.window_id
            if group_complete:
                group_id = self._settlement_group_id_for(last, state)
                if group_id is None:
                    raise NightCoordinatorError(
                        "WINDOW_GROUP_MISSING", "settlement group is missing"
                    )
                timestamp = now or datetime.now(UTC)
                try:
                    state = await self._manager.commit_rule_group(
                        group_id,
                        expected_revision=state.state_revision,
                        now=timestamp,
                    )
                    await self._manager.advance_rule_workflow(
                        expected_revision=state.state_revision,
                        now=timestamp,
                    )
                    return await self._manager.snapshot()
                except (ResolutionError, EventCommitError, ValueError) as exc:
                    raise NightCoordinatorError("RULE_GROUP_REJECTED", str(exc)) from exc
            if position + 1 >= len(ordered):
                raise NightCoordinatorError("NIGHT_COMPLETE", "board has no next night phase")
            return await self._manager.commit_phase_transition(ordered[position + 1].phase, now=now)
        if expected_window_id is not None and board_window.window_id != expected_window_id:
            raise NightCoordinatorError(
                "WINDOW_MISMATCH", "current board window differs from the request"
            )
        physical_window_id = self._physical_window_id_for(board_window, state)
        raw = state.action_windows.get(physical_window_id)
        if raw is None:
            raise NightCoordinatorError(
                "WINDOW_NOT_OPEN", "current board window has not been opened"
            )
        window = _load_window(raw)
        if window.closed_at is not None or window.collection_complete_at is not None:
            raise NightCoordinatorError("WINDOW_CLOSED", "current board window is already closed")
        if board_window.phase is GamePhase.NIGHT_ACTION:
            if not self._action_submissions_complete(state, window):
                raise NightCoordinatorError(
                    "SUBMISSIONS_INCOMPLETE",
                    "every authorized seat must submit before resolution",
                )
        ordered = self._ordered()
        position = next(
            index for index, item in enumerate(ordered) if item.window_id == board_window.window_id
        )
        if position + 1 >= len(ordered) and self._manager.execution_package is None:
            raise NightCoordinatorError("NIGHT_COMPLETE", "board has no next night phase")
        if self._manager.execution_package is None:
            if board_window.phase is GamePhase.NIGHT_TEAM_CHAT:
                try:
                    state = await self._manager.close_action_window(
                        physical_window_id,
                        expected_revision=state.state_revision,
                        now=now,
                    )
                except EventCommitError as exc:
                    raise NightCoordinatorError("WINDOW_CLOSE_FAILED", str(exc)) from exc
            return await self._manager.commit_phase_transition(
                ordered[position + 1].phase,
                now=now,
            )

        timestamp = now or datetime.now(UTC)
        try:
            state = await self._manager.complete_rule_window(
                physical_window_id,
                expected_revision=state.state_revision,
                now=timestamp,
            )
        except EventCommitError as exc:
            raise NightCoordinatorError("WINDOW_COMPLETE_FAILED", str(exc)) from exc

        members = self._group_members(board_window)
        group_is_complete = bool(members) and members[-1].window_id == board_window.window_id
        if group_is_complete:
            group_id = self._settlement_group_id_for(board_window, state)
            if group_id is None:
                raise NightCoordinatorError("WINDOW_GROUP_MISSING", "settlement group is missing")
            try:
                state = await self._manager.commit_rule_group(
                    group_id,
                    request_ids=None,
                    expected_revision=state.state_revision,
                    now=timestamp,
                )
                step = await self._manager.advance_rule_workflow(
                    expected_revision=state.state_revision,
                    now=timestamp,
                )
                state = await self._manager.snapshot()
            except (ResolutionError, EventCommitError, ValueError) as exc:
                raise NightCoordinatorError("RULE_GROUP_REJECTED", str(exc)) from exc
            if step.kind in {"AUTOMATIC", "PLAYER_CHOICE"} or step.queue_pending:
                # The manager has pinned the occurrence/window and current
                # workflow phase.  ModeratorTriggerFlow will drain it through
                # the regular ActionTurnScheduler boundary.
                return state
            # ``advance_rule_workflow`` is the phase restore authority.  Its
            # RETURN/IDLE result has already written the persisted coarse
            # phase and, when applicable, the next logical window cursor.
            return state

        # Same-group windows stay provisional through collection.  A different
        # group commits only after its last window is collected, after which
        # the durable cursor may pause on a trigger and return to this exact
        # next logical board window.
        if state.phase is not board_window.phase:
            return state
        if position + 1 >= len(ordered):
            raise NightCoordinatorError("NIGHT_COMPLETE", "board has no next night phase")
        next_board_window = ordered[position + 1]
        if next_board_window.phase is board_window.phase:
            try:
                await self._manager.advance_rule_workflow(
                    expected_revision=state.state_revision,
                    now=timestamp,
                )
                return await self._manager.snapshot()
            except (ResolutionError, EventCommitError, ValueError) as exc:
                raise NightCoordinatorError("RULE_WORKFLOW_REJECTED", str(exc)) from exc
        return await self._manager.commit_phase_transition(
            next_board_window.phase,
            now=timestamp,
        )

    @staticmethod
    def _collection_complete_for(state: GameState, window_id: str) -> bool:
        raw = state.action_windows.get(window_id)
        if raw is None:
            return False
        window = _load_window(raw)
        return window.closed_at is not None or window.collection_complete_at is not None

    async def confirm_night(
        self,
        resolutions: tuple[ActionResolution, ...],
        *,
        moderator_id: str | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Atomically apply all pending night effects at ``NIGHT_RESOLVE``."""

        del moderator_id  # moderator identity is carried by each resolution
        state = await self._manager.snapshot()
        if state.phase is not GamePhase.NIGHT_RESOLVE:
            raise NightCoordinatorError("PHASE_NOT_ALLOWED", "night effects require NIGHT_RESOLVE")
        if self._manager.execution_package is not None:
            if resolutions:
                raise NightCoordinatorError(
                    "RULE_RESOLUTION_UNAVAILABLE",
                    "executable packages settle manager-recomputed rule requests, "
                    "not supplied rulings",
                )
            return await self.advance_from_current_window(now=now)
        action_ids = [
            item.window_id for item in self._ordered() if item.phase is GamePhase.NIGHT_ACTION
        ]
        if len(action_ids) != 1:
            raise NightCoordinatorError(
                "ACTION_WINDOW_AMBIGUOUS", "V1 night resolution requires exactly one action window"
            )
        resolve_windows = [
            item for item in self._ordered() if item.phase is GamePhase.NIGHT_RESOLVE
        ]
        if len(resolve_windows) > 1:
            raise NightCoordinatorError(
                "RESOLVE_WINDOW_AMBIGUOUS", "V1 night resolution requires one resolve window"
            )
        resolve_window = resolve_windows[0] if resolve_windows else None
        if resolve_window is None:
            raise NightCoordinatorError(
                "WINDOW_NOT_OPEN", "the board resolve window must be opened before confirmation"
            )
        resolve_window_id = self._physical_window_id_for(resolve_window, state)
        resolve_raw = state.action_windows.get(resolve_window_id)
        if resolve_raw is None or _load_window(resolve_raw).closed_at is not None:
            raise NightCoordinatorError(
                "WINDOW_CLOSED", "the board resolve window is not open for confirmation"
            )
        action_window_id = self._physical_window_id(action_ids[0], state)
        pending = {
            request_id
            for request_id, payload in self._window_requests(state, action_window_id).items()
            if payload.get("status") == "PENDING"
        }
        supplied = {item.request_id for item in resolutions}
        action_requests = self._window_requests(state, action_window_id)
        if (
            not pending
            and not resolutions
            and action_requests
            and all(payload.get("status") != "PENDING" for payload in action_requests.values())
        ):
            try:
                return await self._manager.finalize_night_resolution(
                    action_window_id=action_window_id,
                    resolve_window_id=resolve_window_id,
                    expected_revision=state.state_revision,
                    now=now,
                )
            except EventCommitError as exc:
                raise NightCoordinatorError("WINDOW_CLOSE_FAILED", str(exc)) from exc
        if pending != supplied:
            raise NightCoordinatorError(
                "RESOLUTION_INCOMPLETE",
                "resolution must include exactly every pending night request",
            )
        if not resolutions:
            raise NightCoordinatorError("RESOLUTION_EMPTY", "night resolution cannot be empty")
        try:
            committed = await self._manager.commit_night_resolution(
                resolutions,
                action_window_id=action_window_id,
                resolve_window_id=resolve_window_id,
                board=self._board,
                use_rules_engine=self._manager.execution_package is not None,
                expected_revision=state.state_revision,
                now=now,
            )
        except (ResolutionError, EventCommitError) as exc:
            raise NightCoordinatorError("RESOLUTION_REJECTED", str(exc)) from exc
        action_payload = committed.action_windows.get(action_window_id)
        if action_payload is None or _load_window(action_payload).closed_at is None:
            raise NightCoordinatorError(
                "RESOLUTION_INCOMPLETE", "night action window remained open after confirmation"
            )
        resolve_raw = committed.action_windows.get(resolve_window_id)
        if resolve_raw is None or _load_window(resolve_raw).closed_at is None:
            raise NightCoordinatorError(
                "WINDOW_CLOSE_FAILED", "the board resolve window remained open after confirmation"
            )
        if committed.phase is GamePhase.TRIGGER_ACTION:
            # Install the generated window at the same coordinator boundary
            # that discovers the death trigger.  The manager binds its ID to
            # the pending marker atomically, so a retry cannot create a
            # second owner for the dead seat's action.
            await self.open_trigger_action(now=now)
            return await self._manager.snapshot()
        return committed

    async def open_trigger_action(
        self,
        *,
        now: datetime | None = None,
    ) -> NightWindowProgress:
        """Install the pending night death-trigger action window.

        Trigger windows are generated from the granted ability copied onto
        the seat at setup.  No role name or board prose is consulted here;
        the pending marker is the serialized hand-off from NIGHT_RESOLVE.
        """

        state = await self._manager.snapshot()
        if state.phase is not GamePhase.TRIGGER_ACTION:
            raise NightCoordinatorError(
                "PHASE_NOT_ALLOWED", "night trigger action requires TRIGGER_ACTION"
            )
        pending = state.pending_resolution
        if not isinstance(pending, dict) or pending.get("operation") != "NIGHT_RESOLUTION":
            raise NightCoordinatorError(
                "TRIGGER_ACTION_INVALID", "no pending night death trigger is available"
            )
        window_id = pending.get("window_id")
        if isinstance(window_id, str):
            raw = state.action_windows.get(window_id)
            if raw is not None:
                existing = _load_window(raw)
                if existing.closed_at is None:
                    return NightWindowProgress(
                        board_window_id="trigger_action",
                        phase=GamePhase.TRIGGER_ACTION,
                        order=0,
                        dependencies=(),
                        action_window=existing,
                    )
        from .day_resolution import build_trigger_action_window

        try:
            window = build_trigger_action_window(self._board, state)
            committed = await self._manager.commit_action_window(window, now=now)
            installed = _load_window(committed.action_windows[window.window_id])
        except (EventCommitError, ValueError, KeyError) as exc:
            raise NightCoordinatorError("TRIGGER_ACTION_INVALID", str(exc)) from exc
        return NightWindowProgress(
            board_window_id="trigger_action",
            phase=GamePhase.TRIGGER_ACTION,
            order=0,
            dependencies=(),
            action_window=installed,
        )

    async def finish_trigger_action(self, *, now: datetime | None = None) -> GameState:
        """Enter DAY_ANNOUNCE after the night trigger was resolved."""

        state = await self._manager.snapshot()
        if state.phase is not GamePhase.TRIGGER_ACTION:
            raise NightCoordinatorError(
                "PHASE_NOT_ALLOWED", "night trigger completion requires TRIGGER_ACTION"
            )
        if state.pending_resolution is not None:
            raise NightCoordinatorError(
                "TRIGGER_ACTION_PENDING",
                "the night trigger must be resolved, including an explicit PASS",
            )
        trigger_windows = [
            _load_window(raw)
            for raw in state.action_windows.values()
            if isinstance(raw, Mapping) and raw.get("phase") == GamePhase.TRIGGER_ACTION.value
        ]
        if any(window.closed_at is None for window in trigger_windows):
            raise NightCoordinatorError(
                "TRIGGER_ACTION_PENDING", "the night trigger action window is still open"
            )
        try:
            return await self._manager.commit_phase_transition(GamePhase.DAY_ANNOUNCE, now=now)
        except (EventCommitError, ValueError) as exc:
            raise NightCoordinatorError("PHASE_TRANSITION_FAILED", str(exc)) from exc

    # Compact aliases for callers that use the technical plan's vocabulary.
    open = open_next_window
    advance = advance_from_current_window
    resolve = confirm_night


__all__ = [
    "NightCoordinator",
    "NightCoordinatorError",
    "NightWindowConfig",
    "NightWindowProgress",
]
