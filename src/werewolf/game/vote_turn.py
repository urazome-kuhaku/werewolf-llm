"""Runtime scheduling for one daytime vote window.

The vote collector remains the authoritative owner of ballots.  This module
only owns the boundary between a frozen :class:`VoteWindow` and one seat's
``PlayerRuntime``.  Every seat receives a separate request with a private
observation and the same candidate contract; the resulting proposal is
converted to ``VoteRequest`` and submitted through ``GameManager``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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

from ..domain.enums import GamePhase
from .events import GameEvent
from .manager import GameManager
from .sheriff import SheriffElectionError, SheriffElectionState, SheriffElectionStatus
from .state import GameState
from .voting import VoteError, VoteRequest, VoteState, VoteStatus, VoteWindow

VOTE_ACTION_CODE = 201
ABSTAIN_ACTION_CODE = 202


class VoteTurnError(RuntimeError):
    """Base error for runtime vote scheduling failures."""


class VoteTurnBusyError(VoteTurnError):
    """The selected seat already has a vote request that needs a retry."""


class VoteTurnTimeoutError(TimeoutError, VoteTurnError):
    """The runtime exceeded the hard deadline for a vote request."""


class StaleVoteResponse(VoteTurnError):
    """A runtime response no longer belongs to the active vote request."""


@dataclass(frozen=True, slots=True)
class VoteTurnResult:
    """The accepted runtime request and resulting immutable game state."""

    request: TurnRequest
    runtime_result: RuntimeTurnResult
    state: GameState


@dataclass(frozen=True, slots=True)
class _VoteBinding:
    seat: int
    logical_request_id: str
    attempt_no: int
    previous_request_id: str | None = None


def _load_vote_state(raw: object) -> VoteState:
    if not isinstance(raw, dict):
        raise VoteTurnError("VOTE_STATE_INVALID: stored vote state is malformed")
    try:
        return VoteState.model_validate_json(json.dumps(raw))
    except (TypeError, ValueError) as exc:
        raise VoteTurnError("VOTE_STATE_INVALID: stored vote state is malformed") from exc


class VoteTurnScheduler:
    """Run one seat at a time against an installed ordinary vote window.

    The scheduler deliberately keeps in-flight request bindings in memory.
    Ballots themselves are durable in ``GameState`` and are accepted only by
    the manager, so a process restart can safely continue with the first
    missing seat.  A failed or timed-out runtime leaves its binding available
    for an explicit retry; an accepted ballot cannot be submitted twice.
    """

    def __init__(
        self,
        manager: GameManager,
        runtimes: Mapping[int, PlayerRuntime],
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(manager, GameManager):
            raise TypeError("manager must be a GameManager")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._manager = manager
        self._runtimes = dict(runtimes)
        self._timeout_seconds = timeout_seconds
        self._bindings: dict[str, _VoteBinding] = {}
        self._seat_bindings: dict[int, str] = {}

    async def run_next(
        self,
        *,
        seat: int | None = None,
        retry: bool = False,
    ) -> VoteTurnResult:
        """Run the first missing voter, or a specified voter, once.

        ``retry=True`` is required after a timeout, malformed response, or a
        rejected target.  It creates a new physical request while retaining
        the same logical turn, so late output from the previous attempt is
        rejected by request ID and attempt number.
        """

        state, vote_state = await self._active_vote()
        if vote_state.status is not VoteStatus.OPEN:
            raise VoteTurnError("VOTE_WINDOW_CLOSED: vote collection is no longer open")
        missing = vote_state.missing_voters
        if seat is None:
            seat = missing[0] if missing else None
        if seat is None:
            raise VoteTurnError("VOTE_COMPLETE: all eligible voters have submitted")
        if seat not in vote_state.window.eligible_voters:
            raise VoteTurnError("SEAT_NOT_ELIGIBLE: seat has no voting right in this window")
        if seat not in missing:
            raise VoteTurnError("VOTE_ALREADY_SUBMITTED: seat has already submitted a ballot")

        previous_request_id = self._seat_bindings.get(seat)
        if previous_request_id is not None and not retry:
            raise VoteTurnBusyError("TURN_IN_PROGRESS: use retry for the active vote request")
        if retry and previous_request_id is None:
            raise StaleVoteResponse("REQUEST_EXPIRED: no active vote request can be retried")

        player = state.players.get(seat)
        if player is None:
            raise VoteTurnError("SEAT_NOT_ASSIGNED: vote seat is not in the current game")
        runtime = self._runtimes.get(seat)
        if runtime is None:
            raise VoteTurnError(f"RUNTIME_MISSING: no runtime is registered for seat {seat}")
        try:
            runtime_ref = runtime.get_session_ref()
        except Exception as exc:
            raise VoteTurnError("RUNTIME_NOT_STARTED: start the seat runtime first") from exc
        if (
            runtime_ref.game_id != state.game_id
            or runtime_ref.seat != seat
            or runtime_ref.session_epoch != player.session_epoch
        ):
            raise VoteTurnError("SESSION_MISMATCH: runtime is not bound to the vote seat")

        previous_binding = (
            self._bindings.get(previous_request_id) if previous_request_id is not None else None
        )
        logical_request_id = (
            previous_binding.logical_request_id
            if previous_binding is not None
            else self._logical_request_id(state, vote_state.window, seat)
        )
        attempt_no = previous_binding.attempt_no + 1 if previous_binding else 1
        request_id = self._physical_request_id(
            logical_request_id,
            attempt_no,
            state.state_revision + 1,
        )

        if retry and previous_request_id is not None:
            try:
                await runtime.abort(previous_request_id)
            except RuntimeRequestMismatchError:
                # A timed-out or malformed turn may already be idle.  The
                # in-memory binding still prevents the old response being used.
                pass
            except Exception as exc:
                raise VoteTurnError(
                    f"ABORT_FAILED: could not abort vote request {previous_request_id}"
                ) from exc
            # Retrying invalidates the old physical request immediately.  A
            # late completion from that attempt must never be able to submit
            # a ballot after the replacement request has been issued.
            self._bindings.pop(previous_request_id, None)

        events = await self._manager.peek_delivery(seat, player.session_epoch)
        observation = await self._make_observation(state, vote_state, seat, events)
        request = self._make_request(
            state,
            vote_state.window,
            seat=seat,
            session_epoch=player.session_epoch,
            request_id=request_id,
            logical_request_id=logical_request_id,
            attempt_no=attempt_no,
            observation=observation,
        )
        binding = _VoteBinding(
            seat=seat,
            logical_request_id=logical_request_id,
            attempt_no=attempt_no,
            previous_request_id=previous_request_id,
        )
        self._bindings[request_id] = binding
        self._seat_bindings[seat] = request_id

        try:
            if self._timeout_seconds is None:
                result = await runtime.run_turn(request)
            else:
                result = await asyncio.wait_for(
                    runtime.run_turn(request), timeout=self._timeout_seconds
                )
        except TimeoutError as exc:
            raise VoteTurnTimeoutError(
                f"runtime timed out for vote request {request.request_id}"
            ) from exc

        return await self.commit_response(request, result)

    async def retry(self, *, seat: int | None = None) -> VoteTurnResult:
        """Retry the active failed request for ``seat`` or the first failed seat."""

        if seat is None:
            if not self._seat_bindings:
                raise StaleVoteResponse("REQUEST_EXPIRED: no active vote request can be retried")
            seat = next(iter(self._seat_bindings))
        return await self.run_next(seat=seat, retry=True)

    async def commit_response(
        self,
        request: TurnRequest,
        result: RuntimeTurnResult,
    ) -> VoteTurnResult:
        """Validate one runtime response and submit its private ballot."""

        binding = self._bindings.get(request.request_id)
        if binding is None:
            raise StaleVoteResponse("REQUEST_EXPIRED: request is not owned by this scheduler")
        if (
            result.request_id != request.request_id
            or result.logical_request_id != request.logical_request_id
            or result.attempt_no != request.attempt_no
        ):
            raise StaleVoteResponse("REQUEST_MISMATCH: response belongs to another request")
        if not isinstance(result.response, ActionResponse):
            raise RuntimeProtocolError("vote turn requires an action response")

        active, vote_state = await self._active_vote()
        player = active.players.get(binding.seat)
        action_window = request.action_window
        request_seat = request.observation.payload.get("seat")
        request_revision = request.observation.payload.get("observation_revision")
        if (
            player is None
            or binding.seat not in vote_state.window.eligible_voters
            or request.game_id != active.game_id
            or request.session_epoch != vote_state.window.session_epoch
            or player.session_epoch != request.session_epoch
            or active.phase != request.phase
            or action_window is None
            or action_window.window_id != vote_state.window.window_id
            or type(request_seat) is not int
            or request_seat != binding.seat
            or type(request_revision) is not int
            or request_revision != vote_state.window.observation_revision
            or binding.seat not in vote_state.missing_voters
        ):
            raise StaleVoteResponse("REQUEST_EXPIRED: vote request is no longer active")

        vote_request = self._to_vote_request(
            request,
            result.response,
            vote_state.window,
            seat=binding.seat,
        )
        try:
            committed = await self._manager.submit_vote(vote_request)
        except VoteError as exc:
            # Keep the binding so a moderator can explicitly retry a rejected
            # target.  The manager's immutable vote state remains unchanged.
            raise VoteTurnError(f"{exc.code}: {exc}") from exc
        self._bindings.pop(request.request_id, None)
        if self._seat_bindings.get(binding.seat) == request.request_id:
            self._seat_bindings.pop(binding.seat, None)
        return VoteTurnResult(request=request, runtime_result=result, state=committed)

    async def _active_vote(self) -> tuple[GameState, VoteState]:
        state = await self._manager.snapshot()
        if state.vote_state is None:
            raise VoteTurnError("VOTE_WINDOW_NOT_OPEN: there is no active vote window")
        return state, _load_vote_state(state.vote_state)

    async def _make_observation(
        self,
        state: GameState,
        vote_state: VoteState,
        seat: int,
        events: tuple[GameEvent, ...],
    ) -> Observation:
        player_view = vote_state.player_observation(seat)
        # ``peek_delivery`` already applies the event router's seat ACL.  Do
        # not add the private ballot map or another seat's own target here.
        return Observation(
            summary="请从合法候选座位中选择一名玩家投票；若板子允许可选择弃票。",
            events=[
                ObservationEvent(
                    event_id=event.event_id,
                    event_type=str(event.event_type),
                    payload=event.payload.model_dump(mode="json"),
                )
                for event in events
            ],
            payload={
                "seat": seat,
                "phase": state.phase.value,
                "window_id": vote_state.window.window_id,
                "observation_revision": vote_state.window.observation_revision,
                "candidate_seats": list(vote_state.window.candidate_seats),
                "allow_abstain": vote_state.window.allow_abstain,
                "has_submitted": player_view.has_submitted,
            },
        )

    def _make_request(
        self,
        state: GameState,
        window: VoteWindow,
        *,
        seat: int,
        session_epoch: int,
        request_id: str,
        logical_request_id: str,
        attempt_no: int,
        observation: Observation,
    ) -> TurnRequest:
        now = datetime.now(UTC)
        timeout = self._timeout_seconds or 120.0
        allowed = [VOTE_ACTION_CODE]
        if window.allow_abstain:
            allowed.append(ABSTAIN_ACTION_CODE)
        action_window_view = ActionWindowView(
            window_id=window.window_id,
            allowed_action_codes=allowed,
            min_actions=1,
            max_actions=1,
            allow_pass=False,
            candidate_seats=list(window.candidate_seats),
        )
        action_code_descriptions = {
            VOTE_ACTION_CODE: "vote one candidate; targets has exactly one seat",
        }
        if window.allow_abstain:
            action_code_descriptions[ABSTAIN_ACTION_CODE] = "abstain; targets is empty"
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
                action_code_descriptions=action_code_descriptions,
            ),
            deadline=Deadline(
                soft_deadline=now,
                hard_deadline=now + timedelta(seconds=timeout),
            ),
        )

    @staticmethod
    def _to_vote_request(
        request: TurnRequest,
        response: ActionResponse,
        window: VoteWindow,
        *,
        seat: int,
    ) -> VoteRequest:
        if len(response.actions) != 1:
            raise VoteTurnError("ACTION_INVALID: vote response must contain exactly one action")
        action = response.actions[0]
        target: int | None
        if action.action_code == VOTE_ACTION_CODE:
            if len(action.targets) != 1:
                raise VoteTurnError("TARGET_INVALID: vote action requires exactly one target")
            target = action.targets[0]
            if target not in window.candidate_seats:
                raise VoteTurnError("TARGET_NOT_ALLOWED: target is outside the candidate set")
        elif action.action_code == ABSTAIN_ACTION_CODE:
            if not window.allow_abstain:
                raise VoteTurnError("ABSTAIN_NOT_ALLOWED: this board window disallows abstention")
            if action.targets:
                raise VoteTurnError("TARGET_INVALID: abstain action cannot have targets")
            target = None
        else:
            raise VoteTurnError("ACTION_NOT_ALLOWED: action code is not a vote action")
        return VoteRequest(
            request_id=request.request_id,
            game_id=request.game_id,
            window_id=window.window_id,
            seat=seat,
            session_epoch=request.session_epoch,
            observation_revision=window.observation_revision,
            target_seat=target,
        )

    @staticmethod
    def _logical_request_id(state: GameState, window: VoteWindow, seat: int) -> str:
        digest = hashlib.sha256(window.window_id.encode("utf-8")).hexdigest()[:12]
        return f"{state.game_id}-r{state.round_no}-vote-{digest}-s{seat}"

    @staticmethod
    def _physical_request_id(logical_request_id: str, attempt_no: int, revision: int) -> str:
        return f"{logical_request_id}-a{attempt_no}-rev{revision}"[:128]


class SheriffVoteTurnScheduler(VoteTurnScheduler):
    """Collect one private sheriff-election ballot from each seat runtime.

    Sheriff elections store their vote window in ``GameState.sheriff_election``
    and must be committed through ``GameManager.submit_sheriff_vote``.  The
    request/retry boundary is otherwise identical to the ordinary daytime
    vote scheduler, so this adapter inherits the same per-seat capability,
    observation, and stale-response checks without exposing private ballots.
    The election state machine supplies a fresh window ID for a PK round; the
    scheduler therefore creates a new logical request namespace automatically.
    """

    async def _active_vote(self) -> tuple[GameState, VoteState]:
        state = await self._manager.snapshot()
        if state.phase not in {
            GamePhase.SHERIFF_ELECTION,
            GamePhase.SHERIFF_ELECTION_PK,
        }:
            raise VoteTurnError("VOTE_WINDOW_NOT_OPEN: sheriff election voting is not active")
        if state.sheriff_election is None:
            raise VoteTurnError("VOTE_WINDOW_NOT_OPEN: there is no active sheriff election")
        try:
            election = SheriffElectionState.model_validate_json(json.dumps(state.sheriff_election))
        except (TypeError, ValueError) as exc:
            raise VoteTurnError("VOTE_STATE_INVALID: stored sheriff election is malformed") from exc
        if election.status is not SheriffElectionStatus.VOTING or election.vote is None:
            raise VoteTurnError("VOTE_WINDOW_NOT_OPEN: sheriff election vote is not open")
        return state, election.vote

    async def commit_response(
        self,
        request: TurnRequest,
        result: RuntimeTurnResult,
    ) -> VoteTurnResult:
        """Validate and submit a sheriff ballot through the sheriff reducer."""

        binding = self._bindings.get(request.request_id)
        if binding is None:
            raise StaleVoteResponse("REQUEST_EXPIRED: request is not owned by this scheduler")
        if (
            result.request_id != request.request_id
            or result.logical_request_id != request.logical_request_id
            or result.attempt_no != request.attempt_no
        ):
            raise StaleVoteResponse("REQUEST_MISMATCH: response belongs to another request")
        if not isinstance(result.response, ActionResponse):
            raise RuntimeProtocolError("sheriff vote turn requires an action response")

        active, vote_state = await self._active_vote()
        player = active.players.get(binding.seat)
        action_window = request.action_window
        request_seat = request.observation.payload.get("seat")
        request_revision = request.observation.payload.get("observation_revision")
        if (
            player is None
            or binding.seat not in vote_state.window.eligible_voters
            or request.game_id != active.game_id
            or request.session_epoch != vote_state.window.session_epoch
            or player.session_epoch != request.session_epoch
            or active.phase != request.phase
            or action_window is None
            or action_window.window_id != vote_state.window.window_id
            or type(request_seat) is not int
            or request_seat != binding.seat
            or type(request_revision) is not int
            or request_revision != vote_state.window.observation_revision
            or binding.seat not in vote_state.missing_voters
        ):
            raise StaleVoteResponse("REQUEST_EXPIRED: sheriff vote request is no longer active")

        vote_request = self._to_vote_request(
            request,
            result.response,
            vote_state.window,
            seat=binding.seat,
        )
        try:
            committed = await self._manager.submit_sheriff_vote(vote_request)
        except SheriffElectionError as exc:
            # Preserve the binding after a rejected target or stale authority
            # check so the moderator can explicitly retry this seat.
            raise VoteTurnError(f"{exc.code}: {exc}") from exc
        self._bindings.pop(request.request_id, None)
        if self._seat_bindings.get(binding.seat) == request.request_id:
            self._seat_bindings.pop(binding.seat, None)
        return VoteTurnResult(request=request, runtime_result=result, state=committed)

    async def _make_observation(
        self,
        state: GameState,
        vote_state: VoteState,
        seat: int,
        events: tuple[GameEvent, ...],
    ) -> Observation:
        player_view = vote_state.player_observation(seat)
        return Observation(
            summary="请从警长竞选候选座位中选择一名玩家投票；若板子允许可选择弃票。",
            events=[
                ObservationEvent(
                    event_id=event.event_id,
                    event_type=str(event.event_type),
                    payload=event.payload.model_dump(mode="json"),
                )
                for event in events
            ],
            payload={
                "seat": seat,
                "phase": state.phase.value,
                "window_id": vote_state.window.window_id,
                "observation_revision": vote_state.window.observation_revision,
                "candidate_seats": list(vote_state.window.candidate_seats),
                "allow_abstain": vote_state.window.allow_abstain,
                "has_submitted": player_view.has_submitted,
            },
        )

    @staticmethod
    def _logical_request_id(state: GameState, window: VoteWindow, seat: int) -> str:
        digest = hashlib.sha256(window.window_id.encode("utf-8")).hexdigest()[:12]
        return f"{state.game_id}-r{state.round_no}-sheriff-vote-{digest}-s{seat}"


__all__ = [
    "ABSTAIN_ACTION_CODE",
    "VOTE_ACTION_CODE",
    "StaleVoteResponse",
    "VoteTurnBusyError",
    "VoteTurnError",
    "VoteTurnResult",
    "VoteTurnScheduler",
    "VoteTurnTimeoutError",
    "SheriffVoteTurnScheduler",
]
