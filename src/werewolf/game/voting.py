"""Pure, board-policy driven secret voting primitives.

The game coordinator owns the mutable ``GameState`` and serializes commits.
This module only models one voting window and returns new immutable state after
each operation.  In particular, a ballot is never a public event: callers
must use :meth:`VoteState.player_observation` for a player-facing projection.

The rules for a tie are deliberately injected.  The first board does not yet
have an authoritative PK decision, so this module refuses to guess one.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from enum import StrEnum
from threading import Lock
from typing import Annotated, NoReturn, Protocol, TypeAlias

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

_ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", re.ASCII)

Seat = Annotated[int, Field(ge=1, le=64, strict=True)]
Revision = Annotated[int, Field(ge=0, strict=True)]
Weight = Annotated[float, Field(ge=0.0, strict=True)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, strict=True)]


class VoteError(ValueError):
    """Stable error for a rejected vote-window operation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class VoteStatus(StrEnum):
    """Lifecycle of a secret vote collection."""

    OPEN = "OPEN"
    LOCKED = "LOCKED"
    WAITING_GM = "WAITING_GM"
    RESOLVED = "RESOLVED"


class TieAction(StrEnum):
    """Actions a published board may choose for a tied tally."""

    PK = "PK"
    REVOTE = "REVOTE"
    NO_EXILE = "NO_EXILE"
    ELIMINATE = "ELIMINATE"


class _FrozenDict(dict[object, object]):
    """JSON-shaped mapping that rejects mutation after model creation."""

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("vote mappings are immutable; create a new vote state instead")

    def __setitem__(self, key: object, value: object) -> None:
        del key, value
        self._immutable()

    def __delitem__(self, key: object) -> None:
        del key
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, key: object, default: object = None) -> object:
        del key, default
        self._immutable()

    def popitem(self) -> tuple[object, object]:
        self._immutable()

    def setdefault(self, key: object, default: object = None) -> object:
        del key, default
        self._immutable()

    def update(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self._immutable()

    def __ior__(self, value: object) -> _FrozenDict:  # type: ignore[misc]
        del value
        self._immutable()


class _VoteModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    def model_post_init(self, __context: object) -> None:
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, dict) and not isinstance(value, _FrozenDict):
                object.__setattr__(self, field_name, _FrozenDict(value))


def _validate_identifier(value: str, *, name: str) -> str:
    if _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must contain only letters, digits, '.', ':', '_' or '-'")
    return value


class VoteWindow(_VoteModel):
    """Immutable observation barrier and eligibility contract for one vote."""

    schema_version: int = 1
    window_id: Identifier
    game_id: Identifier
    session_epoch: Revision
    observation_revision: Revision
    eligible_voters: tuple[Seat, ...]
    candidate_seats: tuple[Seat, ...]
    vote_weights: dict[Seat, Weight]
    expected_request_ids: dict[Seat, Identifier] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("expected_request_ids", "active_request_ids", "request_ids"),
    )
    allow_abstain: bool = False

    @model_validator(mode="after")
    def validate_contract(self) -> VoteWindow:
        _validate_identifier(self.window_id, name="window_id")
        _validate_identifier(self.game_id, name="game_id")
        if not self.eligible_voters:
            raise ValueError("eligible_voters must not be empty")
        if len(set(self.eligible_voters)) != len(self.eligible_voters):
            raise ValueError("eligible_voters must not contain duplicates")
        if tuple(sorted(self.eligible_voters)) != self.eligible_voters:
            raise ValueError("eligible_voters must be sorted")
        if len(set(self.candidate_seats)) != len(self.candidate_seats):
            raise ValueError("candidate_seats must not contain duplicates")
        if tuple(sorted(self.candidate_seats)) != self.candidate_seats:
            raise ValueError("candidate_seats must be sorted")
        if not self.candidate_seats:
            raise ValueError("candidate_seats must not be empty")
        if set(self.vote_weights) != set(self.eligible_voters):
            raise ValueError("vote_weights must contain exactly the eligible voters")
        if not set(self.expected_request_ids).issubset(self.eligible_voters):
            raise ValueError("expected_request_ids may only refer to eligible voters")
        for request_id in self.expected_request_ids.values():
            _validate_identifier(request_id, name="expected request ID")
        if any(not math.isfinite(weight) for weight in self.vote_weights.values()):
            raise ValueError("vote weights must be finite")
        if any(weight <= 0 for weight in self.vote_weights.values()):
            raise ValueError("vote weights must be greater than zero")
        return self


class VoteRequest(_VoteModel):
    """One untrusted runtime vote request, before authoritative validation."""

    schema_version: int = 1
    request_id: Identifier
    game_id: Identifier
    window_id: Identifier
    seat: Seat
    session_epoch: Revision
    observation_revision: Revision
    target_seat: Seat | None

    @model_validator(mode="after")
    def validate_ids(self) -> VoteRequest:
        _validate_identifier(self.request_id, name="request_id")
        _validate_identifier(self.game_id, name="game_id")
        _validate_identifier(self.window_id, name="window_id")
        return self


class Ballot(_VoteModel):
    """A private accepted ballot, retained for GM tallying only."""

    request_id: Identifier
    seat: Seat
    target_seat: Seat | None
    session_epoch: Revision
    observation_revision: Revision
    vote_weight: Weight


class TieDecision(_VoteModel):
    """Board strategy output for a tied tally."""

    action: TieAction
    candidates: tuple[Seat, ...]
    selected_seat: Seat | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> TieDecision:
        if not self.candidates:
            raise ValueError("tie decision must name at least one candidate")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("tie decision candidates must be unique")
        if self.action is TieAction.ELIMINATE:
            if self.selected_seat not in self.candidates:
                raise ValueError("ELIMINATE must select one tied candidate")
        elif self.selected_seat is not None:
            raise ValueError("only ELIMINATE may select a seat")
        return self


def resolve_board_tie(
    policy: str | None,
    tie_round: int,
    candidates: tuple[int, ...],
    *,
    pk_enabled: bool,
) -> TieDecision:
    """Resolve a board-declared tie policy for one frozen tie round.

    ``tie_round`` is zero for the first tied tally.  Policies that support a
    PK therefore return ``PK`` on round zero and, when they declare a terminal
    re-tie outcome, ``NO_EXILE`` on later rounds.  The same ``NO_EXILE`` value
    is used by the sheriff election adapter; the sheriff state machine maps it
    to its domain-specific ``NO_SHERIFF`` action.

    A missing or unknown policy is an error.  The runtime must never infer a
    tie outcome from the fact that a tally happened to be tied.
    """

    if policy is None:
        raise VoteError("TIE_POLICY_REQUIRED", "board tie policy is unresolved")
    if not isinstance(tie_round, int) or isinstance(tie_round, bool) or tie_round < 0:
        raise VoteError("INVALID_TIE_ROUND", "tie_round must be a non-negative integer")

    no_exile_policies = {
        "no_exile_on_tie",
        "no_sheriff_on_tie",
        "no_election_on_tie",
    }
    pk_policies = {
        "pk_then_revote",
        "revote_until_unique",
        "pk_then_no_exile_on_retie",
        "pk_then_no_sheriff_on_retie",
    }
    if policy in no_exile_policies:
        action = TieAction.NO_EXILE
    elif policy in pk_policies:
        if pk_enabled is not True:
            raise VoteError("TIE_POLICY_UNSUPPORTED", "board PK is not enabled by the board")
        action = (
            TieAction.NO_EXILE
            if policy in {"pk_then_no_exile_on_retie", "pk_then_no_sheriff_on_retie"}
            and tie_round > 0
            else TieAction.PK
        )
    else:
        raise VoteError("TIE_POLICY_UNSUPPORTED", f"unsupported board tie policy: {policy}")
    return TieDecision(action=action, candidates=candidates)


class VoteTally(_VoteModel):
    """Frozen weighted tally produced only after a window is locked."""

    counts: dict[Seat, Weight]
    total_weight: Weight
    ballots_count: int = Field(ge=0, strict=True)
    top_candidates: tuple[Seat, ...]
    is_tie: bool
    winner_seat: Seat | None = None


class PublicVoteResult(_VoteModel):
    """Safe post-confirmation projection; it intentionally has no ballots."""

    tally: VoteTally
    eliminated_seat: Seat | None
    tie_action: TieAction | None = None


class PlayerVoteObservation(_VoteModel):
    """Player-facing projection of a vote window.

    It includes only the candidate contract and the requesting player's own
    submission status.  The private ballot map is never copied here.
    """

    window_id: Identifier
    observation_revision: Revision
    candidate_seats: tuple[Seat, ...]
    eligible: bool
    has_submitted: bool
    own_target_seat: Seat | None = None
    status: VoteStatus
    public_result: PublicVoteResult | None = None


class VoteSubmissionResult(_VoteModel):
    """GM-side result of accepting one ballot."""

    state: VoteState
    ballot: Ballot
    idempotent_replay: bool


class VoteResolutionStrategy(Protocol):
    """Policy supplied by a published board for tied votes."""

    def resolve_tie(self, *, window: VoteWindow, tally: VoteTally) -> TieDecision: ...


TieResolver: TypeAlias = VoteResolutionStrategy | Callable[[VoteWindow, VoteTally], TieDecision]


class VoteState(_VoteModel):
    """Immutable collector state; ``ballots`` is a GM-private field."""

    window: VoteWindow
    status: VoteStatus = VoteStatus.OPEN
    ballots: dict[Seat, Ballot] = Field(default_factory=dict)
    pending_tally: VoteTally | None = None
    tie_decision: TieDecision | None = None
    public_result: PublicVoteResult | None = None
    close_reason: Identifier | None = None

    @classmethod
    def open(cls, window: VoteWindow) -> VoteState:
        return cls(window=window)

    @property
    def missing_voters(self) -> tuple[int, ...]:
        return tuple(seat for seat in self.window.eligible_voters if seat not in self.ballots)

    @property
    def is_complete(self) -> bool:
        return not self.missing_voters

    def _replace(self, **updates: object) -> VoteState:
        values = self.model_dump(mode="python")
        values.update(updates)
        return type(self).model_validate(values)

    def _reject_if_not_open(self) -> None:
        if self.status is not VoteStatus.OPEN:
            raise VoteError("WINDOW_CLOSED", "vote window is no longer accepting ballots")

    def _validate_request(self, request: VoteRequest) -> None:
        if request.game_id != self.window.game_id:
            raise VoteError("GAME_MISMATCH", "request game_id does not match vote window")
        if request.window_id != self.window.window_id:
            raise VoteError("WINDOW_MISMATCH", "request window_id does not match vote window")
        if request.session_epoch != self.window.session_epoch:
            raise VoteError("SESSION_MISMATCH", "request session_epoch does not match vote window")
        if request.observation_revision != self.window.observation_revision:
            raise VoteError(
                "REVISION_MISMATCH",
                "all ballots must use the vote window observation revision",
            )
        expected_request_id = self.window.expected_request_ids.get(request.seat)
        if expected_request_id is not None and request.request_id != expected_request_id:
            raise VoteError("REQUEST_MISMATCH", "request_id does not match the active seat request")
        if request.seat not in self.window.eligible_voters:
            raise VoteError("SEAT_NOT_ELIGIBLE", "seat has no voting right in this window")
        if request.target_seat is None:
            if not self.window.allow_abstain:
                raise VoteError(
                    "ABSTAIN_NOT_ALLOWED", "this board window does not allow abstention"
                )
        elif request.target_seat not in self.window.candidate_seats:
            raise VoteError("TARGET_NOT_ALLOWED", "target is outside the frozen candidate set")

    def submit(self, request: VoteRequest) -> VoteSubmissionResult:
        """Validate and accept one ballot, returning a new state.

        A repeated request with the same payload, or a repeated same-seat vote
        for the same target, is an idempotent replay.  A different target from
        an already-voted seat is always rejected.
        """

        self._reject_if_not_open()
        self._validate_request(request)
        prior_request = next(
            (ballot for ballot in self.ballots.values() if ballot.request_id == request.request_id),
            None,
        )
        if prior_request is not None and (
            prior_request.seat != request.seat or prior_request.target_seat != request.target_seat
        ):
            raise VoteError("IDEMPOTENCY_CONFLICT", "request_id was already used by another ballot")
        prior = self.ballots.get(request.seat)
        if prior is not None:
            if prior.target_seat != request.target_seat:
                raise VoteError(
                    "IDEMPOTENCY_CONFLICT",
                    "seat already submitted a different ballot in this window",
                )
            return VoteSubmissionResult(state=self, ballot=prior, idempotent_replay=True)
        ballot = Ballot(
            request_id=request.request_id,
            seat=request.seat,
            target_seat=request.target_seat,
            session_epoch=request.session_epoch,
            observation_revision=request.observation_revision,
            vote_weight=self.window.vote_weights[request.seat],
        )
        ballots = dict(self.ballots)
        ballots[request.seat] = ballot
        next_state = self._replace(ballots=ballots)
        return VoteSubmissionResult(state=next_state, ballot=ballot, idempotent_replay=False)

    def lock(self, *, force: bool = False, reason: str = "all_votes_received") -> VoteState:
        """Lock collection; incomplete windows require explicit moderator force."""

        self._reject_if_not_open()
        if self.missing_voters and not force:
            raise VoteError(
                "INCOMPLETE_VOTE",
                "all eligible voters must submit before the window can be locked",
            )
        if not reason or _ID_PATTERN.fullmatch(reason) is None:
            raise VoteError("INVALID_CLOSE_REASON", "close reason must be a stable identifier")
        return self._replace(status=VoteStatus.LOCKED, close_reason=reason)

    def build_tally(self, *, tie_resolver: TieResolver | None = None) -> VoteState:
        """Compute a frozen tally and move the window to GM confirmation."""

        if self.status is not VoteStatus.LOCKED:
            raise VoteError("WINDOW_NOT_LOCKED", "lock the vote window before tallying")
        counts = {seat: 0.0 for seat in self.window.candidate_seats}
        total_weight = 0.0
        for ballot in self.ballots.values():
            if ballot.target_seat is not None:
                counts[ballot.target_seat] += ballot.vote_weight
                total_weight += ballot.vote_weight
        maximum = max(counts.values(), default=0.0)
        top = tuple(seat for seat in self.window.candidate_seats if counts[seat] == maximum)
        tally = VoteTally(
            counts=counts,
            total_weight=total_weight,
            ballots_count=len(self.ballots),
            top_candidates=top,
            is_tie=len(top) > 1,
            winner_seat=top[0] if len(top) == 1 else None,
        )
        decision: TieDecision | None = None
        if tally.is_tie:
            if tie_resolver is None:
                raise VoteError(
                    "TIE_POLICY_REQUIRED",
                    "a published board tie policy must resolve a tied tally",
                )
            if hasattr(tie_resolver, "resolve_tie"):
                decision = tie_resolver.resolve_tie(window=self.window, tally=tally)
            else:
                decision = tie_resolver(self.window, tally)
            if not isinstance(decision, TieDecision):
                raise VoteError("INVALID_TIE_POLICY", "tie policy returned an invalid decision")
            if set(decision.candidates) != set(tally.top_candidates):
                raise VoteError(
                    "INVALID_TIE_POLICY",
                    "tie policy must operate on exactly the tied candidates",
                )
        return self._replace(
            status=VoteStatus.WAITING_GM,
            pending_tally=tally,
            tie_decision=decision,
        )

    def finalize_collection(
        self,
        *,
        force: bool = False,
        reason: str = "all_votes_received",
        tie_resolver: TieResolver | None = None,
    ) -> VoteState:
        """Lock and prepare the GM-confirmation tally in one pure operation."""

        return self.lock(force=force, reason=reason).build_tally(tie_resolver=tie_resolver)

    def confirm_tally(self) -> VoteState:
        """Confirm the pending tally and create the first public projection."""

        if self.status is not VoteStatus.WAITING_GM or self.pending_tally is None:
            raise VoteError("TALLY_NOT_PENDING", "there is no tally awaiting GM confirmation")
        tally = self.pending_tally
        action = self.tie_decision.action if self.tie_decision is not None else None
        if not tally.is_tie:
            eliminated = tally.winner_seat
        elif self.tie_decision is not None and self.tie_decision.action is TieAction.ELIMINATE:
            eliminated = self.tie_decision.selected_seat
        else:
            eliminated = None
        result = PublicVoteResult(
            tally=tally,
            eliminated_seat=eliminated,
            tie_action=action,
        )
        return self._replace(status=VoteStatus.RESOLVED, public_result=result)

    def player_observation(self, seat: int) -> PlayerVoteObservation:
        """Return a projection that cannot expose another player's ballot."""

        ballot = self.ballots.get(seat)
        return PlayerVoteObservation(
            window_id=self.window.window_id,
            observation_revision=self.window.observation_revision,
            candidate_seats=self.window.candidate_seats,
            eligible=seat in self.window.eligible_voters,
            has_submitted=ballot is not None,
            own_target_seat=ballot.target_seat if ballot is not None else None,
            status=self.status,
            public_result=self.public_result,
        )

    def public_projection(self) -> PublicVoteResult | None:
        """Return a public result only after GM confirmation."""

        return self.public_result


class VoteCollector:
    """Small thread-safe adapter for parallel runtime responses.

    The authoritative state is still replaced with the immutable ``VoteState``
    produced by each call.  A GameManager can use the same reducer directly;
    this adapter is useful when independent runtime tasks finish concurrently.
    """

    def __init__(self, window: VoteWindow) -> None:
        self._state = VoteState.open(window)
        self._lock = Lock()

    @property
    def state(self) -> VoteState:
        with self._lock:
            return self._state

    def submit(self, request: VoteRequest) -> VoteSubmissionResult:
        with self._lock:
            result = self._state.submit(request)
            self._state = result.state
            return result

    def lock(self, *, force: bool = False, reason: str = "all_votes_received") -> VoteState:
        with self._lock:
            self._state = self._state.lock(force=force, reason=reason)
            return self._state

    def build_tally(self, *, tie_resolver: TieResolver | None = None) -> VoteState:
        with self._lock:
            self._state = self._state.build_tally(tie_resolver=tie_resolver)
            return self._state

    def finalize_collection(
        self,
        *,
        force: bool = False,
        reason: str = "all_votes_received",
        tie_resolver: TieResolver | None = None,
    ) -> VoteState:
        with self._lock:
            self._state = self._state.finalize_collection(
                force=force,
                reason=reason,
                tie_resolver=tie_resolver,
            )
            return self._state

    def confirm_tally(self) -> VoteState:
        with self._lock:
            self._state = self._state.confirm_tally()
            return self._state


def submit_ballot(state: VoteState, request: VoteRequest) -> VoteSubmissionResult:
    """Functional alias used by reducers and tests."""

    return state.submit(request)


# ``VoteSubmissionResult`` refers to ``VoteState`` before its declaration so
# the public result can carry the new immutable collector state.
VoteSubmissionResult.model_rebuild()


__all__ = [
    "Ballot",
    "PlayerVoteObservation",
    "PublicVoteResult",
    "TieAction",
    "TieDecision",
    "TieResolver",
    "VoteError",
    "VoteCollector",
    "VoteRequest",
    "VoteResolutionStrategy",
    "VoteState",
    "VoteStatus",
    "VoteSubmissionResult",
    "VoteTally",
    "VoteWindow",
    "resolve_board_tie",
    "submit_ballot",
]
