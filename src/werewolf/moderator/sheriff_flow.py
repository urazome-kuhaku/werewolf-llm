"""Moderator adapter for the board-defined sheriff election.

The election reducer in :mod:`werewolf.game.sheriff` owns candidates, tie
rounds, and private ballots.  This adapter only exposes the command-shaped
orchestration a host needs: run the frozen campaign queue, open and collect a
private vote, close the tally, confirm the board decision, and finish badge
transfer.  In particular, callers cannot supply a new PK candidate list or a
tie counter when moving between rounds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from werewolf.domain.enums import GamePhase
from werewolf.game.manager import GameManager
from werewolf.game.serial_turn import (
    SerialSpeechResult,
    SerialTurnError,
    SerialTurnScheduler,
)
from werewolf.game.sheriff import (
    SheriffElectionError,
    SheriffElectionState,
    build_sheriff_vote_window,
)
from werewolf.game.state import GameState
from werewolf.game.vote_turn import (
    SheriffVoteTurnScheduler,
    VoteTurnError,
    VoteTurnResult,
)
from werewolf.knowledge.board import BoardDefinition
from werewolf.runtime.player_runtime import PlayerRuntime


class ModeratorSheriffError(RuntimeError):
    """A safe, host-facing error from the sheriff election boundary."""


def _load_election(state: GameState) -> SheriffElectionState:
    raw = state.sheriff_election
    if raw is None:
        raise ModeratorSheriffError("SHERIFF_ELECTION_NOT_STARTED: no election is active")
    try:
        return SheriffElectionState.model_validate_json(json.dumps(raw))
    except (TypeError, ValueError) as exc:
        raise ModeratorSheriffError("SHERIFF_STATE_INVALID: stored election is malformed") from exc


class ModeratorSheriffFlow:
    """Bind one running game to its first-day sheriff election lifecycle.

    The scheduler instances are intentionally kept by the adapter so a
    rejected runtime response can be retried with its active physical request.
    They remain safe across a PK transition because the vote scheduler reads
    the current election window from ``GameState`` and the speech scheduler is
    recreated for the new authoritative speech phase.
    """

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
        self._speech_scheduler: SerialTurnScheduler | None = None
        self._speech_phase: GamePhase | None = None
        self._vote_scheduler = SheriffVoteTurnScheduler(
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
    def speech_scheduler(self) -> SerialTurnScheduler | None:
        """Return the active speech scheduler, if a speech round is open."""

        return self._speech_scheduler

    @property
    def vote_scheduler(self) -> SheriffVoteTurnScheduler:
        return self._vote_scheduler

    async def start(
        self,
        candidates: Sequence[int],
        *,
        speech_order: Sequence[int] | None = None,
    ) -> GameState:
        """Start the election with the moderator's initial candidate list.

        ``GameManager`` validates the list against the frozen board and live
        seats.  Once started, every later candidate list is read from the
        private election record; this method is only valid before an election
        exists.
        """

        candidate_tuple = tuple(candidates)
        order = None if speech_order is None else tuple(speech_order)
        try:
            state = await self._manager.start_sheriff_election(
                self._board,
                candidates=candidate_tuple,
                speech_order=order,
            )
        except (SheriffElectionError, TypeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc
        self._speech_scheduler = self._make_speech_scheduler(state.phase)
        self._speech_phase = state.phase
        return state

    async def speech_next(self) -> SerialSpeechResult:
        """Run the next frozen campaign or PK speaker once."""

        self._require_speech_phase()
        scheduler = self._ensure_speech_scheduler()
        state = self.state
        if state.serial_turn is not None:
            raise ModeratorSheriffError(
                "SHERIFF_SPEECH_IN_PROGRESS: use sheriff speech retry for the active seat"
            )
        if state.current_queue is None:
            try:
                await scheduler.start()
            except (SerialTurnError, RuntimeError, ValueError) as exc:
                raise ModeratorSheriffError(str(exc)) from exc
        elif state.current_queue == ():
            election = self._require_election()
            if len(election.speeches) == len(election.speech_order):
                raise ModeratorSheriffError(
                    "SHERIFF_SPEECH_COMPLETE: the speech queue is exhausted"
                )
            try:
                await scheduler.start()
            except (SerialTurnError, RuntimeError, ValueError) as exc:
                raise ModeratorSheriffError(str(exc)) from exc
        try:
            return await scheduler.run_next()
        except (SerialTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc

    async def speech_retry(self) -> SerialSpeechResult:
        """Retry the active campaign/PK speaker without changing its queue."""

        self._require_speech_phase()
        state = self.state
        if state.serial_turn is None:
            raise ModeratorSheriffError(
                "SHERIFF_SPEECH_NOT_IN_PROGRESS: no active speech can be retried"
            )
        scheduler = self._ensure_speech_scheduler()
        try:
            return await scheduler.retry()
        except (SerialTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc

    # Shell-friendly aliases.
    next_speech = speech_next
    retry_speech = speech_retry

    async def open_vote(self, *, window_id: str | None = None) -> GameState:
        """Open the current election ballot from authoritative election state.

        Candidates, eligible voters, and the observation revision are all
        derived from the persisted election/current game state.  A caller may
        choose a stable window ID for an integration, but cannot replace the
        candidate set or the PK round.
        """

        state = self.state
        election = self._require_election()
        self._require_speech_complete(election)
        if state.phase not in {
            GamePhase.SHERIFF_ELECTION_SPEECH,
            GamePhase.SHERIFF_ELECTION_PK_SPEECH,
        }:
            raise ModeratorSheriffError(
                "SHERIFF_VOTE_NOT_READY: election speech phase is not active"
            )
        weights = {
            seat: state.players[seat].vote_weight
            for seat in election.eligible_voters
            if seat in state.players
        }
        if len(weights) != len(election.eligible_voters):
            raise ModeratorSheriffError("SHERIFF_STATE_INVALID: an eligible voter is unassigned")
        default_id = (
            f"sheriff-vote-d{election.day_no}"
            if election.tie_round == 0
            else f"sheriff-vote-d{election.day_no}-pk{election.tie_round}"
        )
        try:
            window = build_sheriff_vote_window(
                game_id=state.game_id,
                day_no=election.day_no,
                observation_revision=state.state_revision,
                session_epoch=max(
                    (state.players[seat].session_epoch for seat in election.eligible_voters),
                    default=0,
                ),
                eligible_voters=election.eligible_voters,
                candidates=election.candidates,
                vote_weights=weights,
                allow_abstain=self._board.day_flow.vote.allow_abstain,
                window_id=window_id or default_id,
            )
            return await self._manager.open_sheriff_vote_window(
                window,
                expected_revision=state.state_revision,
            )
        except (SheriffElectionError, TypeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc

    async def vote_next(self, seat: int | None = None) -> VoteTurnResult:
        """Collect one private ballot, selecting the first missing seat by default."""

        self._require_vote_phase()
        try:
            return await self._vote_scheduler.run_next(seat=seat)
        except (VoteTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc

    async def vote_retry(self, seat: int | None = None) -> VoteTurnResult:
        """Retry a rejected/failed private ballot for the selected seat."""

        self._require_vote_phase()
        try:
            return await self._vote_scheduler.retry(seat=seat)
        except (VoteTurnError, RuntimeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc

    next_vote = vote_next
    retry_vote = vote_retry

    async def collect(
        self,
        *,
        force: bool = False,
        reason: str = "all_votes_received",
    ) -> GameState:
        """Lock and tally the current ballot using the frozen board policy."""

        self._require_vote_phase()
        self._require_election()
        # ``tie_round`` is deliberately omitted: the reducer derives it from
        # its own state and rejects an externally supplied replacement.
        try:
            return await self._manager.finalize_sheriff_election(
                self._board,
                force=force,
                reason=reason,
                expected_revision=self.state.state_revision,
            )
        except (SheriffElectionError, TypeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc

    finalize = collect

    async def confirm(self) -> GameState:
        """Confirm a unique/no-sheriff decision or atomically enter PK speech."""

        if self.state.phase not in {
            GamePhase.SHERIFF_ELECTION,
            GamePhase.SHERIFF_ELECTION_PK,
        }:
            raise ModeratorSheriffError(
                "SHERIFF_CONFIRM_NOT_READY: election vote phase is required"
            )
        try:
            state = await self._manager.confirm_sheriff_election(
                self._board,
                expected_revision=self.state.state_revision,
            )
        except (SheriffElectionError, TypeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc
        if state.phase is GamePhase.SHERIFF_ELECTION_PK_SPEECH:
            self._speech_scheduler = self._make_speech_scheduler(state.phase)
            self._speech_phase = state.phase
        return state

    confirm_election = confirm

    async def transfer(self) -> GameState:
        """Complete the explicit badge-transfer boundary and enter day speech."""

        try:
            return await self._manager.complete_sheriff_transfer(
                expected_revision=self.state.state_revision,
            )
        except (SheriffElectionError, TypeError, ValueError) as exc:
            raise ModeratorSheriffError(str(exc)) from exc

    complete_transfer = transfer
    finish = transfer

    def progress(self) -> dict[str, object]:
        """Return moderator progress without exposing private ballot targets."""

        state = self.state
        payload: dict[str, object] = {
            "phase": state.phase.value,
            "election": None,
            "speech": None,
            "vote": None,
        }
        if state.sheriff_election is None:
            return payload
        election = _load_election(state)
        payload["election"] = {
            "status": election.status.value,
            "candidates": list(election.candidates),
            "tie_round": election.tie_round,
            "speeches_submitted": sorted(election.speeches),
            "speech_order": list(election.speech_order),
            "decision": (
                election.decision.model_dump(mode="json") if election.decision is not None else None
            ),
            "sheriff_seat": election.sheriff_seat,
        }
        if state.phase in {
            GamePhase.SHERIFF_ELECTION_SPEECH,
            GamePhase.SHERIFF_ELECTION_PK_SPEECH,
        }:
            payload["speech"] = {
                "queue": list(state.current_queue or ()),
                "active_seat": state.serial_turn.seat if state.serial_turn is not None else None,
                "active_request": state.serial_turn.request_id
                if state.serial_turn is not None
                else None,
            }
        vote = election.vote
        if vote is not None:
            payload["vote"] = {
                "window_id": vote.window.window_id,
                "status": vote.status.value,
                "candidate_seats": list(vote.window.candidate_seats),
                "eligible_voter_count": len(vote.window.eligible_voters),
                "submitted_count": len(vote.ballots),
                "missing_count": len(vote.missing_voters),
                "pending_tally": (
                    vote.pending_tally.model_dump(mode="json")
                    if vote.pending_tally is not None
                    else None
                ),
            }
        return payload

    def _make_speech_scheduler(self, phase: GamePhase) -> SerialTurnScheduler:
        return SerialTurnScheduler(
            self._manager,
            self._runtimes,
            timeout_seconds=self._timeout_seconds,
            phase=phase,
        )

    def _ensure_speech_scheduler(self) -> SerialTurnScheduler:
        phase = self.state.phase
        if phase not in {
            GamePhase.SHERIFF_ELECTION_SPEECH,
            GamePhase.SHERIFF_ELECTION_PK_SPEECH,
        }:
            raise ModeratorSheriffError(
                "SHERIFF_SPEECH_NOT_ACTIVE: sheriff speech phase is required"
            )
        if self._speech_scheduler is None or self._speech_phase is not phase:
            self._speech_scheduler = self._make_speech_scheduler(phase)
            self._speech_phase = phase
        return self._speech_scheduler

    def _require_election(self) -> SheriffElectionState:
        try:
            return _load_election(self.state)
        except ModeratorSheriffError:
            raise

    def _require_speech_phase(self) -> None:
        if self.state.phase not in {
            GamePhase.SHERIFF_ELECTION_SPEECH,
            GamePhase.SHERIFF_ELECTION_PK_SPEECH,
        }:
            raise ModeratorSheriffError(
                "SHERIFF_SPEECH_NOT_ACTIVE: sheriff speech phase is required"
            )

    def _require_vote_phase(self) -> None:
        if self.state.phase not in {
            GamePhase.SHERIFF_ELECTION,
            GamePhase.SHERIFF_ELECTION_PK,
        }:
            raise ModeratorSheriffError("SHERIFF_VOTE_NOT_ACTIVE: sheriff vote phase is required")

    def _require_speech_complete(self, election: SheriffElectionState) -> None:
        if self.state.serial_turn is not None:
            raise ModeratorSheriffError(
                "SHERIFF_SPEECH_IN_PROGRESS: finish the active speaker first"
            )
        if self.state.current_queue:
            raise ModeratorSheriffError("SHERIFF_SPEECH_INCOMPLETE: every candidate must speak")
        if set(election.speech_order) != set(election.speeches):
            raise ModeratorSheriffError("SHERIFF_SPEECH_INCOMPLETE: every candidate must speak")


__all__ = ["ModeratorSheriffError", "ModeratorSheriffFlow"]
