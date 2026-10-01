"""Board-bound primitives for the first-day sheriff election.

The published board only guarantees three facts about the sheriff office:
the election happens before the first day's death announcement, the winner
speaks last during the day, and the winner's daytime vote is weighted at the
board value (1.5 on the official twelve-player board).  It does not define a
tie or a withdrawal rule.  This module therefore models the authorised
candidate/speech/vote boundary and refuses to resolve an unspecified tie.

``SheriffElectionState`` is deliberately independent of ``GameState``.  It is
the durable election record that a coordinator can store in a future typed
``GameState.sheriff_election`` field (or an equivalent snapshot slot) without
putting private ballots in public events.  The existing ``VoteState`` remains
the sole ballot reducer.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, NoReturn, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.preview import experimental_preview_enabled

from .sheriff_eligibility import first_day_sheriff_participants
from .state import GameState
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
    resolve_board_tie,
)

Seat = Annotated[int, Field(ge=1, le=64, strict=True)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
Revision = Annotated[int, Field(ge=0, strict=True)]


class SheriffElectionError(ValueError):
    """Stable error raised when an election operation is not authorised."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class SheriffElectionStatus(StrEnum):
    """Durable lifecycle of one first-day election."""

    SPEECH = "SPEECH"
    VOTING = "VOTING"
    WAITING_GM = "WAITING_GM"
    RESOLVED = "RESOLVED"
    NO_SHERIFF = "NO_SHERIFF"


class SheriffElectionAction(StrEnum):
    """Moderator action required after the election tally."""

    ELECT = "ELECT"
    PK = "PK"
    REVOTE = "REVOTE"
    NO_SHERIFF = "NO_SHERIFF"


class _SheriffModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    def model_post_init(self, __context: Any) -> None:
        """Freeze nested mappings used by the immutable election record."""

        del __context
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, dict) and not isinstance(value, _FrozenDict):
                object.__setattr__(self, field_name, _freeze_mapping(value))


class _FrozenDict(dict[object, object]):
    """JSON-compatible mapping that rejects in-place mutation."""

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("sheriff mappings are immutable; create a new state instead")

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


def _freeze_mapping(value: dict[object, object]) -> _FrozenDict:
    return _FrozenDict(
        {
            key: _freeze_mapping(item) if isinstance(item, dict) else item
            for key, item in value.items()
        }
    )


class SheriffCampaignSpeechRequest(_SheriffModel):
    """One candidate's authorised campaign speech submission.

    ``expected_request_id`` is the host-side capability.  If supplied, the
    request must carry exactly that ID; this prevents a runtime from speaking
    for a different seat or replaying a stale request after a restart.
    """

    schema_version: int = 1
    request_id: Identifier
    game_id: Identifier
    day_no: Revision
    seat: Seat
    session_epoch: Revision
    observation_revision: Revision
    text: Annotated[str, Field(min_length=1, max_length=8_000, strict=True)]


class SheriffCampaignSpeech(_SheriffModel):
    """Frozen, public campaign speech accepted by the election reducer."""

    request_id: Identifier
    seat: Seat
    text: Annotated[str, Field(min_length=1, max_length=8_000, strict=True)]
    session_epoch: Revision
    observation_revision: Revision


class SheriffElectionDecision(_SheriffModel):
    """Safe moderator-facing decision after a frozen election tally."""

    action: SheriffElectionAction
    candidates: tuple[Seat, ...]
    elected_seat: Seat | None = None
    tally: VoteTally | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if not self.candidates:
            raise ValueError("decision candidates must not be empty")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("decision candidates must be unique")
        if self.action is SheriffElectionAction.ELECT:
            if self.elected_seat not in self.candidates:
                raise ValueError("ELECT must select one candidate")
        elif self.elected_seat is not None:
            raise ValueError("only ELECT may select a seat")
        return self


class SheriffElectionState(_SheriffModel):
    """Serializable election state with private ballots retained in ``vote``."""

    schema_version: int = 1
    game_id: Identifier
    day_no: Revision
    status: SheriffElectionStatus = SheriffElectionStatus.SPEECH
    candidates: tuple[Seat, ...]
    eligible_voters: tuple[Seat, ...]
    speech_order: tuple[Seat, ...]
    speeches: dict[Seat, SheriffCampaignSpeech] = Field(default_factory=dict)
    expected_speech_request_ids: dict[Seat, Identifier] = Field(default_factory=dict)
    vote: VoteState | None = None
    # A PK starts a fresh VoteState, but the original ballot remains part of
    # the private snapshot so recovery can audit both rounds without ever
    # exposing the ballots to players.
    vote_history: tuple[VoteState, ...] = ()
    tie_round: int = Field(default=0, ge=0, strict=True)
    decision: SheriffElectionDecision | None = None
    sheriff_seat: Seat | None = None

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if not self.candidates:
            raise ValueError("candidates must not be empty")
        if tuple(sorted(self.candidates)) != self.candidates:
            raise ValueError("candidates must be sorted")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("candidates must be unique")
        if not self.eligible_voters:
            raise ValueError("eligible_voters must not be empty")
        if tuple(sorted(self.eligible_voters)) != self.eligible_voters:
            raise ValueError("eligible_voters must be sorted")
        if len(set(self.eligible_voters)) != len(self.eligible_voters):
            raise ValueError("eligible_voters must be unique")
        if not set(self.candidates).issubset(self.eligible_voters):
            raise ValueError("every candidate must be an eligible voter")
        if len(set(self.speech_order)) != len(self.speech_order):
            raise ValueError("speech_order must be unique")
        if not set(self.speech_order).issubset(self.candidates):
            raise ValueError("speech_order may only contain candidates")
        if set(self.speeches) - set(self.candidates):
            raise ValueError("speeches may only contain candidates")
        if not set(self.expected_speech_request_ids).issubset(self.speech_order):
            raise ValueError("expected speech request IDs may only refer to speech candidates")
        if len(set(self.expected_speech_request_ids.values())) != len(
            self.expected_speech_request_ids
        ):
            raise ValueError("expected speech request IDs must be unique")
        if self.vote is not None:
            if set(self.vote.window.eligible_voters) != set(self.eligible_voters):
                raise ValueError("vote eligible voters do not match election")
            if set(self.vote.window.candidate_seats) != set(self.candidates):
                raise ValueError("vote candidates do not match election")
        for prior_vote in self.vote_history:
            if set(prior_vote.window.eligible_voters) != set(self.eligible_voters):
                raise ValueError("historical vote eligible voters do not match election")
        if self.tie_round > 0 and not self.vote_history:
            raise ValueError("a PK round must retain the prior vote in vote_history")
        if self.status is SheriffElectionStatus.SPEECH:
            if self.vote is not None:
                raise ValueError("speech status cannot carry a vote window")
            if self.tie_round == 0 and self.decision is not None:
                raise ValueError("initial speech status cannot carry a decision")
            if self.tie_round > 0 and (
                self.decision is None
                or self.decision.action is not SheriffElectionAction.PK
                or set(self.decision.candidates) != set(self.candidates)
            ):
                raise ValueError("PK speech status requires its frozen PK decision")
            if self.sheriff_seat is not None:
                raise ValueError("speech status cannot carry an elected sheriff")
        elif self.status is SheriffElectionStatus.VOTING:
            if self.vote is None or self.vote.status is not VoteStatus.OPEN:
                raise ValueError("voting status requires an open vote window")
            if self.tie_round == 0 and self.decision is not None:
                raise ValueError("initial voting status cannot carry a decision")
            if self.tie_round > 0 and (
                self.decision is None
                or self.decision.action is not SheriffElectionAction.PK
                or set(self.decision.candidates) != set(self.candidates)
            ):
                raise ValueError("PK voting status requires its frozen PK decision")
            if self.sheriff_seat is not None:
                raise ValueError("voting status cannot carry an elected sheriff")
        elif self.status is SheriffElectionStatus.WAITING_GM:
            if self.vote is None or self.vote.status is not VoteStatus.WAITING_GM:
                raise ValueError("waiting status requires a pending vote tally")
            if self.decision is None:
                raise ValueError("waiting status requires an explicit election decision")
            if self.sheriff_seat is not None:
                raise ValueError("waiting status cannot carry an elected sheriff")
        elif self.status is SheriffElectionStatus.RESOLVED:
            if self.vote is None or self.vote.status is not VoteStatus.RESOLVED:
                raise ValueError("resolved status requires a confirmed vote")
            if self.decision is None or self.decision.action is not SheriffElectionAction.ELECT:
                raise ValueError("resolved status requires an election decision")
            if self.sheriff_seat is None:
                raise ValueError("resolved election must carry a sheriff seat")
        elif self.status is SheriffElectionStatus.NO_SHERIFF:
            if self.vote is None or self.vote.status is not VoteStatus.RESOLVED:
                raise ValueError("NO_SHERIFF requires a confirmed vote")
            if (
                self.decision is None
                or self.decision.action is not SheriffElectionAction.NO_SHERIFF
            ):
                raise ValueError("NO_SHERIFF requires a no-sheriff decision")
            if self.sheriff_seat is not None:
                raise ValueError("NO_SHERIFF cannot carry an elected sheriff")
        if self.sheriff_seat is not None:
            if self.sheriff_seat not in self.candidates:
                raise ValueError("sheriff_seat must be an election candidate")
            if self.decision is None or self.decision.elected_seat != self.sheriff_seat:
                raise ValueError("sheriff_seat must match the elected decision")
        return self

    @classmethod
    def start(
        cls,
        *,
        game_id: str,
        day_no: int,
        candidates: tuple[int, ...],
        eligible_voters: tuple[int, ...],
        speech_order: tuple[int, ...] | None = None,
        expected_speech_request_ids: Mapping[int, str] | None = None,
    ) -> SheriffElectionState:
        """Create a first-day election from an explicit moderator candidate list."""

        candidate_set = tuple(sorted(candidates))
        voters = tuple(sorted(eligible_voters))
        order = candidate_set if speech_order is None else tuple(speech_order)
        return cls(
            game_id=game_id,
            day_no=day_no,
            candidates=candidate_set,
            eligible_voters=voters,
            speech_order=order,
            expected_speech_request_ids=dict(expected_speech_request_ids or {}),
        )

    def _replace(self, **updates: object) -> SheriffElectionState:
        """Apply a transition through Pydantic's full contract validation."""

        values = self.model_dump(mode="python")
        values.update(updates)
        return type(self).model_validate(values)

    def submit_speech(
        self,
        request: SheriffCampaignSpeechRequest,
        *,
        expected_request_id: str | None = None,
    ) -> SheriffElectionState:
        """Accept one candidate speech after checking its capability boundary."""

        if self.status is not SheriffElectionStatus.SPEECH:
            raise SheriffElectionError("SPEECH_CLOSED", "the campaign speech phase is closed")
        if request.game_id != self.game_id:
            raise SheriffElectionError("GAME_MISMATCH", "speech game_id does not match election")
        if request.day_no != self.day_no:
            raise SheriffElectionError("DAY_MISMATCH", "speech day does not match election")
        if request.seat not in self.speech_order:
            raise SheriffElectionError("SEAT_NOT_AUTHORIZED", "seat is not an election candidate")
        active_request_id = self.expected_speech_request_ids.get(request.seat)
        if expected_request_id is not None:
            if active_request_id is not None and active_request_id != expected_request_id:
                raise SheriffElectionError(
                    "REQUEST_MISMATCH", "speech request capability does not match election"
                )
            active_request_id = expected_request_id
        if active_request_id is not None and request.request_id != active_request_id:
            raise SheriffElectionError(
                "REQUEST_MISMATCH", "speech request is stale or unauthorized"
            )
        prior = self.speeches.get(request.seat)
        if prior is not None:
            if (
                prior.request_id != request.request_id
                or prior.text != request.text
                or prior.session_epoch != request.session_epoch
                or prior.observation_revision != request.observation_revision
            ):
                raise SheriffElectionError(
                    "IDEMPOTENCY_CONFLICT", "seat already submitted another speech"
                )
            return self
        prior_request = next(
            (
                speech
                for speech in self.speeches.values()
                if speech.request_id == request.request_id
            ),
            None,
        )
        if prior_request is not None:
            raise SheriffElectionError(
                "IDEMPOTENCY_CONFLICT", "request_id was already used by another speech"
            )
        speech = SheriffCampaignSpeech(
            request_id=request.request_id,
            seat=request.seat,
            text=request.text,
            session_epoch=request.session_epoch,
            observation_revision=request.observation_revision,
        )
        speeches = dict(self.speeches)
        speeches[request.seat] = speech
        return self._replace(speeches=speeches)

    def open_vote(self, window: VoteWindow) -> SheriffElectionState:
        """Install an already-authorised observation-frozen election window."""

        if self.status is not SheriffElectionStatus.SPEECH:
            raise SheriffElectionError("PHASE_NOT_ALLOWED", "election vote is not ready to open")
        if set(window.eligible_voters) != set(self.eligible_voters):
            raise SheriffElectionError("ELIGIBILITY_MISMATCH", "vote voters do not match election")
        if set(window.candidate_seats) != set(self.candidates):
            raise SheriffElectionError(
                "CANDIDATE_MISMATCH", "vote candidates do not match election"
            )
        if self.vote_history and window.window_id == self.vote_history[-1].window.window_id:
            raise SheriffElectionError(
                "WINDOW_ID_REUSED", "PK vote window must use a new independent window ID"
            )
        return self._replace(status=SheriffElectionStatus.VOTING, vote=VoteState.open(window))

    def begin_pk(
        self,
        *,
        expected_speech_request_ids: Mapping[int, str] | None = None,
    ) -> SheriffElectionState:
        """Atomically convert the confirmed first tie into one PK speech round.

        The tied candidates and the first ballot are copied into this private
        record before the new round is opened.  Callers cannot provide a new
        candidate list or tie counter; both are derived from the frozen tally.
        """

        if self.status is not SheriffElectionStatus.WAITING_GM or self.vote is None:
            raise SheriffElectionError("TALLY_NOT_PENDING", "no tied tally awaits PK confirmation")
        if self.decision is None or self.decision.action is not SheriffElectionAction.PK:
            raise SheriffElectionError(
                "ELECTION_ACTION_INVALID", "the pending tally does not request PK"
            )
        if self.tie_round != 0:
            raise SheriffElectionError("PK_ALREADY_STARTED", "this election already has a PK round")
        candidates = tuple(sorted(self.decision.candidates))
        if not candidates:
            raise SheriffElectionError("CANDIDATES_INVALID", "PK requires at least one candidate")
        return self._replace(
            status=SheriffElectionStatus.SPEECH,
            candidates=candidates,
            speech_order=candidates,
            speeches={},
            expected_speech_request_ids=dict(expected_speech_request_ids or {}),
            vote=None,
            vote_history=(*self.vote_history, self.vote),
            tie_round=1,
        )

    def submit_vote(self, request: VoteRequest) -> SheriffElectionState:
        """Submit one ballot through the shared VoteState reducer."""

        if self.status is not SheriffElectionStatus.VOTING or self.vote is None:
            raise SheriffElectionError("VOTE_NOT_OPEN", "the sheriff election vote is not open")
        try:
            result = self.vote.submit(request)
        except VoteError as exc:
            raise SheriffElectionError(exc.code, str(exc)) from exc
        return self._replace(vote=result.state)

    def finalize(
        self,
        *,
        board: BoardDefinition,
        force: bool = False,
        reason: str = "all_votes_received",
        tie_round: int | None = None,
    ) -> SheriffElectionState:
        """Lock and tally the vote using only an explicit board tie policy."""

        if self.status is not SheriffElectionStatus.VOTING or self.vote is None:
            raise SheriffElectionError("VOTE_NOT_OPEN", "the sheriff election vote is not open")
        if board.day_flow.sheriff.enabled is not True:
            raise SheriffElectionError("SHERIFF_DISABLED", "the board does not enable a sheriff")
        policy = board.day_flow.sheriff.tie_policy
        if tie_round is not None and (
            not isinstance(tie_round, int)
            or isinstance(tie_round, bool)
            or tie_round != self.tie_round
        ):
            raise SheriffElectionError(
                "TIE_ROUND_MISMATCH",
                "external tie_round must match the election's current tie_round",
            )
        effective_tie_round = self.tie_round
        resolver = _sheriff_tie_resolver(
            policy, board.day_flow.sheriff.pk_enabled, effective_tie_round
        )
        try:
            tallied = self.vote.finalize_collection(
                force=force,
                reason=reason,
                tie_resolver=resolver,
            )
        except VoteError as exc:
            raise SheriffElectionError(exc.code, str(exc)) from exc
        if tallied.pending_tally is None:
            raise SheriffElectionError("TALLY_INVALID", "election tally was not produced")
        if tallied.pending_tally.is_tie:
            tie_decision = tallied.tie_decision
            if tie_decision is None:
                raise SheriffElectionError("TALLY_INVALID", "tied election has no tie decision")
            if tie_decision.action is TieAction.PK:
                action = SheriffElectionAction.PK
            elif tie_decision.action is TieAction.NO_EXILE:
                action = SheriffElectionAction.NO_SHERIFF
            else:
                raise SheriffElectionError(
                    "TALLY_INVALID",
                    f"unsupported sheriff tie action: {tie_decision.action.value}",
                )
            decision = SheriffElectionDecision(
                action=action,
                candidates=tie_decision.candidates,
                tally=tallied.pending_tally,
            )
            return self._replace(
                status=SheriffElectionStatus.WAITING_GM,
                vote=tallied,
                tie_round=effective_tie_round,
                decision=decision,
            )
        winner = tallied.pending_tally.winner_seat
        if winner is None:
            raise SheriffElectionError("TALLY_INVALID", "unique election tally has no winner")
        decision = SheriffElectionDecision(
            action=SheriffElectionAction.ELECT,
            candidates=(winner,),
            elected_seat=winner,
            tally=tallied.pending_tally,
        )
        return self._replace(
            status=SheriffElectionStatus.WAITING_GM,
            vote=tallied,
            decision=decision,
        )

    def confirm(self) -> SheriffElectionState:
        """Commit the moderator's pending election result into this record.

        A tie that requests PK or revote remains pending.  The caller must
        create the next explicitly board-approved window and call ``open_vote``
        on a new election record; this prevents a generic coordinator from
        silently inventing a second vote.
        """

        if self.status in {SheriffElectionStatus.RESOLVED, SheriffElectionStatus.NO_SHERIFF}:
            return self
        if self.status is not SheriffElectionStatus.WAITING_GM or self.vote is None:
            raise SheriffElectionError("TALLY_NOT_PENDING", "no election tally awaits confirmation")
        if self.decision is None:
            raise SheriffElectionError("TALLY_NOT_PENDING", "election decision is missing")
        self_decision = self.decision
        if self_decision.action is SheriffElectionAction.NO_SHERIFF:
            try:
                confirmed_vote = self.vote.confirm_tally()
            except VoteError as exc:
                raise SheriffElectionError(exc.code, str(exc)) from exc
            return self._replace(
                status=SheriffElectionStatus.NO_SHERIFF,
                vote=confirmed_vote,
                decision=self_decision,
            )
        if self_decision.action is not SheriffElectionAction.ELECT:
            raise SheriffElectionError(
                "ELECTION_ACTION_PENDING",
                f"moderator must handle {self_decision.action.value.lower()} before confirmation",
            )
        if self.vote.status is VoteStatus.WAITING_GM:
            try:
                confirmed_vote = self.vote.confirm_tally()
            except VoteError as exc:
                raise SheriffElectionError(exc.code, str(exc)) from exc
        else:
            confirmed_vote = self.vote
        return self._replace(
            status=SheriffElectionStatus.RESOLVED,
            vote=confirmed_vote,
            decision=self_decision,
            sheriff_seat=self_decision.elected_seat,
        )

    def persisted_payload(self) -> dict[str, object]:
        """Return JSON-shaped data safe for a typed GameState extension field."""

        return self.model_dump(mode="json")


def validate_sheriff_start(
    board: BoardDefinition,
    state: GameState,
    *,
    candidates: tuple[int, ...],
) -> None:
    """Validate the first-day boundary before a coordinator creates a record."""

    sheriff = board.day_flow.sheriff
    if board.status != "published" or (
        board.reviewed_by == "pending-human-review" and not experimental_preview_enabled()
    ):
        raise SheriffElectionError(
            "BOARD_NOT_REVIEWED", "sheriff election requires a reviewed board"
        )
    if (
        state.ruleset is None
        or state.ruleset.board_id != board.board_id
        or state.ruleset.version != board.version
    ):
        raise SheriffElectionError(
            "RULESET_MISMATCH", "board does not match the frozen game ruleset"
        )
    if state.phase is not GamePhase.DAY_ANNOUNCE:
        raise SheriffElectionError("PHASE_NOT_ALLOWED", "sheriff election starts at DAY_ANNOUNCE")
    if state.day_no != 1:
        raise SheriffElectionError(
            "DAY_NOT_ALLOWED", "first-day sheriff election is only available on day 1"
        )
    if sheriff.enabled is not True or sheriff.first_day_election is not True:
        raise SheriffElectionError(
            "SHERIFF_DISABLED", "the board does not enable first-day election"
        )
    eligible = first_day_sheriff_participants(state)
    if tuple(sorted(candidates)) != tuple(candidates) or not candidates:
        raise SheriffElectionError(
            "CANDIDATES_INVALID", "candidate seats must be a non-empty sorted tuple"
        )
    if len(set(candidates)) != len(candidates) or not set(candidates).issubset(eligible):
        raise SheriffElectionError(
            "CANDIDATE_NOT_ELIGIBLE", "all candidates must be unique eligible seats"
        )


def build_sheriff_vote_window(
    *,
    game_id: str,
    day_no: int,
    observation_revision: int,
    session_epoch: int,
    eligible_voters: tuple[int, ...],
    candidates: tuple[int, ...],
    vote_weights: Mapping[int, float],
    expected_request_ids: Mapping[int, str] | None = None,
    allow_abstain: bool = False,
    window_id: str | None = None,
) -> VoteWindow:
    """Build a frozen election vote contract for ``GameManager.open_vote_window``."""

    if day_no != 1:
        raise SheriffElectionError("DAY_NOT_ALLOWED", "sheriff election is first-day only")
    if tuple(sorted(eligible_voters)) != eligible_voters:
        raise SheriffElectionError("ELIGIBILITY_INVALID", "eligible voters must be sorted")
    if tuple(sorted(candidates)) != candidates:
        raise SheriffElectionError("CANDIDATES_INVALID", "candidates must be sorted")
    try:
        return VoteWindow(
            window_id=window_id or f"sheriff-vote-d{day_no}",
            game_id=game_id,
            session_epoch=session_epoch,
            observation_revision=observation_revision,
            eligible_voters=eligible_voters,
            candidate_seats=candidates,
            vote_weights=dict(vote_weights),
            expected_request_ids=dict(expected_request_ids or {}),
            allow_abstain=allow_abstain,
        )
    except (TypeError, ValueError) as exc:
        raise SheriffElectionError("VOTE_WINDOW_INVALID", str(exc)) from exc


def _sheriff_tie_resolver(
    policy: str | None,
    pk_enabled: bool | None,
    tie_round: int,
) -> TieResolver:
    def resolve(window: VoteWindow, tally: VoteTally) -> TieDecision:
        del window
        return resolve_board_tie(
            policy,
            tie_round,
            tuple(tally.top_candidates),
            pk_enabled=pk_enabled is True,
        )

    return resolve


__all__ = [
    "SheriffCampaignSpeech",
    "SheriffCampaignSpeechRequest",
    "SheriffElectionAction",
    "SheriffElectionDecision",
    "SheriffElectionError",
    "SheriffElectionState",
    "SheriffElectionStatus",
    "build_sheriff_vote_window",
    "validate_sheriff_start",
]
