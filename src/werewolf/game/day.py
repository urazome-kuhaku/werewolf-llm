"""Coordinator for the board-defined daytime lifecycle.

The coordinator owns sequencing and guard checks only.  Player effects are
still a moderator resolution concern; in particular, confirming a vote does
not silently mark a player dead.  This keeps a published board's exile,
trigger, last-word, and victory rules outside a generic vote collector.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.preview import experimental_preview_enabled
from werewolf.runtime.player_runtime import PlayerRuntime

from .day_resolution import (
    DayExileDecision,
    build_day_exile_decision_for_window,
)
from .events import EventType, GameEvent, PublicAnnouncementPayload
from .manager import GameManager
from .serial_turn import (
    SerialSpeechResult,
    SerialTurnError,
    SerialTurnScheduler,
    TurnQueue,
)
from .state import GameState, utc_now
from .vote_turn import VoteTurnError, VoteTurnResult, VoteTurnScheduler
from .voting import (
    TieAction,
    TieDecision,
    TieResolver,
    VoteError,
    VoteRequest,
    VoteState,
    VoteStatus,
    VoteTally,
    VoteWindow,
)


class DayCoordinatorError(RuntimeError):
    """Raised when a daytime lifecycle guard or board contract fails."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DayVoteProgress:
    """The frozen vote contract installed for one daytime vote."""

    window: VoteWindow
    visibility_during_collection: str
    is_pk: bool


@dataclass(frozen=True, slots=True)
class DaySpeechProgress:
    """The queue and phase used by one daytime serial speech section."""

    phase: GamePhase
    queue: TurnQueue
    is_pk: bool


def _vote_state(raw: object) -> VoteState:
    if not isinstance(raw, dict):
        raise DayCoordinatorError("VOTE_STATE_INVALID", "stored vote state is malformed")
    try:
        return VoteState.model_validate_json(json.dumps(raw))
    except (TypeError, ValueError) as exc:
        raise DayCoordinatorError("VOTE_STATE_INVALID", "stored vote state is malformed") from exc


def _event_id(state: GameState) -> int:
    typed = [event.event_id for event in state.events if isinstance(event, GameEvent)]
    return max(typed, default=0) + 1


def _night_death_seats(state: GameState, board: BoardDefinition) -> tuple[int, ...]:
    """Return seats killed by the current night resolution.

    Death provenance is read from the frozen NIGHT_ACTION window and its
    committed resolution effects.  A dead seat from an older round therefore
    cannot be mistaken for the current announcement, and poison or hunter
    shots remain distinguishable by their persisted cause.
    """

    policy = board.day_flow.last_words.night_death_policy
    if policy == "none" or (policy == "first_night_only" and state.round_no != 0):
        return ()
    eligible_causes = set(board.day_flow.last_words.eligible_death_causes)
    if not eligible_causes:
        return ()
    seats: set[int] = set()
    for raw_resolution in state.resolutions:
        if not isinstance(raw_resolution, dict):
            continue
        window_id = raw_resolution.get("window_id")
        if not isinstance(window_id, str):
            continue
        raw_window = state.action_windows.get(window_id)
        if (
            not isinstance(raw_window, dict)
            or raw_window.get("phase") != GamePhase.NIGHT_ACTION.value
        ):
            continue
        visible = raw_window.get("visible_context")
        if not isinstance(visible, dict) or visible.get("night_round") != state.round_no:
            continue
        try:
            resolutions = raw_resolution.get("actions")
        except AttributeError:  # pragma: no cover - guarded by dict check
            continue
        if not isinstance(resolutions, (list, tuple)):
            continue
        for entry in resolutions:
            if not isinstance(entry, dict):
                continue
            effects = entry.get("effects")
            if not isinstance(effects, (list, tuple)):
                continue
            killed: set[int] = set()
            causes: dict[int, str] = {}
            for effect in effects:
                if not isinstance(effect, dict):
                    continue
                target = effect.get("target_seat")
                if type(target) is not int:
                    continue
                if effect.get("effect_type") == "SET_ALIVE" and effect.get("value") is False:
                    killed.add(target)
                elif effect.get("effect_type") == "SET_DEATH_CAUSE":
                    cause = effect.get("value")
                    if isinstance(cause, str):
                        causes[target] = cause
            for seat in killed:
                player = state.players.get(seat)
                cause = causes.get(seat)
                if (
                    player is not None
                    and not player.alive
                    and player.death_cause == cause
                    and cause in eligible_causes
                ):
                    seats.add(seat)
    return tuple(sorted(seats))


def _night_announcement_death_seats(state: GameState) -> tuple[int, ...]:
    """Return every death caused by the current night's committed work.

    This projection serves the public dawn announcement and intentionally has
    no last-words policy or eligible-cause filter.  A board may suppress a
    last-words turn for a poison death while still requiring that death to be
    announced.  The current round is recovered from the frozen night action
    window; a trigger action is included only when its frozen provenance says
    it belongs to a ``NIGHT_RESOLUTION`` and points back to that round's
    original resolution.
    """

    current_round = state.round_no
    current_night_resolution_ids: set[str] = set()
    current_night_window_ids: set[str] = set()
    deaths: set[int] = set()

    def collect(raw_resolution: object) -> None:
        if not isinstance(raw_resolution, Mapping):
            return
        raw_actions = raw_resolution.get("actions")
        if not isinstance(raw_actions, (list, tuple)):
            return
        for raw_action in raw_actions:
            if not isinstance(raw_action, Mapping):
                continue
            raw_effects = raw_action.get("effects")
            if not isinstance(raw_effects, (list, tuple)):
                continue
            killed: set[int] = set()
            causes: dict[int, str] = {}
            for raw_effect in raw_effects:
                if not isinstance(raw_effect, Mapping):
                    continue
                target = raw_effect.get("target_seat")
                if type(target) is not int:
                    continue
                if (
                    raw_effect.get("effect_type") == "SET_ALIVE"
                    and raw_effect.get("value") is False
                ):
                    killed.add(target)
                elif raw_effect.get("effect_type") == "SET_DEATH_CAUSE":
                    cause = raw_effect.get("value")
                    if isinstance(cause, str):
                        causes[target] = cause
            for seat in killed:
                player = state.players.get(seat)
                cause = causes.get(seat)
                # Valid death resolutions persist both effects.  Requiring
                # the matching current death cause prevents an old SET_ALIVE
                # record from re-announcing a historical death.
                if (
                    player is not None
                    and not player.alive
                    and cause is not None
                    and player.death_cause == cause
                ):
                    deaths.add(seat)

    for raw_resolution in state.resolutions:
        if not isinstance(raw_resolution, Mapping):
            continue
        resolution_id = raw_resolution.get("resolution_id")
        window_id = raw_resolution.get("window_id")
        if not isinstance(resolution_id, str) or not isinstance(window_id, str):
            continue
        raw_window = state.action_windows.get(window_id)
        if not isinstance(raw_window, Mapping):
            continue
        phase = raw_window.get("phase")
        visible = raw_window.get("visible_context")
        if (
            phase == GamePhase.NIGHT_ACTION.value
            and isinstance(visible, Mapping)
            and visible.get("night_round") == current_round
        ):
            current_night_resolution_ids.add(resolution_id)
            current_night_window_ids.add(window_id)
            collect(raw_resolution)

    if not current_night_resolution_ids:
        return ()

    for raw_resolution in state.resolutions:
        if not isinstance(raw_resolution, Mapping):
            continue
        window_id = raw_resolution.get("window_id")
        if not isinstance(window_id, str) or window_id in current_night_window_ids:
            continue
        raw_window = state.action_windows.get(window_id)
        if not isinstance(raw_window, Mapping):
            continue
        visible = raw_window.get("visible_context")
        if (
            raw_window.get("phase") == GamePhase.TRIGGER_ACTION.value
            and isinstance(visible, Mapping)
            and visible.get("operation") == "NIGHT_RESOLUTION"
            and visible.get("resolution_id") in current_night_resolution_ids
        ):
            collect(raw_resolution)

    return tuple(sorted(deaths))


class DayCoordinator:
    """Coordinate announcement, discussion, vote, optional PK, and resolve."""

    def __init__(
        self,
        manager: GameManager,
        board: BoardDefinition,
        runtimes: Mapping[int, PlayerRuntime] | None = None,
        *,
        snapshot_id: str | None = None,
        timeout_seconds: float | None = None,
        tie_resolver: TieResolver | None = None,
    ) -> None:
        if not isinstance(manager, GameManager):
            raise TypeError("manager must be a GameManager")
        if not isinstance(board, BoardDefinition):
            raise TypeError("board must be a BoardDefinition")
        if board.status != "published":
            raise DayCoordinatorError(
                "BOARD_NOT_PUBLISHED", "day coordination requires a published board"
            )
        if board.reviewed_by == "pending-human-review" and not experimental_preview_enabled():
            raise DayCoordinatorError(
                "BOARD_NOT_REVIEWED", "day coordination requires a human-reviewed board"
            )
        state = manager.state
        if state.ruleset is None:
            raise DayCoordinatorError("RULESET_MISSING", "game has no frozen ruleset reference")
        if state.ruleset.board_id != board.board_id or state.ruleset.version != board.version:
            raise DayCoordinatorError("RULESET_MISMATCH", "board does not match the game ruleset")
        if snapshot_id is not None and state.ruleset.snapshot_id != snapshot_id:
            raise DayCoordinatorError(
                "SNAPSHOT_MISMATCH", "board is not bound to the game snapshot"
            )
        self._manager = manager
        self._board = board
        self._runtimes = dict(runtimes or {})
        self._timeout_seconds = timeout_seconds
        self._tie_resolver = tie_resolver
        self._speech_scheduler: SerialTurnScheduler | None = None
        self._speech_progress: DaySpeechProgress | None = None
        self._vote_scheduler = VoteTurnScheduler(
            manager,
            self._runtimes,
            timeout_seconds=timeout_seconds,
        )
        self._pk_candidates: tuple[int, ...] = ()
        self._restore_pk_candidates(state)

    def _restore_pk_candidates(self, state: GameState) -> None:
        """Recover PK candidates from the durable vote snapshot.

        PK candidates are part of the confirmed tally contract.  Keeping them
        only on a coordinator instance made a process restart lose the frozen
        candidate set even though the vote itself was already resolved.
        """

        if state.vote_state is None:
            return
        try:
            vote_state = _vote_state(state.vote_state)
        except DayCoordinatorError:
            return
        if vote_state.tie_decision is not None and vote_state.tie_decision.action is TieAction.PK:
            self._pk_candidates = tuple(sorted(vote_state.tie_decision.candidates))
        elif (
            vote_state.public_result is not None
            and vote_state.public_result.tie_action is TieAction.PK
        ):
            self._pk_candidates = tuple(sorted(vote_state.public_result.tally.top_candidates))

    @property
    def board(self) -> BoardDefinition:
        return self._board

    @property
    def vote_visibility(self) -> str:
        """The immutable board setting for collection visibility."""

        return self._board.day_flow.vote.visibility_during_collection

    @property
    def speech_progress(self) -> DaySpeechProgress | None:
        return self._speech_progress

    @property
    def vote_progress(self) -> DayVoteProgress | None:
        state = self._manager.state
        if state.vote_state is None:
            return None
        vote_state = _vote_state(state.vote_state)
        return DayVoteProgress(
            window=vote_state.window,
            visibility_during_collection=self.vote_visibility,
            is_pk=vote_state.window.window_id.startswith("day-pk-vote-")
            or state.phase is GamePhase.VOTE_PK,
        )

    async def announce(
        self,
        content: str | None = None,
        *,
        now: datetime | None = None,
    ) -> GameState:
        """Publish the moderator's day announcement and enter DAY_SPEECH.

        Repeating the call for the same day is idempotent if the announcement
        event was already committed before a process interruption.
        """

        state = await self._manager.snapshot()
        if state.phase is not GamePhase.DAY_ANNOUNCE:
            raise DayCoordinatorError("PHASE_NOT_ALLOWED", "day announcement requires DAY_ANNOUNCE")
        await self.commit_announcement(content, now=now)
        timestamp = now or utc_now()
        return await self._manager.commit_phase_transition(GamePhase.DAY_SPEECH, now=timestamp)

    async def commit_announcement(
        self,
        content: str | None = None,
        *,
        now: datetime | None = None,
    ) -> GameState:
        """Commit the public day announcement while keeping DAY_ANNOUNCE active.

        The sheriff command uses this boundary before starting its dedicated
        election phase.  It also gives hosts a place to require last words
        before moving from the announcement into ordinary speeches.
        """

        state = await self._manager.snapshot()
        if state.phase not in {GamePhase.DAY_ANNOUNCE, GamePhase.DAY_SPEECH}:
            raise DayCoordinatorError(
                "PHASE_NOT_ALLOWED", "announcement requires DAY_ANNOUNCE or DAY_SPEECH"
            )
        death_seats = _night_announcement_death_seats(state)
        last_words_death_seats = _night_death_seats(state, self._board)
        if content is None:
            if self._board.day_flow.announce_deaths:
                if death_seats:
                    text = (
                        f"第 {state.day_no} 天开始。昨夜死亡座位："
                        + "、".join(str(seat) for seat in death_seats)
                        + (
                            "。请按板子规则处理遗言。"
                            if self._board.day_flow.last_words.enabled and last_words_death_seats
                            else "。"
                        )
                    )
                else:
                    text = f"第 {state.day_no} 天开始。昨夜平安。"
            else:
                text = f"第 {state.day_no} 天开始。"
        else:
            text = content
        if not isinstance(text, str) or not text.strip() or len(text) > 8_000:
            raise DayCoordinatorError("ANNOUNCEMENT_INVALID", "announcement must be non-empty")
        correlation_id = f"day-announce-r{state.round_no}-d{state.day_no}"
        already = any(
            isinstance(event, GameEvent)
            and event.event_type is EventType.ANNOUNCEMENT
            and event.correlation_id == correlation_id
            for event in state.events
        )
        timestamp = now or utc_now()
        if not already:
            event = GameEvent.public(
                event_id=_event_id(state),
                game_id=state.game_id,
                state_revision=state.state_revision + 1,
                round_no=state.round_no,
                phase=GamePhase.DAY_ANNOUNCE,
                created_at=timestamp,
                event_type=EventType.ANNOUNCEMENT,
                eligible_seats=tuple(sorted(state.players)),
                payload=PublicAnnouncementPayload(content=text),
                correlation_id=correlation_id,
            )
            state = await self._manager.commit_events((event,), now=timestamp)
        return state

    async def advance_from_announce(self, *, now: datetime | None = None) -> GameState:
        """Enter discussion without creating an announcement event."""

        state = await self._manager.snapshot()
        if state.phase is not GamePhase.DAY_ANNOUNCE:
            raise DayCoordinatorError(
                "PHASE_NOT_ALLOWED", "announcement boundary requires DAY_ANNOUNCE"
            )
        return await self._manager.commit_phase_transition(GamePhase.DAY_SPEECH, now=now)

    async def open_speech(
        self,
        queue: TurnQueue | Sequence[int] | None = None,
        *,
        is_pk: bool = False,
        now: datetime | None = None,
    ) -> DaySpeechProgress:
        """Install the serial speech queue for ordinary or PK discussion."""

        state = await self._manager.snapshot()
        expected_phase = GamePhase.VOTE_PK_SPEECH if is_pk else GamePhase.DAY_SPEECH
        if state.phase is GamePhase.DAY_ANNOUNCE and not is_pk:
            state = await self._manager.commit_phase_transition(GamePhase.DAY_SPEECH, now=now)
        elif state.phase is not expected_phase:
            raise DayCoordinatorError(
                "PHASE_NOT_ALLOWED", f"speech requires {expected_phase.value}"
            )
        if queue is None:
            seats = tuple(seat for seat, player in sorted(state.players.items()) if player.alive)
            sheriff = self._board.day_flow.sheriff
            sheriff_seat = state.sheriff_seat
            if sheriff.final_speech and sheriff_seat in seats:
                seats = tuple(seat for seat in seats if seat != sheriff_seat) + (sheriff_seat,)
            if is_pk and self._pk_candidates:
                seats = self._pk_candidates
        else:
            seats = tuple(queue.seats if isinstance(queue, TurnQueue) else queue)
        if not seats:
            raise DayCoordinatorError("NO_ELIGIBLE_SEATS", "speech queue has no live seats")
        if len(set(seats)) != len(seats):
            raise DayCoordinatorError("QUEUE_INVALID", "speech queue contains duplicate seats")
        unknown = tuple(seat for seat in seats if seat not in state.players)
        dead = tuple(
            seat for seat in seats if seat in state.players and not state.players[seat].alive
        )
        if unknown:
            raise DayCoordinatorError(
                "SEAT_NOT_ASSIGNED", f"speech queue has unknown seats: {unknown}"
            )
        if dead:
            raise DayCoordinatorError("PLAYER_DEAD", f"speech queue has dead seats: {dead}")
        selected = TurnQueue(seats)
        if self._speech_scheduler is None:
            self._speech_scheduler = SerialTurnScheduler(
                self._manager,
                self._runtimes,
                timeout_seconds=self._timeout_seconds,
                phase=expected_phase,
            )
        if state.current_queue is None or state.current_queue == ():
            await self._speech_scheduler.start(selected)
        elif tuple(state.current_queue) != seats:
            raise DayCoordinatorError("QUEUE_CONFLICT", "another serial queue is already installed")
        self._speech_progress = DaySpeechProgress(expected_phase, selected, is_pk)
        return self._speech_progress

    async def run_next_speech(self) -> SerialSpeechResult:
        """Run the current queue head and commit its public speech."""

        if self._speech_scheduler is None:
            state = await self._manager.snapshot()
            if state.phase not in {GamePhase.DAY_SPEECH, GamePhase.VOTE_PK_SPEECH}:
                raise DayCoordinatorError("PHASE_NOT_ALLOWED", "no speech phase is active")
            await self.open_speech(is_pk=state.phase is GamePhase.VOTE_PK_SPEECH)
        assert self._speech_scheduler is not None
        try:
            return await self._speech_scheduler.run_next()
        except SerialTurnError as exc:
            raise DayCoordinatorError("SPEECH_FAILED", str(exc)) from exc

    async def retry_speech(self) -> SerialSpeechResult:
        if self._speech_scheduler is None:
            raise DayCoordinatorError("SPEECH_NOT_OPEN", "no speech request is available to retry")
        try:
            return await self._speech_scheduler.retry()
        except SerialTurnError as exc:
            raise DayCoordinatorError("SPEECH_RETRY_FAILED", str(exc)) from exc

    async def advance_from_speech(self, *, now: datetime | None = None) -> GameState:
        """Close ordinary discussion after the queue has been consumed."""

        state = await self._manager.snapshot()
        if state.phase is not GamePhase.DAY_SPEECH or state.current_queue != ():
            raise DayCoordinatorError(
                "SPEECH_INCOMPLETE", "all daytime speeches must be committed first"
            )
        self._speech_scheduler = None
        self._speech_progress = None
        return await self._manager.commit_phase_transition(GamePhase.VOTE, now=now)

    async def advance_from_pk_speech(self, *, now: datetime | None = None) -> GameState:
        """Close PK speeches and enter the board-defined PK vote phase."""

        state = await self._manager.snapshot()
        if state.phase is not GamePhase.VOTE_PK_SPEECH or state.current_queue != ():
            raise DayCoordinatorError(
                "SPEECH_INCOMPLETE", "all PK speeches must be committed first"
            )
        self._speech_scheduler = None
        self._speech_progress = None
        return await self._manager.commit_phase_transition(GamePhase.VOTE_PK, now=now)

    async def open_vote(
        self, *, is_pk: bool = False, now: datetime | None = None
    ) -> DayVoteProgress:
        """Open one observation-frozen board vote window."""

        state = await self._manager.snapshot()
        expected_phase = GamePhase.VOTE_PK if is_pk else GamePhase.VOTE
        if state.phase is not expected_phase:
            raise DayCoordinatorError("PHASE_NOT_ALLOWED", f"vote requires {expected_phase.value}")
        if is_pk and not self._board.day_flow.pk.enabled:
            raise DayCoordinatorError("PK_DISABLED", "the published board does not enable PK")
        if state.vote_state is not None:
            current = _vote_state(state.vote_state)
            if current.status is not VoteStatus.RESOLVED:
                raise DayCoordinatorError("VOTE_WINDOW_ACTIVE", "another vote window is active")
        eligible = tuple(
            seat
            for seat, player in sorted(state.players.items())
            if player.alive and player.can_vote
        )
        if not eligible:
            raise DayCoordinatorError("NO_ELIGIBLE_VOTERS", "there are no live voters")
        if is_pk:
            candidates = self._pk_candidates
            if not candidates:
                raise DayCoordinatorError("PK_CANDIDATES_MISSING", "PK candidates are not frozen")
            if len(candidates) > self._board.day_flow.pk.max_candidates:
                raise DayCoordinatorError(
                    "PK_CANDIDATES_INVALID", "PK candidate count exceeds board limit"
                )
        else:
            candidates = tuple(
                seat for seat, player in sorted(state.players.items()) if player.alive
            )
        if not candidates:
            raise DayCoordinatorError("NO_CANDIDATES", "there are no live vote candidates")
        epochs = {state.players[seat].session_epoch for seat in eligible}
        if len(epochs) != 1:
            raise DayCoordinatorError(
                "SESSION_MISMATCH", "one vote window requires one session epoch"
            )
        window_id = (
            f"day-pk-vote-r{state.round_no}-d{state.day_no}"
            if is_pk
            else f"day-vote-r{state.round_no}-d{state.day_no}"
        )
        expected_request_ids = {
            seat: request_id
            for seat in eligible
            if (request_id := state.players[seat].current_request_id) is not None
        }
        window = VoteWindow(
            window_id=window_id,
            game_id=state.game_id,
            session_epoch=next(iter(epochs)),
            observation_revision=state.state_revision,
            eligible_voters=eligible,
            candidate_seats=tuple(sorted(candidates)),
            vote_weights={seat: state.players[seat].vote_weight for seat in eligible},
            expected_request_ids=expected_request_ids,
            allow_abstain=self._board.day_flow.vote.allow_abstain,
        )
        committed = await self._manager.open_vote_window(window, now=now)
        current = _vote_state(committed.vote_state)
        # A new durable vote window gets a fresh request-binding boundary.
        # Ballots remain in the manager; failed runtime turns from a previous
        # window must never be retried against this one.
        self._vote_scheduler = VoteTurnScheduler(
            self._manager,
            self._runtimes,
            timeout_seconds=self._timeout_seconds,
        )
        return DayVoteProgress(
            current.window,
            self.vote_visibility,
            is_pk,
        )

    async def submit_vote(self, request: VoteRequest, *, now: datetime | None = None) -> GameState:
        try:
            return await self._manager.submit_vote(request, now=now)
        except VoteError as exc:
            raise DayCoordinatorError(exc.code, str(exc)) from exc

    async def run_next_vote(self, *, seat: int | None = None) -> VoteTurnResult:
        """Ask the next eligible seat for one private ballot."""

        try:
            return await self._vote_scheduler.run_next(seat=seat)
        except VoteTurnError as exc:
            raise DayCoordinatorError("VOTE_FAILED", str(exc)) from exc

    async def retry_vote(self, *, seat: int | None = None) -> VoteTurnResult:
        """Retry a timed-out or rejected runtime ballot."""

        try:
            return await self._vote_scheduler.retry(seat=seat)
        except VoteTurnError as exc:
            raise DayCoordinatorError("VOTE_RETRY_FAILED", str(exc)) from exc

    async def finalize_vote(
        self,
        *,
        force: bool = False,
        reason: str = "all_votes_received",
        tie_resolver: TieResolver | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Lock ballots and create a moderator-pending tally."""

        # The board policy is the only default source for a tie decision.  A
        # caller may still inject a resolver for a reviewed board variant or
        # for a test, but the round is always derived from the authoritative
        # phase here.  In particular, a caller cannot claim that a PK ballot
        # is the first round by passing an arbitrary round number.
        state = await self._manager.snapshot()
        if state.phase not in {GamePhase.VOTE, GamePhase.VOTE_PK}:
            raise DayCoordinatorError(
                "PHASE_NOT_ALLOWED", "vote finalization is outside a vote phase"
            )
        if tie_resolver is not None:
            resolver = tie_resolver
        elif self._tie_resolver is not None:
            resolver = self._tie_resolver
        else:
            policy = self._board.day_flow.vote.tie_policy
            if policy is None:
                # Keep the explicit missing-strategy rejection in the voting
                # reducer.  This is significant for a board whose knowledge
                # package has not declared tie behavior.
                resolver = None
            else:
                tie_round = 0 if state.phase is GamePhase.VOTE else 1

                def resolver(window: VoteWindow, tally: VoteTally) -> TieDecision:
                    # Import lazily so non-tie votes remain usable with an
                    # older persisted package while the shared resolver is
                    # being loaded.  The function is called only when the
                    # reducer has found a tie.
                    from .voting import resolve_board_tie

                    del window
                    candidates = tuple(tally.top_candidates)
                    return resolve_board_tie(
                        policy,
                        tie_round,
                        candidates,
                        pk_enabled=self._board.day_flow.pk.enabled,
                    )

        try:
            return await self._manager.finalize_vote(
                force=force,
                reason=reason,
                tie_resolver=resolver,
                now=now,
            )
        except VoteError as exc:
            raise DayCoordinatorError(exc.code, str(exc)) from exc

    async def confirm_vote(self, *, now: datetime | None = None) -> GameState:
        """Confirm the safe tally and route to PK or DAY_RESOLVE atomically."""

        state = await self._manager.snapshot()
        if state.phase not in {GamePhase.VOTE, GamePhase.VOTE_PK}:
            raise DayCoordinatorError(
                "PHASE_NOT_ALLOWED", "vote confirmation is outside a vote phase"
            )
        if state.vote_state is None:
            raise DayCoordinatorError("VOTE_WINDOW_NOT_OPEN", "there is no vote window to confirm")
        pending = _vote_state(state.vote_state)
        if pending.status is not VoteStatus.WAITING_GM:
            if pending.status is VoteStatus.RESOLVED:
                self._restore_pk_candidates(state)
                return await self._manager.confirm_vote_and_transition(self._board, now=now)
            raise DayCoordinatorError(
                "TALLY_NOT_PENDING", "a vote tally is not awaiting confirmation"
            )
        decision = pending.tie_decision
        if decision is not None and decision.action is TieAction.PK:
            if not self._board.day_flow.pk.enabled:
                raise DayCoordinatorError(
                    "PK_DISABLED", "tie resolver requested PK but board PK is disabled"
                )
            self._pk_candidates = tuple(sorted(decision.candidates))
        return await self._manager.confirm_vote_and_transition(self._board, now=now)

    async def confirm_exile(
        self,
        target_seat: int | None = None,
        *,
        now: datetime | None = None,
    ) -> GameState:
        """Commit a moderator-confirmed exile result for the resolved vote.

        The vote result remains a public tally fact.  This explicit boundary
        applies board role rules and is the only daytime path that changes a
        player's alive, death-cause, or vote-right fields.
        """

        state = await self._manager.snapshot()
        if state.phase is not GamePhase.DAY_RESOLVE:
            raise DayCoordinatorError(
                "PHASE_NOT_ALLOWED", "exile confirmation requires DAY_RESOLVE"
            )
        if state.vote_state is None:
            raise DayCoordinatorError("VOTE_WINDOW_NOT_OPEN", "there is no vote result to resolve")
        resolved = _vote_state(state.vote_state)
        if resolved.status is not VoteStatus.RESOLVED or resolved.public_result is None:
            raise DayCoordinatorError(
                "TALLY_NOT_CONFIRMED", "confirm the vote before resolving exile"
            )
        if target_seat != resolved.public_result.eliminated_seat:
            raise DayCoordinatorError(
                "TARGET_MISMATCH", "exile target must match the confirmed vote result"
            )
        try:
            if self._manager.execution_package is not None:
                return await self._manager.commit_confirmed_vote_exile(
                    target_seat=resolved.public_result.eliminated_seat,
                    vote_window_id=resolved.window.window_id,
                    expected_revision=state.state_revision,
                    now=now,
                )
            decision: DayExileDecision = build_day_exile_decision_for_window(
                self._board,
                state,
                resolved.public_result,
                window_id=resolved.window.window_id,
            )
            return await self._manager.commit_day_exile(decision, now=now)
        except (ValueError, RuntimeError) as exc:
            raise DayCoordinatorError("EXILE_RULE_INVALID", str(exc)) from exc

    async def finish_resolution(self, *, now: datetime | None = None) -> GameState:
        """Move a moderator-completed day boundary to VICTORY_CHECK."""

        state = await self._manager.snapshot()
        if state.phase not in {GamePhase.DAY_RESOLVE, GamePhase.TRIGGER_ACTION}:
            raise DayCoordinatorError(
                "PHASE_NOT_ALLOWED", "day resolution requires DAY_RESOLVE or TRIGGER_ACTION"
            )
        if state.phase is GamePhase.TRIGGER_ACTION and state.pending_resolution is not None:
            # A trigger phase is a real moderator boundary.  The pending
            # marker is installed together with the exile/death result and is
            # cleared only by ``commit_action_resolution`` after the actor's
            # request has been resolved (including an explicit PASS).  Check
            # it here before asking the manager to advance so the coordinator
            # cannot accidentally turn an unserved trigger prompt into a
            # victory check.
            raise DayCoordinatorError(
                "TRIGGER_ACTION_PENDING",
                "the trigger action must be resolved, including an explicit PASS, "
                "before day completion",
            )
        return await self._manager.commit_phase_transition(GamePhase.VICTORY_CHECK, now=now)

    # Vocabulary aliases used by the moderator shell and flow tests.
    start_day = announce
    open_day = announce
    open_speech_window = open_speech
    run_speech = run_next_speech
    advance = advance_from_speech
    open_vote_window = open_vote
    submit_ballot = submit_vote
    next_vote = run_next_vote
    vote_retry = retry_vote
    resolve_vote = confirm_vote
    resolve_exile = confirm_exile
    confirm_exile_result = confirm_exile
    confirm_day = finish_resolution


__all__ = ["DayCoordinator", "DayCoordinatorError", "DaySpeechProgress", "DayVoteProgress"]
