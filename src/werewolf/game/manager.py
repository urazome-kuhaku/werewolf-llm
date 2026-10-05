"""Serialized authoritative state commits for one game.

The manager is deliberately small: board specific rules still arrive through
the frozen action window and validation context.  It owns the only replacement
of the in-memory :class:`~werewolf.game.state.GameState`; model, HTTP, and
filesystem work belongs outside this module and outside its lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import JsonValue

from werewolf.domain.enums import Channel, GamePhase, RunStatus
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.preview import experimental_preview_enabled
from werewolf.knowledge.role import TargetKind, TriggerEffect, TriggerEvent, TriggerMode
from werewolf.rules.adapter import (
    RuleAdapterError,
    RuleExecutionAdapter,
)
from werewolf.rules.adapter import (
    initial_rule_state as build_initial_rule_state,
)
from werewolf.rules.models import (
    AbilityInstance,
    DisclosureProjection,
    DomainFact,
    ExecutionPackage,
    ResolutionBatch,
    RuleHook,
    SkillRequest,
    SkillSpec,
)
from werewolf.rules.predicates import evaluate_predicate
from werewolf.rules.selectors import select_seats

from .actions import (
    ActionRegistry,
    ActionRequest,
    ActionValidationContext,
    ActionValidationError,
    ActionWindow,
    ValidatedActionRequest,
    validate_action_request,
)
from .events import (
    DeliveryCursor,
    EventType,
    GameEvent,
    GmAuditPayload,
    PrivateNoticePayload,
    PrivateRolePayload,
    PrivateSeerResultPayload,
    PrivateWitchTargetPayload,
    PublicAnnouncementPayload,
    PublicSpeechPayload,
    PublicVoteBallot,
    PublicVoteResultPayload,
    TeamNoticePayload,
    TeamSpeechPayload,
)
from .message_router import DeliveryCursorError, DeliverySessionError, MessageRouter
from .phase import can_transition, transition_phase
from .resolution import (
    ActionDisposition,
    ActionResolution,
    ActionResolutionEntry,
    ResolutionEffect,
    ResolutionStatus,
)
from .setup import PlayerAssignmentPlan, build_ability_instances
from .sheriff import (
    SheriffCampaignSpeechRequest,
    SheriffElectionError,
    SheriffElectionState,
    SheriffElectionStatus,
    validate_sheriff_start,
)
from .sheriff_eligibility import first_day_sheriff_participants, is_first_day_sheriff_boundary
from .state import (
    AbilityInstanceState,
    GameState,
    GrantedAbility,
    GrantedTriggerAbility,
    PlayerState,
    RuleBoundary,
    RuleCommitReceipt,
    RuleDeferredDisclosure,
    RuleExecutionIdentity,
    RuleFactRecord,
    RuleLedgerEntry,
    RuleRelationValue,
    RuleReturnPoint,
    RuleStateValue,
    RuleTriggerOccurrence,
    RuleUseRecord,
    RuleWorkflowCursor,
    RuleWorkflowStep,
    SerialTurnBinding,
    utc_now,
)
from .victory import evaluate_victory
from .voting import (
    PlayerVoteObservation,
    TieAction,
    TieResolver,
    VoteError,
    VoteRequest,
    VoteState,
    VoteStatus,
    VoteWindow,
)

if TYPE_CHECKING:
    from .day_resolution import DayExileDecision


class RevisionConflict(ValueError):
    """Raised when a commit was prepared against an obsolete state revision."""


class StatePatchError(ValueError):
    """Raised when a patch does not describe exactly one supported mutation."""


class EventCommitError(ValueError):
    """Raised when an event or delivery commit is not authorized."""


# Persistence remains outside the manager, but a victory boundary may provide
# one serialized writer.  The callback receives the fully reduced candidate
# state and returns the public snapshot reference that was durably published.
# Keeping this protocol here lets the manager install the same reference in
# memory before releasing its commit lock.
SnapshotWriter = Callable[[GameState], Awaitable[Mapping[str, JsonValue]]]


class ResolutionError(ValueError):
    """Raised when a moderator resolution cannot be committed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _action_registry_digest(registry: ActionRegistry) -> str:
    """Hash the canonical complete registry contract frozen for one game."""

    payload = json.dumps(
        registry.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DeliveryAck:
    """The successful result needed to acknowledge one runtime delivery.

    This is deliberately a small value object.  It contains no model output
    and cannot grant visibility: the manager rechecks the seat, session,
    event audience, request, and current revision while holding its lock.
    """

    seat: int
    session_epoch: int
    request_id: str
    event_ids: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.seat, bool) or not 1 <= self.seat <= 64:
            raise ValueError("seat must be an integer between 1 and 64")
        if isinstance(self.session_epoch, bool) or self.session_epoch < 0:
            raise ValueError("session_epoch must be a non-negative integer")
        if not self.request_id or len(self.request_id) > 128:
            raise ValueError("request_id must be a non-empty string of at most 128 characters")
        if self.event_ids is not None:
            if tuple(sorted(set(self.event_ids))) != self.event_ids:
                raise ValueError("event_ids must be sorted and unique")
            if any(isinstance(event_id, bool) or event_id < 0 for event_id in self.event_ids):
                raise ValueError("event_ids must contain non-negative integers")


@dataclass(frozen=True, slots=True)
class _RuleRequestBinding:
    """Map one interpreter request back to its validated game request item."""

    skill_request: SkillRequest
    request_id: str
    action_index: int
    action_code: int


@dataclass(frozen=True, slots=True)
class StatePatch:
    """A validated, pure state change ready for one serialized commit.

    ``validated_request`` is intentionally different from ``ActionRequest``:
    callers must run the request through the authoritative validator before a
    patch can be reduced.  ``GameManager.commit_action_request`` performs that
    validation while holding the commit lock, after rebuilding its context
    from the current state.
    """

    expected_revision: int
    target_phase: GamePhase | None = None
    validated_request: ValidatedActionRequest | None = None
    events_to_append: tuple[GameEvent, ...] = ()
    delivery_ack: DeliveryAck | None = None
    now: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.expected_revision, int) or isinstance(self.expected_revision, bool):
            raise TypeError("expected_revision must be an integer")
        if self.expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        lifecycle_mutation = self.target_phase is not None or self.validated_request is not None
        event_mutation = bool(self.events_to_append) or self.delivery_ack is not None
        if lifecycle_mutation == event_mutation:
            raise StatePatchError("a patch must contain exactly one mutation")
        if self.target_phase is not None and self.validated_request is not None:
            raise StatePatchError("a patch must contain exactly one lifecycle mutation")
        if self.now is not None:
            if self.now.tzinfo is None or self.now.utcoffset() is None:
                raise ValueError("patch timestamp must include a timezone")
            object.__setattr__(self, "now", self.now.astimezone(UTC))

    @classmethod
    def phase_transition(
        cls,
        target: GamePhase,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> StatePatch:
        return cls(expected_revision=expected_revision, target_phase=target, now=now)

    @classmethod
    def action_request(
        cls,
        request: ValidatedActionRequest,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> StatePatch:
        return cls(
            expected_revision=expected_revision,
            validated_request=request,
            now=now,
        )

    @classmethod
    def event_delivery(
        cls,
        events: Iterable[GameEvent] = (),
        ack: DeliveryAck | None = None,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> StatePatch:
        """Build the private event-plus-ack patch used by the manager."""

        event_tuple = tuple(events)
        if not event_tuple and ack is None:
            raise StatePatchError("an event patch must append events or acknowledge delivery")
        return cls(
            expected_revision=expected_revision,
            events_to_append=event_tuple,
            delivery_ack=ack,
            now=now,
        )

    @classmethod
    def append_events(
        cls,
        events: Iterable[GameEvent],
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> StatePatch:
        return cls.event_delivery(events, expected_revision=expected_revision, now=now)


def _revision_check(state: GameState, expected_revision: int) -> None:
    if state.state_revision != expected_revision:
        raise RevisionConflict(
            f"state revision mismatch: expected {expected_revision}, actual {state.state_revision}"
        )


def _aware_commit_time(now: datetime | None) -> datetime:
    value = utc_now() if now is None else now
    if value.tzinfo is None or value.utcoffset() is None:
        raise EventCommitError("TIMESTAMP: commit timestamp must include a timezone")
    return value.astimezone(UTC)


def _stable_rule_identifier(*components: object) -> str:
    payload = json.dumps(components, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _frozen_string_tuple(value: object) -> tuple[str, ...] | None:
    """Narrow frozen JSON arrays before comparing typed fact identities."""

    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(cast(str, item) for item in value)


def _request_payload(request: ValidatedActionRequest) -> dict[str, object]:
    payload = request.model_dump(mode="json")
    payload["status"] = "PENDING"
    return payload


def _load_action_window(raw: object) -> ActionWindow:
    """Validate an in-state window, accepting its JSON snapshot form."""

    if not isinstance(raw, dict):
        raise ValueError("action window must be a mapping")
    # ``GameState`` freezes JSON extension containers recursively, which turns
    # nested arrays into tuples.  Round-trip the wire-shaped snapshot before
    # strict Pydantic validation so nested visible context remains JSON data.
    data = json.loads(json.dumps(raw))
    phase = data.get("phase")
    if isinstance(phase, str):
        data["phase"] = GamePhase(phase)
    elif not isinstance(phase, GamePhase):
        raise ValueError("action window phase is missing or invalid")
    return ActionWindow.model_validate(data)


def _is_sheriff_badge_window(window: ActionWindow) -> bool:
    """Return whether ``window`` is the explicit sheriff badge capability."""

    return window.visible_context.get("kind") == "sheriff_badge"


def _sheriff_badge_allowed_phases(state: GameState) -> frozenset[GamePhase]:
    """Return the daytime phases authorized for the current badge boundary.

    The first-day election keeps its election record while the public day
    speech boundary is active.  That is the only circumstance in which a
    badge action may run during ``DAY_SPEECH``; later day speech without an
    election context must remain outside the badge protocol.
    """

    phases = {
        GamePhase.DAY_ANNOUNCE,
        GamePhase.DAY_RESOLVE,
        GamePhase.TRIGGER_ACTION,
    }
    if state.phase is GamePhase.DAY_SPEECH and state.sheriff_election is not None:
        phases.add(GamePhase.DAY_SPEECH)
    return frozenset(phases)


def _sheriff_badge_trigger_origin(state: GameState) -> tuple[str, str] | None:
    """Return the latest closed trigger's operation and source resolution ID."""

    for raw_window in reversed(tuple(state.action_windows.values())):
        try:
            window = _load_action_window(raw_window)
        except (TypeError, ValueError):
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


def _sheriff_badge_binding_error(state: GameState, window: ActionWindow) -> str | None:
    """Return a stable error when a badge window is not bound to the office.

    Badge windows are a privileged dead-seat exception to the normal action
    protocol.  The visible ``kind`` marker alone is never sufficient: every
    request must still point at the currently held office, its session epoch,
    and the exact frozen candidate set installed by the manager.
    """

    if not _is_sheriff_badge_window(window):
        return None
    marker = state.sheriff_badge
    if not isinstance(marker, dict) or marker.get("status") != "OPEN":
        return "BADGE_ACTION_INVALID: no open sheriff badge marker is bound"
    source = marker.get("source_seat")
    office = marker.get("office_seat")
    epoch = marker.get("source_session_epoch")
    window_id = marker.get("window_id")
    if type(source) is not int or type(office) is not int or type(epoch) is not int:
        return "BADGE_ACTION_INVALID: sheriff badge marker is malformed"
    if window_id != window.window_id or office != source or state.sheriff_seat != office:
        return "BADGE_ACTION_INVALID: badge window is not bound to the current office"
    player = state.players.get(source)
    if player is None or player.session_epoch != epoch:
        return "BADGE_ACTION_INVALID: badge window uses an obsolete sheriff session"
    if state.phase not in _sheriff_badge_allowed_phases(state) or (window.phase is not state.phase):
        return "BADGE_ACTION_INVALID: badge window is outside the daytime boundary"
    if window.game_id != state.game_id or window.session_epoch != epoch:
        return "BADGE_ACTION_INVALID: badge window game or session does not match"
    if window.allowed_seats != (source,):
        return "BADGE_ACTION_INVALID: badge window authorizes another seat"
    if window.allowed_action_codes != (201, 202):
        return "BADGE_ACTION_INVALID: badge window action contract is invalid"
    if window.min_actions != 1 or window.max_actions != 1 or window.allow_pass:
        return "BADGE_ACTION_INVALID: badge window cardinality is invalid"
    candidates = window.visible_context.get("candidate_seats")
    if not isinstance(candidates, (list, tuple)):
        return "BADGE_ACTION_INVALID: badge candidates are missing"
    legal = tuple(
        sorted(
            seat
            for seat in candidates
            if type(seat) is int
            and seat in state.players
            and seat != source
            and state.players[seat].alive
            and state.players[seat].can_vote
        )
    )
    if tuple(candidates) != legal:
        return "BADGE_ACTION_INVALID: badge candidates are not authoritative"
    marker_candidates = marker.get("candidate_seats")
    if isinstance(marker_candidates, (list, tuple)) and tuple(marker_candidates) != legal:
        return "BADGE_ACTION_INVALID: badge candidates changed after opening"
    if marker_candidates is not None and not isinstance(marker_candidates, (list, tuple)):
        return "BADGE_ACTION_INVALID: badge candidates marker is malformed"
    if state.phase is GamePhase.TRIGGER_ACTION:
        if state.pending_resolution is not None:
            return "BADGE_ACTION_INVALID: trigger resolution is still pending"
        origin = _sheriff_badge_trigger_origin(state)
        if origin is None or origin[0] != "DAY_EXILE":
            return "BADGE_ACTION_INVALID: trigger badge requires a closed DAY_EXILE source"
        if window.visible_context.get("origin") != "DAY_EXILE":
            return "BADGE_ACTION_INVALID: badge origin is not bound to DAY_EXILE"
        if window.visible_context.get("origin_resolution_id") != origin[1]:
            return "BADGE_ACTION_INVALID: badge source resolution does not match"
    if player.alive and player.can_vote:
        return "BADGE_ACTION_INVALID: the sheriff is still eligible"
    return None


def _active_team_chat_window(state: GameState) -> ActionWindow:
    """Load the single open team window authorized by the current state."""

    if state.phase is not GamePhase.NIGHT_TEAM_CHAT:
        raise EventCommitError("PHASE_MISMATCH: serial team speech requires NIGHT_TEAM_CHAT")
    candidates: list[ActionWindow] = []
    for raw in state.action_windows.values():
        try:
            window = _load_action_window(raw)
        except ValueError as exc:
            raise EventCommitError("WINDOW_INVALID: installed action window is malformed") from exc
        if window.phase is GamePhase.NIGHT_TEAM_CHAT and window.accepts_submissions:
            if window.game_id != state.game_id:
                raise EventCommitError("GAME_MISMATCH: team window belongs to another game")
            candidates.append(window)
    if not candidates:
        raise EventCommitError(
            "TEAM_WINDOW_NOT_OPEN: no open NIGHT_TEAM_CHAT action window is installed"
        )
    if len(candidates) != 1:
        raise EventCommitError(
            "TEAM_WINDOW_AMBIGUOUS: more than one open NIGHT_TEAM_CHAT window is installed"
        )
    return candidates[0]


def _validate_team_chat_seat(state: GameState, seat: int) -> ActionWindow:
    """Recheck a serial team speaker against the frozen team window."""

    window = _active_team_chat_window(state)
    if seat not in window.allowed_seats:
        raise EventCommitError(
            "TEAM_SEAT_NOT_AUTHORIZED: seat is not authorized by the active team window"
        )
    player = state.players.get(seat)
    if player is None:
        raise EventCommitError("SEAT_NOT_ASSIGNED: team speaker is not a current player")
    if player.session_epoch != window.session_epoch:
        raise EventCommitError("SESSION_MISMATCH: team window uses an obsolete session")
    return window


def _load_vote_state(raw: object) -> VoteState:
    """Validate the private vote collector stored in a game snapshot."""

    if not isinstance(raw, dict):
        raise EventCommitError("VOTE_STATE_INVALID: current vote state is not a mapping")
    try:
        # ``GameState.vote_state`` is stored in JSON form, where enum values
        # are strings and integer mapping keys are necessarily strings.
        return VoteState.model_validate_json(json.dumps(raw))
    except ValueError as exc:
        raise EventCommitError("VOTE_STATE_INVALID: current vote state is malformed") from exc


def _vote_state_payload(state: VoteState) -> dict[str, object]:
    """Return the JSON snapshot form used by ``GameState.vote_state``."""

    return state.model_dump(mode="json")


def _validate_vote_board(board: BoardDefinition, state: GameState) -> None:
    """Check that a vote publication policy belongs to this frozen game."""

    if not isinstance(board, BoardDefinition):
        raise VoteError("BOARD_INVALID", "board must be a validated BoardDefinition")
    if board.status != "published" or (
        board.reviewed_by == "pending-human-review" and not experimental_preview_enabled()
    ):
        raise VoteError("BOARD_NOT_REVIEWED", "vote publication requires a reviewed board")
    if (
        state.ruleset is None
        or state.ruleset.board_id != board.board_id
        or state.ruleset.version != board.version
    ):
        raise VoteError("RULESET_MISMATCH", "board does not match the frozen game ruleset")


def _public_vote_payload(
    vote_state: VoteState,
    *,
    board: BoardDefinition | None,
    vote_kind: Literal["day", "day_pk", "sheriff", "sheriff_pk"],
    elected_seat: int | None = None,
) -> PublicVoteResultPayload:
    """Project a confirmed private vote according to the frozen board policy.

    A missing board is retained only for older low-level callers and produces
    the historical tally-only event.  Production coordinators always pass
    their validated board, so no caller-controlled reveal flag can widen the
    projection.
    """

    result = vote_state.public_result
    if result is None:  # pragma: no cover - callers confirm before projecting
        raise EventCommitError("VOTE_RESULT_INVALID: confirmation produced no result")
    reveal = "totals_only" if board is None else board.day_flow.vote.reveal_after_close
    if reveal == "ballots_and_totals":
        ballots = tuple(
            PublicVoteBallot(
                voter_seat=seat,
                target_seat=ballot.target_seat,
                weight=ballot.vote_weight,
            )
            for seat, ballot in sorted(vote_state.ballots.items())
        )
        tally = dict(result.tally.counts)
    elif reveal == "totals_only":
        ballots = ()
        tally = dict(result.tally.counts)
    elif reveal == "none":
        ballots = ()
        tally = {}
    else:  # pragma: no cover - BoardDefinition constrains this field
        raise EventCommitError("VOTE_POLICY_INVALID: unsupported reveal_after_close policy")
    return PublicVoteResultPayload(
        vote_kind=vote_kind,
        eliminated_seat=(None if vote_kind.startswith("sheriff") else result.eliminated_seat),
        elected_seat=elected_seat,
        tally=tally,
        ballots=ballots,
    )


def _load_sheriff_election(raw: object) -> SheriffElectionState:
    """Validate the private sheriff record restored from a game snapshot."""

    if not isinstance(raw, dict):
        raise EventCommitError("SHERIFF_STATE_INVALID: election record is not a mapping")
    try:
        return SheriffElectionState.model_validate_json(json.dumps(raw))
    except ValueError as exc:
        raise EventCommitError("SHERIFF_STATE_INVALID: election record is malformed") from exc


def _sheriff_election_payload(state: SheriffElectionState) -> dict[str, object]:
    """Return the JSON snapshot form used by ``GameState.sheriff_election``."""

    return state.persisted_payload()


def _sheriff_serial_speech_queue(
    state: GameState,
    phase: GamePhase,
) -> tuple[int, ...]:
    """Return the authoritative, not-yet-spoken sheriff candidate queue.

    Sheriff campaign speech is a frozen candidate protocol.  A caller may
    never turn it into a generic seat queue: the order comes from the
    election record and candidates that already submitted a speech are
    removed from that order.  The same rule is used for the initial and PK
    speech phases so recovery cannot reinsert a non-candidate seat.
    """

    if phase not in {
        GamePhase.SHERIFF_ELECTION_SPEECH,
        GamePhase.SHERIFF_ELECTION_PK_SPEECH,
    }:
        raise EventCommitError(
            "PHASE_INVALID: sheriff serial speech requires a sheriff speech phase"
        )
    if state.phase is not phase:
        raise EventCommitError(f"PHASE_MISMATCH: sheriff serial speech requires {phase.value}")
    election = _load_sheriff_election(state.sheriff_election)
    if election.status is not SheriffElectionStatus.SPEECH:
        raise EventCommitError(
            "ELECTION_STATUS_INVALID: sheriff serial speech requires SPEECH status"
        )
    if phase is GamePhase.SHERIFF_ELECTION_SPEECH and election.tie_round != 0:
        raise EventCommitError("PHASE_MISMATCH: PK election speeches require the PK phase")
    if phase is GamePhase.SHERIFF_ELECTION_PK_SPEECH and election.tie_round == 0:
        raise EventCommitError(
            "PHASE_MISMATCH: initial election speeches require the initial phase"
        )
    return tuple(seat for seat in election.speech_order if seat not in election.speeches)


def _validate_sheriff_board(board: BoardDefinition, state: GameState) -> None:
    """Ensure every coordinator operation uses the game's frozen board."""

    if not isinstance(board, BoardDefinition):
        raise SheriffElectionError("BOARD_INVALID", "board must be a validated BoardDefinition")
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
    if board.day_flow.sheriff.enabled is not True:
        raise SheriffElectionError("SHERIFF_DISABLED", "the board does not enable a sheriff")


def _state_data(state: GameState) -> dict[str, Any]:
    """Copy state for a reducer with JSON extension containers unfrozen."""

    data = state.model_dump(mode="python", exclude={"vote_state"}, warnings=False)
    # ``GameState`` freezes nested JSON arrays as tuples in memory.  The
    # extension fields are persisted JSON and must be converted back to lists
    # before a replacement model is validated. JSON event records are thawed
    # the same way while typed protocol records stay in Python form.
    data["events"] = tuple(
        event if isinstance(event, GameEvent) else json.loads(json.dumps(event))
        for event in state.events
    )
    for field_name in (
        "action_windows",
        "action_requests",
        "pending_resolution",
        "knowledge_receipts",
        "winner",
        "last_snapshot",
        "sheriff_election",
        "sheriff_badge",
    ):
        data[field_name] = json.loads(json.dumps(data[field_name]))
    for field_name in ("resolutions", "knowledge_receipts", "moderator_audit"):
        data[field_name] = tuple(json.loads(json.dumps(data[field_name])))
    data["rule_state"] = tuple(_rule_state_payload(item) for item in state.rule_state)
    data["rule_ledger"] = tuple(_rule_ledger_payload(item) for item in state.rule_ledger)
    data["rule_receipts"] = tuple(item.model_dump(mode="python") for item in state.rule_receipts)
    if state.vote_state is not None:
        data["vote_state"] = json.loads(json.dumps(state.vote_state))
    return data


def _rule_state_payload(value: RuleStateValue) -> dict[str, object]:
    """Keep typed tuple fields while thawing only the nested JSON value."""

    return {
        "schema_version": value.schema_version,
        "scope": value.scope,
        "scope_id": value.scope_id,
        "skill_id": value.skill_id,
        "key": value.key,
        "value_type": value.value_type,
        "value": json.loads(json.dumps(value.value)),
        "source_batch_id": value.source_batch_id,
        "expiry_policy": value.expiry_policy,
        "expires_at_round": value.expires_at_round,
        "expires_at_hook": value.expires_at_hook,
    }


def _rule_ledger_payload(value: RuleLedgerEntry) -> dict[str, object]:
    """Thaw fact data without converting strict timestamps or tuple fields."""

    return {
        "schema_version": value.schema_version,
        "batch_id": value.batch_id,
        "package_id": value.package_id,
        "group_id": value.group_id,
        "timing": value.timing,
        "read_revision": value.read_revision,
        "committed_revision": value.committed_revision,
        "round_no": value.round_no,
        "request_ids": value.request_ids,
        "actor_seats": value.actor_seats,
        "skill_ids": value.skill_ids,
        "action_codes": value.action_codes,
        "history_updates": tuple(item.model_dump(mode="python") for item in value.history_updates),
        "facts": tuple(
            {
                "schema_version": item.schema_version,
                "fact_id": item.fact_id,
                "fact_type": item.fact_type,
                "source_rule_id": item.source_rule_id,
                "source_request_id": item.source_request_id,
                "actor_seat": item.actor_seat,
                "target_seat": item.target_seat,
                "death_cause": item.death_cause,
                "tags": item.tags,
                "data": json.loads(json.dumps(item.data)),
            }
            for item in value.facts
        ),
        "outcome_digest": value.outcome_digest,
        "created_at": value.created_at,
    }


def _players_ready(state: GameState) -> bool:
    """Return whether every assigned seat has a committed knowledge receipt set."""

    return bool(state.players) and all(
        bool(player.knowledge_receipt_ids) for player in state.players.values()
    )


def _night_death_trigger_candidates(
    before: GameState | None,
    after: GameState,
    resolutions: Iterable[ActionResolution],
    *,
    action_window_id: str,
) -> tuple[tuple[int, GrantedTriggerAbility, str, str], ...]:
    """Find unconsumed death triggers caused by one night action window.

    ``before`` is supplied for the normal atomic path.  Recovery after the
    effect bundle was committed has no earlier in-memory state, so the
    explicit ``SET_ALIVE=false`` effects in the persisted resolutions define
    the death set there.
    """

    source_by_seat: dict[int, str] = {}
    for resolution in resolutions:
        if resolution.window_id != action_window_id:
            continue
        for entry in resolution.actions:
            for effect in entry.effects:
                if (
                    effect.effect_type == "SET_ALIVE"
                    and effect.value is False
                    and effect.target_seat not in source_by_seat
                ):
                    source_by_seat[effect.target_seat] = resolution.resolution_id

    candidates: list[tuple[int, GrantedTriggerAbility, str, str]] = []
    for seat, source_resolution_id in source_by_seat.items():
        player = after.players.get(seat)
        previous = before.players.get(seat) if before is not None else None
        if (
            player is None
            or player.alive
            or not isinstance(player.death_cause, str)
            or (previous is not None and not previous.alive)
        ):
            continue
        for ability in player.granted_trigger_abilities:
            trigger = ability.trigger
            if (
                not ability.consumed
                and trigger.event is TriggerEvent.DEATH_CONFIRMED
                and player.death_cause in trigger.allowed_death_causes
            ):
                candidates.append((seat, ability, player.death_cause, source_resolution_id))
    return tuple(candidates)


def _night_private_result_events(
    state: GameState,
    resolutions: Iterable[ActionResolution],
    *,
    registry: ActionRegistry,
    next_revision: int,
    now: datetime,
) -> tuple[GameEvent, ...]:
    """Build private classic result events inside the night transaction.

    The action request is the player's intent; the pre-commit ``state`` is
    the frozen observation used for private information.  In particular,
    seer results use the target's faction from that snapshot and never expose
    the target role.  Existing correlation IDs make recovery/idempotent calls
    unable to append duplicate private notices.
    """

    events = _typed_events(state)
    known_correlations = {event.correlation_id for event in events if event.correlation_id}
    candidates = tuple(resolutions)
    output: list[GameEvent] = []
    next_event_id = max((event.event_id for event in events), default=0) + 1
    seer_code = next(
        (item.action_code for item in registry.actions if item.action_name == "SEER_INSPECT"),
        None,
    )

    for resolution in candidates:
        if resolution.status not in {
            ResolutionStatus.CONFIRMED,
            ResolutionStatus.OVERRIDDEN,
        }:
            continue
        raw_request = state.action_requests.get(resolution.request_id)
        if not isinstance(raw_request, Mapping):
            continue
        actor_seat = raw_request.get("seat")
        if not isinstance(actor_seat, int) or isinstance(actor_seat, bool):
            continue
        actor = state.players.get(actor_seat)
        if actor is None:
            continue
        for entry in resolution.actions:
            if entry.disposition not in {
                ActionDisposition.CONFIRMED,
                ActionDisposition.OVERRIDDEN,
            }:
                continue
            action = entry.resolved_action or entry.requested_action
            if seer_code is not None and action.action_code == seer_code and action.targets:
                target = action.targets[0]
                target_player = state.players.get(target)
                if target_player is None:
                    continue
                correlation = f"night-seer-result-{resolution.request_id}-{entry.action_index}"
                if correlation in known_correlations:
                    continue
                output.append(
                    GameEvent.private(
                        event_id=next_event_id,
                        game_id=state.game_id,
                        state_revision=next_revision,
                        round_no=state.round_no,
                        phase=GamePhase.NIGHT_RESOLVE,
                        created_at=now,
                        event_type=EventType.SEER_RESULT,
                        seat=actor_seat,
                        actor_seat=actor_seat,
                        correlation_id=correlation,
                        payload=PrivateSeerResultPayload(
                            target_seat=target,
                            faction_id=target_player.faction_id,
                        ),
                    )
                )
                known_correlations.add(correlation)
                next_event_id += 1
    return tuple(output)


def _batch_live_actor_seats(
    state: GameState,
    resolutions: Iterable[ActionResolution],
) -> frozenset[int]:
    """Snapshot actors that were alive when a simultaneous batch was read."""

    seats: set[int] = set()
    for resolution in resolutions:
        payload = state.action_requests.get(resolution.request_id)
        if not isinstance(payload, Mapping):
            continue
        seat = payload.get("seat")
        if (
            isinstance(seat, int)
            and not isinstance(seat, bool)
            and seat in state.players
            and state.players[seat].alive
        ):
            seats.add(seat)
    return frozenset(seats)


def _authoritative_vote_window(
    state: GameState,
    window: VoteWindow,
    *,
    allow_first_day_sheriff_participants: bool = False,
) -> VoteWindow:
    """Check a proposed window against current player facts.

    ``VoteWindow`` freezes the observation boundary and weights, while the
    manager still verifies that those values came from the current state.  A
    missing current request is allowed for scheduler-created windows; when a
    request is present it is checked again for every ballot submission.
    """

    if window.game_id != state.game_id:
        raise VoteError("GAME_MISMATCH", "vote window does not belong to the current game")
    if window.observation_revision != state.state_revision:
        raise VoteError(
            "REVISION_MISMATCH",
            "vote window observation_revision must equal the current state revision",
        )
    sheriff_exception = allow_first_day_sheriff_participants and is_first_day_sheriff_boundary(
        state
    )
    sheriff_participants = first_day_sheriff_participants(state) if sheriff_exception else ()
    for seat in window.eligible_voters:
        player = state.players.get(seat)
        if player is None:
            raise VoteError("SEAT_NOT_ASSIGNED", "eligible voter is not assigned in the game")
        if player.session_epoch != window.session_epoch:
            raise VoteError("SESSION_MISMATCH", "eligible voter session is obsolete")
        if seat not in sheriff_participants and (not player.alive or not player.can_vote):
            raise VoteError("SEAT_NOT_ELIGIBLE", "eligible voter is dead or has no voting right")
        if player.vote_weight != window.vote_weights[seat]:
            raise VoteError(
                "VOTE_WEIGHT_MISMATCH",
                "vote weight does not match the authoritative player state",
            )
        expected = window.expected_request_ids.get(seat)
        if expected is not None and player.current_request_id is not None:
            if expected != player.current_request_id:
                raise VoteError("REQUEST_MISMATCH", "vote window request is no longer active")
    for seat in window.candidate_seats:
        player = state.players.get(seat)
        if player is None:
            raise VoteError("TARGET_NOT_ALLOWED", "vote candidate is not assigned in the game")
        if seat not in sheriff_participants and not player.alive:
            raise VoteError("TARGET_NOT_ALLOWED", "vote candidate is not alive")
    return window


def _authoritative_vote_request(
    state: GameState,
    vote_state: VoteState,
    request: VoteRequest,
    *,
    allow_first_day_sheriff_participants: bool = False,
) -> None:
    """Recheck mutable player authorization before invoking ``VoteState``."""

    player = state.players.get(request.seat)
    if player is None:
        raise VoteError("SEAT_NOT_ASSIGNED", "vote seat is not assigned in the game")
    if player.session_epoch != request.session_epoch:
        raise VoteError("SESSION_MISMATCH", "vote request uses an obsolete session")
    sheriff_exception = allow_first_day_sheriff_participants and is_first_day_sheriff_boundary(
        state
    )
    sheriff_participants = first_day_sheriff_participants(state) if sheriff_exception else ()
    if request.seat not in sheriff_participants and (not player.alive or not player.can_vote):
        raise VoteError("SEAT_NOT_ELIGIBLE", "player is dead or has no voting right")
    expected = vote_state.window.expected_request_ids.get(request.seat)
    if expected is not None and request.request_id != expected:
        raise VoteError("REQUEST_MISMATCH", "request_id does not match the frozen seat request")
    if player.current_request_id is not None and player.current_request_id != request.request_id:
        raise VoteError("REQUEST_MISMATCH", "request_id is no longer the player's active request")
    expected_weight = vote_state.window.vote_weights.get(request.seat)
    if expected_weight is None or player.vote_weight != expected_weight:
        raise VoteError(
            "VOTE_WEIGHT_MISMATCH",
            "vote weight changed after the vote window was opened",
        )


def _reduce_action_request(
    state: GameState,
    patch: StatePatch,
    *,
    delivery_ack: DeliveryAck | None = None,
) -> GameState:
    request = patch.validated_request
    if request is None:  # pragma: no cover - guarded by StatePatch
        raise StatePatchError("action patch is missing validated_request")

    def acknowledge_candidate(current: GameState) -> DeliveryCursor | None:
        if delivery_ack is None:
            return None
        return _acknowledge_delivery(current, _typed_events(current), delivery_ack)

    previous = state.action_requests.get(request.request_id)
    if previous is not None:
        previous_fingerprint = previous.get("request_fingerprint")
        if previous_fingerprint == request.request_fingerprint:
            # An identical retry is a read-only idempotent replay.  In
            # particular, it must not consume another revision unless a
            # previously frozen delivery still needs its successful ack.
            cursor_candidate = acknowledge_candidate(state)
            if cursor_candidate is None:
                return state
            data = _state_data(state)
            cursors = dict(data["delivery_cursors"])
            cursors[request.seat] = cursor_candidate
            data["delivery_cursors"] = cursors
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = patch.now or utc_now()
            return GameState.model_validate(data)
        raise ActionValidationError(
            "IDEMPOTENCY_CONFLICT",
            "request_id was already committed with a different request payload",
        )

    raw_window = state.action_windows.get(request.window_id)
    if raw_window is not None and not _load_action_window(raw_window).accepts_submissions:
        raise ActionValidationError("WINDOW_CLOSED", "action window no longer accepts new requests")

    data = _state_data(state)
    action_requests = dict(data["action_requests"])
    action_requests[request.request_id] = _request_payload(request)
    data["action_requests"] = action_requests

    # Keep the generic persisted window projection in sync.  This is only
    # submission bookkeeping; skills, resources, and game effects remain
    # untouched until a future ActionResolution commit.
    windows = dict(data["action_windows"])
    raw_window = windows.get(request.window_id)
    if raw_window is not None:
        window = _load_action_window(raw_window)
        if request.request_id not in window.submitted_request_ids:
            # ``GameState.action_windows`` is a JsonValue container, so
            # write the wire form back rather than enum/datetime objects.
            window_data = window.model_dump(mode="json")
            window_data["submitted_request_ids"] = [
                *window.submitted_request_ids,
                request.request_id,
            ]
            fingerprints = dict(window.submitted_request_fingerprints)
            fingerprints[request.request_id] = request.request_fingerprint
            window_data["submitted_request_fingerprints"] = fingerprints
            windows[request.window_id] = window_data
    data["action_windows"] = windows
    cursor_candidate = acknowledge_candidate(state)
    if cursor_candidate is not None:
        cursors = dict(data["delivery_cursors"])
        cursors[request.seat] = cursor_candidate
        data["delivery_cursors"] = cursors
    data["state_revision"] = state.state_revision + 1
    data["updated_at"] = patch.now or utc_now()
    return GameState.model_validate(data)


def _stored_action_request(state: GameState, request_id: str) -> ActionRequest:
    """Load the immutable request payload recorded by ``commit_action_request``."""

    raw = state.action_requests.get(request_id)
    if raw is None or not isinstance(raw, dict):
        raise ResolutionError("REQUEST_NOT_FOUND", "request_id is not a committed action request")
    payload = dict(raw)
    for key in (
        "status",
        "validated_at",
        "request_fingerprint",
        "idempotent_replay",
        "resolution_id",
        "rule_receipt_id",
        "rule_group_id",
        "rule_disposition",
        "rule_dispositions",
    ):
        payload.pop(key, None)
    phase = payload.get("phase")
    if isinstance(phase, str):
        payload["phase"] = GamePhase(phase)
    try:
        return ActionRequest.model_validate(payload)
    except ValueError as exc:
        raise ResolutionError("REQUEST_INVALID", "stored action request is malformed") from exc


def _resolution_entry_action(
    entry: ActionResolutionEntry,
    *,
    request: ActionRequest,
    registry: ActionRegistry,
) -> tuple[Any, Any]:
    """Validate one ruling's action identity and return its effective action."""

    if entry.action_index >= len(request.actions):
        raise ResolutionError(
            "ACTION_INDEX", "resolution action index is outside the request bundle"
        )
    requested = request.actions[entry.action_index]
    if entry.requested_action != requested:
        raise ResolutionError(
            "REQUEST_ACTION_MISMATCH",
            "resolution requested_action does not match the committed request",
        )
    try:
        definition = registry.get(requested.action_code)
    except KeyError as exc:
        raise ResolutionError(
            "UNKNOWN_ACTION", "request action is absent from the registry"
        ) from exc

    effective = entry.resolved_action or requested
    if effective.action_code != requested.action_code:
        raise ResolutionError(
            "ACTION_CODE_MISMATCH",
            "a resolution may change targets but cannot change the requested action type",
        )
    if entry.resolved_action is not None and entry.disposition is ActionDisposition.CONFIRMED:
        if entry.resolved_action != requested:
            raise ResolutionError(
                "DISPOSITION_MISMATCH",
                "a changed effective action must be marked OVERRIDDEN",
            )
    if len(effective.targets) != definition.target_count:
        raise ResolutionError("TARGET_COUNT", "resolved action has the wrong target count")
    # A resource action's exact cost belongs to the immutable grant copied
    # onto the actor.  The reducer checks that value after selecting the
    # grant; this shape check still rejects a cancelled action carrying a
    # fabricated cost and keeps resource-less actions at zero.
    expected_cost = 0 if entry.disposition is ActionDisposition.CANCELLED else None
    if expected_cost == 0 and entry.resource_cost != expected_cost:
        raise ResolutionError(
            "RESOURCE_COST_MISMATCH",
            f"resource_cost must be 0 for cancelled {definition.action_name}",
        )
    if definition.resource_id is None and entry.resource_cost != 0:
        raise ResolutionError(
            "RESOURCE_COST_MISMATCH",
            f"resource_cost must be 0 for {definition.action_name}",
        )
    if entry.disposition is ActionDisposition.CANCELLED:
        if entry.resolved_action is not None or entry.effects:
            raise ResolutionError(
                "CANCELLED_EFFECT",
                "cancelled actions cannot carry an effective action or effects",
            )
    return effective, definition


def _validate_resolution_effect(
    effect: ResolutionEffect,
    *,
    entry: ActionResolutionEntry,
    effective_action: object,
    definition: object,
    actor_seat: int,
    state: GameState,
    seen_effect_ids: set[str],
) -> tuple[str, object | None]:
    """Validate effect shape and target binding without mutating state."""

    if effect.effect_id in seen_effect_ids:
        raise ResolutionError("DUPLICATE_EFFECT", "effect_id must be unique in a resolution")
    seen_effect_ids.add(effect.effect_id)
    if effect.action_index != entry.action_index:
        raise ResolutionError(
            "EFFECT_ACTION_MISMATCH", "effect action_index does not match its entry"
        )
    target = effect.target_seat
    if target not in state.players:
        raise ResolutionError("TARGET_NOT_ASSIGNED", "effect target is not an assigned seat")

    value = effect.value
    if effect.effect_type == "SET_ALIVE":
        if not isinstance(value, bool):
            raise ResolutionError("EFFECT_VALUE", "SET_ALIVE requires a boolean value")
    elif effect.effect_type == "SET_CAN_VOTE":
        if not isinstance(value, bool):
            raise ResolutionError("EFFECT_VALUE", "SET_CAN_VOTE requires a boolean value")
    elif effect.effect_type == "SET_DEATH_CAUSE":
        if value is not None and not isinstance(value, str):
            raise ResolutionError("EFFECT_VALUE", "SET_DEATH_CAUSE requires a string or null")
    elif effect.effect_type == "SET_VOTE_WEIGHT":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ResolutionError(
                "EFFECT_VALUE", "SET_VOTE_WEIGHT requires a finite non-negative number"
            )
    elif effect.effect_type == "ADJUST_RESOURCE":
        if effect.resource_id is None:
            raise ResolutionError("RESOURCE_ID", "ADJUST_RESOURCE requires resource_id")
        if target != actor_seat:
            raise ResolutionError("RESOURCE_TARGET", "resource changes may only affect the actor")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ResolutionError("EFFECT_VALUE", "ADJUST_RESOURCE requires an integer delta")
        resource_id = getattr(definition, "resource_id")
        if resource_id is not None and effect.resource_id == resource_id:
            raise ResolutionError(
                "RESOURCE_DUPLICATE",
                "the action resource is consumed by resource_cost, not a duplicate effect",
            )
    if effect.effect_type != "ADJUST_RESOURCE":
        action_targets = getattr(effective_action, "targets")
        if action_targets:
            if target not in action_targets:
                raise ResolutionError(
                    "EFFECT_TARGET_MISMATCH",
                    "effect target must be one of the resolved action targets",
                )
        elif target != actor_seat:
            raise ResolutionError(
                "EFFECT_TARGET_MISMATCH",
                "a no-target action may only affect its acting seat",
            )
    return effect.effect_type, value


def _pending_trigger_ability(
    state: GameState,
    window: ActionWindow,
    *,
    require_bound: bool = False,
) -> tuple[PlayerState, GrantedTriggerAbility] | None:
    """Validate a trigger window against its assigned ability contract.

    A trigger window is a privileged exception because its actor may already
    be dead.  Every binding fact is read from the pending resolution and the
    private granted ability copy; a role ID or a client supplied visible
    context can never grant that exception.
    """

    pending = state.pending_resolution
    if state.phase is not GamePhase.TRIGGER_ACTION or not isinstance(pending, dict):
        return None
    if pending.get("status") != "TRIGGER_ACTION_REQUIRED":
        return None
    resolution_id = pending.get("resolution_id")
    seat = pending.get("seat")
    ability_id = pending.get("ability_id")
    action_code = pending.get("action_code")
    event_value = pending.get("trigger_event")
    if (
        not isinstance(resolution_id, str)
        or not resolution_id
        or isinstance(seat, bool)
        or not isinstance(seat, int)
        or not isinstance(ability_id, str)
        or not ability_id
        or isinstance(action_code, bool)
        or not isinstance(action_code, int)
        or not isinstance(event_value, str)
    ):
        return None
    expected_window_id = pending.get("window_id")
    if require_bound and not isinstance(expected_window_id, str):
        return None
    if expected_window_id is None:
        expected_window_id = f"{resolution_id}-trigger-{ability_id}"
        if len(expected_window_id) > 128:
            expected_window_id = f"trigger-{seat}-{resolution_id[-60:]}-{ability_id[-40:]}"
    if expected_window_id != window.window_id:
        return None
    player = state.players.get(seat)
    if player is None:
        return None
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
    if ability is None or ability.trigger.event.value != event_value:
        return None
    trigger = ability.trigger
    cause = pending.get("death_cause")
    if trigger.event is TriggerEvent.DEATH_CONFIRMED:
        if (
            player.alive
            or not isinstance(cause, str)
            or player.death_cause != cause
            or cause not in trigger.allowed_death_causes
        ):
            return None
    elif trigger.event is TriggerEvent.EXILE_SELECTED:
        # This event is before death; a surviving choice trigger may still
        # keep the actor alive, while a non-surviving one is explicitly bound
        # to the pending decision and cannot be fabricated by a dead seat.
        if cause == "exiled" and player.alive:
            return None
    if window.allowed_seats != (seat,):
        return None
    if window.allowed_role_ids:
        return None
    expected_codes = (action_code, 299) if trigger.allow_pass else (action_code,)
    if window.allowed_action_codes != expected_codes:
        return None
    if window.min_actions != 1 or window.max_actions != 1:
        return None
    if window.allow_pass != trigger.allow_pass:
        return None
    if window.visible_context.get("trigger_event") != event_value:
        return None
    if window.visible_context.get("ability_id") != ability_id:
        return None
    if window.visible_context.get("action_code") != action_code:
        return None
    if window.visible_context.get("resolution_id") != resolution_id:
        return None
    if window.visible_context.get("death_cause") != cause:
        return None
    if window.visible_context.get("snapshot_revision") != pending.get("snapshot_revision"):
        return None
    return player, ability


def _is_trigger_action_window(
    state: GameState,
    window: ActionWindow,
    *,
    require_bound: bool = False,
) -> bool:
    """Return whether a window is bound to a current granted trigger."""

    return _pending_trigger_ability(state, window, require_bound=require_bound) is not None


def _is_trigger_window_state(
    state: GameState,
    window: ActionWindow,
    *,
    require_bound: bool = False,
) -> bool:
    """Validate trigger binding and its frozen candidate set."""

    bound = _pending_trigger_ability(state, window, require_bound=require_bound)
    if bound is None:
        return False
    _player, ability = bound
    candidates = window.visible_context.get("candidate_seats")
    if not isinstance(candidates, (list, tuple)):
        return False
    expected_candidates: tuple[int, ...]
    if ability.target_rule.kind is TargetKind.NONE:
        expected_candidates = ()
    else:
        expected_candidates = tuple(
            sorted(
                seat
                for seat, player in state.players.items()
                if (player.alive or ability.target_rule.allow_dead)
                and (ability.target_rule.allow_self or seat != window.allowed_seats[0])
            )
        )
    normalized_candidates = tuple(
        item for item in candidates if isinstance(item, int) and not isinstance(item, bool)
    )
    return normalized_candidates == expected_candidates


def _looks_like_trigger_window(window: ActionWindow) -> bool:
    """Return whether a trigger-phase window needs a granted trigger bind.

    Sheriff badge transfer is also a daytime ``TRIGGER_ACTION`` boundary, but
    it is an office capability with its own durable marker and action
    contract.  Treating its phase alone as a trigger bind would reject a
    legitimate badge request before the badge-specific authorization runs.
    """

    return window.phase is GamePhase.TRIGGER_ACTION and not _is_sheriff_badge_window(window)


def _active_night_abilities(
    player: PlayerState,
    window: ActionWindow,
    registry: ActionRegistry,
) -> tuple[GrantedAbility, ...]:
    """Return the seat's currently usable active grants for one night window.

    ``ActionValidationContext`` is runtime input and may be stale or forged.
    The grant copied into ``PlayerState`` at setup is the authorization source;
    this helper only selects grants that are usable in the current phase and
    whose resource/usage contract is still available.
    """

    if window.phase is not GamePhase.NIGHT_ACTION:
        return ()
    usable: list[GrantedAbility] = []
    for ability in player.granted_abilities:
        if ability.timing is not window.phase or window.phase not in ability.allowed_phases:
            continue
        if ability.action_code not in window.allowed_action_codes:
            continue
        usage_limit = ability.usage_limit
        if usage_limit is not None and (
            usage_limit.max_uses is not None and ability.uses_consumed >= usage_limit.max_uses
        ):
            continue
        try:
            definition = registry.get(ability.action_code)
        except KeyError:
            continue
        if definition.resource_id is None:
            if ability.resource is not None:
                continue
        elif ability.resource is None or ability.resource.resource_id != definition.resource_id:
            continue
        if ability.resource is not None:
            cost = ability.resource.cost_per_use
            if player.skill_resources.get(ability.resource.resource_id, 0) < cost:
                continue
        elif definition.resource_id is not None:
            # The branch above already rejects this shape; keep the condition
            # explicit so a future registry resource cannot become implicit.
            continue
        usable.append(ability)
    return tuple(usable)


def _grant_target_seats(
    state: GameState,
    player: PlayerState,
    ability: GrantedAbility,
    registry: ActionRegistry,
) -> tuple[int, ...]:
    """Build the narrow target universe declared by an active grant."""

    rule = ability.target_rule
    if rule.kind is TargetKind.NONE:
        return ()
    try:
        definition = registry.get(ability.action_code)
    except KeyError as exc:
        raise ResolutionError(
            "UNKNOWN_ACTION", "active ability action is absent from the registry"
        ) from exc

    candidate_seats = (player.seat,) if rule.kind is TargetKind.SELF else tuple(state.players)
    seats = tuple(
        sorted(
            seat
            for seat in candidate_seats
            for target in (state.players[seat],)
            if (target.alive or rule.allow_dead)
            and (rule.allow_self or seat != player.seat)
            and not (
                definition.target_policy == "alive_non_authorized_wolf"
                and target.faction_id == player.faction_id
            )
        )
    )
    return seats


def _current_kill_target(
    state: GameState,
    window_id: str,
    registry: ActionRegistry,
) -> int | None:
    """Return the frozen pending WOLF_KILL target for one action window.

    The target is an inter-action dependency.  It must be rebuilt from the
    committed request log at both request and resolution time; accepting the
    value supplied by a moderator/runtime context would allow a witch heal to
    bypass the board's current-kill contract.
    """

    try:
        wolf_code = next(
            definition.action_code
            for definition in registry.actions
            if definition.action_name == "WOLF_KILL"
        )
    except StopIteration:
        return None
    target: int | None = None
    for payload in state.action_requests.values():
        if not isinstance(payload, Mapping) or payload.get("window_id") != window_id:
            continue
        if payload.get("status") not in {"PENDING", "CONFIRMED"}:
            continue
        actions = payload.get("actions")
        if not isinstance(actions, (list, tuple)):
            continue
        for raw_action in actions:
            if not isinstance(raw_action, Mapping) or raw_action.get("action_code") != wolf_code:
                continue
            targets = raw_action.get("targets")
            if not isinstance(targets, (list, tuple)) or len(targets) != 1:
                continue
            value = targets[0]
            if isinstance(value, int) and not isinstance(value, bool):
                if target is not None and target != value:
                    raise ResolutionError(
                        "KILL_TARGET_AMBIGUOUS",
                        "multiple committed wolf kill targets cannot satisfy a dependent action",
                    )
                target = value
    return target


def _active_grant_for_action(
    state: GameState,
    player: PlayerState,
    window: ActionWindow,
    action_code: int,
    *,
    registry: ActionRegistry,
    usage_counts: Mapping[str, int] | None = None,
) -> GrantedAbility | None:
    """Return the authoritative ACTIVE grant for one submitted action.

    A request may be collected before its moderator resolution.  The grant
    therefore has to be selected again at resolution time, using the current
    immutable player record and a local usage projection for earlier entries
    in the same bundle.  ``usage_counts`` is deliberately local to one
    reducer call; the committed value is written back only after every entry
    has passed preflight.
    """

    registry.get(action_code)
    candidates = tuple(
        ability
        for ability in player.granted_abilities
        if ability.action_code == action_code
        and window.phase in ability.allowed_phases
        and ability.timing in ability.allowed_phases
        and ability.timing is window.phase
        and (
            ability.usage_limit is None
            or ability.usage_limit.max_uses is None
            or ability.uses_consumed + (usage_counts or {}).get(ability.ability_id, 0)
            < ability.usage_limit.max_uses
        )
    )
    if not candidates:
        matching = tuple(
            ability
            for ability in player.granted_abilities
            if ability.action_code == action_code
            and window.phase in ability.allowed_phases
            and ability.timing is window.phase
        )
        if matching:
            raise ResolutionError(
                "ABILITY_USAGE_EXCEEDED",
                f"active ability {action_code} has no remaining uses",
            )
        return None
    return candidates[0]


# Compatibility aliases for old coordinator call sites.  They intentionally
# delegate to the generic ability contract and contain no role-name fallback.
_is_hunter_trigger_window = _is_trigger_action_window
_is_hunter_shoot_window_state = _is_trigger_window_state
_looks_like_hunter_trigger_window = _looks_like_trigger_window


def _reduce_action_resolution(
    state: GameState,
    resolution: ActionResolution,
    *,
    registry: ActionRegistry,
    expected_revision: int,
    now: datetime | None = None,
    staged_actor_seats: frozenset[int] | None = None,
    validation_state: GameState | None = None,
) -> GameState:
    """Apply one complete moderator resolution as an all-or-nothing reducer."""

    if resolution.game_id != state.game_id:
        raise ResolutionError("GAME_MISMATCH", "resolution does not belong to the current game")

    existing_payload = next(
        (
            item
            for item in state.resolutions
            if isinstance(item, dict) and item.get("resolution_id") == resolution.resolution_id
        ),
        None,
    )
    if existing_payload is not None:
        try:
            existing = ActionResolution.model_validate(existing_payload)
        except ValueError as exc:
            raise ResolutionError("RESOLUTION_INVALID", "stored resolution is malformed") from exc
        if existing == resolution:
            return state
        raise ResolutionError(
            "IDEMPOTENCY_CONFLICT", "resolution_id was already committed differently"
        )

    _revision_check(state, expected_revision)
    if resolution.base_revision != state.state_revision:
        raise ResolutionError(
            "REVISION_MISMATCH",
            "resolution base_revision does not match the current state revision",
        )

    previous_for_request = [
        item
        for item in state.resolutions
        if isinstance(item, dict) and item.get("request_id") == resolution.request_id
    ]
    if previous_for_request:
        raise ResolutionError(
            "REQUEST_ALREADY_RESOLVED", "an action request can only be resolved once"
        )

    request = _stored_action_request(state, resolution.request_id)
    if request.game_id != resolution.game_id or request.window_id != resolution.window_id:
        raise ResolutionError("REQUEST_MISMATCH", "resolution does not match the committed request")
    if request.session_epoch != resolution.session_epoch:
        raise ResolutionError("SESSION_MISMATCH", "resolution uses an obsolete session epoch")
    player = state.players.get(request.seat)
    if player is None:
        raise ResolutionError("SEAT_NOT_ASSIGNED", "request actor is not assigned")
    if player.session_epoch != resolution.session_epoch:
        raise ResolutionError("SESSION_MISMATCH", "actor session is obsolete")

    raw_window = state.action_windows.get(resolution.window_id)
    if raw_window is None:
        raise ResolutionError("WINDOW_NOT_FOUND", "resolution window is absent from current state")
    try:
        window = _load_action_window(raw_window)
    except ValueError as exc:
        raise ResolutionError("WINDOW_INVALID", "resolution window is malformed") from exc
    if window.game_id != state.game_id:
        raise ResolutionError("GAME_MISMATCH", "resolution window belongs to another game")
    trigger_action = _is_trigger_window_state(state, window, require_bound=True)
    if _looks_like_trigger_window(window) and not trigger_action:
        raise ResolutionError(
            "TRIGGER_ACTION_INVALID",
            "resolution window is not bound to the pending granted ability",
        )
    if trigger_action and resolution.status is not ResolutionStatus.CONFIRMED:
        raise ResolutionError(
            "TRIGGER_STATUS_INVALID",
            "a trigger action must be confirmed, including an explicit PASS",
        )
    if (
        not player.alive
        and not trigger_action
        and (staged_actor_seats is None or request.seat not in staged_actor_seats)
    ):
        raise ResolutionError("PLAYER_DEAD", "dead players cannot resolve ordinary actions")
    # Requests are collected in NIGHT_ACTION, while their effects are
    # intentionally confirmed at the separate NIGHT_RESOLVE boundary.
    if window.phase != state.phase and not (
        window.phase is GamePhase.NIGHT_ACTION and state.phase is GamePhase.NIGHT_RESOLVE
    ):
        raise ResolutionError(
            "PHASE_MISMATCH", "resolution window is not active in the current phase"
        )
    if window.session_epoch != resolution.session_epoch:
        raise ResolutionError("SESSION_MISMATCH", "resolution window session is obsolete")
    if window.closed_at is not None:
        raise ResolutionError("WINDOW_CLOSED", "action window is already closed")
    if resolution.status is ResolutionStatus.CANCELLED and any(
        entry.disposition is not ActionDisposition.CANCELLED for entry in resolution.actions
    ):
        raise ResolutionError(
            "STATUS_MISMATCH", "CANCELLED bundles require every action to be cancelled"
        )
    if resolution.status is ResolutionStatus.OVERRIDDEN and not any(
        entry.disposition is ActionDisposition.OVERRIDDEN for entry in resolution.actions
    ):
        raise ResolutionError("STATUS_MISMATCH", "OVERRIDDEN bundles require an overridden action")
    if resolution.status is ResolutionStatus.CONFIRMED and any(
        entry.disposition is ActionDisposition.CANCELLED for entry in resolution.actions
    ):
        raise ResolutionError(
            "STATUS_MISMATCH", "CONFIRMED bundles cannot contain cancelled actions"
        )
    expected_indexes = tuple(range(len(request.actions)))
    if tuple(entry.action_index for entry in resolution.actions) != expected_indexes:
        raise ResolutionError(
            "ACTION_BUNDLE_MISMATCH",
            "resolution must rule on every request action exactly once",
        )

    target_state = state if validation_state is None else validation_state
    effective_actions: list[tuple[ActionResolutionEntry, object, object]] = []
    seen_effect_ids: set[str] = set()
    ability_usage_increments: dict[str, int] = {}
    for entry in resolution.actions:
        effective, definition = _resolution_entry_action(entry, request=request, registry=registry)
        if entry.disposition is not ActionDisposition.CANCELLED and effective.action_code != 299:
            grant = _active_grant_for_action(
                state,
                player,
                window,
                effective.action_code,
                registry=registry,
                usage_counts=ability_usage_increments,
            )
            if grant is None:
                # Trigger grants are validated by the trigger binding below;
                # ordinary night actions must always be backed by the active
                # setup grant.  This also prevents a moderator from charging
                # an arbitrary resource cost for an ungranted action.
                if window.phase is GamePhase.NIGHT_ACTION:
                    raise ResolutionError(
                        "ABILITY_NOT_GRANTED",
                        "ordinary action has no usable active ability grant",
                    )
            else:
                allowed_targets = set(_grant_target_seats(target_state, player, grant, registry))
                if any(target not in allowed_targets for target in effective.targets):
                    raise ResolutionError(
                        "TARGET_NOT_ALLOWED",
                        "resolved action target is outside the authoritative grant target set",
                    )
                if definition.resource_id is not None:
                    if (
                        grant.resource is None
                        or grant.resource.resource_id != definition.resource_id
                    ):
                        raise ResolutionError(
                            "RESOURCE_CONTRACT_MISMATCH",
                            "active grant resource does not match the action registry",
                        )
                    if entry.resource_cost != grant.resource.cost_per_use:
                        raise ResolutionError(
                            "RESOURCE_COST_MISMATCH",
                            "resource_cost must equal the frozen grant cost_per_use",
                        )
                ability_usage_increments[grant.ability_id] = (
                    ability_usage_increments.get(grant.ability_id, 0) + 1
                )
            if definition.target_policy == "current_kill_not_self":
                current_kill = _current_kill_target(target_state, window.window_id, registry)
                if current_kill is None or effective.targets != (current_kill,):
                    raise ResolutionError(
                        "TARGET_NOT_ALLOWED",
                        "dependent action must target the currently committed wolf kill",
                    )
        for effect in entry.effects:
            _validate_resolution_effect(
                effect,
                entry=entry,
                effective_action=effective,
                definition=definition,
                actor_seat=request.seat,
                state=state,
                seen_effect_ids=seen_effect_ids,
            )
        effective_actions.append((entry, effective, definition))

    # Preflight every resource transition before constructing any player
    # replacement.  This is the atomicity boundary for skill consumption.
    balances: dict[int, dict[str, int]] = {
        seat: dict(player.skill_resources) for seat, player in state.players.items()
    }
    for entry, _effective, definition in effective_actions:
        resource_id = getattr(definition, "resource_id")
        if entry.resource_cost:
            current = balances[request.seat].get(resource_id, 0)
            if current < entry.resource_cost:
                raise ResolutionError(
                    "RESOURCE_UNAVAILABLE",
                    f"resource {resource_id} would become negative",
                )
            balances[request.seat][resource_id] = current - entry.resource_cost
        for effect in entry.effects:
            if effect.effect_type == "ADJUST_RESOURCE":
                resource_id = effect.resource_id
                if resource_id is None:  # pragma: no cover - shape checked above
                    raise ResolutionError("RESOURCE_ID", "resource effect has no resource_id")
                delta = effect.value
                if isinstance(delta, bool) or not isinstance(delta, int):  # pragma: no cover
                    raise ResolutionError("EFFECT_VALUE", "resource delta must be an integer")
                current = balances[request.seat].get(resource_id, 0)
                if current + delta < 0:
                    raise ResolutionError(
                        "RESOURCE_UNAVAILABLE",
                        f"resource {resource_id} would become negative",
                    )
                balances[request.seat][resource_id] = current + delta

    timestamp = now or utc_now()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ResolutionError("TIMESTAMP", "commit timestamp must include a timezone")
    timestamp = timestamp.astimezone(UTC)
    data = _state_data(state)
    players = dict(data["players"])
    for seat, skill_resources in balances.items():
        player_data = dict(players[seat])
        player_data["skill_resources"] = skill_resources
        players[seat] = player_data
    if ability_usage_increments:
        actor_player_data = dict(players[request.seat])
        granted_abilities = []
        for raw_ability in actor_player_data.get("granted_abilities", ()):
            ability_data = dict(raw_ability)
            ability_id = ability_data.get("ability_id")
            increment = (
                ability_usage_increments.get(ability_id, 0) if isinstance(ability_id, str) else 0
            )
            if increment:
                ability_data["uses_consumed"] = (
                    int(ability_data.get("uses_consumed", 0)) + increment
                )
            granted_abilities.append(ability_data)
        actor_player_data["granted_abilities"] = tuple(granted_abilities)
        players[request.seat] = actor_player_data
    for entry, _effective, _definition in effective_actions:
        for effect in entry.effects:
            player_data = dict(players[effect.target_seat])
            if effect.effect_type == "SET_ALIVE":
                player_data["alive"] = cast(bool, effect.value)
            elif effect.effect_type == "SET_DEATH_CAUSE":
                player_data["death_cause"] = effect.value
            elif effect.effect_type == "SET_CAN_VOTE":
                player_data["can_vote"] = cast(bool, effect.value)
            elif effect.effect_type == "SET_VOTE_WEIGHT":
                player_data["vote_weight"] = float(cast(float, effect.value))
            # ADJUST_RESOURCE was applied during the balance preflight above.
            # The player copy already contains the resulting balance, so do
            # not replay the effect here.
            players[effect.target_seat] = player_data
    trigger_binding = _pending_trigger_ability(state, window, require_bound=True)
    if trigger_action and trigger_binding is None:  # pragma: no cover - guarded above
        raise ResolutionError("TRIGGER_ACTION_INVALID", "trigger binding disappeared during commit")
    if trigger_action and trigger_binding is not None:
        _trigger_player, trigger_ability = trigger_binding
        actor_player_data = dict(players[request.seat])
        granted = []
        for raw_ability in actor_player_data.get("granted_trigger_abilities", ()):
            item = dict(raw_ability)
            if item.get("ability_id") == trigger_ability.ability_id:
                item["consumed"] = True
            granted.append(item)
        actor_player_data["granted_trigger_abilities"] = tuple(granted)
        players[request.seat] = actor_player_data
    actor_data = dict(players[request.seat])
    if actor_data.get("current_request_id") == resolution.request_id:
        # A resolved physical request must not block the same seat's next
        # round. Failed or timed-out requests keep this binding for retry.
        actor_data["current_request_id"] = None
        players[request.seat] = actor_data
    data["players"] = players

    requests = dict(data["action_requests"])
    request_payload = dict(requests[resolution.request_id])
    request_payload["status"] = resolution.status.value
    request_payload["resolution_id"] = resolution.resolution_id
    requests[resolution.request_id] = request_payload
    data["action_requests"] = requests

    window_payload = window.model_dump(mode="json")
    window_requests = [
        (request_id, payload)
        for request_id, payload in state.action_requests.items()
        if isinstance(payload, dict) and payload.get("window_id") == window.window_id
    ]
    submitted_seats = {
        payload.get("seat")
        for _request_id, payload in window_requests
        if isinstance(payload.get("seat"), int)
    }
    all_requests_resolved = all(
        request_id == resolution.request_id or payload.get("status") != "PENDING"
        for request_id, payload in window_requests
    )
    all_allowed_seats_submitted = set(window.allowed_seats).issubset(submitted_seats)
    if all_requests_resolved and (len(window.allowed_seats) == 1 or all_allowed_seats_submitted):
        window_payload["closed_at"] = timestamp.isoformat()
    windows = dict(data["action_windows"])
    windows[resolution.window_id] = window_payload
    data["action_windows"] = windows

    resolutions = list(data["resolutions"])
    resolutions.append(resolution.model_dump(mode="json"))
    data["resolutions"] = tuple(resolutions)
    pending = data.get("pending_resolution")
    if trigger_action:
        # A trigger action is a one-shot boundary.  Clearing the marker only
        # after the moderator resolution has been reduced prevents a second
        # window from being opened, while still leaving the entire effect
        # commit atomic with the close of the request window.
        data["pending_resolution"] = None
    elif isinstance(pending, dict) and pending.get("request_id") == resolution.request_id:
        data["pending_resolution"] = None
    audits = list(data["moderator_audit"])
    audits.append(
        {
            "operation": "ACTION_RESOLUTION",
            "resolution_id": resolution.resolution_id,
            "bundle_id": resolution.bundle_id,
            "game_id": resolution.game_id,
            "window_id": resolution.window_id,
            "request_id": resolution.request_id,
            "moderator_id": resolution.moderator_id,
            "status": resolution.status.value,
            "reason": resolution.reason,
            "base_revision": state.state_revision,
            "committed_revision": state.state_revision + 1,
            "created_at": timestamp.isoformat(),
        }
    )
    data["moderator_audit"] = tuple(audits)
    data["state_revision"] = state.state_revision + 1
    data["updated_at"] = timestamp
    return GameState.model_validate(data)


def _typed_events(state: GameState) -> tuple[GameEvent, ...]:
    """Restore typed events from in-memory or JSON snapshot representations."""

    restored: list[GameEvent] = []
    for event in state.events:
        if isinstance(event, GameEvent):
            restored.append(event)
            continue
        if isinstance(event, dict):
            try:
                restored.append(GameEvent.model_validate_json(json.dumps(event)))
            except (TypeError, ValueError) as exc:
                raise EventCommitError(
                    "event delivery requires a typed event log; migrate the legacy "
                    "JSON snapshot first"
                ) from exc
            continue
        raise EventCommitError(
            "event delivery requires a typed event log; migrate the legacy JSON snapshot first"
        )
    event_ids = tuple(event.event_id for event in restored)
    if tuple(sorted(set(event_ids))) != event_ids:
        raise EventCommitError("event delivery requires sorted, unique event IDs")
    if any(event.game_id != state.game_id for event in restored):
        raise EventCommitError("event delivery contains an event from another game")
    return tuple(restored)


def _validate_new_events(
    state: GameState,
    events: tuple[GameEvent, ...],
    *,
    next_revision: int,
) -> tuple[GameEvent, ...]:
    """Validate an append-only event batch before it enters a candidate state."""

    if not events:
        return ()
    if any(not isinstance(event, GameEvent) for event in events):
        raise EventCommitError("events must contain GameEvent instances")
    current_events = _typed_events(state)
    current_ids = {event.event_id for event in current_events}
    ids = tuple(event.event_id for event in events)
    if tuple(sorted(set(ids))) != ids:
        raise EventCommitError("event IDs must be sorted and unique")
    if any(event_id in current_ids for event_id in ids):
        raise EventCommitError("event_id was already committed")
    if current_events and ids[0] <= current_events[-1].event_id:
        raise EventCommitError("event IDs must increase monotonically")
    for event in events:
        if event.game_id != state.game_id:
            raise EventCommitError("event belongs to another game")
        if event.state_revision != next_revision:
            raise EventCommitError(
                "event state_revision must match the revision created by this commit"
            )
    return events


def _player_cursor(state: GameState, ack: DeliveryAck) -> DeliveryCursor:
    player = state.players.get(ack.seat)
    if player is None:
        raise EventCommitError("SEAT_NOT_ASSIGNED: delivery seat is not in the current game")
    if player.session_epoch != ack.session_epoch:
        raise EventCommitError("SESSION_MISMATCH: delivery uses an obsolete session epoch")
    cursor = state.delivery_cursors.get(ack.seat)
    if cursor is None:
        return DeliveryCursor(session_epoch=ack.session_epoch)
    if cursor.session_epoch != ack.session_epoch:
        raise DeliverySessionError(
            f"session_epoch mismatch for seat {ack.seat}: "
            f"cursor={cursor.session_epoch}, requested={ack.session_epoch}"
        )
    return cursor


def _acknowledge_delivery(
    state: GameState,
    events: tuple[GameEvent, ...],
    ack: DeliveryAck,
) -> DeliveryCursor | None:
    """Return the new cursor, or ``None`` for an idempotent retry."""

    cursor = _player_cursor(state, ack)
    event_by_id = {event.event_id: event for event in events}
    if cursor.in_flight_request_id is not None:
        if cursor.in_flight_request_id != ack.request_id:
            raise EventCommitError("REQUEST_EXPIRED: delivery request is no longer active")
        selected = cursor.in_flight_event_ids if ack.event_ids is None else ack.event_ids
        for event_id in selected:
            event = event_by_id.get(event_id)
            if event is None or ack.seat not in event.audience:
                raise EventCommitError("event is not authorized for the delivery seat")
        try:
            candidate = cursor.acknowledge(
                request_id=ack.request_id,
                event_ids=selected,
                session_epoch=ack.session_epoch,
            )
        except ValueError as exc:
            raise EventCommitError(str(exc)) from exc
        return candidate if candidate != cursor else None

    # A successful ack can be retried after the state has already advanced.
    # Treat the same committed event IDs as an idempotent replay, but still
    # verify their audience so a caller cannot use old IDs to probe secrets.
    selected_ids = ack.event_ids
    if (
        selected_ids is not None
        and selected_ids
        and all(event_id <= cursor.committed_event_id for event_id in selected_ids)
    ):
        for event_id in selected_ids:
            event = event_by_id.get(event_id)
            if event is None or ack.seat not in event.audience:
                raise EventCommitError("event is not authorized for the delivery seat")
        return None

    router = MessageRouter(events, {ack.seat: cursor})
    try:
        candidate = router.prepare_ack(
            ack.seat,
            ack.session_epoch,
            request_id=ack.request_id,
            event_ids=selected_ids,
        ).acknowledge(
            request_id=ack.request_id,
            session_epoch=ack.session_epoch,
        )
    except (DeliveryCursorError, DeliverySessionError, ValueError) as exc:
        raise EventCommitError(str(exc)) from exc
    return candidate if candidate != cursor else None


def _reduce_event_delivery(
    state: GameState,
    patch: StatePatch,
) -> GameState:
    """Append events and/or acknowledge one delivery in one state replacement."""

    _revision_check(state, patch.expected_revision)
    next_revision = state.state_revision + 1
    events_to_append = _validate_new_events(
        state,
        patch.events_to_append,
        next_revision=next_revision,
    )
    existing_events = _typed_events(state) if (events_to_append or patch.delivery_ack) else ()
    candidate_events = (*existing_events, *events_to_append)
    cursor_candidate: DeliveryCursor | None = None
    if patch.delivery_ack is not None:
        cursor_candidate = _acknowledge_delivery(state, candidate_events, patch.delivery_ack)

    if not events_to_append and cursor_candidate is None:
        return state

    data = _state_data(state)
    if events_to_append:
        data["events"] = candidate_events
    if patch.delivery_ack is not None and cursor_candidate is not None:
        cursors = dict(data["delivery_cursors"])
        cursors[patch.delivery_ack.seat] = cursor_candidate
        data["delivery_cursors"] = cursors
    data["state_revision"] = next_revision
    data["updated_at"] = patch.now or utc_now()
    return GameState.model_validate(data)


def reduce_state(state: GameState, patch: StatePatch) -> GameState:
    """Purely reduce one patch into a new immutable ``GameState``.

    The old state is never mutated.  Every non-replay success increments the
    revision exactly once; all Pydantic validation happens before the caller
    replaces its state reference.
    """

    if not isinstance(state, GameState):
        raise TypeError("state must be a GameState")
    if not isinstance(patch, StatePatch):
        raise TypeError("patch must be a StatePatch")
    _revision_check(state, patch.expected_revision)

    if patch.events_to_append or patch.delivery_ack is not None:
        return _reduce_event_delivery(state, patch)

    if patch.target_phase is not None:
        return transition_phase(
            state,
            patch.target_phase,
            expected_revision=patch.expected_revision,
            now=patch.now,
        )
    return _reduce_action_request(state, patch)


class GameManager:
    """The single serialized commit owner for one authoritative game state."""

    def __init__(
        self,
        state: GameState,
        *,
        registry: ActionRegistry,
        execution_package: ExecutionPackage | None = None,
        legacy_compatibility: bool = False,
    ) -> None:
        if not isinstance(state, GameState):
            raise TypeError("state must be a GameState")
        if not isinstance(registry, ActionRegistry):
            raise TypeError("registry must be an ActionRegistry")
        if type(legacy_compatibility) is not bool:
            raise TypeError("legacy_compatibility must be a bool")
        if legacy_compatibility and execution_package is None:
            raise ValueError("legacy compatibility scheduling requires a frozen execution package")
        if state.execution_identity is not None and execution_package is None:
            raise ValueError("game state has an execution identity but no frozen execution package")
        if execution_package is not None:
            if not isinstance(execution_package, ExecutionPackage):
                raise TypeError("execution_package must be an ExecutionPackage")
            ruleset = state.ruleset
            if ruleset is None:
                raise ValueError("an executable package requires a frozen ruleset reference")
            if (
                getattr(execution_package, "board_id", None) != ruleset.board_id
                or getattr(execution_package, "board_version", None) != ruleset.version
            ):
                raise ValueError("execution package does not match the game's frozen ruleset")
            pinned_identity = RuleExecutionIdentity(
                package_id=execution_package.package_id,
                board_id=execution_package.board_id,
                board_version=execution_package.board_version,
                execution_digest=execution_package.package_id,
                action_registry_digest=_action_registry_digest(registry),
            )
            if state.execution_identity is not None:
                if state.execution_identity != pinned_identity:
                    raise ValueError(
                        "execution package does not match the game's pinned execution identity"
                    )
            elif state.ability_instances or state.rule_state or state.rule_ledger:
                raise ValueError(
                    "legacy game has partial rule-execution state without a pinned identity"
                )
            elif state.players:
                # Schema-1 snapshots predate the execution identity. Convert
                # them once from the exact frozen package supplied by the
                # snapshot loader, then retain that identity for every later
                # restore so same-board package drift is rejected.
                instances = build_ability_instances(state.players, execution_package)
                migrated_data = _state_data(state)
                migrated_data["execution_identity"] = pinned_identity
                migrated_data["ability_instances"] = instances
                migrated_data["rule_state"] = tuple(
                    _rule_state_payload(item)
                    for item in build_initial_rule_state(
                        execution_package, instances, seats=tuple(state.players)
                    )
                )
                state = GameState.model_validate(migrated_data)
        self._state = state
        self._registry = registry
        self._execution_package = execution_package
        self._legacy_compatibility = legacy_compatibility
        self._rules = (
            RuleExecutionAdapter(
                execution_package,
                action_registry_digest=_action_registry_digest(registry),
                legacy_compatibility=legacy_compatibility,
            )
            if execution_package is not None
            else None
        )
        self._lock = asyncio.Lock()

    @property
    def registry(self) -> ActionRegistry:
        """Return this game's immutable action registry from its frozen package."""

        return self._registry

    @property
    def execution_package(self) -> ExecutionPackage | None:
        """Return the exact immutable execution package pinned at construction."""

        return self._execution_package

    @property
    def legacy_compatibility(self) -> bool:
        """Whether this snapshot uses its audited pre-schema-two scheduler."""

        return self._legacy_compatibility

    @property
    def state(self) -> GameState:
        """Return the current immutable state reference."""

        return self._state

    def _rule_group_requests(
        self,
        state: GameState,
        request_ids: Iterable[str],
        *,
        timing: str,
        group_id_override: str | None = None,
    ) -> tuple[tuple[SkillRequest, ...], tuple[_RuleRequestBinding, ...], str]:
        """Bind stored, window-authorized intents to frozen skill instances."""

        if self._rules is None or self._execution_package is None:
            raise ResolutionError("RULE_PACKAGE_MISSING", "game has no pinned execution package")
        bindings: list[_RuleRequestBinding] = []
        sorted_request_ids = tuple(sorted(request_ids))
        group_identity = json.dumps(
            {
                "game_id": state.game_id,
                "timing": timing,
                "round_no": state.round_no,
                "request_ids": sorted_request_ids,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        group_digest = hashlib.sha256(group_identity.encode("utf-8")).hexdigest()
        group_id = group_id_override or f"{timing.lower()}:{state.round_no}:{group_digest}"
        for request_id in sorted_request_ids:
            request = _stored_action_request(state, request_id)
            raw = state.action_requests.get(request_id)
            if not isinstance(raw, Mapping) or raw.get("status") != "PENDING":
                raise ResolutionError("REQUEST_NOT_PENDING", "rule request is not pending")
            window = self._window_for_request(state, request)
            player = state.players.get(request.seat)
            if player is None:
                raise ResolutionError("SEAT_NOT_ASSIGNED", "rule request actor is not assigned")
            if (
                request.game_id != state.game_id
                or window.game_id != state.game_id
                or request.window_id != window.window_id
                or request.session_epoch != player.session_epoch
                or request.session_epoch != window.session_epoch
            ):
                raise ResolutionError("SESSION_MISMATCH", "rule request session/window is obsolete")
            if request.phase is not None and request.phase is not window.phase:
                raise ResolutionError(
                    "PHASE_MISMATCH", "rule request phase does not match its window"
                )
            if request.seat not in window.allowed_seats:
                raise ResolutionError(
                    "SEAT_NOT_ALLOWED", "request actor is outside its frozen window"
                )
            if window.allowed_role_ids and player.role_id not in window.allowed_role_ids:
                raise ResolutionError(
                    "ROLE_NOT_ALLOWED", "request actor role is outside its frozen window"
                )
            rule_occurrence = self._rule_occurrence_for_window(state, window)
            trigger_action = (
                _is_trigger_window_state(state, window, require_bound=True)
                or rule_occurrence is not None
            )
            if window.phase is GamePhase.TRIGGER_ACTION and not trigger_action:
                raise ResolutionError("TRIGGER_ACTION_INVALID", "trigger window is no longer bound")
            if not player.alive and not trigger_action:
                raise ResolutionError(
                    "PLAYER_DEAD", "dead players cannot submit ordinary rule actions"
                )
            if window.phase.value != timing:
                raise ResolutionError(
                    "TIMING_MISMATCH", "rule timing does not match the active window"
                )

            for index, action in enumerate(request.actions):
                if action.action_code not in window.allowed_action_codes:
                    raise ResolutionError(
                        "ACTION_NOT_ALLOWED", "action code is outside its frozen window"
                    )
                if action.action_code == 299 and not window.allow_pass:
                    raise ResolutionError(
                        "PASS_NOT_ALLOWED", "the frozen action window does not allow pass"
                    )
                if len(action.targets) > 16 or len(set(action.targets)) != len(action.targets):
                    raise ResolutionError("TARGET_INVALID", "rule request targets are malformed")
                pass_binding_values: tuple[tuple[AbilityInstanceState, SkillSpec], ...]
                if action.action_code == 299:
                    if action.targets or action.parameters:
                        raise ResolutionError(
                            "PASS_INVALID",
                            "PASS requests cannot carry targets or skill parameters",
                        )
                    pass_skills = self._rule_skill_instances(
                        state,
                        request.seat,
                        timing,
                        allowed_codes=set(window.allowed_action_codes),
                        trigger_only=trigger_action,
                        logical_window_id=window.logical_window_id,
                    )
                    completed = self._rule_completed_skill_ids(state, window)
                    pass_skills = tuple(
                        (instance, skill)
                        for instance, skill in pass_skills
                        if set(skill.after_skills).issubset(completed)
                    )
                    action_specs = {
                        item.action_code: item for item in self._execution_package.actions
                    }
                    pass_action = action_specs.get(299)
                    if (
                        not window.allow_pass
                        or pass_action is None
                        or not pass_action.allow_pass
                        or not pass_skills
                        or any(
                            (skill_action := action_specs.get(skill.action_code)) is None
                            or not skill_action.allow_pass
                            for _instance, skill in pass_skills
                        )
                    ):
                        raise ResolutionError(
                            "PASS_NOT_ALLOWED",
                            "PASS must bind all currently usable skills that declare PASS",
                        )
                    pass_binding_values = pass_skills
                else:
                    instances = [
                        item
                        for item in state.ability_instances
                        if item.actor_seat == request.seat
                        and item.action_code == action.action_code
                        and item.enabled
                        and not item.consumed
                    ]
                    if len(instances) != 1:
                        raise ResolutionError(
                            "ABILITY_NOT_GRANTED", "request does not bind one active skill instance"
                        )
                    instance = instances[0]
                    skill = next(
                        (
                            item
                            for item in self._execution_package.skills
                            if item.skill_id == instance.skill_id
                            and item.action_code == action.action_code
                        ),
                        None,
                    )
                    if skill is None:
                        raise ResolutionError(
                            "SKILL_NOT_REQUESTABLE", "request does not resolve to a frozen skill"
                        )
                    pass_binding_values = ((instance, skill),)

                for instance, skill in pass_binding_values:
                    if instance.grant_kind == "TRIGGER" and not trigger_action:
                        raise ResolutionError(
                            "TRIGGER_ACTION_INVALID", "trigger skill outside its bound window"
                        )
                    if (
                        instance.grant_kind == "ACTIVE"
                        and trigger_action
                        and rule_occurrence is None
                    ):
                        raise ResolutionError(
                            "ABILITY_NOT_GRANTED", "active skill cannot replace a trigger grant"
                        )
                    if skill.mode != "PLAYER":
                        raise ResolutionError(
                            "SKILL_NOT_REQUESTABLE", "frozen skill is not player-requestable"
                        )
                    if skill.skill_id != instance.skill_id:
                        raise ResolutionError(
                            "ABILITY_NOT_GRANTED",
                            "skill instance does not match the frozen package",
                        )
                    if rule_occurrence is not None and (
                        rule_occurrence.actor_seat != request.seat
                        or rule_occurrence.ability_instance_id != instance.ability_instance_id
                        or rule_occurrence.skill_id != skill.skill_id
                        or action.action_code not in {skill.action_code, 299}
                    ):
                        raise ResolutionError(
                            "RULE_OCCURRENCE_INVALID",
                            "trigger request does not match its durable occurrence binding",
                        )
                    if action.action_code != 299:
                        completed = self._rule_completed_skill_ids(state, window)
                        if not set(skill.after_skills).issubset(completed):
                            raise ResolutionError(
                                "SKILL_DEPENDENCY_MISSING",
                                "skill predecessors have no valid current-window request",
                            )
                if action.action_code != 299:
                    instance, skill = pass_binding_values[0]
                    legacy_grants = [
                        item
                        for item in player.granted_abilities
                        if item.action_code == action.action_code and item.timing is window.phase
                    ]
                    if legacy_grants:
                        allowed = set(
                            _grant_target_seats(state, player, legacy_grants[0], self._registry)
                        )
                        if any(target not in allowed for target in action.targets):
                            raise ResolutionError(
                                "TARGET_NOT_ALLOWED", "target is outside the setup grant"
                            )
                    candidates = window.visible_context.get("candidate_seats")
                    if isinstance(candidates, (list, tuple)) and any(
                        target not in candidates for target in action.targets
                    ):
                        raise ResolutionError(
                            "TARGET_NOT_ALLOWED", "target is outside the frozen candidate list"
                        )
                    if rule_occurrence is not None:
                        authorized = set(self._trigger_target_seats(state, rule_occurrence, skill))
                        if any(target not in authorized for target in action.targets):
                            raise ResolutionError(
                                "TARGET_NOT_ALLOWED",
                                "trigger target is outside the frozen selector result",
                            )
                for bound_instance, bound_skill in pass_binding_values:
                    request_key = (
                        "rule-"
                        + hashlib.sha256(
                            f"{state.game_id}:{request_id}:{index}:"
                            f"{bound_instance.ability_instance_id}".encode()
                        ).hexdigest()
                    )
                    skill_request = SkillRequest(
                        request_id=request_key,
                        ability_instance_id=bound_instance.ability_instance_id,
                        skill_id=bound_skill.skill_id,
                        action_code=bound_skill.action_code,
                        actor_seat=request.seat,
                        targets=() if action.action_code == 299 else action.targets,
                        parameters={} if action.action_code == 299 else action.parameters,
                        passed=action.action_code == 299,
                        origin="PLAYER",
                        trigger_occurrence_id=(
                            rule_occurrence.occurrence_id if rule_occurrence is not None else None
                        ),
                        source_fact_id=(
                            rule_occurrence.source_fact_id if rule_occurrence is not None else None
                        ),
                        window_id=(
                            window.window_id
                            if rule_occurrence is not None
                            or bound_skill.trigger is not None
                            or bound_skill.window_ids
                            or bound_skill.hook_ids
                            else None
                        ),
                        logical_window_id=(
                            window.logical_window_id
                            if rule_occurrence is not None
                            or bound_skill.trigger is not None
                            or bound_skill.window_ids
                            or bound_skill.hook_ids
                            else None
                        ),
                        hook_id=window.hook_id,
                    )
                    bindings.append(
                        _RuleRequestBinding(
                            skill_request=skill_request,
                            request_id=request_id,
                            action_index=index,
                            action_code=action.action_code,
                        )
                    )
        skills_by_id = {skill.skill_id: skill for skill in self._execution_package.skills}
        bound_skills = {
            item.skill_request.skill_id
            for item in bindings
            if item.skill_request.skill_id is not None
        }
        for binding in bindings:
            skill = skills_by_id.get(cast(str, binding.skill_request.skill_id))
            if skill is None:
                continue
            missing = set(skill.after_skills) - bound_skills
            if missing:
                raise ResolutionError(
                    "SKILL_DEPENDENCY_MISSING",
                    "a declared predecessor skill has no valid request in this action group: "
                    + ", ".join(sorted(missing)),
                )
        return (
            tuple(item.skill_request for item in bindings),
            tuple(bindings),
            group_id,
        )

    def _validate_rule_batch(
        self,
        state: GameState,
        batch: ResolutionBatch,
        requests: tuple[SkillRequest, ...],
        *,
        group_id: str,
        timing: str,
        extra_ability_instances: tuple[AbilityInstance, ...] = (),
    ) -> None:
        """Reject malformed interpreter output before any durable projection."""

        package = self._execution_package
        if package is None:
            raise ResolutionError("RULE_PACKAGE_MISSING", "game has no pinned execution package")
        if self._rules is None:
            raise ResolutionError("RULE_PACKAGE_MISSING", "game has no pinned rule interpreter")
        if (
            batch.package_id != package.package_id
            or batch.board_id != package.board_id
            or batch.board_version != package.board_version
            or batch.read_revision != state.state_revision
            or batch.round_number != state.round_no
            or batch.group_id != group_id
        ):
            raise ResolutionError(
                "RULE_BATCH_STALE", "interpreter batch is stale or belongs to another package"
            )
        try:
            expected_batch = self._rules.plan(
                state,
                requests,
                group_id=group_id,
                timing=timing,
                extra_ability_instances=extra_ability_instances,
            )
        except (RuleAdapterError, TypeError, ValueError) as exc:
            raise ResolutionError("RULE_PLAN_INVALID", str(exc)) from exc
        if batch != expected_batch:
            raise ResolutionError(
                "RULE_BATCH_INVALID",
                "interpreter output differs from a fresh package-derived plan",
            )
        by_id = {request.request_id: request for request in requests}
        dispositions = {item.request_id: item for item in batch.dispositions}
        if len(dispositions) != len(batch.dispositions) or set(dispositions) != set(by_id):
            raise ResolutionError(
                "RULE_BATCH_INVALID", "batch dispositions do not cover its requests"
            )
        for request_id, request in by_id.items():
            disposition = dispositions[request_id]
            if (
                disposition.ability_instance_id != request.ability_instance_id
                or disposition.skill_id != request.skill_id
                or disposition.status not in {"ACCEPTED", "PASSED"}
                or (request.passed and disposition.status != "PASSED")
                or (not request.passed and disposition.status != "ACCEPTED")
            ):
                raise ResolutionError(
                    "RULE_REQUEST_REJECTED", "rules package rejected a committed request"
                )
        intents = {item.effect_id: item for item in batch.intents}
        if len(intents) != len(batch.intents):
            raise ResolutionError(
                "RULE_BATCH_INVALID", "batch contains duplicate effect intent IDs"
            )
        skills = {skill.skill_id: skill for skill in package.skills}
        request_by_id = by_id
        interaction_ids = {rule.interaction_id for rule in package.interactions}
        for intent in batch.intents:
            intent_request = request_by_id.get(intent.source_request_id)
            intent_skill = skills.get(intent.skill_id)
            if (
                intent_request is None
                or intent_skill is None
                or intent_request.skill_id != intent.skill_id
                or intent_request.ability_instance_id != intent.ability_instance_id
                or intent_request.actor_seat != intent.actor_seat
                or intent.source_rule_id not in interaction_ids
                and intent.source_rule_id
                not in {
                    effect.effect_id for effect in intent_skill.effects + intent_skill.pass_effects
                }
            ):
                raise ResolutionError(
                    "RULE_PROVENANCE_INVALID", "effect intent provenance is not declared"
                )
            if intent.target_seat is not None and (
                intent.target_seat not in state.players
                or intent.target_seat not in intent.authorized_targets
            ):
                raise ResolutionError(
                    "RULE_TARGET_INVALID", "effect target is outside its declared authorization"
                )
        for resolved in batch.effects:
            source = intents.get(resolved.effect_id)
            if (
                source is None
                or source.source_request_id != resolved.source_request_id
                or source.source_rule_id != resolved.source_rule_id
                or source.effect_type != resolved.effect_type
                or source.target_seat != resolved.target_seat
            ):
                raise ResolutionError(
                    "RULE_PROVENANCE_INVALID", "resolved effect has no matching source intent"
                )
        mortality = {item.seat: item for item in batch.mortality}
        if len(mortality) != len(batch.mortality) or any(
            seat not in state.players for seat in mortality
        ):
            raise ResolutionError(
                "RULE_BATCH_INVALID", "batch mortality has duplicate or unknown seats"
            )
        declarations = {(item.skill_id, item.key): item for item in package.state_declarations}
        seen_updates: set[tuple[str, str | None, str, str]] = set()
        applied_effect_sources = {
            (item.source_request_id, item.source_rule_id, item.effect_type)
            for item in batch.effects
            if item.applied
        }
        for update in batch.state_updates:
            state_request = request_by_id.get(update.source_request_id)
            instance = next(
                (
                    item
                    for item in state.ability_instances
                    if item.ability_instance_id == update.source_ability_instance_id
                ),
                None,
            )
            declaration = declarations.get((update.skill_id, update.key))
            owner = (
                update.ability_instance_id
                if update.scope == "ABILITY"
                else f"seat-{update.seat}"
                if update.scope == "SEAT"
                else None
            )
            if (
                state_request is None
                or instance is None
                or state_request.ability_instance_id != update.source_ability_instance_id
                or state_request.actor_seat != instance.actor_seat
                or state_request.skill_id != update.skill_id
                or instance.skill_id != update.skill_id
                or declaration is None
                or declaration.scope != update.scope
                or declaration.expiry_policy != update.expiry_policy
                or (update.scope == "ABILITY" and owner != instance.ability_instance_id)
                or (
                    update.scope == "SEAT"
                    and update.seat != instance.actor_seat
                    and update.seat not in update.authorized_targets
                )
                or (update.scope == "GAME" and update.seat is not None)
                or (
                    update.source_request_id,
                    update.source_rule_id,
                    "STATE_SET",
                )
                not in applied_effect_sources
            ):
                raise ResolutionError(
                    "RULE_STATE_INVALID", "state update is outside declared skill state"
                )
            signature = (update.scope, owner, update.skill_id, update.key)
            if signature in seen_updates:
                raise ResolutionError("RULE_STATE_INVALID", "duplicate state update")
            seen_updates.add(signature)

        def validate_typed_source(
            source_request_id: str,
            source_rule_id: str,
            source_skill_id: str,
            source_ability_instance_id: str,
            effect_type: str,
        ) -> tuple[SkillRequest, AbilityInstanceState]:
            source_request = request_by_id.get(source_request_id)
            source_instance = next(
                (
                    item
                    for item in state.ability_instances
                    if item.ability_instance_id == source_ability_instance_id
                ),
                None,
            )
            if (
                source_request is None
                or source_instance is None
                or source_request.ability_instance_id != source_ability_instance_id
                or source_request.skill_id != source_skill_id
                or source_instance.skill_id != source_skill_id
                or (source_request_id, source_rule_id, effect_type) not in applied_effect_sources
            ):
                raise ResolutionError(
                    "RULE_PROVENANCE_INVALID", "typed update source is not an applied frozen effect"
                )
            return source_request, source_instance

        for player_update in batch.player_updates:
            player_request, player_source = validate_typed_source(
                player_update.source_request_id,
                player_update.source_rule_id,
                player_update.source_skill_id,
                player_update.source_ability_instance_id,
                "PLAYER_FIELD_SET",
            )
            if (
                player_update.seat not in state.players
                or player_request.actor_seat != player_source.actor_seat
                or player_update.seat != player_source.actor_seat
                and player_update.seat not in player_update.authorized_targets
            ):
                raise ResolutionError(
                    "RULE_TARGET_INVALID", "player field update target is not authorized"
                )
        for resource_update in batch.resource_updates:
            resource_request, resource_source = validate_typed_source(
                resource_update.source_request_id,
                resource_update.source_rule_id,
                resource_update.source_skill_id,
                resource_update.source_ability_instance_id,
                "RESOURCE_DELTA",
            )
            if (
                resource_update.seat not in state.players
                or resource_request.actor_seat != resource_source.actor_seat
                or resource_update.seat != resource_source.actor_seat
                and resource_update.seat not in resource_update.authorized_targets
                or resource_update.resource_id
                not in {item.resource_id for item in package.resource_declarations}
            ):
                raise ResolutionError(
                    "RULE_TARGET_INVALID", "resource update target or declaration is invalid"
                )
        for relation_update in batch.relation_updates:
            relation_request, relation_source = validate_typed_source(
                relation_update.source_request_id,
                relation_update.source_rule_id,
                relation_update.source_skill_id,
                relation_update.source_ability_instance_id,
                "RELATION_ADD" if relation_update.operation == "ADD" else "RELATION_REMOVE",
            )
            if (
                relation_update.relation_type
                not in {item.relation_type for item in package.relation_declarations}
                or any(
                    seat not in state.players
                    for seat in (relation_update.source_seat, relation_update.target_seat)
                )
                or relation_request.actor_seat != relation_source.actor_seat
                or any(
                    seat != relation_source.actor_seat
                    and seat not in relation_update.authorized_targets
                    for seat in (relation_update.source_seat, relation_update.target_seat)
                )
            ):
                raise ResolutionError(
                    "RULE_TARGET_INVALID", "relation update target or declaration is invalid"
                )
        for ability_update in batch.ability_updates:
            ability_request, ability_source = validate_typed_source(
                ability_update.source_request_id,
                ability_update.source_rule_id,
                ability_update.source_skill_id,
                ability_update.source_ability_instance_id,
                "ABILITY_GRANT" if ability_update.operation == "GRANT" else "ABILITY_REVOKE",
            )
            target_skill = skills.get(ability_update.skill_id)
            if (
                target_skill is None
                or ability_update.grant_id not in {item.grant_id for item in target_skill.grants}
                or ability_update.target_seat not in state.players
                or ability_request.actor_seat != ability_source.actor_seat
                or ability_update.target_seat != ability_source.actor_seat
                and ability_update.target_seat not in ability_update.authorized_targets
                or ability_update.operation == "GRANT"
                and not ability_update.ability_instance_id
                or ability_update.operation == "REVOKE"
                and ability_update.ability_instance_id is not None
            ):
                raise ResolutionError(
                    "ABILITY_UPDATE_INVALID", "ability update is outside frozen authorization"
                )
        for flow_update in batch.flow_updates:
            flow_request, _flow_source = validate_typed_source(
                flow_update.source_request_id,
                flow_update.source_rule_id,
                flow_update.source_skill_id,
                flow_update.source_ability_instance_id,
                "FLOW",
            )
            if (
                flow_request.hook_id != flow_update.hook_id
                or flow_request.logical_window_id != flow_update.logical_window_id
            ):
                raise ResolutionError(
                    "RULE_FLOW_INVALID", "flow update is detached from its bound request hook"
                )
        for cost in batch.cost_updates:
            cost_request = request_by_id.get(cost.source_request_id)
            instance = next(
                (
                    item
                    for item in state.ability_instances
                    if item.ability_instance_id == cost.ability_instance_id
                ),
                None,
            )
            cost_skill = skills.get(instance.skill_id) if instance is not None else None
            declared = next(
                (
                    item
                    for item in (cost_skill.usage.costs if cost_skill is not None else ())
                    if item.resource_id == cost.resource_id and item.amount == cost.amount
                ),
                None,
            )
            if (
                cost_request is None
                or instance is None
                or declared is None
                or cost_request.ability_instance_id != cost.ability_instance_id
                or cost_request.actor_seat != cost.actor_seat
            ):
                raise ResolutionError(
                    "RULE_COST_INVALID", "cost update is outside declared skill usage"
                )
        declared_timings = {
            skill_timing for skill in package.skills for skill_timing in skill.timing
        }
        trusted_speech_hook_timing = (
            timing == GamePhase.TRIGGER_ACTION.value
            and bool(requests)
            and all(self._is_trusted_speech_hook_request(state, request) for request in requests)
        )
        if requests and timing not in declared_timings and not trusted_speech_hook_timing:
            raise ResolutionError(
                "RULE_TIMING_INVALID", "batch timing is not declared by the package"
            )
        if not requests and not any(row.phase == timing for row in package.window_metadata):
            legacy_window_proof = not package.window_metadata and any(
                isinstance(raw_window, Mapping)
                and (raw_window.get("settlement_group_id") or raw_window.get("window_id"))
                == group_id
                and raw_window.get("phase") == timing
                for raw_window in state.action_windows.values()
            )
            if not legacy_window_proof:
                raise ResolutionError(
                    "RULE_TIMING_INVALID", "empty batch timing has no frozen action window"
                )

    def _rule_projection_events(
        self,
        state: GameState,
        batch: ResolutionBatch,
        requests: tuple[SkillRequest, ...],
        *,
        timestamp: datetime,
        next_revision: int,
        hook_id: str | None = None,
        logical_window_id: str | None = None,
        validate_only: bool = False,
        frozen_team_audiences: Mapping[str, tuple[int, ...]] | None = None,
    ) -> tuple[GameEvent, ...]:
        """Render only package-authorized, independently checked disclosure projections."""

        package = self._execution_package
        if package is None:
            return ()
        requests_by_id = {item.request_id: item for item in requests}
        skills = {skill.skill_id: skill for skill in package.skills}
        old = _typed_events(state)
        correlations = {item.correlation_id for item in old if item.correlation_id}
        next_id = max((item.event_id for item in old), default=0) + 1
        output: list[GameEvent] = []
        for projection in batch.disclosures:
            request = requests_by_id.get(projection.source_request_id)
            skill = skills.get(projection.skill_id)
            if request is None or skill is None or request.skill_id != projection.skill_id:
                raise ResolutionError(
                    "RULE_DISCLOSURE_INVALID", "disclosure has unknown provenance"
                )
            declaration = next(
                (
                    item
                    for item in skill.disclosures
                    if item.disclosure_id == projection.disclosure_id
                    and item.audience == projection.audience
                    and item.event_type == projection.event_type
                ),
                None,
            )
            if declaration is None:
                # Interaction projections carry the declaration's stable
                # disclosure ID, not the parent interaction ID.
                declaration = next(
                    (
                        item
                        for rule in package.interactions
                        for item in rule.disclosures
                        if item.disclosure_id == projection.disclosure_id
                        and item.audience == projection.audience
                        and item.event_type == projection.event_type
                    ),
                    None,
                )
            if declaration is None:
                raise ResolutionError(
                    "RULE_DISCLOSURE_INVALID", "disclosure is not declared by the package"
                )
            seats = tuple(sorted(set(projection.recipients)))
            if any(seat not in state.players for seat in seats):
                raise ResolutionError(
                    "RULE_DISCLOSURE_INVALID", "disclosure recipient is unassigned"
                )
            if projection.audience == "SELF" and seats != (request.actor_seat,):
                raise ResolutionError(
                    "RULE_DISCLOSURE_INVALID", "SELF disclosure recipient mismatch"
                )
            if projection.audience == "ALL" and seats != tuple(sorted(state.players)):
                raise ResolutionError(
                    "RULE_DISCLOSURE_INVALID", "ALL disclosure recipient mismatch"
                )
            if projection.audience == "TEAM":
                expected = (
                    frozen_team_audiences.get(request.request_id)
                    if frozen_team_audiences is not None
                    else None
                )
                if expected is None:
                    actor_groups = set(state.players[request.actor_seat].chat_group_ids)
                    expected = tuple(
                        sorted(
                            seat
                            for seat, player in state.players.items()
                            if actor_groups.intersection(player.chat_group_ids)
                        )
                    ) or (request.actor_seat,)
                if seats != expected:
                    raise ResolutionError(
                        "RULE_DISCLOSURE_INVALID", "TEAM disclosure exceeds authorized chat seats"
                    )
            channel = (
                Channel.PUBLIC
                if projection.audience == "ALL"
                else Channel.TEAM
                if projection.audience == "TEAM"
                else Channel.PRIVATE
            )
            is_due = (
                projection.hook == "immediate"
                or hook_id is None
                or projection.hook == hook_id
                or projection.hook == logical_window_id
                or projection.hook == request.logical_window_id
                or projection.hook == request.hook_id
                or projection.hook == state.phase.value
            )
            if validate_only or not is_due:
                continue
            event_type = EventType(projection.event_type or projection.disclosure_id)
            encoded = json.dumps(projection.fields, sort_keys=True, separators=(",", ":"))
            for seat in seats if channel is Channel.PRIVATE else (None,):
                correlation_id = hashlib.sha256(
                    f"{batch.package_id}:{projection.source_request_id}:"
                    f"{projection.disclosure_id}:{seat or 'all'}".encode()
                ).hexdigest()
                correlation = f"rule-disclosure-{correlation_id}"
                if correlation in correlations:
                    continue
                payload: object
                if channel is Channel.PRIVATE:
                    if projection.event_type == "inspection_result":
                        target_seat = projection.fields.get("target_seat")
                        faction_id = projection.fields.get("faction_id")
                        if type(target_seat) is not int or not isinstance(faction_id, str):
                            raise ResolutionError(
                                "RULE_DISCLOSURE_INVALID", "inspection projection is incomplete"
                            )
                        event_type = EventType.SEER_RESULT
                        payload = PrivateSeerResultPayload(
                            target_seat=target_seat, faction_id=faction_id
                        )
                    elif projection.event_type == "wolf_attack_proposed":
                        target_seat = projection.fields.get("target_seat")
                        if type(target_seat) is not int:
                            raise ResolutionError(
                                "RULE_DISCLOSURE_INVALID", "wolf target projection is incomplete"
                            )
                        event_type = EventType.WITCH_TARGET
                        payload = PrivateWitchTargetPayload(target_seat=target_seat)
                    else:
                        payload = PrivateNoticePayload(content=encoded)
                    output.append(
                        GameEvent.private(
                            event_id=next_id,
                            game_id=state.game_id,
                            state_revision=next_revision,
                            round_no=state.round_no,
                            phase=state.phase,
                            created_at=timestamp,
                            event_type=event_type,
                            seat=cast(int, seat),
                            actor_seat=request.actor_seat,
                            correlation_id=correlation,
                            payload=cast(Any, payload),
                        )
                    )
                elif channel is Channel.TEAM:
                    output.append(
                        GameEvent.team(
                            event_id=next_id,
                            game_id=state.game_id,
                            state_revision=next_revision,
                            round_no=state.round_no,
                            phase=state.phase,
                            created_at=timestamp,
                            event_type=event_type,
                            authorized_seats=seats,
                            actor_seat=request.actor_seat,
                            correlation_id=correlation,
                            payload=TeamNoticePayload(content=encoded),
                        )
                    )
                else:
                    output.append(
                        GameEvent.public(
                            event_id=next_id,
                            game_id=state.game_id,
                            state_revision=next_revision,
                            round_no=state.round_no,
                            phase=state.phase,
                            created_at=timestamp,
                            event_type=event_type,
                            eligible_seats=seats,
                            actor_seat=request.actor_seat,
                            correlation_id=correlation,
                            payload=PublicAnnouncementPayload(content=encoded),
                        )
                    )
                correlations.add(correlation)
                next_id += 1
        return tuple(output)

    def _deferred_rule_disclosures(
        self,
        state: GameState,
        batch: ResolutionBatch,
        requests: tuple[SkillRequest, ...],
        *,
        timing: str,
    ) -> tuple[RuleDeferredDisclosure, ...]:
        """Freeze valid projections whose declared hook is later than this commit."""

        request_by_id = {item.request_id: item for item in requests}
        # Validate every projection before it can enter persistent deferred state.
        self._rule_projection_events(
            state,
            batch,
            requests,
            timestamp=state.updated_at,
            next_revision=state.state_revision + 1,
            hook_id=timing,
            validate_only=True,
        )
        existing_ids = {item.delivery_id for item in state.rule_deferred_disclosures}
        output: list[RuleDeferredDisclosure] = []
        for projection in batch.disclosures:
            request = request_by_id.get(projection.source_request_id)
            if request is None:
                raise ResolutionError(
                    "RULE_DISCLOSURE_INVALID", "disclosure has unknown request provenance"
                )
            if projection.hook in {
                "immediate",
                timing,
                request.logical_window_id,
                request.hook_id,
                state.phase.value,
            }:
                continue
            delivery_id = _stable_rule_identifier(
                "disclosure",
                batch.package_id,
                batch.batch_id,
                projection.source_request_id,
                projection.disclosure_id,
            )
            if delivery_id in existing_ids:
                continue
            output.append(
                RuleDeferredDisclosure(
                    delivery_id=delivery_id,
                    package_id=batch.package_id,
                    source_batch_id=batch.batch_id,
                    source_request_id=request.request_id,
                    source_revision=state.state_revision,
                    source_round_no=state.round_no,
                    source_phase=state.phase,
                    source_window_id=request.window_id,
                    source_logical_window_id=request.logical_window_id,
                    actor_seat=request.actor_seat,
                    ability_instance_id=request.ability_instance_id,
                    action_code=request.action_code,
                    skill_id=projection.skill_id,
                    disclosure_id=projection.disclosure_id,
                    audience=projection.audience,
                    recipients=tuple(sorted(set(projection.recipients))),
                    source_team_roster=(
                        tuple(sorted(set(projection.recipients)))
                        if projection.audience == "TEAM"
                        else ()
                    ),
                    fields=json.loads(json.dumps(projection.fields)),
                    hook=projection.hook,
                    event_type=projection.event_type,
                )
            )
            existing_ids.add(delivery_id)
        return tuple(output)

    async def publish_rule_dependency_disclosures(
        self,
        seat: int,
        window_id: str,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Publish only declared predecessor disclosures needed by one actor."""

        if type(seat) is not int or seat < 1:
            raise TypeError("seat must be a positive integer")
        if not isinstance(window_id, str) or not window_id:
            raise TypeError("window_id must be a non-empty string")
        async with self._lock:
            state = self._state
            revision = state.state_revision if expected_revision is None else expected_revision
            _revision_check(state, revision)
            if self._rules is None or self._execution_package is None:
                raise ResolutionError(
                    "RULE_PACKAGE_MISSING", "game has no pinned execution package"
                )
            if state.phase is not GamePhase.NIGHT_ACTION:
                raise ResolutionError(
                    "PHASE_MISMATCH", "dependency disclosures require NIGHT_ACTION"
                )
            raw_window = state.action_windows.get(window_id)
            if raw_window is None:
                raise ResolutionError("WINDOW_NOT_FOUND", "night action window is not installed")
            try:
                window = _load_action_window(raw_window)
            except (TypeError, ValueError) as exc:
                raise ResolutionError("WINDOW_INVALID", "night action window is malformed") from exc
            if (
                window.phase is not GamePhase.NIGHT_ACTION
                or window.closed_at is not None
                or seat not in window.allowed_seats
            ):
                raise ResolutionError("WINDOW_MISMATCH", "actor is outside the open action window")
            active = self._rule_skill_instances(
                state,
                seat,
                GamePhase.NIGHT_ACTION.value,
                allowed_codes=set(window.allowed_action_codes),
                logical_window_id=window.logical_window_id,
            )
            needed = {
                predecessor for _instance, skill in active for predecessor in skill.after_skills
            }
            if not needed:
                return state
            pending_ids = {
                request_id
                for request_id, payload in state.action_requests.items()
                if isinstance(payload, Mapping)
                and payload.get("window_id") == window_id
                and payload.get("status") == "PENDING"
            }
            if not pending_ids:
                return state
            requests, _bindings, group_id = self._rule_group_requests(
                state,
                pending_ids,
                timing=GamePhase.NIGHT_ACTION.value,
            )
            try:
                batch = self._rules.plan(
                    state,
                    requests,
                    group_id=group_id,
                    timing=GamePhase.NIGHT_ACTION.value,
                )
            except (RuleAdapterError, TypeError, ValueError) as exc:
                raise ResolutionError("RULE_PLAN_INVALID", str(exc)) from exc
            projections = tuple(
                item
                for item in batch.disclosures
                if item.skill_id in needed
                and item.hook == GamePhase.NIGHT_ACTION.value
                and seat in item.recipients
            )
            if not projections:
                return state
            timestamp = now or utc_now()
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise EventCommitError("TIMESTAMP: commit timestamp must include a timezone")
            timestamp = timestamp.astimezone(UTC)
            projected = batch.model_copy(update={"disclosures": projections})
            events = self._rule_projection_events(
                state,
                projected,
                requests,
                timestamp=timestamp,
                next_revision=revision + 1,
            )
            if not events:
                return state
            _validate_new_events(state, events, next_revision=revision + 1)
            data = _state_data(state)
            data["events"] = (*_typed_events(state), *events)
            data["state_revision"] = revision + 1
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    def _release_due_rule_disclosures(
        self,
        state: GameState,
        hook_id: str,
        logical_window_id: str | None,
        *,
        timestamp: datetime,
        next_revision: int,
    ) -> GameState:
        """Append one exact-hook disclosure batch and remove it atomically."""

        due = tuple(
            item
            for item in state.rule_deferred_disclosures
            if item.hook == hook_id or item.hook == logical_window_id
        )
        if not due:
            return state
        package = self._execution_package
        if package is None or state.execution_identity is None:
            raise ResolutionError(
                "RULE_PACKAGE_MISSING", "deferred disclosure has no pinned execution package"
            )
        if any(item.package_id != package.package_id for item in due):
            raise ResolutionError(
                "RULE_DISCLOSURE_INVALID", "deferred disclosure package identity changed"
            )
        projections = tuple(
            DisclosureProjection(
                disclosure_id=item.disclosure_id,
                source_request_id=item.source_request_id,
                skill_id=item.skill_id,
                audience=item.audience,
                recipients=item.recipients,
                fields=json.loads(json.dumps(item.fields)),
                hook=item.hook,
                event_type=item.event_type,
            )
            for item in due
        )
        requests = tuple(
            SkillRequest(
                request_id=item.source_request_id,
                ability_instance_id=item.ability_instance_id,
                skill_id=item.skill_id,
                action_code=item.action_code,
                actor_seat=item.actor_seat,
                origin="PLAYER",
                window_id=item.source_window_id,
                logical_window_id=item.source_logical_window_id,
                hook_id=(
                    cast(RuleHook, item.hook)
                    if item.hook in {"DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"}
                    else None
                ),
            )
            for item in due
        )
        synthetic_batch = ResolutionBatch(
            batch_id=due[0].source_batch_id,
            package_id=due[0].package_id,
            board_id=state.execution_identity.board_id,
            board_version=state.execution_identity.board_version,
            read_revision=due[0].source_revision,
            round_number=due[0].source_round_no,
            group_id=due[0].source_batch_id,
            dispositions=(),
            disclosures=projections,
        )
        events = self._rule_projection_events(
            state,
            synthetic_batch,
            requests,
            timestamp=timestamp,
            next_revision=next_revision,
            hook_id=hook_id,
            logical_window_id=logical_window_id,
            frozen_team_audiences={
                item.source_request_id: item.source_team_roster
                for item in due
                if item.audience == "TEAM"
            },
        )
        if events:
            _validate_new_events(state, events, next_revision=next_revision)
        due_ids = {item.delivery_id for item in due}
        data = _state_data(state)
        data["rule_deferred_disclosures"] = tuple(
            item.model_dump(mode="python")
            for item in state.rule_deferred_disclosures
            if item.delivery_id not in due_ids
        )
        data["events"] = (*_typed_events(state), *events)
        data["state_revision"] = next_revision
        data["updated_at"] = timestamp
        return GameState.model_validate(data)

    async def publish_due_rule_disclosures(
        self,
        hook_id: str,
        logical_window_id: str | None = None,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Publish fixed rule projections at their exact declared hook once.

        ``hook_id`` is a frozen phase value, logical window ID, or day-speech
        hook. A logical-window caller supplies the same ID in both positional
        fields so a projection cannot be released by a neighboring window.
        Queue removal and event append share one state revision, making restore
        and retry idempotent.
        """

        if not isinstance(hook_id, str) or not hook_id:
            raise TypeError("hook_id must be a non-empty string")
        if logical_window_id is not None and (
            not isinstance(logical_window_id, str) or not logical_window_id
        ):
            raise TypeError("logical_window_id must be a non-empty string")
        timestamp = _aware_commit_time(now)
        async with self._lock:
            state = self._state
            revision = state.state_revision if expected_revision is None else expected_revision
            _revision_check(state, revision)
            due = tuple(
                item
                for item in state.rule_deferred_disclosures
                if item.hook == hook_id or item.hook == logical_window_id
            )
            if not due:
                return state
            package = self._execution_package
            if package is None or state.execution_identity is None:
                raise ResolutionError(
                    "RULE_PACKAGE_MISSING", "deferred disclosure has no pinned execution package"
                )
            if any(item.package_id != package.package_id for item in due):
                raise ResolutionError(
                    "RULE_DISCLOSURE_INVALID", "deferred disclosure package identity changed"
                )
            declared_hooks = (
                {item.window_id for item in package.window_metadata}
                | {phase.value for phase in GamePhase}
                | {
                    "DAY_SPEECH_BEFORE",
                    "DAY_SPEECH_AFTER",
                }
            )
            if hook_id not in declared_hooks:
                raise ResolutionError(
                    "RULE_DISCLOSURE_INVALID", "publication hook is not frozen by the package"
                )
            if hook_id in {"DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"}:
                if (
                    state.phase is not GamePhase.DAY_SPEECH
                    or state.pending_resolution is not None
                    or state.serial_turn is not None
                    or any(item.is_pending for item in state.rule_boundaries)
                    or (
                        isinstance(state.sheriff_badge, Mapping)
                        and state.sheriff_badge.get("status") == "OPEN"
                    )
                ):
                    raise ResolutionError(
                        "RULE_HOOK_NOT_ALLOWED", "day-speech disclosure hook is not active"
                    )
                if hook_id == "DAY_SPEECH_BEFORE" and not state.current_queue:
                    raise ResolutionError(
                        "RULE_HOOK_NOT_ALLOWED", "BEFORE hook requires an ordinary queue head"
                    )
                if hook_id == "DAY_SPEECH_AFTER":
                    last_turn = state.last_serial_turn
                    if last_turn is None or not any(
                        event.event_id in last_turn.event_ids
                        and event.event_type is EventType.SPEECH
                        and event.phase is GamePhase.DAY_SPEECH
                        and event.actor_seat == last_turn.seat
                        for event in _typed_events(state)
                    ):
                        raise ResolutionError(
                            "RULE_HOOK_NOT_ALLOWED",
                            "AFTER hook requires the exact committed ordinary speech turn",
                        )
            elif logical_window_id is not None:
                if hook_id != logical_window_id:
                    raise ResolutionError(
                        "RULE_HOOK_NOT_ALLOWED", "logical-window hook ID must match its window"
                    )
                row = next(
                    (item for item in package.window_metadata if item.window_id == hook_id),
                    None,
                )
                if row is None or state.phase.value != row.phase:
                    raise ResolutionError(
                        "RULE_HOOK_NOT_ALLOWED", "logical-window disclosure is outside its phase"
                    )
                installed = tuple(
                    _load_action_window(raw)
                    for raw in state.action_windows.values()
                    if isinstance(raw, Mapping) and raw.get("logical_window_id") == hook_id
                )
                if not installed or not any(
                    item.phase.value == row.phase and item.closed_at is None for item in installed
                ):
                    raise ResolutionError(
                        "RULE_HOOK_NOT_ALLOWED", "logical-window disclosure has no active window"
                    )
            else:
                try:
                    expected_phase = GamePhase(hook_id)
                except ValueError as exc:
                    raise ResolutionError(
                        "RULE_HOOK_NOT_ALLOWED", "publication hook is not an active phase"
                    ) from exc
                if state.phase is not expected_phase:
                    raise ResolutionError(
                        "RULE_HOOK_NOT_ALLOWED", "phase disclosure hook is not the active phase"
                    )
            committed = self._release_due_rule_disclosures(
                state,
                hook_id,
                logical_window_id,
                timestamp=timestamp,
                next_revision=revision + 1,
            )
            self._state = committed
            return committed

    def _rule_return_point(
        self,
        state: GameState,
        requests: tuple[SkillRequest, ...],
        bindings: tuple[_RuleRequestBinding, ...],
    ) -> RuleReturnPoint:
        """Bind workflow resumption to the active speech turn or next frozen window."""

        cursor = state.rule_workflow_cursor
        if (
            cursor is not None
            and cursor.status in {"DRAINING", "WAITING_CHOICE", "WAITING_BOUNDARY"}
            and cursor.return_point is not None
        ):
            return cursor.return_point
        if (
            cursor is not None
            and cursor.status == "COLLECTING"
            and state.phase is GamePhase.TRIGGER_ACTION
            and cursor.return_point is not None
            and cursor.active_occurrence_id is not None
            and all(
                request.trigger_occurrence_id == cursor.active_occurrence_id
                and request.source_fact_id is not None
                for request in requests
            )
            and bool(requests)
            and all(
                (
                    window := self._window_for_request(
                        state, _stored_action_request(state, binding.request_id)
                    )
                ).window_id
                in cursor.active_window_ids
                and (occurrence := self._rule_occurrence_for_window(state, window)) is not None
                and occurrence.occurrence_id == cursor.active_occurrence_id
                for binding in bindings
            )
        ):
            # Completing an installed rule choice temporarily changes the
            # cursor to COLLECTING. Preserve its original source return point
            # only when the active occurrence and physical window prove that
            # this collection is the same trigger workflow.
            return cursor.return_point
        if state.serial_turn is not None:
            turn = state.serial_turn
            request_hook = next((item.hook_id for item in requests if item.hook_id), None)
            return RuleReturnPoint(
                phase=GamePhase.DAY_SPEECH,
                hook_id=request_hook,
                speaker_seat=turn.seat,
                serial_turn_id=turn.request_id,
                event_ids=turn.event_ids,
                day_no=state.day_no,
            )

        current_windows = tuple(
            self._window_for_request(state, _stored_action_request(state, binding.request_id))
            for binding in bindings
        )
        settlement_group_id = cursor.settlement_group_id if cursor is not None else None
        if (
            cursor is not None
            and cursor.status == "COLLECTING"
            and settlement_group_id is not None
            and current_windows
            and all(
                (window.settlement_group_id or window.window_id) == settlement_group_id
                for window in current_windows
            )
        ):
            grouped = tuple(
                _load_action_window(raw)
                for raw in state.action_windows.values()
                if isinstance(raw, Mapping)
                and (raw.get("settlement_group_id") or raw.get("window_id")) == settlement_group_id
            )
            if grouped:
                current_windows = grouped
        elif (
            not current_windows
            and cursor is not None
            and cursor.status == "COLLECTING"
            and settlement_group_id is not None
            and cursor.active_window_ids
        ):
            # Empty frozen windows have no request bindings from which to
            # derive their successor. Use only the physical windows recorded
            # by the live collecting cursor, and require the full set to
            # still belong to its settlement group. An IDLE cursor may retain
            # an older group's fields and is deliberately not a source here.
            active_windows = tuple(
                _load_action_window(state.action_windows[window_id])
                for window_id in cursor.active_window_ids
                if window_id in state.action_windows
            )
            if len(active_windows) == len(cursor.active_window_ids) and all(
                (window.settlement_group_id or window.window_id) == settlement_group_id
                for window in active_windows
            ):
                current_windows = active_windows
        if not current_windows:
            return RuleReturnPoint(phase=state.phase, day_no=state.day_no)
        window = current_windows[0]
        if window.phase in {
            GamePhase.NIGHT_TEAM_CHAT,
            GamePhase.NIGHT_ACTION,
            GamePhase.NIGHT_RESOLVE,
        }:
            metadata = (
                self._execution_package.window_metadata
                if self._execution_package is not None
                else ()
            )
            rows = {item.window_id: item for item in metadata}
            window = max(
                current_windows,
                key=lambda item: (
                    rows[item.logical_window_id].order if item.logical_window_id in rows else 0
                ),
            )
            next_id = window.next_window_id
            next_row = next((item for item in metadata if item.window_id == next_id), None)
            return RuleReturnPoint(
                phase=GamePhase(next_row.phase) if next_row is not None else GamePhase.DAY_ANNOUNCE,
                window_id=window.window_id,
                logical_window_id=next_id,
                day_no=state.day_no,
            )
        return RuleReturnPoint(
            phase=state.phase,
            hook_id=window.hook_id,
            window_id=window.window_id,
            logical_window_id=window.logical_window_id,
            day_no=state.day_no,
        )

    def _ordinary_speech_hook_source(
        self,
        state: GameState,
        hook_id: RuleHook,
    ) -> tuple[str, RuleReturnPoint]:
        """Prove a BEFORE queue head or completed ordinary AFTER speech source."""

        if state.phase is not GamePhase.DAY_SPEECH:
            raise EventCommitError("RULE_HOOK_NOT_ALLOWED: speech hook requires DAY_SPEECH")
        election_status = (
            state.sheriff_election.get("status")
            if isinstance(state.sheriff_election, Mapping)
            else None
        )
        if (
            state.pending_resolution is not None
            or any(item.is_pending for item in state.rule_boundaries)
            or (
                isinstance(state.sheriff_badge, Mapping)
                and state.sheriff_badge.get("status") == "OPEN"
            )
            or election_status in {"SPEECH", "VOTING", "WAITING_GM"}
        ):
            raise EventCommitError(
                "RULE_HOOK_NOT_ALLOWED: speech hooks are closed during a special boundary"
            )
        if hook_id == "DAY_SPEECH_BEFORE":
            if state.serial_turn is not None or not state.current_queue:
                raise EventCommitError(
                    "RULE_HOOK_NOT_ALLOWED: BEFORE requires an unstarted ordinary queue head"
                )
            speaker = state.current_queue[0]
            source_id = _stable_rule_identifier(
                "speech-before",
                state.game_id,
                state.day_no,
                speaker,
                ",".join(str(item) for item in state.current_queue),
            )
            return source_id, RuleReturnPoint(
                phase=GamePhase.DAY_SPEECH,
                hook_id=hook_id,
                speaker_seat=speaker,
                serial_turn_id=f"before-{source_id}",
                day_no=state.day_no,
            )

        if hook_id != "DAY_SPEECH_AFTER" or state.serial_turn is not None:
            raise EventCommitError(
                "RULE_HOOK_NOT_ALLOWED: AFTER requires a completed ordinary speech turn"
            )
        last_turn = state.last_serial_turn
        if last_turn is None:
            raise EventCommitError(
                "RULE_HOOK_NOT_ALLOWED: AFTER has no completed ordinary speech reference"
            )
        speech = next(
            (
                event
                for event in _typed_events(state)
                if event.event_id in last_turn.event_ids
                and event.event_type is EventType.SPEECH
                and event.phase is GamePhase.DAY_SPEECH
                and event.actor_seat == last_turn.seat
            ),
            None,
        )
        if speech is None:
            raise EventCommitError(
                "RULE_HOOK_NOT_ALLOWED: AFTER source is not an ordinary speech event"
            )
        source_id = _stable_rule_identifier(
            "speech-after",
            state.game_id,
            state.day_no,
            last_turn.request_id,
            ",".join(str(item) for item in last_turn.event_ids),
        )
        return source_id, RuleReturnPoint(
            phase=GamePhase.DAY_SPEECH,
            hook_id=hook_id,
            speaker_seat=last_turn.seat,
            serial_turn_id=last_turn.request_id,
            event_ids=last_turn.event_ids,
            day_no=state.day_no,
        )

    def _speech_hook_occurrences(
        self,
        state: GameState,
        hook_id: RuleHook,
        *,
        source_id: str,
    ) -> tuple[RuleTriggerOccurrence, ...]:
        """Build only frozen PLAYER hook choices bound to one speech source."""

        package = self._execution_package
        if package is None:
            return ()
        skills = {item.skill_id: item for item in package.skills}
        start_order = max((item.order for item in state.rule_trigger_queue), default=-1) + 1
        eligible_instances = {
            instance.ability_instance_id: (instance, skill)
            for seat in sorted(state.players)
            for instance, skill in self._rule_skill_instances(
                state,
                seat,
                GamePhase.DAY_SPEECH.value,
            )
        }
        if self._rules is None:
            return ()
        try:
            observation = self._rules.observation(
                state,
                group_id=f"speech-hook:{state.game_id}:{state.day_no}:{hook_id}:{source_id}",
                timing=GamePhase.DAY_SPEECH.value,
            )
        except (RuleAdapterError, TypeError, ValueError):
            return ()
        observed_players = {item.seat: item for item in observation.players}
        candidates: list[tuple[int, str, RuleTriggerOccurrence]] = []
        for instance in state.ability_instances:
            skill = skills.get(instance.skill_id)
            player = state.players.get(instance.actor_seat)
            if eligible_instances.get(instance.ability_instance_id) != (instance, skill):
                continue
            if (
                skill is None
                or player is None
                or not player.alive
                or not instance.enabled
                or instance.consumed
                or instance.grant_kind != "ACTIVE"
                or skill.mode != "PLAYER"
                or skill.trigger is not None
                or instance.grant_id not in {item.grant_id for item in skill.grants}
                or hook_id not in skill.hook_ids
                or GamePhase.DAY_SPEECH.value not in skill.timing
            ):
                continue
            occurrence_id = _stable_rule_identifier(
                "hook-occurrence",
                state.game_id,
                state.day_no,
                hook_id,
                source_id,
                instance.ability_instance_id,
            )
            if not self._rule_skill_condition_is_eligible(
                observation,
                skill,
                instance,
                actor=observed_players.get(instance.actor_seat),
                request={
                    "request_id": f"hook-{occurrence_id}",
                    "action_code": skill.action_code,
                    "passed": False,
                    "actor_seat": instance.actor_seat,
                    "target_count": 0,
                    "parameters": {},
                    "window_id": f"rule-trigger-{occurrence_id}",
                    "logical_window_id": skill.window_ids[0] if skill.window_ids else None,
                    "hook_id": hook_id,
                },
            ):
                continue
            source_batch_id = _stable_rule_identifier(
                "hook-batch", state.game_id, state.day_no, hook_id, source_id
            )
            candidates.append(
                (
                    instance.actor_seat,
                    instance.ability_instance_id,
                    RuleTriggerOccurrence(
                        occurrence_id=occurrence_id,
                        kind="HOOK",
                        source_fact_id=source_id,
                        source_batch_id=source_batch_id,
                        ability_instance_id=instance.ability_instance_id,
                        skill_id=skill.skill_id,
                        actor_seat=instance.actor_seat,
                        mode="PLAYER_CHOICE",
                        order=start_order,
                        hook_id=hook_id,
                        logical_window_id=None,
                    ),
                )
            )
        candidates.sort(key=lambda item: (item[0], item[1]))
        return tuple(
            occurrence.model_copy(update={"order": start_order + index})
            for index, (_seat, _instance, occurrence) in enumerate(candidates)
        )

    @staticmethod
    def _rule_skill_condition_is_eligible(
        observation: object,
        skill: SkillSpec,
        instance: AbilityInstanceState,
        *,
        actor: object | None,
        request: Mapping[str, object],
        source_fact: DomainFact | None = None,
        target: object | None = None,
    ) -> bool:
        """Check the stable portion of a frozen skill condition at offer time.

        Player-selected target, request-shape, and item references remain for
        the interpreter to evaluate after an actual choice. Actor, observation,
        and per-instance state references are already authoritative here.
        """

        condition = skill.condition
        if condition is None:
            return True

        def has_deferred_reference(value: object) -> bool:
            if isinstance(value, Mapping):
                if value.get("op") == "ref" and value.get("source") in {
                    "target",
                    "request",
                    "item",
                }:
                    return True
                return any(has_deferred_reference(item) for item in value.values())
            if isinstance(value, (tuple, list)):
                return any(has_deferred_reference(item) for item in value)
            return False

        if has_deferred_reference(condition.model_dump(mode="python")):
            return True
        observed_skill_state = getattr(observation, "skill_state", ())
        skill_state = {
            item.key: item.value
            for item in observed_skill_state
            if item.ability_instance_id == instance.ability_instance_id
            and item.skill_id == skill.skill_id
        }
        try:
            return evaluate_predicate(
                condition,
                {
                    "actor": actor,
                    "target": target,
                    "request": request,
                    "request_targets": (),
                    "source_fact": source_fact,
                    "observation": observation,
                    "skill_state": skill_state,
                    "skill": skill,
                    "item": None,
                },
            )
        except (TypeError, ValueError):
            return False

    def _confirmed_death_facts(
        self,
        state: GameState,
        batch: ResolutionBatch,
    ) -> tuple[RuleFactRecord, ...]:
        """Create one canonical durable fact for each newly confirmed death."""

        source_facts = tuple(
            item for item in batch.outcomes if item.fact_type.upper() == "DEATH_CONFIRMED"
        )
        result: list[RuleFactRecord] = []
        for outcome in sorted(batch.mortality, key=lambda item: item.seat):
            player = state.players.get(outcome.seat)
            if not outcome.deceased or player is None or not player.alive:
                continue
            source_fact = next(
                (
                    fact
                    for fact in source_facts
                    if fact.target_seat == outcome.seat and fact.death_cause == outcome.death_cause
                ),
                None,
            )
            cause_effects = tuple(
                sorted(
                    (
                        effect
                        for effect in batch.effects
                        if effect.effect_id in outcome.cause_effect_ids and effect.applied
                    ),
                    key=lambda effect: effect.effect_id,
                )
            )
            primary_source = cause_effects[0] if cause_effects else None
            fact_data = json.loads(json.dumps(source_fact.data)) if source_fact is not None else {}
            if not isinstance(fact_data, dict):
                fact_data = {}
            fact_data["cause_effect_ids"] = list(outcome.cause_effect_ids)
            fact_data["source_request_ids"] = list(outcome.source_request_ids)
            fact_data["sources"] = [
                {
                    "effect_id": effect.effect_id,
                    "source_rule_id": effect.source_rule_id,
                    "source_request_id": effect.source_request_id,
                    "actor_seat": effect.actor_seat,
                }
                for effect in cause_effects
            ]
            result.append(
                RuleFactRecord(
                    fact_id=(
                        source_fact.fact_id
                        if source_fact is not None
                        else _stable_rule_identifier(
                            "death-confirmed",
                            state.game_id,
                            batch.batch_id,
                            outcome.seat,
                            outcome.death_cause or "unknown",
                        )
                    ),
                    fact_type=(
                        "death_confirmed" if self._legacy_compatibility else "DEATH_CONFIRMED"
                    ),
                    source_rule_id=(
                        primary_source.source_rule_id if primary_source is not None else None
                    ),
                    source_request_id=(
                        primary_source.source_request_id if primary_source is not None else None
                    ),
                    actor_seat=primary_source.actor_seat if primary_source is not None else None,
                    target_seat=outcome.seat,
                    death_cause=outcome.death_cause,
                    tags=(
                        source_fact.tags
                        if source_fact is not None and source_fact.tags
                        else (outcome.death_cause,)
                        if outcome.death_cause
                        else ()
                    ),
                    data={**fact_data, "round_number": state.round_no},
                )
            )
        return tuple(result)

    def _rule_boundaries_for_deaths(
        self,
        state: GameState,
        batch: ResolutionBatch,
        death_facts: tuple[RuleFactRecord, ...],
        return_point: RuleReturnPoint,
        *,
        timing: str,
        timestamp: datetime,
    ) -> tuple[RuleBoundary, ...]:
        if not death_facts:
            return ()
        package = self._execution_package
        policy = package.boundary_policy if package is not None else None
        night_death = timing in {
            GamePhase.NIGHT_TEAM_CHAT.value,
            GamePhase.NIGHT_ACTION.value,
            GamePhase.NIGHT_RESOLVE.value,
        }
        phase_policy_applies = (
            policy is not None
            and policy.last_words_enabled
            and (
                (
                    night_death
                    and policy.night_death_policy == "every_night"
                    or night_death
                    and policy.night_death_policy == "first_night_only"
                    and state.round_no == 0
                )
                or (not night_death and policy.day_death_policy == "every_day")
            )
        )
        eligible_causes = set(policy.eligible_death_causes if policy is not None else ())
        last_words_seats = tuple(
            fact.target_seat
            for fact in death_facts
            if phase_policy_applies
            and fact.target_seat is not None
            and fact.death_cause in eligible_causes
        )
        sheriff_required = bool(
            policy is not None
            and policy.sheriff_enabled
            and policy.badge_transfer_enabled is True
            and policy.badge_transfer_on_death is True
            and state.sheriff_seat in {item.target_seat for item in death_facts}
        )
        boundary = RuleBoundary(
            boundary_id=_stable_rule_identifier(
                "boundary",
                state.game_id,
                batch.batch_id,
                *(fact.fact_id for fact in death_facts),
            ),
            source_group_id=batch.group_id,
            source_batch_id=batch.batch_id,
            death_fact_ids=tuple(fact.fact_id for fact in death_facts),
            death_seats=tuple(
                fact.target_seat for fact in death_facts if fact.target_seat is not None
            ),
            return_point=return_point,
            last_words_required=bool(last_words_seats),
            last_words_seats=last_words_seats,
            sheriff_badge_required=sheriff_required,
            created_at=timestamp,
            completed_at=None if last_words_seats or sheriff_required else timestamp,
        )
        return (boundary,)

    def _queue_confirmed_rule_facts(
        self,
        state: GameState,
        facts: tuple[RuleFactRecord, ...],
        *,
        source_batch_id: str,
    ) -> tuple[RuleTriggerOccurrence, ...]:
        """Synthesize only frozen automatic/choice triggers from persisted facts."""

        package = self._execution_package
        if package is None or not facts or self._rules is None:
            return ()
        source_facts_list: list[RuleFactRecord] = []
        seen_deaths: set[tuple[int | None, str | None]] = set()
        for fact in facts:
            if fact.fact_type.upper() == "DEATH_CONFIRMED":
                death_identity = (fact.target_seat, fact.death_cause)
                if death_identity in seen_deaths:
                    continue
                seen_deaths.add(death_identity)
            source_facts_list.append(fact)
        source_facts = tuple(source_facts_list)
        occupied = {item.occurrence_id for item in state.rule_trigger_queue}
        candidates: list[tuple[str, str, RuleTriggerOccurrence]] = []
        for fact in source_facts:
            domain_fact = DomainFact(
                fact_id=fact.fact_id,
                fact_type=fact.fact_type,
                source_rule_id=fact.source_rule_id,
                source_request_id=fact.source_request_id,
                actor_seat=fact.actor_seat,
                target_seat=fact.target_seat,
                death_cause=fact.death_cause,
                tags=fact.tags,
                data=json.loads(json.dumps(fact.data)),
            )
            for instance in state.ability_instances:
                if not instance.enabled or instance.consumed:
                    continue
                skill = next(
                    (item for item in package.skills if item.skill_id == instance.skill_id),
                    None,
                )
                trigger = skill.trigger if skill is not None else None
                legacy_grant = self._legacy_trigger_grant(state, instance, skill)
                if (
                    skill is None
                    or instance.grant_id not in {item.grant_id for item in skill.grants}
                    or self._rule_instance_capacity_reason(state, instance, skill) is not None
                ):
                    continue
                if trigger is not None:
                    if fact.fact_type not in trigger.fact_types:
                        continue
                    mode = trigger.mode
                elif (
                    legacy_grant is not None
                    and legacy_grant.trigger.event is TriggerEvent.DEATH_CONFIRMED
                    and legacy_grant.trigger.mode is TriggerMode.PLAYER_CHOICE
                    and fact.fact_type.lower() == TriggerEvent.DEATH_CONFIRMED.value.lower()
                    and fact.target_seat == instance.actor_seat
                    and fact.death_cause in legacy_grant.trigger.allowed_death_causes
                    and state.players.get(instance.actor_seat) is not None
                    and not state.players[instance.actor_seat].alive
                    and state.players[instance.actor_seat].death_cause == fact.death_cause
                ):
                    mode = "PLAYER_CHOICE"
                else:
                    continue
                occurrence_id = _stable_rule_identifier(
                    "occurrence", state.game_id, fact.fact_id, instance.ability_instance_id
                )
                if occurrence_id in occupied:
                    continue
                if trigger is not None and trigger.condition is not None:
                    try:
                        observation = self._rules.observation(
                            state,
                            group_id=f"trigger-match:{occurrence_id}",
                            timing=GamePhase.TRIGGER_ACTION.value,
                        )
                        observed_players = {item.seat: item for item in observation.players}
                        actor = observed_players.get(instance.actor_seat)
                        target = observed_players.get(fact.target_seat or 0)
                        if actor is None or not evaluate_predicate(
                            trigger.condition,
                            {
                                "actor": actor,
                                "target": target,
                                "source_fact": domain_fact,
                                "observation": observation,
                                "skill_state": {
                                    item.key: item.value
                                    for item in observation.skill_state
                                    if item.ability_instance_id == instance.ability_instance_id
                                },
                                "item": None,
                            },
                        ):
                            continue
                    except (RuleAdapterError, TypeError, ValueError):
                        continue
                if skill.condition is not None:
                    try:
                        condition_observation = self._rules.observation(
                            state,
                            group_id=f"trigger-skill-condition:{occurrence_id}",
                            timing=GamePhase.TRIGGER_ACTION.value,
                        )
                        observed_players = {
                            item.seat: item for item in condition_observation.players
                        }
                        source_domain_fact = DomainFact(
                            fact_id=fact.fact_id,
                            fact_type=fact.fact_type,
                            source_rule_id=fact.source_rule_id,
                            source_request_id=fact.source_request_id,
                            actor_seat=fact.actor_seat,
                            target_seat=fact.target_seat,
                            death_cause=fact.death_cause,
                            tags=fact.tags,
                            data=json.loads(json.dumps(fact.data)),
                        )
                        if not self._rule_skill_condition_is_eligible(
                            condition_observation,
                            skill,
                            instance,
                            actor=observed_players.get(instance.actor_seat),
                            target=(
                                observed_players.get(fact.target_seat)
                                if fact.target_seat is not None
                                else None
                            ),
                            source_fact=source_domain_fact,
                            request={
                                "request_id": f"trigger-{occurrence_id}",
                                "action_code": skill.action_code,
                                "passed": False,
                                "actor_seat": instance.actor_seat,
                                "target_count": 0,
                                "parameters": {},
                                "window_id": f"trigger-{occurrence_id}",
                                "logical_window_id": (
                                    skill.window_ids[0] if skill.window_ids else None
                                ),
                                "hook_id": None,
                            },
                        ):
                            continue
                    except (RuleAdapterError, TypeError, ValueError):
                        continue
                occurrence = RuleTriggerOccurrence(
                    occurrence_id=occurrence_id,
                    kind="TRIGGER",
                    source_fact_id=fact.fact_id,
                    source_batch_id=source_batch_id,
                    ability_instance_id=instance.ability_instance_id,
                    skill_id=skill.skill_id,
                    actor_seat=instance.actor_seat,
                    mode=mode,
                    order=0,
                    hook_id=None,
                    logical_window_id=skill.window_ids[0] if skill.window_ids else None,
                )
                candidates.append((fact.fact_id, instance.ability_instance_id, occurrence))
        candidates.sort(key=lambda item: (item[0], item[1], item[2].occurrence_id))
        start_order = max((item.order for item in state.rule_trigger_queue), default=-1) + 1
        return tuple(
            occurrence.model_copy(update={"order": start_order + index})
            for index, (_fact_id, _instance_id, occurrence) in enumerate(candidates)
        )

    def _legacy_trigger_grant(
        self,
        state: GameState,
        instance: AbilityInstanceState | None,
        skill: SkillSpec | None,
    ) -> GrantedTriggerAbility | None:
        """Resolve a schema-1 trigger only inside the pinned compat path."""

        if (
            not self._legacy_compatibility
            or instance is None
            or skill is None
            or skill.trigger is not None
            or instance.grant_kind != "TRIGGER"
            or instance.action_code != skill.action_code
            or instance.grant_id not in {item.grant_id for item in skill.grants}
        ):
            return None
        player = state.players.get(instance.actor_seat)
        if player is None:
            return None
        matches = tuple(
            ability
            for ability in player.granted_trigger_abilities
            if ability.action_code == skill.action_code and not ability.consumed
        )
        return matches[0] if len(matches) == 1 else None

    def _apply_rule_batch(
        self,
        state: GameState,
        batch: ResolutionBatch,
        requests: tuple[SkillRequest, ...],
        bindings: tuple[_RuleRequestBinding, ...],
        resolutions: tuple[ActionResolution, ...],
        *,
        group_id: str,
        timing: str,
        timestamp: datetime,
        extra_ability_instances: tuple[AbilityInstance, ...] = (),
    ) -> GameState:
        """Atomically project one validated package batch into durable state."""

        self._validate_rule_batch(
            state,
            batch,
            requests,
            group_id=group_id,
            timing=timing,
            extra_ability_instances=extra_ability_instances,
        )
        data = _state_data(state)
        players_data = {
            seat: player.model_dump(mode="python") for seat, player in state.players.items()
        }
        instances = {
            item.ability_instance_id: item.model_dump(mode="json")
            for item in state.ability_instances
        }
        disposition_by_id = {item.request_id: item for item in batch.dispositions}
        request_payloads = dict(data["action_requests"])
        resolution_by_external = {item.request_id: item for item in resolutions}
        trigger_window_bindings: dict[int, tuple[str, str, str]] = {}
        for binding in bindings:
            external = _stored_action_request(state, binding.request_id)
            payload = request_payloads[binding.request_id]
            window = self._window_for_request(state, external)
            # New queue-backed rule occurrences own their ability instance,
            # request binding, and completion receipt. The legacy trigger
            # grant compatibility path only applies to its verified old
            # resolution shape and must not consume a similarly coded grant.
            is_rule_occurrence_window = window.phase is GamePhase.TRIGGER_ACTION and isinstance(
                window.visible_context.get("rule_occurrence_id"), str
            )
            trigger_binding = (
                _pending_trigger_ability(state, window, require_bound=True)
                if window.phase is GamePhase.TRIGGER_ACTION and not is_rule_occurrence_window
                else None
            )
            if trigger_binding is not None:
                trigger_ability = trigger_binding[1]
                trigger_window_bindings[external.seat] = (
                    trigger_ability.ability_id,
                    binding.request_id,
                    binding.skill_request.ability_instance_id,
                )
            elif is_rule_occurrence_window and self._legacy_compatibility:
                occurrence = next(
                    (
                        item
                        for item in state.rule_trigger_queue
                        if item.occurrence_id == window.visible_context.get("rule_occurrence_id")
                        and item.status == "WAITING_CHOICE"
                        and item.kind == "TRIGGER"
                    ),
                    None,
                )
                legacy_instance = next(
                    (
                        item
                        for item in state.ability_instances
                        if occurrence is not None
                        and item.ability_instance_id == occurrence.ability_instance_id
                        and item.actor_seat == external.seat
                        and item.grant_kind == "TRIGGER"
                    ),
                    None,
                )
                legacy_skill = (
                    next(
                        (
                            item
                            for item in self._execution_package.skills
                            if occurrence is not None and item.skill_id == occurrence.skill_id
                        ),
                        None,
                    )
                    if occurrence is not None and self._execution_package is not None
                    else None
                )
                legacy_grant = self._legacy_trigger_grant(
                    state,
                    legacy_instance,
                    legacy_skill,
                )
                if legacy_grant is not None:
                    trigger_window_bindings[external.seat] = (
                        legacy_grant.ability_id,
                        binding.request_id,
                        binding.skill_request.ability_instance_id,
                    )
            disp = disposition_by_id[binding.skill_request.request_id]
            resolution = resolution_by_external.get(binding.request_id)
            payload = dict(payload)
            # The action-request lifecycle status is consumed by phase and
            # snapshot boundaries. A player PASS is a resolved request, so
            # keep that lifecycle status in the shared resolution vocabulary
            # and retain the interpreter's more specific outcome separately.
            payload["status"] = "CONFIRMED"
            payload["rule_receipt_id"] = batch.batch_id
            payload["rule_group_id"] = group_id
            rule_dispositions = list(payload.get("rule_dispositions", ()))
            rule_dispositions.append(
                {
                    "skill_request_id": binding.skill_request.request_id,
                    "action_index": binding.action_index,
                    "action_code": binding.action_code,
                    "ability_instance_id": binding.skill_request.ability_instance_id,
                    "skill_id": binding.skill_request.skill_id,
                    "status": disp.status,
                    "reason": disp.reason,
                }
            )
            payload["rule_dispositions"] = rule_dispositions
            prior_rule_disposition = payload.get("rule_disposition")
            payload["rule_disposition"] = (
                disp.status if prior_rule_disposition in (None, disp.status) else "ACCEPTED"
            )
            if resolution is not None:
                payload["resolution_id"] = resolution.resolution_id
            request_payloads[binding.request_id] = payload
            actor = players_data[external.seat]
            if actor.get("current_request_id") == binding.request_id:
                actor["current_request_id"] = None

        for use_update in batch.use_updates:
            if not use_update.accepted:
                continue
            request = next(
                (item for item in requests if item.request_id == use_update.source_request_id),
                None,
            )
            if request is not None and request.origin == "HOST":
                continue
            instance = instances.get(use_update.ability_instance_id)
            if instance is None:
                raise ResolutionError(
                    "ABILITY_INSTANCE_INVALID", "use update references an unknown instance"
                )
            instance["uses_consumed"] = int(instance.get("uses_consumed", 0)) + 1
            seat = use_update.actor_seat
            actor = players_data[seat]
            authority_instance = next(
                (
                    item
                    for item in state.ability_instances
                    if item.ability_instance_id == use_update.ability_instance_id
                ),
                None,
            )
            granted = []
            for raw in actor.get("granted_abilities", ()):
                item = dict(raw)
                if (
                    authority_instance is not None
                    and item.get("ability_id") == authority_instance.grant_id
                    and item.get("action_code") == use_update.action_code
                ):
                    item["uses_consumed"] = int(item.get("uses_consumed", 0)) + 1
                granted.append(item)
            actor["granted_abilities"] = tuple(granted)

        rule_state_values = list(state.rule_state)
        for state_update in batch.state_updates:
            declaration = next(
                item
                for item in cast(ExecutionPackage, self._execution_package).state_declarations
                if item.skill_id == state_update.skill_id and item.key == state_update.key
            )
            scope_id = (
                f"seat-{state_update.seat}"
                if state_update.scope == "SEAT"
                else state_update.ability_instance_id
                if state_update.scope == "ABILITY"
                else None
            )
            expires_at_round = None
            expires_at_hook = None
            if state_update.expiry_policy in {"ROUND_END", "NEXT_NIGHT_START"}:
                # Rounds advance only at the ordinary victory boundary. This
                # makes expiry discrete and recoverable from the frozen game
                # phase; no wall-clock timer is involved.
                expires_at_round = state.round_no + 1
            if state_update.expiry_policy == "NEXT_NIGHT_START":
                expires_at_hook = GamePhase.NIGHT_TEAM_CHAT.value
            value_data = {
                "scope": state_update.scope,
                "scope_id": scope_id,
                "key": state_update.key,
                "value_type": declaration.value_type,
                "value": json.loads(json.dumps(state_update.value)),
                "source_batch_id": batch.batch_id,
                "skill_id": state_update.skill_id,
                "source_request_id": state_update.source_request_id,
                "source_rule_id": state_update.source_rule_id,
                "source_ability_instance_id": state_update.source_ability_instance_id,
                "expiry_policy": state_update.expiry_policy,
                "expires_at_round": expires_at_round,
                "expires_at_hook": expires_at_hook,
            }
            rule_state_values = [
                item
                for item in rule_state_values
                if not (
                    item.scope == state_update.scope
                    and item.scope_id == scope_id
                    and item.skill_id == state_update.skill_id
                    and item.key == state_update.key
                )
            ]
            rule_state_values.append(RuleStateValue.model_validate(value_data))
        data["rule_state"] = tuple(_rule_state_payload(item) for item in rule_state_values)

        # RESOURCE_DELTA effects and ordinary skill costs share the same
        # resource balance, so apply both in this one reducer before checking
        # the frozen package bounds.
        resource_deltas: dict[tuple[int, str], int] = {}
        for cost in batch.cost_updates:
            key = (cost.actor_seat, cost.resource_id)
            resource_deltas[key] = resource_deltas.get(key, 0) - cost.amount
        for resource_update in batch.resource_updates:
            source_instance = next(
                (
                    item
                    for item in state.ability_instances
                    if item.ability_instance_id == resource_update.source_ability_instance_id
                ),
                None,
            )
            if source_instance is None or (
                resource_update.seat != source_instance.actor_seat
                and resource_update.seat not in resource_update.authorized_targets
            ):
                raise ResolutionError(
                    "RULE_TARGET_INVALID", "resource update target is not authorized"
                )
            key = (resource_update.seat, resource_update.resource_id)
            resource_deltas[key] = resource_deltas.get(key, 0) + resource_update.delta
        execution_package = cast(ExecutionPackage, self._execution_package)
        resource_bounds: dict[str, tuple[int, int | None]] = {
            item.resource_id: (item.min_value, item.max_value)
            for item in execution_package.resource_declarations
        }
        # Schema-1 skill costs predate RESOURCE_DELTA declarations. Their
        # exact resource and amount are frozen on SkillSpec and validated in
        # _validate_rule_batch; allow those controlled deductions to use a
        # zero lower bound without granting the skill arbitrary deltas.
        for cost_skill in execution_package.skills:
            for cost_spec in cost_skill.usage.costs:
                resource_bounds.setdefault(cost_spec.resource_id, (0, None))
        for (seat, resource_id), delta in resource_deltas.items():
            bounds = resource_bounds.get(resource_id)
            if bounds is None:
                raise ResolutionError(
                    "RESOURCE_INVALID", "resource update is outside the frozen declaration"
                )
            balances = dict(players_data[seat].get("skill_resources", {}))
            current = balances.get(resource_id, 0)
            low, high = bounds
            updated = current + delta
            if type(current) is not int or updated < low or (high is not None and updated > high):
                raise ResolutionError(
                    "RESOURCE_UNAVAILABLE", "rule batch resource balance is outside its bounds"
                )
            balances[resource_id] = updated
            players_data[seat]["skill_resources"] = balances

        player_field_values = {"role_id", "faction_id", "victory_group_id", "chat_group_ids"}
        for player_update in batch.player_updates:
            if player_update.player_field not in player_field_values:
                raise ResolutionError(
                    "PLAYER_FIELD_INVALID", "rule update names an unsupported player field"
                )
            source_instance = next(
                (
                    item
                    for item in state.ability_instances
                    if item.ability_instance_id == player_update.source_ability_instance_id
                ),
                None,
            )
            if source_instance is None or (
                player_update.seat not in player_update.authorized_targets
                and player_update.seat != source_instance.actor_seat
            ):
                raise ResolutionError(
                    "RULE_TARGET_INVALID", "player field update target is not authorized"
                )
            value: object = player_update.value
            if player_update.player_field == "chat_group_ids" and isinstance(value, list):
                value = tuple(value)
            players_data[player_update.seat][player_update.player_field] = value

        relation_values = list(state.rule_relations)
        for relation_update in batch.relation_updates:
            source_instance = next(
                (
                    item
                    for item in state.ability_instances
                    if item.ability_instance_id == relation_update.source_ability_instance_id
                ),
                None,
            )
            if source_instance is None or any(
                seat not in relation_update.authorized_targets
                and seat != source_instance.actor_seat
                for seat in (relation_update.source_seat, relation_update.target_seat)
            ):
                raise ResolutionError("RULE_TARGET_INVALID", "relation endpoint is not authorized")
            relation_values = [
                item for item in relation_values if item.relation_id != relation_update.relation_id
            ]
            if relation_update.operation == "ADD":
                expires_at_round = None
                expires_at_hook = None
                if relation_update.expiry_policy == "ROUND_END":
                    expires_at_round = state.round_no + 1
                elif relation_update.expiry_policy == "NEXT_NIGHT_START":
                    expires_at_round = state.round_no + 1
                    expires_at_hook = GamePhase.NIGHT_TEAM_CHAT.value
                relation_values.append(
                    RuleRelationValue(
                        relation_id=relation_update.relation_id,
                        relation_type=relation_update.relation_type,
                        source_seat=relation_update.source_seat,
                        target_seat=relation_update.target_seat,
                        source_skill_id=relation_update.source_skill_id,
                        source_rule_id=relation_update.source_rule_id,
                        source_request_id=relation_update.source_request_id,
                        created_round=state.round_no,
                        source_ability_instance_id=relation_update.source_ability_instance_id,
                        expiry_policy=relation_update.expiry_policy or "NEVER",
                        expires_at_round=expires_at_round,
                        expires_at_hook=expires_at_hook,
                    )
                )
        data["rule_relations"] = tuple(item.model_dump(mode="python") for item in relation_values)

        for ability_update in batch.ability_updates:
            target_skill = next(
                (
                    skill
                    for skill in cast(ExecutionPackage, self._execution_package).skills
                    if skill.skill_id == ability_update.skill_id
                ),
                None,
            )
            source_instance = next(
                (
                    item
                    for item in state.ability_instances
                    if item.ability_instance_id == ability_update.source_ability_instance_id
                ),
                None,
            )
            if (
                target_skill is None
                or ability_update.grant_id not in {grant.grant_id for grant in target_skill.grants}
                or source_instance is None
                or ability_update.target_seat not in ability_update.authorized_targets
                and ability_update.target_seat != source_instance.actor_seat
            ):
                raise ResolutionError(
                    "ABILITY_UPDATE_INVALID", "ability update is outside frozen authorization"
                )
            active = [
                index
                for index, item in enumerate(instances.values())
                if item.get("actor_seat") == ability_update.target_seat
                and item.get("skill_id") == ability_update.skill_id
                and item.get("grant_id") == ability_update.grant_id
                and item.get("enabled") is True
                and item.get("consumed") is not True
            ]
            if ability_update.operation == "REVOKE":
                if len(active) != 1:
                    raise ResolutionError(
                        "ABILITY_REVOKE_INVALID", "revoke must name one active grant instance"
                    )
                instance_id = tuple(instances)[active[0]]
                instance = instances[instance_id]
                instance["enabled"] = False
                # Legacy paths consult PlayerState grants. Remove only the
                # same grant identity; another grant sharing its action code
                # must retain its own authorization and usage counter.
                actor = players_data[ability_update.target_seat]
                actor["granted_abilities"] = tuple(
                    item
                    for raw in actor.get("granted_abilities", ())
                    if (item := dict(raw)).get("ability_id") != ability_update.grant_id
                )
                actor["granted_trigger_abilities"] = tuple(
                    item
                    for raw in actor.get("granted_trigger_abilities", ())
                    if (item := dict(raw)).get("ability_id") != ability_update.grant_id
                )
                continue
            new_instance_id = ability_update.ability_instance_id
            if (
                not isinstance(new_instance_id, str)
                or not new_instance_id
                or new_instance_id in instances
            ):
                raise ResolutionError(
                    "ABILITY_GRANT_INVALID", "grant requires a new stable instance identity"
                )
            if active:
                raise ResolutionError(
                    "ABILITY_GRANT_INVALID", "grant would duplicate an active ability instance"
                )
            instances[new_instance_id] = AbilityInstanceState(
                ability_instance_id=new_instance_id,
                skill_id=target_skill.skill_id,
                grant_id=ability_update.grant_id,
                action_code=target_skill.action_code,
                actor_seat=ability_update.target_seat,
                grant_kind="TRIGGER" if target_skill.trigger is not None else "ACTIVE",
                uses_consumed=0,
                consumed=False,
                enabled=True,
            ).model_dump(mode="json")

        for effect in batch.effects:
            if (
                not effect.applied
                or effect.effect_type != "SET_CAN_VOTE"
                or effect.target_seat is None
            ):
                continue
            players_data[effect.target_seat]["can_vote"] = effect.value
        for outcome in batch.mortality:
            if outcome.deceased:
                player = players_data[outcome.seat]
                if player.get("alive") is not True:
                    raise ResolutionError(
                        "PLAYER_ALREADY_RESOLVED", "rule batch kills a player already dead"
                    )
                player["alive"] = False
                player["death_cause"] = outcome.death_cause

        # Trigger actions consume their exact bound legacy trigger in the
        # same replacement, including an explicit PASS.
        for seat, (ability_id, _request_id, instance_id) in trigger_window_bindings.items():
            actor = players_data[seat]
            replaced = []
            found = False
            for raw in actor.get("granted_trigger_abilities", ()):
                item = dict(raw)
                if item.get("ability_id") == ability_id:
                    if item.get("consumed") is True:
                        raise ResolutionError(
                            "TRIGGER_ALREADY_CONSUMED", "trigger was already consumed"
                        )
                    item["consumed"] = True
                    found = True
                replaced.append(item)
            if not found:
                raise ResolutionError("TRIGGER_NOT_GRANTED", "bound trigger grant is missing")
            actor["granted_trigger_abilities"] = tuple(replaced)
            instance = instances.get(instance_id)
            if (
                instance is None
                or instance.get("actor_seat") != seat
                or instance.get("grant_kind") != "TRIGGER"
            ):
                raise ResolutionError(
                    "TRIGGER_NOT_GRANTED",
                    "bound trigger ability instance is missing",
                )
            instance["consumed"] = True
            instance["enabled"] = False

        consumed_trigger_keys: set[tuple[int, str]] = set()
        for effect in batch.effects:
            if effect.effect_type != "CONSUME_ABILITY" or not effect.applied:
                continue
            if effect.target_seat is None or not isinstance(effect.value, str) or not effect.value:
                raise ResolutionError(
                    "ABILITY_CONSUMPTION_INVALID",
                    "CONSUME_ABILITY requires an assigned target and stable ability ID",
                )
            trigger_key = (effect.target_seat, effect.value)
            if trigger_key in consumed_trigger_keys:
                raise ResolutionError(
                    "ABILITY_CONSUMPTION_AMBIGUOUS",
                    "one rule batch cannot consume the same trigger more than once",
                )
            consumed_trigger_keys.add(trigger_key)
            target_data = players_data.get(effect.target_seat)
            if target_data is None:
                raise ResolutionError(
                    "ABILITY_CONSUMPTION_TARGET_INVALID",
                    "CONSUME_ABILITY target is not assigned",
                )
            trigger_records = [
                (index, dict(raw))
                for index, raw in enumerate(target_data.get("granted_trigger_abilities", ()))
                if isinstance(raw, Mapping) and raw.get("ability_id") == effect.value
            ]
            if len(trigger_records) != 1:
                raise ResolutionError(
                    "ABILITY_CONSUMPTION_INVALID",
                    "CONSUME_ABILITY does not name exactly one granted trigger ability",
                )
            trigger_index, trigger_record = trigger_records[0]
            if trigger_record.get("consumed") is True:
                raise ResolutionError(
                    "ABILITY_ALREADY_CONSUMED", "the named trigger ability was already consumed"
                )
            matched_instances = [
                (instance_id, instance)
                for instance_id, instance in instances.items()
                if instance.get("actor_seat") == effect.target_seat
                and instance.get("grant_kind") == "TRIGGER"
                and instance.get("grant_id") == effect.value
            ]
            if len(matched_instances) > 1:
                raise ResolutionError(
                    "ABILITY_CONSUMPTION_AMBIGUOUS",
                    "CONSUME_ABILITY maps to multiple executable trigger instances",
                )
            trigger_record["consumed"] = True
            trigger_values = list(target_data.get("granted_trigger_abilities", ()))
            trigger_values[trigger_index] = trigger_record
            target_data["granted_trigger_abilities"] = tuple(trigger_values)
            if matched_instances:
                _instance_id, instance = matched_instances[0]
                instance["consumed"] = True
                instance["enabled"] = False

        data["players"] = {
            seat: PlayerState.model_validate(value) for seat, value in players_data.items()
        }
        data["ability_instances"] = tuple(
            AbilityInstanceState.model_validate(value) for value in instances.values()
        )
        data["action_requests"] = request_payloads
        for resolution in resolutions:
            existing = next(
                (
                    item
                    for item in data["resolutions"]
                    if isinstance(item, dict)
                    and item.get("resolution_id") == resolution.resolution_id
                ),
                None,
            )
            if existing is None:
                data["resolutions"] = (*data["resolutions"], resolution.model_dump(mode="json"))

        history = tuple(
            RuleUseRecord.model_validate(item.model_dump(mode="python"))
            for item in batch.history_updates
        )
        facts = tuple(
            RuleFactRecord.model_validate(
                {
                    **item.model_dump(mode="python"),
                    "data": json.loads(json.dumps(item.data)),
                }
            )
            for item in (*batch.outcomes, *batch.facts)
            if item.fact_type.upper() != "DEATH_CONFIRMED"
        )
        confirmed_death_facts = self._confirmed_death_facts(state, batch)
        # Persist exactly one canonical row for each newly confirmed death.
        # It reuses the interpreter fact ID when available and keeps all
        # applied causal effect/request provenance in its data payload.
        facts = (*facts, *confirmed_death_facts)
        digest = hashlib.sha256(
            json.dumps(
                batch.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        external_request_ids = tuple(
            sorted(
                {item.request_id for item in bindings}
                | {item.request_id for item in requests if item.origin == "HOST"}
            )
        )
        actors = tuple(
            sorted(
                {
                    _stored_action_request(state, item).seat
                    for item in (binding.request_id for binding in bindings)
                }
                | {item.actor_seat for item in requests if item.origin == "HOST"}
            )
        )
        skill_ids = tuple(
            sorted(
                {item.skill_request.skill_id for item in bindings if item.skill_request.skill_id}
                | {item.skill_id for item in requests if item.skill_id}
            )
        )
        action_codes = tuple(
            sorted(
                {item.action_code for item in bindings}
                | {item.action_code for item in requests if item.origin == "HOST"}
            )
        )
        ledger = RuleLedgerEntry(
            batch_id=batch.batch_id,
            package_id=batch.package_id,
            group_id=group_id,
            timing=timing,
            read_revision=state.state_revision,
            committed_revision=state.state_revision + 1,
            round_no=state.round_no,
            request_ids=external_request_ids,
            actor_seats=actors,
            skill_ids=skill_ids,
            action_codes=action_codes,
            history_updates=history,
            facts=facts,
            outcome_digest=digest,
            created_at=timestamp,
        )
        receipt = RuleCommitReceipt(
            batch_id=batch.batch_id,
            package_id=batch.package_id,
            group_id=group_id,
            timing=timing,
            read_revision=state.state_revision,
            committed_revision=state.state_revision + 1,
            request_ids=external_request_ids,
            occurrence_ids=tuple(
                sorted(
                    {
                        item.trigger_occurrence_id
                        for item in requests
                        if item.trigger_occurrence_id is not None
                    }
                )
            ),
            outcome_digest=digest,
        )
        if any(item.batch_id == batch.batch_id for item in state.rule_receipts):
            raise ResolutionError("RULE_BATCH_REPLAY", "rule batch was already committed")
        data["rule_ledger"] = (*data["rule_ledger"], _rule_ledger_payload(ledger))
        data["rule_receipts"] = (*data["rule_receipts"], receipt.model_dump(mode="python"))
        deferred_disclosures = self._deferred_rule_disclosures(
            state,
            batch,
            requests,
            timing=timing,
        )
        events = self._rule_projection_events(
            state,
            batch,
            requests,
            timestamp=timestamp,
            next_revision=state.state_revision + 1,
            hook_id=timing,
        )
        if events:
            _validate_new_events(state, events, next_revision=state.state_revision + 1)
            data["events"] = (*_typed_events(state), *events)
        candidate_state = GameState.model_validate(data)
        return_point = self._rule_return_point(state, requests, bindings)
        new_occurrences = self._queue_confirmed_rule_facts(
            candidate_state,
            tuple(facts),
            source_batch_id=batch.batch_id,
        )
        queued_ids = {item.occurrence_id for item in candidate_state.rule_trigger_queue}
        queue = (
            *candidate_state.rule_trigger_queue,
            *(item for item in new_occurrences if item.occurrence_id not in queued_ids),
        )
        new_boundaries = self._rule_boundaries_for_deaths(
            state,
            batch,
            confirmed_death_facts,
            return_point,
            timing=timing,
            timestamp=timestamp,
        )
        boundaries = (*candidate_state.rule_boundaries, *new_boundaries)
        existing_cursor = state.rule_workflow_cursor
        has_pending_work = any(
            item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
            for item in candidate_state.rule_trigger_queue
        ) or any(item.is_pending for item in candidate_state.rule_boundaries)
        independent_workflow = existing_cursor is None or (
            existing_cursor.status in {"IDLE", "RETURN_READY"} and not has_pending_work
        )
        cursor = (
            RuleWorkflowCursor(
                budget_limit=(existing_cursor.budget_limit if existing_cursor is not None else 512)
            )
            if independent_workflow
            else existing_cursor
        )
        assert cursor is not None
        flow_update = batch.flow_updates[0] if batch.flow_updates else None
        pending_queue = any(item.status in {"QUEUED", "READY", "WAITING_CHOICE"} for item in queue)
        pending_boundaries = tuple(item for item in boundaries if item.is_pending)
        cursor = cursor.model_copy(
            update={
                "cursor_id": cursor.cursor_id
                or _stable_rule_identifier("workflow", state.game_id, state.round_no, group_id),
                "settlement_group_id": group_id,
                "return_point": return_point,
                "next_logical_window_id": return_point.logical_window_id,
                "pending_flow_action": (
                    flow_update.action if flow_update is not None else cursor.pending_flow_action
                ),
                "pending_boundary_id": (
                    pending_boundaries[0].boundary_id if pending_boundaries else None
                ),
                "status": "DRAINING" if pending_queue or pending_boundaries else "RETURN_READY",
                "error_code": None,
            }
        )
        data = _state_data(candidate_state)
        data["rule_deferred_disclosures"] = tuple(
            item.model_dump(mode="python")
            for item in (*state.rule_deferred_disclosures, *deferred_disclosures)
        )
        data["rule_trigger_queue"] = tuple(item.model_dump(mode="python") for item in queue)
        data["rule_boundaries"] = tuple(item.model_dump(mode="python") for item in boundaries)
        data["rule_workflow_cursor"] = cursor.model_dump(mode="python")
        return GameState.model_validate(data)

    def _required_rule_window_submitters(
        self,
        state: GameState,
        window: ActionWindow,
    ) -> frozenset[int]:
        """Resolve personal and chat-group collection obligations from the package."""

        rule_occurrence = self._rule_occurrence_for_window(state, window)
        if rule_occurrence is not None:
            return frozenset({rule_occurrence.actor_seat})
        package = self._execution_package
        if package is None or not package.skills:
            return frozenset(window.allowed_seats)
        skills_by_code = {
            skill.action_code: skill
            for skill in package.skills
            if skill.action_code in window.allowed_action_codes
            and window.phase.value in skill.timing
            and (not skill.window_ids or window.logical_window_id in skill.window_ids)
        }
        individual: set[int] = set()
        group_candidates: dict[tuple[int, str], list[int]] = {}
        for seat in window.allowed_seats:
            player = state.players.get(seat)
            if player is None or not player.alive:
                continue
            active_codes = {
                instance.action_code
                for instance in state.ability_instances
                if instance.actor_seat == seat and instance.enabled and not instance.consumed
            }
            eligible = [skill for code, skill in skills_by_code.items() if code in active_codes]
            for skill in eligible:
                if skill.coordination_scope == "INDIVIDUAL":
                    individual.add(seat)
                    continue
                groups = player.chat_group_ids or (f"seat-{seat}",)
                for group in groups:
                    group_candidates.setdefault((skill.action_code, group), []).append(seat)
        required = set(individual)
        required.update(min(seats) for seats in group_candidates.values() if seats)
        # A window with no matching live executable participant is still a
        # collector boundary. It has no synthetic PASS obligation.
        return frozenset(required)

    async def complete_rule_window(
        self,
        window_id: str,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> GameState:
        """Freeze one rule window's input collection without settling it."""

        timestamp = _aware_commit_time(now)
        async with self._lock:
            state = self._state
            _revision_check(state, expected_revision)
            raw = state.action_windows.get(window_id)
            if raw is None:
                raise EventCommitError("WINDOW_NOT_FOUND: rule window is not installed")
            try:
                window = _load_action_window(raw)
            except (TypeError, ValueError) as exc:
                raise EventCommitError("WINDOW_INVALID: rule window is malformed") from exc
            if window.closed_at is not None:
                raise EventCommitError("WINDOW_CLOSED: rule window is already settled")
            if window.collection_complete_at is not None:
                return state
            pending_inputs = tuple(
                payload
                for payload in state.action_requests.values()
                if isinstance(payload, Mapping)
                and payload.get("window_id") == window_id
                and payload.get("status") == "PENDING"
            )
            if any(
                isinstance(payload, Mapping)
                and payload.get("window_id") == window_id
                and payload.get("status")
                in {"OPEN", "REQUESTED", "SUBMITTING", "IN_FLIGHT", "PROCESSING"}
                for payload in state.action_requests.values()
            ):
                raise EventCommitError("WINDOW_INPUT_IN_FLIGHT: an action request is unfinished")
            if window.phase is GamePhase.NIGHT_TEAM_CHAT:
                # Team chat is authorized by the persisted serial speech queue,
                # not by an executable SkillSpec or a synthetic PASS request.
                # The moderator flow performs the frozen speaker/plan checks;
                # the manager independently requires that the queue was
                # started and fully drained before accepting this boundary.
                if state.current_queue is None:
                    raise EventCommitError(
                        "WINDOW_INPUT_MISSING: team speech queue has not started"
                    )
                if state.current_queue or state.serial_turn is not None:
                    raise EventCommitError(
                        "WINDOW_INPUT_IN_FLIGHT: team speech queue is not complete"
                    )
            elif not window.collection_only:
                submitted_seats = {
                    seat for payload in pending_inputs if type(seat := payload.get("seat")) is int
                }
                expected_seats = self._required_rule_window_submitters(state, window)
                legacy_empty_resolve = (
                    self._execution_package is not None
                    and not self._execution_package.window_metadata
                    and window.phase is GamePhase.NIGHT_RESOLVE
                    and window.allowed_action_codes == (299,)
                    and window.allow_pass
                    and not expected_seats
                    and not pending_inputs
                )
                if (
                    self._execution_package is not None
                    and self._execution_package.skills
                    and not expected_seats
                    and not legacy_empty_resolve
                ):
                    raise EventCommitError(
                        "WINDOW_INVALID: interactive window has no frozen eligible ability instance"
                    )
                missing = expected_seats.difference(submitted_seats)
                if missing:
                    raise EventCommitError(
                        "WINDOW_INPUT_MISSING: every eligible participant must "
                        "submit an action or PASS"
                    )
            if (
                self._execution_package is not None
                and self._execution_package.window_metadata
                and window.phase is not GamePhase.TRIGGER_ACTION
            ):
                logical_id = window.logical_window_id
                metadata = tuple(self._execution_package.window_metadata)
                row = next((item for item in metadata if item.window_id == logical_id), None)
                if row is None:
                    raise EventCommitError("WINDOW_INVALID: logical window is not frozen")
                if row.phase != window.phase.value:
                    raise EventCommitError(
                        "WINDOW_INVALID: phase differs from frozen window metadata"
                    )
                later = next((item for item in metadata if item.order == row.order + 1), None)
                expected_next = later.window_id if later is not None else None
                if window.next_window_id != expected_next:
                    raise EventCommitError(
                        "WINDOW_INVALID: successor differs from frozen window order"
                    )
            # Existing windows are recursively frozen inside GameState; do
            # not overwrite the thawed `_state_data` JSON with those tuples.
            windows = json.loads(json.dumps(state.action_windows))
            windows[window_id] = window.model_copy(
                update={"collection_complete_at": timestamp}
            ).model_dump(mode="json")
            basic_night_window = (
                self._execution_package is not None
                and not self._execution_package.window_metadata
                and window.phase
                in {
                    GamePhase.NIGHT_TEAM_CHAT,
                    GamePhase.NIGHT_ACTION,
                    GamePhase.NIGHT_RESOLVE,
                }
            )
            group_id = window.settlement_group_id or (
                f"night:{state.round_no}" if basic_night_window else window.window_id
            )
            if basic_night_window and window.settlement_group_id is None:
                # Upgrade an old preinstalled physical window at this manager
                # commit boundary. The legacy phase order supplies the only
                # allowed grouping proof when the frozen A summary is empty.
                window = window.model_copy(update={"settlement_group_id": group_id})
                windows[window_id] = window.model_copy(
                    update={"collection_complete_at": timestamp}
                ).model_dump(mode="json")
            group_window_ids = tuple(
                sorted(
                    key
                    for key, raw_window in windows.items()
                    if isinstance(raw_window, Mapping)
                    and (raw_window.get("settlement_group_id") or key) == group_id
                    and raw_window.get("closed_at") is None
                )
            )
            cursor = state.rule_workflow_cursor or RuleWorkflowCursor()
            pending_occurrences = any(
                item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                for item in state.rule_trigger_queue
            )
            pending_boundaries = any(item.is_pending for item in state.rule_boundaries)
            if (
                cursor.status in {"IDLE", "RETURN_READY"}
                and not pending_occurrences
                and not pending_boundaries
            ):
                # A newly opened collection starts a fresh causal budget.
                # Subsequent windows in the same COLLECTING group retain it.
                cursor = RuleWorkflowCursor(budget_limit=cursor.budget_limit)
            cursor = cursor.model_copy(
                update={
                    "cursor_id": cursor.cursor_id
                    or _stable_rule_identifier("workflow", state.game_id, state.round_no, group_id),
                    "settlement_group_id": group_id,
                    "active_window_ids": group_window_ids,
                    "completed_collection_window_ids": tuple(
                        dict.fromkeys((*cursor.completed_collection_window_ids, window_id))
                    ),
                    "status": "COLLECTING",
                    "error_code": None,
                }
            )
            data = _state_data(state)
            data["action_windows"] = windows
            data["rule_workflow_cursor"] = cursor.model_dump(mode="python")
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_rule_group(
        self,
        group_id: str,
        request_ids: Iterable[str] | None = None,
        *,
        occurrence_ids: Iterable[str] = (),
        candidate_batch: ResolutionBatch | None = None,
        expected_revision: int,
        now: datetime | None = None,
    ) -> GameState:
        """Re-plan and atomically commit one fully collected frozen rule group."""

        if not isinstance(group_id, str) or not group_id:
            raise EventCommitError("RULE_GROUP_INVALID: group ID is required")
        timestamp = _aware_commit_time(now)
        raw_occurrences = tuple(occurrence_ids)
        supplied_occurrences = tuple(sorted(set(raw_occurrences)))
        if len(supplied_occurrences) != len(raw_occurrences):
            raise EventCommitError("RULE_OCCURRENCE_INVALID: occurrence IDs must be unique")
        raw_request_ids = None if request_ids is None else tuple(request_ids)
        supplied_request_ids = (
            None if raw_request_ids is None else tuple(sorted(set(raw_request_ids)))
        )
        if raw_request_ids is not None and len(supplied_request_ids or ()) != len(raw_request_ids):
            raise EventCommitError("REQUEST_INVALID: request IDs must be unique")

        async with self._lock:
            state = self._state
            _revision_check(state, expected_revision)
            if self._rules is None or self._execution_package is None:
                raise ResolutionError(
                    "RULE_PACKAGE_MISSING", "game has no pinned execution package"
                )
            prior_receipts = tuple(
                item for item in state.rule_receipts if item.group_id == group_id
            )
            if prior_receipts:
                if len(prior_receipts) != 1:
                    raise ResolutionError(
                        "RULE_GROUP_REPLAY", "group has multiple durable commit receipts"
                    )
                receipt = prior_receipts[0]
                if receipt.package_id != self._execution_package.package_id:
                    raise ResolutionError(
                        "RULE_GROUP_REPLAY", "group receipt belongs to another package"
                    )
                if supplied_request_ids is not None and supplied_request_ids != receipt.request_ids:
                    raise ResolutionError(
                        "RULE_GROUP_REPLAY", "request IDs differ from the committed group"
                    )
                if supplied_occurrences and supplied_occurrences != receipt.occurrence_ids:
                    raise ResolutionError(
                        "RULE_GROUP_REPLAY", "occurrence IDs differ from the committed group"
                    )
                if candidate_batch is not None:
                    candidate_digest = hashlib.sha256(
                        json.dumps(
                            candidate_batch.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    if candidate_digest != receipt.outcome_digest:
                        raise ResolutionError(
                            "RULE_GROUP_REPLAY", "candidate batch differs from the committed group"
                        )
                return state
            windows: dict[str, ActionWindow] = {}
            for key, raw_window in state.action_windows.items():
                try:
                    window = _load_action_window(raw_window)
                except (TypeError, ValueError) as exc:
                    raise EventCommitError(
                        "WINDOW_INVALID: stored action window is malformed"
                    ) from exc
                if window.settlement_group_id == group_id or window.window_id == group_id:
                    windows[key] = window
            package = self._execution_package
            if package.window_metadata:
                group_mapping = package.window_settlement_groups
                expected_logical = tuple(
                    row.window_id
                    for row in package.window_metadata
                    if (group_id == f"night:{state.round_no}" and not group_mapping)
                    or (
                        group_mapping
                        and group_id == f"night:{state.round_no}:{group_mapping.get(row.window_id)}"
                    )
                )
                if expected_logical:
                    by_logical: dict[str, list[ActionWindow]] = {}
                    for window in windows.values():
                        if window.logical_window_id is not None:
                            by_logical.setdefault(window.logical_window_id, []).append(window)
                    missing_logical = [
                        logical_id
                        for logical_id in expected_logical
                        if not by_logical.get(logical_id)
                    ]
                    if missing_logical:
                        raise EventCommitError(
                            "WINDOW_GROUP_INCOMPLETE: frozen settlement group has "
                            "uninstalled windows"
                        )
                    for logical_id in expected_logical:
                        physical = by_logical[logical_id]
                        if any(
                            window.settlement_group_id != group_id
                            or window.collection_complete_at is None
                            or window.closed_at is not None
                            for window in physical
                        ):
                            raise EventCommitError(
                                "WINDOW_GROUP_INCOMPLETE: every frozen group window "
                                "must be collected"
                            )
                    rows = {row.window_id: row for row in package.window_metadata}
                    for logical_id in expected_logical:
                        for dependency in rows[logical_id].depends_on:
                            dependency_group = (
                                f"night:{state.round_no}"
                                if not group_mapping
                                else (f"night:{state.round_no}:{group_mapping.get(dependency)}")
                            )
                            dependency_windows = [
                                raw_dependency
                                for raw_dependency in state.action_windows.values()
                                if isinstance(raw_dependency, Mapping)
                                and raw_dependency.get("logical_window_id") == dependency
                                and raw_dependency.get("settlement_group_id") == dependency_group
                            ]
                            if not dependency_windows:
                                raise EventCommitError(
                                    "WINDOW_DEPENDENCY_MISSING: frozen predecessor has "
                                    "no durable window"
                                )
                            same_group = dependency in expected_logical
                            if same_group:
                                ready = all(
                                    _load_action_window(item).collection_complete_at is not None
                                    for item in dependency_windows
                                )
                            else:
                                ready = all(
                                    _load_action_window(item).closed_at is not None
                                    for item in dependency_windows
                                )
                            if not ready:
                                raise EventCommitError(
                                    "WINDOW_DEPENDENCY_OPEN: frozen predecessor is not settled"
                                )
            for window in windows.values():
                if window.closed_at is None and window.collection_complete_at is None:
                    raise EventCommitError(
                        "WINDOW_COLLECTION_OPEN: every window in the rule group must be complete"
                    )
            physical_ids = set(windows)
            pending_ids = tuple(
                sorted(
                    request_id
                    for request_id, payload in state.action_requests.items()
                    if isinstance(payload, Mapping)
                    and payload.get("status") == "PENDING"
                    and (
                        payload.get("window_id") in physical_ids
                        or (not physical_ids and request_id in (supplied_request_ids or ()))
                    )
                )
            )
            request_ids_to_commit = (
                pending_ids if supplied_request_ids is None else supplied_request_ids
            )
            if set(request_ids_to_commit) != set(pending_ids):
                raise ResolutionError(
                    "RESOLUTION_INCOMPLETE", "request IDs must cover every pending group request"
                )
            occurrences_by_id = {item.occurrence_id: item for item in state.rule_trigger_queue}
            selected_occurrences: list[RuleTriggerOccurrence] = []
            for occurrence_id in supplied_occurrences:
                occurrence = occurrences_by_id.get(occurrence_id)
                if occurrence is None or occurrence.status not in {
                    "QUEUED",
                    "READY",
                    "WAITING_CHOICE",
                }:
                    raise ResolutionError(
                        "RULE_OCCURRENCE_INVALID", "occurrence is absent or already consumed"
                    )
                cursor = state.rule_workflow_cursor
                if cursor is None or cursor.active_occurrence_id != occurrence_id:
                    raise ResolutionError(
                        "RULE_OCCURRENCE_INVALID", "occurrence is not the active workflow step"
                    )
                if occurrence.mode == "AUTOMATIC" and occurrence.status != "READY":
                    raise ResolutionError(
                        "RULE_OCCURRENCE_INVALID",
                        "automatic occurrence was not scheduled by advance",
                    )
                if occurrence.mode == "PLAYER_CHOICE" and occurrence.status != "WAITING_CHOICE":
                    raise ResolutionError(
                        "RULE_OCCURRENCE_INVALID", "choice occurrence was not installed by advance"
                    )
                selected_occurrences.append(occurrence)
            if not windows:
                cursor = state.rule_workflow_cursor
                active_auto = (
                    len(selected_occurrences) == 1
                    and selected_occurrences[0].mode == "AUTOMATIC"
                    and selected_occurrences[0].status == "READY"
                    and cursor is not None
                    and cursor.active_occurrence_id == selected_occurrences[0].occurrence_id
                    and cursor.status == "DRAINING"
                    and cursor.settlement_group_id == group_id
                )
                if not active_auto or supplied_request_ids not in {None, ()}:
                    raise ResolutionError(
                        "RULE_GROUP_INVALID",
                        "settlement requires an installed frozen window or active "
                        "automatic occurrence",
                    )
            auto_occurrences = tuple(
                item for item in selected_occurrences if item.mode == "AUTOMATIC"
            )
            if any(item.mode == "PLAYER_CHOICE" for item in selected_occurrences):
                if not request_ids_to_commit:
                    raise ResolutionError(
                        "RULE_OCCURRENCE_INVALID", "player-choice occurrence requires its request"
                    )

            if request_ids_to_commit:
                request_windows = [
                    _load_action_window(
                        state.action_windows[_stored_action_request(state, rid).window_id]
                    )
                    for rid in request_ids_to_commit
                ]
                timings = {window.phase.value for window in request_windows}
                if len(timings) != 1:
                    raise ResolutionError(
                        "TIMING_MISMATCH", "one settlement group must use one timing"
                    )
                timing = next(iter(timings))
            elif auto_occurrences:
                timing = GamePhase.TRIGGER_ACTION.value
            elif windows:
                timing = windows[sorted(windows)[0]].phase.value
            else:
                raise ResolutionError("RULE_GROUP_EMPTY", "rule group has no windows or requests")

            requests: list[SkillRequest] = []
            bindings: list[_RuleRequestBinding] = []
            if request_ids_to_commit:
                physical_group = self._rule_group_requests(
                    state,
                    request_ids_to_commit,
                    timing=timing,
                    group_id_override=group_id,
                )
                requests.extend(physical_group[0])
                bindings.extend(physical_group[1])
            for occurrence in auto_occurrences:
                request = self._automatic_skill_request(state, occurrence, group_id=group_id)
                requests.append(request)
            request_tuple = tuple(requests)
            try:
                batch = self._rules.plan(
                    state,
                    request_tuple,
                    group_id=group_id,
                    timing=timing,
                )
            except (RuleAdapterError, TypeError, ValueError) as exc:
                raise ResolutionError("RULE_PLAN_INVALID", str(exc)) from exc
            if candidate_batch is not None and candidate_batch != batch:
                raise ResolutionError(
                    "RULE_BATCH_INVALID", "candidate batch differs from locked package re-plan"
                )
            self._validate_rule_batch(
                state,
                batch,
                request_tuple,
                group_id=group_id,
                timing=timing,
            )
            candidate = self._apply_rule_batch(
                state,
                batch,
                request_tuple,
                tuple(bindings),
                (),
                group_id=group_id,
                timing=timing,
                timestamp=timestamp,
            )
            data = _state_data(candidate)
            action_windows = dict(data["action_windows"])
            for window_id, window in windows.items():
                if window.closed_at is None:
                    action_windows[window_id] = window.model_copy(
                        update={"closed_at": timestamp}
                    ).model_dump(mode="json")
            data["action_windows"] = action_windows
            completed = set(supplied_occurrences)
            completed.update(
                item.trigger_occurrence_id
                for item in request_tuple
                if item.trigger_occurrence_id is not None
            )
            queue = tuple(
                item.model_copy(update={"status": "COMPLETED"})
                if item.occurrence_id in completed
                else item
                for item in candidate.rule_trigger_queue
            )
            data["rule_trigger_queue"] = tuple(item.model_dump(mode="python") for item in queue)
            cursor = candidate.rule_workflow_cursor or RuleWorkflowCursor()
            data["rule_workflow_cursor"] = cursor.model_copy(
                update={
                    "settlement_group_id": group_id,
                    "active_occurrence_id": None,
                    "active_window_ids": tuple(sorted(windows)),
                    "status": "DRAINING"
                    if any(item.status in {"QUEUED", "READY", "WAITING_CHOICE"} for item in queue)
                    else "RETURN_READY",
                    "pending_boundary_id": None,
                }
            ).model_dump(mode="python")
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    def _has_terminal_night_boundary_source(
        self,
        state: GameState,
        boundary: RuleBoundary,
    ) -> bool:
        """Prove that a DAY_ANNOUNCE boundary resumes after the final night window."""

        point = boundary.return_point
        package = self._execution_package
        night_phases = {
            GamePhase.NIGHT_TEAM_CHAT,
            GamePhase.NIGHT_ACTION,
            GamePhase.NIGHT_RESOLVE,
        }
        cursor = state.rule_workflow_cursor
        already_staged = (
            point.day_no is not None
            and state.day_no == point.day_no + 1
            and state.phase is GamePhase.DAY_ANNOUNCE
            and cursor is not None
            and cursor.status == "WAITING_BOUNDARY"
            and cursor.pending_boundary_id == boundary.boundary_id
        )
        if (
            package is None
            or not package.window_metadata
            or point.phase is not GamePhase.DAY_ANNOUNCE
            or point.window_id is None
            or point.logical_window_id is not None
            or point.day_no is None
            or (state.day_no != point.day_no and not already_staged)
        ):
            return False

        raw_source = state.action_windows.get(point.window_id)
        if raw_source is None:
            return False
        try:
            source = _load_action_window(raw_source)
        except (TypeError, ValueError):
            return False
        if (
            source.game_id != state.game_id
            or source.phase not in night_phases
            or source.closed_at is None
            or source.logical_window_id is None
            or source.next_window_id is not None
            or (source.settlement_group_id or source.window_id) != boundary.source_group_id
        ):
            return False

        rows = tuple(package.window_metadata)
        source_row = next(
            (row for row in rows if row.window_id == source.logical_window_id),
            None,
        )
        if (
            source_row is None
            or source_row.phase != source.phase.value
            or source_row.order != max(row.order for row in rows)
        ):
            return False

        group_windows: list[ActionWindow] = []
        for raw in state.action_windows.values():
            if not isinstance(raw, Mapping):
                continue
            raw_group = raw.get("settlement_group_id") or raw.get("window_id")
            if raw_group != boundary.source_group_id:
                continue
            try:
                group_windows.append(_load_action_window(raw))
            except (TypeError, ValueError):
                return False
        if not group_windows or any(window.closed_at is None for window in group_windows):
            return False

        matching_ledgers = tuple(
            ledger
            for ledger in state.rule_ledger
            if ledger.batch_id == boundary.source_batch_id
            and ledger.group_id == boundary.source_group_id
            and ledger.timing in {phase.value for phase in night_phases}
            and ledger.round_no == state.round_no
            and set(boundary.death_fact_ids).issubset(
                {
                    fact.fact_id
                    for fact in ledger.facts
                    if fact.fact_type.upper() == "DEATH_CONFIRMED"
                }
            )
        )
        if len(matching_ledgers) != 1:
            return False
        ledger = matching_ledgers[0]
        return any(
            receipt.batch_id == ledger.batch_id
            and receipt.package_id == ledger.package_id
            and receipt.group_id == ledger.group_id
            and receipt.timing == ledger.timing
            and receipt.committed_revision == ledger.committed_revision
            and receipt.request_ids == ledger.request_ids
            and receipt.outcome_digest == ledger.outcome_digest
            for receipt in state.rule_receipts
        )

    async def advance_rule_workflow(
        self,
        *,
        expected_revision: int,
        now: datetime | None = None,
        hook_id: RuleHook | None = None,
    ) -> RuleWorkflowStep:
        """Advance the one durable trigger/cursor queue or return its boundary."""

        timestamp = _aware_commit_time(now)
        async with self._lock:
            state = self._state
            _revision_check(state, expected_revision)
            cursor = state.rule_workflow_cursor or RuleWorkflowCursor()
            if cursor.status == "ERROR":
                return RuleWorkflowStep(
                    kind="IDLE",
                    cursor_id=cursor.cursor_id,
                    queue_pending=True,
                    return_point=cursor.return_point,
                )
            if cursor.status == "COLLECTING":
                # Collection completion is not settlement. Only
                # commit_rule_group may close this cursor's frozen group and
                # move it into trigger draining/return; repeated runner polls
                # must preserve the collecting proof verbatim.
                return RuleWorkflowStep(
                    kind="IDLE",
                    cursor_id=cursor.cursor_id,
                    queue_pending=True,
                    return_point=cursor.return_point,
                )
            if (
                hook_id is None
                and cursor.status == "IDLE"
                and cursor.pending_flow_action is None
                and not any(
                    item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                    for item in state.rule_trigger_queue
                )
                and not any(item.is_pending for item in state.rule_boundaries)
            ):
                # Legacy callers poll the authority on ordinary empty turns.
                # An idle cursor has no workflow return edge to execute.
                return RuleWorkflowStep(
                    kind="IDLE",
                    cursor_id=cursor.cursor_id,
                    queue_pending=False,
                    return_point=cursor.return_point,
                )
            if hook_id is not None:
                foreign_pending = any(
                    item.status in {"QUEUED", "READY", "WAITING_CHOICE"} and item.hook_id != hook_id
                    for item in state.rule_trigger_queue
                )
                if foreign_pending:
                    return RuleWorkflowStep(
                        kind="IDLE",
                        cursor_id=cursor.cursor_id,
                        queue_pending=True,
                        return_point=cursor.return_point,
                    )
                resuming_hook = (
                    cursor.return_point is not None
                    and cursor.return_point.hook_id == hook_id
                    and cursor.status in {"DRAINING", "WAITING_CHOICE", "RETURN_READY"}
                    and any(item.hook_id == hook_id for item in state.rule_trigger_queue)
                )
                if not resuming_hook:
                    source_id, hook_return_point = self._ordinary_speech_hook_source(state, hook_id)
                    hook_occurrences = self._speech_hook_occurrences(
                        state, hook_id, source_id=source_id
                    )
                    existing_ids = {item.occurrence_id for item in state.rule_trigger_queue}
                    new_hook_occurrences = tuple(
                        item for item in hook_occurrences if item.occurrence_id not in existing_ids
                    )
                    if new_hook_occurrences:
                        if (
                            cursor.status in {"IDLE", "RETURN_READY"}
                            and not any(
                                item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                                for item in state.rule_trigger_queue
                            )
                            and not any(item.is_pending for item in state.rule_boundaries)
                        ):
                            # An ordinary speech hook is a new causal workflow
                            # unless it was installed as part of a pending chain.
                            cursor = RuleWorkflowCursor(budget_limit=cursor.budget_limit)
                        data = _state_data(state)
                        queue = (*state.rule_trigger_queue, *new_hook_occurrences)
                        next_cursor = cursor.model_copy(
                            update={
                                "cursor_id": cursor.cursor_id
                                or _stable_rule_identifier(
                                    "hook-workflow",
                                    state.game_id,
                                    state.day_no,
                                    hook_id,
                                    source_id,
                                ),
                                "return_point": hook_return_point,
                                "status": "DRAINING",
                            }
                        )
                        data["rule_trigger_queue"] = tuple(
                            item.model_dump(mode="python") for item in queue
                        )
                        data["rule_workflow_cursor"] = next_cursor.model_dump(mode="python")
                        state = GameState.model_validate(data)
                        cursor = state.rule_workflow_cursor or next_cursor
                    elif (
                        state.phase is GamePhase.DAY_SPEECH
                        and cursor.status in {"IDLE", "RETURN_READY"}
                        and not any(
                            item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                            for item in state.rule_trigger_queue
                        )
                        and not any(item.is_pending for item in state.rule_boundaries)
                    ):
                        # A valid speech boundary with no eligible hook skill
                        # is an empty poll. Do not route it through the return
                        # phase transition path or mutate the game.
                        return RuleWorkflowStep(
                            kind="IDLE",
                            cursor_id=cursor.cursor_id,
                            queue_pending=False,
                            return_point=hook_return_point,
                        )
            pending = sorted(
                (
                    item
                    for item in state.rule_trigger_queue
                    if item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                    and (hook_id is None or item.hook_id == hook_id)
                ),
                key=lambda item: (item.order, item.source_fact_id, item.ability_instance_id),
            )
            if (
                hook_id is not None
                and not pending
                and any(
                    item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                    for item in state.rule_trigger_queue
                )
            ):
                return RuleWorkflowStep(
                    kind="IDLE",
                    cursor_id=cursor.cursor_id,
                    queue_pending=True,
                    return_point=cursor.return_point,
                )
            if pending and pending[0].status in {"QUEUED", "READY"}:
                invalid_reason = self._rule_occurrence_invalid_reason(state, pending[0])
                if invalid_reason is not None:
                    skipped = pending[0].model_copy(update={"status": "FAILED"})
                    queue = tuple(
                        skipped if item.occurrence_id == skipped.occurrence_id else item
                        for item in state.rule_trigger_queue
                    )
                    next_cursor = cursor.model_copy(
                        update={
                            "active_occurrence_id": None,
                            "active_window_ids": (),
                            "steps_used": cursor.steps_used + 1,
                            "status": "DRAINING",
                            "error_code": None,
                        }
                    )
                    data = _state_data(state)
                    data["rule_trigger_queue"] = tuple(
                        item.model_dump(mode="python") for item in queue
                    )
                    data["rule_workflow_cursor"] = next_cursor.model_dump(mode="python")
                    data["moderator_audit"] = (
                        *data["moderator_audit"],
                        {
                            "operation": "RULE_OCCURRENCE_SKIPPED",
                            "occurrence_id": skipped.occurrence_id,
                            "source_fact_id": skipped.source_fact_id,
                            "ability_instance_id": skipped.ability_instance_id,
                            "skill_id": skipped.skill_id,
                            "reason": invalid_reason,
                            "base_revision": state.state_revision,
                            "committed_revision": state.state_revision + 1,
                            "created_at": timestamp.isoformat(),
                        },
                    )
                    data["state_revision"] = state.state_revision + 1
                    data["updated_at"] = timestamp
                    self._state = GameState.model_validate(data)
                    return RuleWorkflowStep(
                        kind="IDLE",
                        cursor_id=next_cursor.cursor_id,
                        queue_pending=True,
                        return_point=next_cursor.return_point,
                    )
            if pending and cursor.active_occurrence_id == pending[0].occurrence_id:
                active = pending[0]
                if active.mode == "AUTOMATIC" and active.status == "READY":
                    return self._workflow_step(
                        active, self._workflow_skill(active), cursor, "AUTOMATIC"
                    )
                if active.mode == "PLAYER_CHOICE" and active.status == "WAITING_CHOICE":
                    raw_window = state.action_windows.get(active.window_id or "")
                    if raw_window is None:
                        raise EventCommitError(
                            "RULE_OCCURRENCE_INVALID: waiting choice window is missing"
                        )
                    waiting_action_window = _load_action_window(raw_window)
                    return self._workflow_step(
                        active,
                        self._workflow_skill(active),
                        cursor,
                        "PLAYER_CHOICE",
                        waiting_action_window,
                    )
            if cursor.steps_used >= cursor.budget_limit and pending:
                next_cursor = cursor.model_copy(
                    update={"status": "ERROR", "error_code": "RULE_WORKFLOW_BUDGET_EXHAUSTED"}
                )
                data = _state_data(state)
                data["rule_workflow_cursor"] = next_cursor.model_dump(mode="python")
                data["moderator_audit"] = (
                    *data["moderator_audit"],
                    {
                        "operation": "RULE_WORKFLOW_ERROR",
                        "error_code": "RULE_WORKFLOW_BUDGET_EXHAUSTED",
                        "pending_occurrence_ids": [item.occurrence_id for item in pending],
                        "base_revision": state.state_revision,
                        "committed_revision": state.state_revision + 1,
                        "created_at": timestamp.isoformat(),
                    },
                )
                data["state_revision"] = state.state_revision + 1
                data["updated_at"] = timestamp
                self._state = GameState.model_validate(data)
                return RuleWorkflowStep(
                    kind="IDLE",
                    cursor_id=next_cursor.cursor_id,
                    queue_pending=True,
                    return_point=next_cursor.return_point,
                )
            if pending:
                occurrence = pending[0]
                skill = self._workflow_skill(occurrence)
                instance = next(
                    (
                        item
                        for item in state.ability_instances
                        if item.ability_instance_id == occurrence.ability_instance_id
                        and item.actor_seat == occurrence.actor_seat
                        and item.enabled
                        and not item.consumed
                    ),
                    None,
                )
                if instance is None:
                    raise EventCommitError("RULE_OCCURRENCE_INVALID: ability instance is inactive")
                if occurrence.mode == "AUTOMATIC":
                    next_occurrence = occurrence.model_copy(update={"status": "READY"})
                    queue = tuple(
                        next_occurrence if item.occurrence_id == occurrence.occurrence_id else item
                        for item in state.rule_trigger_queue
                    )
                    next_cursor = cursor.model_copy(
                        update={
                            "cursor_id": cursor.cursor_id
                            or _stable_rule_identifier(
                                "workflow", state.game_id, state.round_no, occurrence.occurrence_id
                            ),
                            "settlement_group_id": f"automatic-{occurrence.occurrence_id}",
                            "active_occurrence_id": occurrence.occurrence_id,
                            "steps_used": cursor.steps_used + 1,
                            "status": "DRAINING",
                            "error_code": None,
                        }
                    )
                    data = _state_data(state)
                    data["rule_trigger_queue"] = tuple(
                        item.model_dump(mode="python") for item in queue
                    )
                    data["rule_workflow_cursor"] = next_cursor.model_dump(mode="python")
                    data["state_revision"] = state.state_revision + 1
                    data["updated_at"] = timestamp
                    self._state = GameState.model_validate(data)
                    return self._workflow_step(occurrence, skill, next_cursor, "AUTOMATIC")

                raw_window = state.action_windows.get(occurrence.window_id or "")
                action_window: ActionWindow
                if raw_window is not None:
                    action_window = _load_action_window(raw_window)
                else:
                    action_window = self._build_trigger_action_window(
                        state, occurrence, skill, now=timestamp
                    )
                    action_windows = dict(_state_data(state)["action_windows"])
                    action_windows[action_window.window_id] = action_window.model_dump(mode="json")
                    data = _state_data(state)
                    data["action_windows"] = action_windows
                    updated_occurrence = occurrence.model_copy(
                        update={"status": "WAITING_CHOICE", "window_id": action_window.window_id}
                    )
                    data["rule_trigger_queue"] = tuple(
                        updated_occurrence.model_dump(mode="python")
                        if item.occurrence_id == occurrence.occurrence_id
                        else item.model_dump(mode="python")
                        for item in state.rule_trigger_queue
                    )
                    next_cursor = cursor.model_copy(
                        update={
                            "cursor_id": cursor.cursor_id
                            or _stable_rule_identifier(
                                "workflow", state.game_id, state.round_no, occurrence.occurrence_id
                            ),
                            "active_occurrence_id": occurrence.occurrence_id,
                            "active_window_ids": (action_window.window_id,),
                            "settlement_group_id": action_window.settlement_group_id,
                            "steps_used": cursor.steps_used + 1,
                            "status": "WAITING_CHOICE",
                            "error_code": None,
                        }
                    )
                    data["rule_workflow_cursor"] = next_cursor.model_dump(mode="python")
                    data["phase"] = GamePhase.TRIGGER_ACTION
                    data["pending_resolution"] = {
                        "operation": "RULE_TRIGGER",
                        "status": "RULE_TRIGGER_ACTION_REQUIRED",
                        "occurrence_id": occurrence.occurrence_id,
                        "source_fact_id": occurrence.source_fact_id,
                        "window_id": action_window.window_id,
                        "actor_seat": occurrence.actor_seat,
                        "base_revision": state.state_revision,
                    }
                    data["state_revision"] = state.state_revision + 1
                    data["updated_at"] = timestamp
                    self._state = GameState.model_validate(data)
                    return self._workflow_step(
                        updated_occurrence, skill, next_cursor, "PLAYER_CHOICE", action_window
                    )
                return self._workflow_step(
                    occurrence, skill, cursor, "PLAYER_CHOICE", action_window
                )

            boundary = None
            if cursor.status == "WAITING_BOUNDARY" and cursor.pending_boundary_id is not None:
                boundary = next(
                    (
                        item
                        for item in state.rule_boundaries
                        if item.boundary_id == cursor.pending_boundary_id
                    ),
                    None,
                )
            if boundary is None:
                boundary = next((item for item in state.rule_boundaries if item.is_pending), None)
            if boundary is not None:
                updated_boundaries = self._refresh_boundary_completion(state, boundary)
                refreshed = next(
                    (
                        item
                        for item in updated_boundaries
                        if item.boundary_id == boundary.boundary_id
                    ),
                    boundary,
                )
                data = _state_data(state)
                data["rule_boundaries"] = tuple(
                    item.model_dump(mode="python") for item in updated_boundaries
                )
                if not refreshed.is_pending:
                    refreshed = refreshed.model_copy(update={"completed_at": timestamp})
                    data["rule_boundaries"] = tuple(
                        item.model_dump(mode="python")
                        if item.boundary_id != refreshed.boundary_id
                        else refreshed.model_dump(mode="python")
                        for item in updated_boundaries
                    )
                    pending_occurrences = any(
                        item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                        for item in state.rule_trigger_queue
                    )
                    next_boundary = next(
                        (item for item in updated_boundaries if item.is_pending),
                        None,
                    )
                    next_cursor = cursor.model_copy(
                        update={
                            "pending_boundary_id": (
                                next_boundary.boundary_id if next_boundary is not None else None
                            ),
                            "status": (
                                "DRAINING"
                                if pending_occurrences or next_boundary is not None
                                else "RETURN_READY"
                            ),
                        }
                    )
                    data["rule_workflow_cursor"] = next_cursor.model_dump(mode="python")
                    data["state_revision"] = state.state_revision + 1
                    data["updated_at"] = timestamp
                    self._state = GameState.model_validate(data)
                    return RuleWorkflowStep(
                        kind="IDLE",
                        cursor_id=next_cursor.cursor_id,
                        queue_pending=pending_occurrences or next_boundary is not None,
                        return_point=next_cursor.return_point,
                    )
                next_cursor = cursor.model_copy(
                    update={
                        "pending_boundary_id": refreshed.boundary_id,
                        "status": "WAITING_BOUNDARY",
                        "active_occurrence_id": None,
                    }
                )
                data["rule_workflow_cursor"] = next_cursor.model_dump(mode="python")
                terminal_night_announcement = (
                    refreshed.return_point.phase is GamePhase.DAY_ANNOUNCE
                    and self._has_terminal_night_boundary_source(state, refreshed)
                )
                boundary_target_phase = (
                    GamePhase.DAY_ANNOUNCE
                    if terminal_night_announcement
                    else GamePhase.DAY_RESOLVE
                    if refreshed.return_point.phase is GamePhase.DAY_SPEECH
                    or (
                        refreshed.return_point.logical_window_id is not None
                        and refreshed.return_point.phase
                        in {
                            GamePhase.NIGHT_TEAM_CHAT,
                            GamePhase.NIGHT_ACTION,
                            GamePhase.NIGHT_RESOLVE,
                        }
                    )
                    else GamePhase.DAY_ANNOUNCE
                    if refreshed.return_point.phase
                    in {GamePhase.NIGHT_ACTION, GamePhase.NIGHT_RESOLVE, GamePhase.NIGHT_TEAM_CHAT}
                    else GamePhase.DAY_RESOLVE
                )
                if state.phase is not boundary_target_phase:
                    data["phase"] = boundary_target_phase
                    if (
                        boundary_target_phase is GamePhase.DAY_ANNOUNCE
                        and state.day_no == refreshed.return_point.day_no
                    ):
                        data["day_no"] = state.day_no + 1
                data["state_revision"] = state.state_revision + 1
                data["updated_at"] = timestamp
                self._state = GameState.model_validate(data)
                return RuleWorkflowStep(
                    kind="RETURN",
                    return_point=refreshed.return_point,
                    next_phase=boundary_target_phase,
                    queue_pending=False,
                    cursor_id=next_cursor.cursor_id,
                    boundary=refreshed,
                )

            target_phase: GamePhase | None = None
            if cursor.pending_flow_action == "ADVANCE_TO_NIGHT":
                target_phase = GamePhase.VICTORY_CHECK
            elif cursor.return_point is not None:
                target_phase = cursor.return_point.phase
            if target_phase is None:
                target_phase = state.phase
            if cursor.pending_flow_action == "ADVANCE_TO_NIGHT":
                return_allowed = target_phase is GamePhase.VICTORY_CHECK and can_transition(
                    state.phase, target_phase
                )
            else:
                return_allowed = self._valid_rule_workflow_return(state, cursor, target_phase)
            if not return_allowed:
                raise EventCommitError(
                    "PHASE_INVALID: workflow cannot return "
                    f"{state.phase.value} to {target_phase.value}"
                )
            next_cursor = cursor.model_copy(
                update={
                    "status": "IDLE",
                    "active_occurrence_id": None,
                    "active_window_ids": (),
                    "pending_boundary_id": None,
                    "pending_flow_action": None,
                    "error_code": None,
                }
            )
            data = _state_data(state)
            data["rule_workflow_cursor"] = next_cursor.model_dump(mode="python")
            data["pending_resolution"] = None
            if cursor.pending_flow_action == "ADVANCE_TO_NIGHT":
                if state.serial_turn is not None:
                    raise EventCommitError(
                        "RULE_FLOW_ACTION_BLOCKED: ordinary serial turn is still active"
                    )
                cancelled_queue = tuple(state.current_queue or ())
                data["current_queue"] = ()
                if cancelled_queue:
                    data["moderator_audit"] = (
                        *data["moderator_audit"],
                        {
                            "operation": "DAY_SPEECH_SUFFIX_CANCELLED",
                            "source_cursor_id": cursor.cursor_id,
                            "source_hook_id": (
                                cursor.return_point.hook_id
                                if cursor.return_point is not None
                                else None
                            ),
                            "source_serial_turn_id": (
                                cursor.return_point.serial_turn_id
                                if cursor.return_point is not None
                                else None
                            ),
                            "source_day_no": state.day_no,
                            "cancelled_seats": list(cancelled_queue),
                            "base_revision": state.state_revision,
                            "committed_revision": state.state_revision + 1,
                            "created_at": timestamp.isoformat(),
                        },
                    )
            # Keep the workflow cursor and the lifecycle edge in one atomic
            # state replacement. For standard edges, reuse the phase reducer
            # so day/round counters follow the same semantics as an ordinary
            # coordinator transition.
            intermediate = GameState.model_validate(data)
            if can_transition(state.phase, target_phase):
                returned = transition_phase(
                    intermediate,
                    target_phase,
                    expected_revision=state.state_revision,
                    now=timestamp,
                )
            else:
                data = _state_data(intermediate)
                data["phase"] = target_phase
                data["state_revision"] = state.state_revision + 1
                data["updated_at"] = timestamp
                returned = GameState.model_validate(data)
            if returned.phase is GamePhase.NIGHT_TEAM_CHAT or returned.round_no > state.round_no:
                returned_data = _state_data(returned)
                returned_data["rule_state"] = tuple(
                    _rule_state_payload(value)
                    for value in returned.rule_state
                    if not (
                        value.expires_at_round is not None
                        and value.expires_at_round <= returned.round_no
                        and (
                            value.expiry_policy == "ROUND_END"
                            or (
                                value.expiry_policy == "NEXT_NIGHT_START"
                                and returned.phase is GamePhase.NIGHT_TEAM_CHAT
                            )
                        )
                    )
                )
                returned_data["rule_relations"] = tuple(
                    value.model_dump(mode="python")
                    for value in returned.rule_relations
                    if not (
                        value.expires_at_round is not None
                        and value.expires_at_round <= returned.round_no
                        and (
                            value.expiry_policy == "ROUND_END"
                            or (
                                value.expiry_policy == "NEXT_NIGHT_START"
                                and returned.phase is GamePhase.NIGHT_TEAM_CHAT
                            )
                        )
                    )
                )
                returned = GameState.model_validate(returned_data)
            returned = self._release_due_rule_disclosures(
                returned,
                target_phase.value,
                None,
                timestamp=timestamp,
                next_revision=returned.state_revision,
            )
            self._state = returned
            return RuleWorkflowStep(
                kind="RETURN",
                return_point=next_cursor.return_point,
                next_phase=target_phase,
                queue_pending=False,
                cursor_id=next_cursor.cursor_id,
            )

    def _rule_occurrence_for_window(
        self,
        state: GameState,
        window: ActionWindow,
    ) -> RuleTriggerOccurrence | None:
        if self._execution_package is None or window.phase is not GamePhase.TRIGGER_ACTION:
            return None
        occurrence_id = window.visible_context.get("rule_occurrence_id")
        source_fact_id = window.visible_context.get("rule_source_fact_id")
        actor_seat = window.visible_context.get("rule_actor_seat")
        instance_id = window.visible_context.get("rule_ability_instance_id")
        skill_id = window.visible_context.get("rule_skill_id")
        action_code = window.visible_context.get("rule_action_code")
        if (
            not isinstance(occurrence_id, str)
            or not isinstance(source_fact_id, str)
            or type(actor_seat) is not int
            or not isinstance(instance_id, str)
            or not isinstance(skill_id, str)
            or type(action_code) is not int
        ):
            return None
        cursor = state.rule_workflow_cursor
        pending = state.pending_resolution
        if (
            state.phase is not GamePhase.TRIGGER_ACTION
            or cursor is None
            or cursor.status not in {"WAITING_CHOICE", "COLLECTING"}
            or cursor.active_occurrence_id != occurrence_id
            or window.window_id not in cursor.active_window_ids
            or cursor.settlement_group_id != window.settlement_group_id
            or not isinstance(pending, Mapping)
            or pending.get("operation") != "RULE_TRIGGER"
            or pending.get("status") != "RULE_TRIGGER_ACTION_REQUIRED"
            or pending.get("occurrence_id") != occurrence_id
            or pending.get("window_id") != window.window_id
            or pending.get("source_fact_id") != source_fact_id
            or pending.get("actor_seat") != actor_seat
        ):
            return None
        occurrence = next(
            (
                item
                for item in state.rule_trigger_queue
                if item.occurrence_id == occurrence_id
                and item.source_fact_id == source_fact_id
                and item.actor_seat == actor_seat
                and item.ability_instance_id == instance_id
                and item.skill_id == skill_id
                and item.status == "WAITING_CHOICE"
                and item.mode == "PLAYER_CHOICE"
            ),
            None,
        )
        if occurrence is None or occurrence.window_id != window.window_id:
            return None
        skill = next(
            (item for item in self._execution_package.skills if item.skill_id == skill_id), None
        )
        instance = next(
            (
                item
                for item in state.ability_instances
                if item.ability_instance_id == instance_id
                and item.actor_seat == actor_seat
                and item.skill_id == skill_id
                and item.enabled
                and not item.consumed
            ),
            None,
        )
        player = state.players.get(actor_seat)
        if (
            skill is None
            or instance is None
            or player is None
            or skill.action_code != action_code
            or window.game_id != state.game_id
            or window.session_epoch != player.session_epoch
            or window.min_actions != 1
            or window.max_actions != 1
        ):
            return None
        if window.allowed_seats != (actor_seat,) or window.allowed_role_ids:
            return None
        expected_codes = (skill.action_code, 299) if window.allow_pass else (skill.action_code,)
        if (
            window.allowed_action_codes != expected_codes
            or window.logical_window_id != occurrence.logical_window_id
        ):
            return None
        stored_window = state.action_windows.get(window.window_id)
        if stored_window is not None:
            try:
                if _load_action_window(stored_window) != window:
                    return None
            except (TypeError, ValueError):
                return None
        if occurrence.kind == "HOOK":
            if (
                occurrence.hook_id not in skill.hook_ids
                or skill.trigger is not None
                or window.hook_id != occurrence.hook_id
            ):
                return None
            cursor = state.rule_workflow_cursor
            return_point = cursor.return_point if cursor is not None else None
            if return_point is None or return_point.hook_id != occurrence.hook_id:
                return None
            if occurrence.hook_id == "DAY_SPEECH_BEFORE":
                if (
                    not state.current_queue
                    or state.current_queue[0] != return_point.speaker_seat
                    or return_point.serial_turn_id != f"before-{source_fact_id}"
                ):
                    return None
                expected_source = _stable_rule_identifier(
                    "speech-before",
                    state.game_id,
                    state.day_no,
                    return_point.speaker_seat,
                    ",".join(str(item) for item in state.current_queue),
                )
            else:
                last_turn = state.last_serial_turn
                if (
                    last_turn is None
                    or last_turn.request_id != return_point.serial_turn_id
                    or last_turn.seat != return_point.speaker_seat
                    or last_turn.event_ids != return_point.event_ids
                    or not any(
                        event.event_id in last_turn.event_ids
                        and event.event_type is EventType.SPEECH
                        and event.phase is GamePhase.DAY_SPEECH
                        and event.actor_seat == last_turn.seat
                        for event in _typed_events(state)
                    )
                ):
                    return None
                expected_source = _stable_rule_identifier(
                    "speech-after",
                    state.game_id,
                    state.day_no,
                    last_turn.request_id,
                    ",".join(str(item) for item in last_turn.event_ids),
                )
            if occurrence.source_fact_id != expected_source or occurrence.source_batch_id != (
                _stable_rule_identifier(
                    "hook-batch",
                    state.game_id,
                    state.day_no,
                    occurrence.hook_id,
                    expected_source,
                )
            ):
                return None
        else:
            source_entry = next(
                (
                    entry
                    for entry in state.rule_ledger
                    if entry.batch_id == occurrence.source_batch_id
                    and any(item.fact_id == source_fact_id for item in entry.facts)
                ),
                None,
            )
            fact = (
                next(
                    (item for item in source_entry.facts if item.fact_id == source_fact_id),
                    None,
                )
                if source_entry is not None
                else None
            )
            if fact is None:
                return None
            if skill.trigger is not None:
                if (
                    skill.trigger.mode != "PLAYER_CHOICE"
                    or fact.fact_type not in skill.trigger.fact_types
                ):
                    return None
                try:
                    observation = (
                        self._rules.observation(
                            state,
                            group_id=f"trigger-check:{occurrence.occurrence_id}",
                            timing=GamePhase.TRIGGER_ACTION.value,
                        )
                        if self._rules is not None
                        else None
                    )
                    if observation is None:
                        return None
                    observed_fact = DomainFact(
                        fact_id=fact.fact_id,
                        fact_type=fact.fact_type,
                        source_rule_id=fact.source_rule_id,
                        source_request_id=fact.source_request_id,
                        actor_seat=fact.actor_seat,
                        target_seat=fact.target_seat,
                        death_cause=fact.death_cause,
                        tags=fact.tags,
                        data=json.loads(json.dumps(fact.data)),
                    )
                    observation = observation.model_copy(
                        update={"facts": (*observation.facts, observed_fact)}
                    )
                    context = {
                        "actor": next(
                            item for item in observation.players if item.seat == actor_seat
                        ),
                        "target": next(
                            (item for item in observation.players if item.seat == fact.target_seat),
                            None,
                        ),
                        "source_fact": observed_fact,
                        "observation": observation,
                        "skill_state": {
                            item.key: item.value
                            for item in observation.skill_state
                            if item.ability_instance_id == instance_id
                        },
                        "item": None,
                    }
                    if not evaluate_predicate(skill.trigger.condition, context):
                        return None
                except (StopIteration, TypeError, ValueError):
                    return None
            else:
                legacy = next(
                    (
                        item
                        for item in player.granted_trigger_abilities
                        if item.action_code == skill.action_code and not item.consumed
                    ),
                    None,
                )
                if (
                    legacy is None
                    or sum(
                        item.action_code == skill.action_code and not item.consumed
                        for item in player.granted_trigger_abilities
                    )
                    != 1
                    or legacy.trigger.mode is not TriggerMode.PLAYER_CHOICE
                ):
                    return None
                trigger_event = legacy.trigger.event.value
                if trigger_event == TriggerEvent.DEATH_CONFIRMED.value:
                    if (
                        fact.fact_type not in {"DEATH_CONFIRMED", "death_confirmed"}
                        or fact.target_seat != actor_seat
                        or player.alive
                        or fact.death_cause not in legacy.trigger.allowed_death_causes
                        or player.death_cause != fact.death_cause
                    ):
                        return None
                elif fact.fact_type != trigger_event:
                    return None
        supplied_targets = window.visible_context.get("candidate_seats")
        public_target_set = tuple(sorted(state.players))
        if (
            not isinstance(supplied_targets, (list, tuple))
            or tuple(supplied_targets) != public_target_set
        ):
            return None
        targets_by_action = window.visible_context.get("targets_by_action")
        action_targets = (
            targets_by_action.get(str(skill.action_code))
            if isinstance(targets_by_action, Mapping)
            else None
        )
        if (
            not isinstance(action_targets, (list, tuple))
            or tuple(action_targets) != public_target_set
        ):
            return None
        return occurrence

    def _is_trusted_speech_hook_request(
        self,
        state: GameState,
        request: SkillRequest,
    ) -> bool:
        """Permit TRIGGER_ACTION timing only for a proven DAY_SPEECH hook."""

        if (
            request.hook_id not in {"DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"}
            or request.window_id is None
            or request.trigger_occurrence_id is None
            or request.source_fact_id is None
        ):
            return False
        raw_window = state.action_windows.get(request.window_id)
        if raw_window is None:
            return False
        try:
            window = _load_action_window(raw_window)
        except (TypeError, ValueError):
            return False
        occurrence = self._rule_occurrence_for_window(state, window)
        if (
            occurrence is None
            or occurrence.kind != "HOOK"
            or occurrence.occurrence_id != request.trigger_occurrence_id
            or occurrence.source_fact_id != request.source_fact_id
            or occurrence.actor_seat != request.actor_seat
            or occurrence.ability_instance_id != request.ability_instance_id
            or occurrence.skill_id != request.skill_id
            or occurrence.hook_id != request.hook_id
        ):
            return False
        skill = (
            next(
                (
                    item
                    for item in self._execution_package.skills
                    if item.skill_id == occurrence.skill_id
                ),
                None,
            )
            if self._execution_package is not None
            else None
        )
        return bool(
            skill is not None
            and skill.trigger is None
            and request.hook_id in skill.hook_ids
            and GamePhase.DAY_SPEECH.value in skill.timing
            and self._trusted_active_hook_return_point(state, occurrence)
        )

    @staticmethod
    def _trusted_active_hook_return_point(
        state: GameState,
        occurrence: RuleTriggerOccurrence,
    ) -> bool:
        """Verify the ordinary speech boundary that created this occurrence."""

        cursor = state.rule_workflow_cursor
        return_point = cursor.return_point if cursor is not None else None
        if (
            cursor is None
            or cursor.status not in {"WAITING_CHOICE", "COLLECTING"}
            or return_point is None
            or return_point.phase is not GamePhase.DAY_SPEECH
            or return_point.hook_id != occurrence.hook_id
            or return_point.day_no != state.day_no
            or occurrence.hook_id not in {"DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"}
        ):
            return False
        if occurrence.hook_id == "DAY_SPEECH_BEFORE":
            if (
                state.serial_turn is not None
                or not state.current_queue
                or state.current_queue[0] != return_point.speaker_seat
                or return_point.serial_turn_id != f"before-{occurrence.source_fact_id}"
                or return_point.event_ids
            ):
                return False
            expected_source = _stable_rule_identifier(
                "speech-before",
                state.game_id,
                state.day_no,
                return_point.speaker_seat,
                ",".join(str(item) for item in state.current_queue),
            )
        else:
            last_turn = state.last_serial_turn
            if (
                state.serial_turn is not None
                or last_turn is None
                or last_turn.request_id != return_point.serial_turn_id
                or last_turn.seat != return_point.speaker_seat
                or last_turn.event_ids != return_point.event_ids
                or not any(
                    event.event_id in last_turn.event_ids
                    and event.event_type is EventType.SPEECH
                    and event.phase is GamePhase.DAY_SPEECH
                    and event.actor_seat == last_turn.seat
                    for event in _typed_events(state)
                )
            ):
                return False
            expected_source = _stable_rule_identifier(
                "speech-after",
                state.game_id,
                state.day_no,
                last_turn.request_id,
                ",".join(str(item) for item in last_turn.event_ids),
            )
        return occurrence.source_fact_id == expected_source

    def _trigger_target_seats(
        self,
        state: GameState,
        occurrence: RuleTriggerOccurrence,
        skill: SkillSpec,
    ) -> tuple[int, ...]:
        if self._rules is None:
            return ()
        observation = self._rules.observation(
            state,
            group_id=f"trigger-targets:{occurrence.occurrence_id}",
            timing=GamePhase.TRIGGER_ACTION.value,
        )
        actor = next(
            (item for item in observation.players if item.seat == occurrence.actor_seat),
            None,
        )
        if actor is None:
            return ()
        skill_state = {
            item.key: item.value
            for item in observation.skill_state
            if item.ability_instance_id == occurrence.ability_instance_id
        }
        return tuple(
            sorted(
                set(
                    select_seats(
                        skill.targets.selector,
                        {
                            "actor": actor,
                            "observation": observation,
                            "skill_state": skill_state,
                            "request_targets": (),
                        },
                    )
                )
            )
        )

    def _workflow_skill(self, occurrence: RuleTriggerOccurrence) -> SkillSpec:
        package = self._execution_package
        if package is None:
            raise EventCommitError("RULE_PACKAGE_MISSING: game has no pinned execution package")
        skill = next(
            (item for item in package.skills if item.skill_id == occurrence.skill_id), None
        )
        if skill is None:
            raise EventCommitError("RULE_OCCURRENCE_INVALID: trigger skill is not frozen")
        return skill

    def _rule_occurrence_invalid_reason(
        self,
        state: GameState,
        occurrence: RuleTriggerOccurrence,
    ) -> str | None:
        """Check that a queued source and its frozen ability are still usable."""

        package = self._execution_package
        if package is None or self._rules is None:
            return "execution_package_missing"
        skill = next(
            (item for item in package.skills if item.skill_id == occurrence.skill_id), None
        )
        if skill is None:
            return "skill_not_frozen"
        instance = next(
            (
                item
                for item in state.ability_instances
                if item.ability_instance_id == occurrence.ability_instance_id
                and item.actor_seat == occurrence.actor_seat
                and item.skill_id == occurrence.skill_id
                and item.enabled
                and not item.consumed
                and item.grant_id in {grant.grant_id for grant in skill.grants}
            ),
            None,
        )
        if instance is None:
            return "ability_instance_inactive"
        capacity_reason = self._rule_instance_capacity_reason(state, instance, skill)
        if capacity_reason is not None:
            return capacity_reason
        actor = state.players.get(occurrence.actor_seat)
        if actor is None:
            return "actor_missing"

        source_fact: RuleFactRecord | None = None
        domain_fact: DomainFact | None = None
        if occurrence.kind == "HOOK":
            if (
                occurrence.mode != "PLAYER_CHOICE"
                or instance.grant_kind != "ACTIVE"
                or skill.mode != "PLAYER"
                or skill.trigger is not None
                or occurrence.hook_id not in skill.hook_ids
                or GamePhase.DAY_SPEECH.value not in skill.timing
                or not actor.alive
            ):
                return "hook_skill_no_longer_eligible"
            cursor = state.rule_workflow_cursor
            if cursor is None or cursor.return_point is None:
                return "hook_return_point_missing"
            try:
                actual_source, actual_return = self._ordinary_speech_hook_source(
                    state,
                    occurrence.hook_id,
                )
            except EventCommitError:
                return "ordinary_speech_source_expired"
            if (
                actual_source != occurrence.source_fact_id
                or cursor.return_point != actual_return
                or occurrence.source_batch_id
                != _stable_rule_identifier(
                    "hook-batch",
                    state.game_id,
                    state.day_no,
                    occurrence.hook_id,
                    actual_source,
                )
            ):
                return "ordinary_speech_source_mismatch"
            timing = GamePhase.DAY_SPEECH.value
        else:
            trigger = skill.trigger
            source_fact = next(
                (
                    fact
                    for entry in state.rule_ledger
                    if entry.batch_id == occurrence.source_batch_id
                    for fact in entry.facts
                    if fact.fact_id == occurrence.source_fact_id
                ),
                None,
            )
            if source_fact is None:
                return "trigger_source_fact_expired"
            if trigger is not None:
                if (
                    trigger.mode != occurrence.mode
                    or instance.grant_kind != "TRIGGER"
                    or source_fact.fact_type not in trigger.fact_types
                ):
                    return "trigger_skill_no_longer_eligible"
            else:
                legacy_grant = self._legacy_trigger_grant(state, instance, skill)
                if (
                    legacy_grant is None
                    or legacy_grant.trigger.event is not TriggerEvent.DEATH_CONFIRMED
                    or legacy_grant.trigger.mode.value != occurrence.mode
                    or source_fact.fact_type.lower() != TriggerEvent.DEATH_CONFIRMED.value.lower()
                    or source_fact.target_seat != occurrence.actor_seat
                    or actor.alive
                    or source_fact.death_cause not in legacy_grant.trigger.allowed_death_causes
                    or actor.death_cause != source_fact.death_cause
                ):
                    return "legacy_trigger_source_no_longer_eligible"
            domain_fact = DomainFact(
                fact_id=source_fact.fact_id,
                fact_type=source_fact.fact_type,
                source_rule_id=source_fact.source_rule_id,
                source_request_id=source_fact.source_request_id,
                actor_seat=source_fact.actor_seat,
                target_seat=source_fact.target_seat,
                death_cause=source_fact.death_cause,
                tags=source_fact.tags,
                data=json.loads(json.dumps(source_fact.data)),
            )
            timing = GamePhase.TRIGGER_ACTION.value

        try:
            observation = self._rules.observation(
                state,
                group_id=f"occurrence-eligibility:{occurrence.occurrence_id}",
                timing=timing,
            )
        except (RuleAdapterError, TypeError, ValueError):
            return "eligibility_observation_invalid"
        observed_players = {item.seat: item for item in observation.players}
        if occurrence.kind == "TRIGGER" and skill.trigger is not None:
            observed_fact = domain_fact
            if observed_fact is None:
                return "trigger_source_fact_expired"
            trigger_context = {
                "actor": observed_players.get(occurrence.actor_seat),
                "target": (
                    observed_players.get(observed_fact.target_seat)
                    if observed_fact.target_seat is not None
                    else None
                ),
                "source_fact": observed_fact,
                "observation": observation,
                "skill_state": {
                    item.key: item.value
                    for item in observation.skill_state
                    if item.ability_instance_id == instance.ability_instance_id
                },
                "item": None,
            }
            try:
                if not evaluate_predicate(skill.trigger.condition, trigger_context):
                    return "trigger_condition_no_longer_met"
            except (TypeError, ValueError):
                return "trigger_condition_invalid"
        if not self._rule_skill_condition_is_eligible(
            observation,
            skill,
            instance,
            actor=observed_players.get(occurrence.actor_seat),
            target=(
                observed_players.get(domain_fact.target_seat)
                if domain_fact is not None and domain_fact.target_seat is not None
                else None
            ),
            source_fact=domain_fact,
            request={
                "request_id": occurrence.request_id or occurrence.occurrence_id,
                "action_code": skill.action_code,
                "passed": False,
                "actor_seat": occurrence.actor_seat,
                "target_count": 0,
                "parameters": {},
                "window_id": occurrence.window_id,
                "logical_window_id": occurrence.logical_window_id,
                "hook_id": occurrence.hook_id,
            },
        ):
            return "skill_condition_no_longer_met"
        return None

    def _automatic_skill_request(
        self,
        state: GameState,
        occurrence: RuleTriggerOccurrence,
        *,
        group_id: str,
    ) -> SkillRequest:
        skill = self._workflow_skill(occurrence)
        if skill.trigger is None or skill.trigger.mode != "AUTOMATIC":
            raise EventCommitError(
                "RULE_OCCURRENCE_INVALID: automatic execution requires a frozen automatic trigger"
            )
        facts = {fact.fact_id: fact for entry in state.rule_ledger for fact in entry.facts}
        fact = facts.get(occurrence.source_fact_id)
        if fact is None or fact.fact_type not in skill.trigger.fact_types:
            raise EventCommitError("RULE_OCCURRENCE_INVALID: source fact is not confirmed")
        instance = next(
            (
                item
                for item in state.ability_instances
                if item.ability_instance_id == occurrence.ability_instance_id
                and item.actor_seat == occurrence.actor_seat
                and item.skill_id == skill.skill_id
                and item.enabled
                and not item.consumed
            ),
            None,
        )
        if instance is None:
            raise EventCommitError("RULE_OCCURRENCE_INVALID: ability instance is inactive")
        return SkillRequest(
            request_id=f"automatic-{occurrence.occurrence_id}",
            ability_instance_id=instance.ability_instance_id,
            skill_id=skill.skill_id,
            action_code=skill.action_code,
            actor_seat=occurrence.actor_seat,
            origin="AUTOMATIC",
            trigger_occurrence_id=occurrence.occurrence_id,
            source_fact_id=occurrence.source_fact_id,
            window_id=occurrence.window_id or f"auto-{occurrence.occurrence_id}",
            logical_window_id=occurrence.logical_window_id
            or (skill.window_ids[0] if skill.window_ids else None),
            hook_id=occurrence.hook_id,
        )

    def _build_trigger_action_window(
        self,
        state: GameState,
        occurrence: RuleTriggerOccurrence,
        skill: SkillSpec,
        *,
        now: datetime,
    ) -> ActionWindow:
        if self._rules is None or self._execution_package is None:
            raise EventCommitError("RULE_PACKAGE_MISSING: game has no pinned execution package")
        player = state.players.get(occurrence.actor_seat)
        if player is None:
            raise EventCommitError("RULE_OCCURRENCE_INVALID: trigger actor is unassigned")
        try:
            authorized_targets = self._trigger_target_seats(state, occurrence, skill)
        except (TypeError, ValueError) as exc:
            raise EventCommitError("RULE_TARGET_INVALID: trigger selector failed") from exc
        if any(seat not in state.players for seat in authorized_targets):
            raise EventCommitError("RULE_TARGET_INVALID: trigger selector returned an unknown seat")
        action_spec = next(
            (
                item
                for item in self._execution_package.actions
                if item.action_code == skill.action_code
            ),
            None,
        )
        if action_spec is None:
            raise EventCommitError("RULE_ACTION_INVALID: trigger action is not frozen")
        allow_pass = bool(action_spec.allow_pass and skill.mode == "PLAYER")
        allowed_codes = (skill.action_code, 299) if allow_pass else (skill.action_code,)
        window_id = f"rule-trigger-{occurrence.occurrence_id}"
        if len(window_id) > 128:
            window_id = f"trigger-{_stable_rule_identifier(occurrence.occurrence_id)}"
        logical_window_id = occurrence.logical_window_id or (
            skill.window_ids[0] if skill.window_ids else None
        )
        # Keep the private selector result inside the manager. A narrowed list
        # would disclose hidden roles, factions, or unannounced deaths. The
        # request reducer re-runs that selector against the frozen snapshot.
        public_candidates = tuple(sorted(state.players))
        return ActionWindow(
            window_id=window_id,
            game_id=state.game_id,
            session_epoch=player.session_epoch,
            phase=GamePhase.TRIGGER_ACTION,
            allowed_seats=(occurrence.actor_seat,),
            allowed_action_codes=allowed_codes,
            min_actions=1,
            max_actions=1,
            allow_pass=allow_pass,
            opened_at=now,
            settlement_group_id=f"trigger-{occurrence.occurrence_id}",
            logical_window_id=logical_window_id,
            hook_id=occurrence.hook_id,
            visible_context={
                "rule_occurrence_id": occurrence.occurrence_id,
                "rule_source_fact_id": occurrence.source_fact_id,
                "rule_actor_seat": occurrence.actor_seat,
                "rule_ability_instance_id": occurrence.ability_instance_id,
                "rule_skill_id": occurrence.skill_id,
                "rule_action_code": skill.action_code,
                "candidate_seats": list(public_candidates),
                "targets_by_action": {str(skill.action_code): list(public_candidates)},
            },
            max_submissions_per_seat=1,
        )

    @staticmethod
    def _workflow_step(
        occurrence: RuleTriggerOccurrence,
        skill: SkillSpec,
        cursor: RuleWorkflowCursor,
        kind: Literal["AUTOMATIC", "PLAYER_CHOICE"],
        action_window: ActionWindow | None = None,
    ) -> RuleWorkflowStep:
        return RuleWorkflowStep(
            kind=kind,
            occurrence_id=occurrence.occurrence_id,
            source_fact_id=occurrence.source_fact_id,
            actor_seat=occurrence.actor_seat,
            ability_instance_id=occurrence.ability_instance_id,
            skill_id=occurrence.skill_id,
            mode=occurrence.mode,
            window_id=action_window.window_id
            if action_window is not None
            else occurrence.window_id,
            hook_id=occurrence.hook_id,
            action_window=action_window,
            return_point=cursor.return_point,
            queue_pending=True,
            cursor_id=cursor.cursor_id,
        )

    def _refresh_boundary_completion(
        self,
        state: GameState,
        selected: RuleBoundary,
    ) -> tuple[RuleBoundary, ...]:
        """Rebuild boundary completion from persisted host evidence."""

        completed_seats = set(selected.last_words_completed_seats)
        try:
            events_by_id = {event.event_id: event for event in _typed_events(state)}
        except EventCommitError:
            # A malformed or legacy event log is not proof that a boundary
            # speech was committed. Existing tuple state remains authoritative.
            events_by_id = {}
        for audit in state.moderator_audit:
            if (
                audit.get("operation") != "RULE_BOUNDARY_LAST_WORDS_SPEECH_COMPLETE"
                or audit.get("boundary_id") != selected.boundary_id
                or audit.get("source_group_id") != selected.source_group_id
                or audit.get("source_batch_id") != selected.source_batch_id
                or _frozen_string_tuple(audit.get("death_fact_ids")) != selected.death_fact_ids
            ):
                continue
            seat = audit.get("seat")
            event_id = audit.get("speech_event_id")
            request_id = audit.get("request_id")
            logical_request_id = audit.get("logical_request_id")
            session_epoch = audit.get("session_epoch")
            attempt_no = audit.get("attempt_no")
            if (
                type(seat) is not int
                or seat not in selected.last_words_seats
                or type(event_id) is not int
                or not isinstance(request_id, str)
                or not request_id
                or not isinstance(logical_request_id, str)
                or not logical_request_id
                or type(session_epoch) is not int
                or session_epoch < 0
                or type(attempt_no) is not int
                or attempt_no < 1
            ):
                continue
            event = events_by_id.get(event_id)
            if (
                event is None
                or event.event_type is not EventType.SPEECH
                or event.channel is not Channel.PUBLIC
                or not isinstance(event.payload, PublicSpeechPayload)
                or event.actor_seat != seat
                or event.payload.speaker_seat != seat
                or event.correlation_id != logical_request_id
                or audit.get("phase") != event.phase.value
                or audit.get("speech_event_revision") != event.state_revision
                or audit.get("committed_revision") != event.state_revision
            ):
                continue
            completed_seats.add(seat)
        badge_completed = False
        marker = state.sheriff_badge
        if selected.sheriff_badge_required and isinstance(marker, Mapping):
            source_seat = marker.get("source_seat")
            request_id = marker.get("request_id")
            marker_death_fact_ids = _frozen_string_tuple(marker.get("death_fact_ids"))
            marker_matches = (
                marker.get("status") == "COMPLETE"
                and marker.get("rule_boundary_id") == selected.boundary_id
                and marker.get("source_group_id") == selected.source_group_id
                and marker.get("source_batch_id") == selected.source_batch_id
                and marker_death_fact_ids == selected.death_fact_ids
                and type(source_seat) is int
                and source_seat in selected.death_seats
                and isinstance(request_id, str)
                and bool(request_id)
            )
            completion_audited = any(
                audit.get("operation") == "SHERIFF_BADGE_COMPLETE"
                and audit.get("rule_boundary_id") == selected.boundary_id
                and audit.get("source_group_id") == selected.source_group_id
                and audit.get("source_batch_id") == selected.source_batch_id
                and _frozen_string_tuple(audit.get("death_fact_ids")) == selected.death_fact_ids
                and audit.get("request_id") == request_id
                and audit.get("source_seat") == source_seat
                for audit in state.moderator_audit
            )
            if marker_matches and completion_audited:
                badge_completed = True
        updated = selected.model_copy(
            update={
                "last_words_completed_seats": tuple(
                    seat for seat in selected.last_words_seats if seat in completed_seats
                ),
                "sheriff_badge_completed": badge_completed,
            }
        )
        return tuple(
            updated if boundary.boundary_id == selected.boundary_id else boundary
            for boundary in state.rule_boundaries
        )

    @staticmethod
    def _rule_work_blocks_lifecycle(state: GameState) -> bool:
        """Return whether a queued rule settlement or boundary must still run."""

        cursor = state.rule_workflow_cursor
        if cursor is not None and (
            cursor.status
            in {"COLLECTING", "DRAINING", "WAITING_CHOICE", "WAITING_BOUNDARY", "ERROR"}
            or cursor.pending_flow_action is not None
        ):
            return True
        if any(
            item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
            for item in state.rule_trigger_queue
        ) or any(item.is_pending for item in state.rule_boundaries):
            return True
        for raw_window in state.action_windows.values():
            try:
                window = _load_action_window(raw_window)
            except (TypeError, ValueError):
                return True
            if window.collection_complete_at is not None and window.closed_at is None:
                return True
        return False

    def _is_unsettled_group_successor(
        self,
        state: GameState,
        target_phase: GamePhase,
    ) -> bool:
        """Allow only the next frozen window phase during group collection."""

        cursor = state.rule_workflow_cursor
        package = self._execution_package
        if (
            cursor is None
            or cursor.status != "COLLECTING"
            or not cursor.settlement_group_id
            or package is None
        ):
            return False
        if not package.window_metadata:
            # A/basic packages intentionally keep an empty metadata summary
            # to preserve their pinned identity. Their fixed legacy sequence
            # proves only Team Chat -> Action -> Resolve. The current durable
            # collection and canonical group prove the edge; caller window
            # IDs and next-window claims do not.
            phase_order = (
                GamePhase.NIGHT_TEAM_CHAT,
                GamePhase.NIGHT_ACTION,
                GamePhase.NIGHT_RESOLVE,
            )
            try:
                current_index = phase_order.index(state.phase)
            except ValueError:
                return False
            if (
                current_index + 1 >= len(phase_order)
                or phase_order[current_index + 1] is not target_phase
                or not can_transition(state.phase, target_phase)
                or state.serial_turn is not None
                or any(
                    item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                    for item in state.rule_trigger_queue
                )
                or any(item.is_pending for item in state.rule_boundaries)
            ):
                return False
            if state.phase is GamePhase.NIGHT_TEAM_CHAT and state.current_queue != ():
                return False
            group_windows = tuple(
                _load_action_window(raw)
                for raw in state.action_windows.values()
                if isinstance(raw, Mapping)
                and (raw.get("settlement_group_id") or raw.get("window_id"))
                == cursor.settlement_group_id
            )
            if not group_windows or any(
                item.closed_at is not None or item.collection_complete_at is None
                for item in group_windows
            ):
                return False
            phase_ranks = {phase: rank for rank, phase in enumerate(phase_order)}
            group_ranks = tuple(phase_ranks.get(item.phase, -1) for item in group_windows)
            if (
                min(group_ranks) < 0
                or max(group_ranks) != current_index
                or not set(cursor.active_window_ids).issubset(
                    {item.window_id for item in group_windows}
                )
                or not cursor.active_window_ids
            ):
                return False
            group_window_ids = {item.window_id for item in group_windows}
            if any(
                isinstance(payload, Mapping)
                and payload.get("window_id") in group_window_ids
                and payload.get("status")
                in {"OPEN", "REQUESTED", "SUBMITTING", "IN_FLIGHT", "PROCESSING"}
                for payload in state.action_requests.values()
            ):
                return False
            # Accepted requests remain PENDING until the entire frozen group
            # is committed. They are safe across an adjacent same-group phase
            # only when their source window is already durably collected; an
            # open or in-flight request above still blocks progression.
            if any(
                isinstance(payload, Mapping)
                and payload.get("window_id") in group_window_ids
                and payload.get("status") == "PENDING"
                and not any(
                    item.window_id == payload.get("window_id")
                    and item.collection_complete_at is not None
                    and item.closed_at is None
                    for item in group_windows
                )
                for payload in state.action_requests.values()
            ):
                return False
            if cursor.settlement_group_id == f"night:{state.round_no}":
                return True
            # A snapshot created before the canonical group upgrade can use
            # its physical source-window ID. In that narrow case a durable,
            # unbound successor window is required as additional proof.
            if any(
                item.logical_window_id is not None or item.next_window_id is not None
                for item in group_windows
            ):
                return False
            return any(
                isinstance(raw, Mapping)
                and raw.get("phase") == target_phase.value
                and raw.get("game_id") == state.game_id
                and raw.get("logical_window_id") is None
                and raw.get("settlement_group_id") is None
                and raw.get("next_window_id") is None
                and raw.get("collection_complete_at") is None
                and raw.get("closed_at") is None
                for raw in state.action_windows.values()
            )
        rows = tuple(sorted(package.window_metadata, key=lambda item: item.order))
        groups = package.window_settlement_groups

        def group_id_for(logical_id: str) -> str:
            if not groups:
                return f"night:{state.round_no}"
            return f"night:{state.round_no}:{groups.get(logical_id)}"

        group_rows = tuple(
            row for row in rows if group_id_for(row.window_id) == cursor.settlement_group_id
        )
        if not group_rows:
            return False
        group_windows = tuple(
            _load_action_window(raw)
            for raw in state.action_windows.values()
            if isinstance(raw, Mapping)
            and raw.get("settlement_group_id") == cursor.settlement_group_id
        )
        if (
            not group_windows
            or any(
                item.closed_at is not None or item.collection_complete_at is None
                for item in group_windows
            )
            or any(item.logical_window_id is None for item in group_windows)
        ):
            return False
        installed_logical_ids = {cast(str, item.logical_window_id) for item in group_windows}
        if not installed_logical_ids.issubset({row.window_id for row in group_rows}):
            return False
        latest_id = max(
            installed_logical_ids,
            key=lambda logical_id: next(
                row.order for row in group_rows if row.window_id == logical_id
            ),
        )
        latest_order = next(row.order for row in group_rows if row.window_id == latest_id)
        latest_index = next(index for index, row in enumerate(rows) if row.window_id == latest_id)
        if latest_index + 1 >= len(rows):
            return False
        successor = rows[latest_index + 1]
        if (
            group_id_for(successor.window_id) != cursor.settlement_group_id
            or successor.phase != target_phase.value
            or successor.order <= latest_order
        ):
            return False
        latest_windows = tuple(
            item for item in group_windows if item.logical_window_id == latest_id
        )
        if not latest_windows or any(
            item.next_window_id != successor.window_id for item in latest_windows
        ):
            return False
        for dependency in successor.depends_on:
            expected_dependency_group = group_id_for(dependency)
            dependency_windows = tuple(
                _load_action_window(raw)
                for raw in state.action_windows.values()
                if isinstance(raw, Mapping)
                and raw.get("logical_window_id") == dependency
                and raw.get("settlement_group_id") == expected_dependency_group
            )
            if not dependency_windows:
                return False
            if expected_dependency_group == cursor.settlement_group_id:
                if any(item.collection_complete_at is None for item in dependency_windows):
                    return False
            elif any(item.closed_at is None for item in dependency_windows):
                return False
        return True

    def _valid_rule_workflow_return(
        self,
        state: GameState,
        cursor: RuleWorkflowCursor,
        target_phase: GamePhase,
    ) -> bool:
        """Validate the narrow phase edges needed to resume frozen work."""

        if (
            cursor.status not in {"DRAINING", "RETURN_READY"}
            or cursor.error_code is not None
            or any(
                item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
                for item in state.rule_trigger_queue
            )
            or any(item.is_pending for item in state.rule_boundaries)
        ):
            return False
        return_point = cursor.return_point
        if return_point is None or target_phase is not return_point.phase:
            return False
        if state.phase is target_phase or can_transition(state.phase, target_phase):
            return True
        has_current_day_host_exile = any(
            isinstance(audit, Mapping)
            and audit.get("operation") == "DAY_EXILE"
            and audit.get("outcome_code") == "exiled"
            and type(audit.get("target_seat")) is int
            and isinstance(audit.get("vote_window_id"), str)
            and isinstance(audit.get("rule_batch_id"), str)
            and any(
                ledger.batch_id == audit.get("rule_batch_id")
                and ledger.group_id == f"day-resolve:{state.round_no}:{audit.get('vote_window_id')}"
                and ledger.timing == GamePhase.DAY_RESOLVE.value
                and ledger.round_no == state.round_no
                and ledger.committed_revision == audit.get("committed_revision")
                and ledger.skill_ids == ("exile_resolution",)
                and ledger.action_codes == (203,)
                and ledger.actor_seats == (audit.get("target_seat"),)
                and len(ledger.request_ids) == 1
                and ledger.request_ids[0].startswith("host-")
                and any(
                    fact.fact_type.upper() == "DEATH_CONFIRMED"
                    and fact.target_seat == audit.get("target_seat")
                    and fact.death_cause == "exiled"
                    and fact.data.get("round_number") == state.round_no
                    for fact in ledger.facts
                )
                and any(
                    receipt.batch_id == ledger.batch_id
                    and receipt.group_id == ledger.group_id
                    and receipt.timing == GamePhase.DAY_RESOLVE.value
                    and receipt.committed_revision == ledger.committed_revision
                    and receipt.request_ids == ledger.request_ids
                    for receipt in state.rule_receipts
                )
                for ledger in state.rule_ledger
            )
            for audit in state.moderator_audit
        )
        if (
            target_phase is GamePhase.DAY_RESOLVE
            and state.phase is GamePhase.TRIGGER_ACTION
            and state.day_no == return_point.day_no
            and has_current_day_host_exile
        ):
            return True
        if (
            return_point.phase is GamePhase.DAY_SPEECH
            and state.phase in {GamePhase.DAY_RESOLVE, GamePhase.TRIGGER_ACTION}
            and state.day_no == return_point.day_no
        ):
            return True
        if (
            return_point.logical_window_id is not None
            and return_point.window_id is not None
            and self._execution_package is not None
            and state.day_no == return_point.day_no
            and state.phase
            in {
                GamePhase.DAY_RESOLVE,
                GamePhase.TRIGGER_ACTION,
                GamePhase.NIGHT_TEAM_CHAT,
                GamePhase.NIGHT_ACTION,
                GamePhase.NIGHT_RESOLVE,
            }
            and cursor.next_logical_window_id == return_point.logical_window_id
        ):
            source_raw = state.action_windows.get(return_point.window_id)
            if source_raw is None:
                return False
            source = _load_action_window(source_raw)
            if source.closed_at is None or source.logical_window_id is None:
                return False
            rows = sorted(self._execution_package.window_metadata, key=lambda item: item.order)
            source_row = next(
                (row for row in rows if row.window_id == source.logical_window_id),
                None,
            )
            source_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row.window_id == source.logical_window_id
                ),
                None,
            )
            target_row = next(
                (
                    row
                    for row in rows
                    if row.window_id == return_point.logical_window_id
                    and row.phase == target_phase.value
                ),
                None,
            )
            if source_index is None or source_row is None or target_row is None:
                return False
            if (
                source_index + 1 >= len(rows)
                or rows[source_index + 1] != target_row
                or source.next_window_id != target_row.window_id
            ):
                return False
            source_group = source.settlement_group_id or source.window_id
            if not all(
                item.closed_at is not None
                for item in (
                    _load_action_window(raw)
                    for raw in state.action_windows.values()
                    if isinstance(raw, Mapping)
                    and (raw.get("settlement_group_id") or raw.get("window_id")) == source_group
                )
            ):
                return False
            for dependency in target_row.depends_on:
                dependency_windows = tuple(
                    _load_action_window(raw)
                    for raw in state.action_windows.values()
                    if isinstance(raw, Mapping) and raw.get("logical_window_id") == dependency
                )
                if not dependency_windows or any(
                    item.closed_at is None for item in dependency_windows
                ):
                    return False
            return True
        return False

    async def snapshot(self) -> GameState:
        """Read a state reference through the same serialization boundary."""

        async with self._lock:
            return self._state

    async def bind_snapshot_reference(
        self,
        reference: Mapping[str, JsonValue],
        *,
        expected_revision: int | None = None,
    ) -> GameState:
        """Bind a verified durable snapshot reference to the live state.

        ``GameSnapshotStore`` writes the active projection as part of its
        atomic publish.  This narrow operation mirrors that reference in the
        manager without inventing a second game revision; callers must supply
        the revision they snapshotted so a concurrent game commit cannot make
        the in-memory and active references diverge.
        """

        if not isinstance(reference, Mapping):
            raise EventCommitError("SNAPSHOT_REFERENCE_INVALID: reference must be a mapping")
        required_reference = {
            "snapshot_id",
            "snapshot_revision",
            "state_revision",
            "created_at",
            "manifest_sha256",
        }
        if set(reference) != required_reference:
            raise EventCommitError("SNAPSHOT_REFERENCE_INVALID: reference fields are invalid")
        state_revision = reference.get("state_revision")
        if type(state_revision) is not int or state_revision < 0:
            raise EventCommitError("SNAPSHOT_REFERENCE_INVALID: state revision is invalid")

        async with self._lock:
            current = self._state
            revision = current.state_revision if expected_revision is None else expected_revision
            _revision_check(current, revision)
            if state_revision != current.state_revision:
                raise EventCommitError(
                    "SNAPSHOT_REFERENCE_INVALID: reference does not match current state"
                )
            data = _state_data(current)
            data["last_snapshot"] = dict(reference)
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit(self, patch: StatePatch) -> GameState:
        """Apply one already validated lifecycle patch under the commit lock.

        Action request patches carry a validation result, but that result is
        not proof that the request was validated against this manager's
        current state.  Keeping them out of this generic entry point closes a
        public bypass around :meth:`commit_action_request`, which rebuilds
        the authoritative context while holding the same lock.
        """

        async with self._lock:
            if patch.validated_request is not None:
                raise StatePatchError(
                    "action request patches must be committed through commit_action_request"
                )
            if patch.events_to_append or patch.delivery_ack is not None:
                raise StatePatchError(
                    "event patches must be committed through the delivery methods"
                )
            if (
                patch.target_phase is GamePhase.NIGHT_TEAM_CHAT
                and self._state.phase is GamePhase.PLAYER_PREPARE
                and not _players_ready(self._state)
            ):
                raise EventCommitError(
                    "PHASE_BLOCKED: all players must complete knowledge preparation"
                )
            candidate = reduce_state(self._state, patch)
            self._state = candidate
            return candidate

    async def commit_moderator_operation(
        self,
        *,
        operation: str,
        command: str,
        expected_revision: int,
        target_phase: GamePhase | None = None,
        run_status: RunStatus | None = None,
        reason: str = "",
        now: datetime | None = None,
    ) -> GameState:
        """Commit one narrow moderator mutation together with its audit.

        Moderator commands must use the same serialized replacement boundary
        as player actions and coordinator transitions.  The operation can
        append an audit by itself, change a phase, or change the operational
        status; it cannot replace arbitrary state supplied by a caller.
        ``expected_revision`` is deliberately required so a command that was
        based on an old status cannot overwrite a newer action or resolution.
        """

        if not isinstance(operation, str) or not operation or len(operation) > 64:
            raise EventCommitError("MODERATOR_OPERATION_INVALID: operation is invalid")
        if not isinstance(command, str) or not command or len(command) > 128:
            raise EventCommitError("MODERATOR_OPERATION_INVALID: command is invalid")
        if not isinstance(reason, str):
            raise EventCommitError("MODERATOR_OPERATION_INVALID: reason is invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise EventCommitError(
                "MODERATOR_OPERATION_INVALID: expected_revision must be non-negative"
            )
        if target_phase is not None and not isinstance(target_phase, GamePhase):
            try:
                target_phase = GamePhase(target_phase)
            except (TypeError, ValueError) as exc:
                raise EventCommitError(
                    "MODERATOR_OPERATION_INVALID: target phase is invalid"
                ) from exc
        if run_status is not None and not isinstance(run_status, RunStatus):
            try:
                run_status = RunStatus(run_status)
            except (TypeError, ValueError) as exc:
                raise EventCommitError(
                    "MODERATOR_OPERATION_INVALID: run status is invalid"
                ) from exc
        if target_phase is not None and run_status is not None:
            raise EventCommitError(
                "MODERATOR_OPERATION_INVALID: phase and run status cannot change together"
            )

        commit_time = utc_now() if now is None else now
        if commit_time.tzinfo is None or commit_time.utcoffset() is None:
            raise EventCommitError("MODERATOR_OPERATION_INVALID: timestamp must include a timezone")
        commit_time = commit_time.astimezone(UTC)

        async with self._lock:
            current = self._state
            _revision_check(current, expected_revision)

            if target_phase is not None:
                if current.sheriff_election is not None and current.phase in {
                    GamePhase.SHERIFF_ELECTION_SPEECH,
                    GamePhase.SHERIFF_ELECTION,
                    GamePhase.SHERIFF_ELECTION_PK_SPEECH,
                    GamePhase.SHERIFF_ELECTION_PK,
                }:
                    election = _load_sheriff_election(current.sheriff_election)
                    if election.status not in {
                        SheriffElectionStatus.RESOLVED,
                        SheriffElectionStatus.NO_SHERIFF,
                    }:
                        raise EventCommitError(
                            "PHASE_BLOCKED: sheriff election requires its dedicated commit path"
                        )
                if current.run_status in {
                    RunStatus.PAUSED,
                    RunStatus.FAILED,
                    RunStatus.CLOSED,
                }:
                    raise EventCommitError(
                        f"STATUS_BLOCKED: phase transition is unavailable while game is "
                        f"{current.run_status.value}"
                    )
                if (
                    current.phase is GamePhase.PLAYER_PREPARE
                    and target_phase is GamePhase.NIGHT_TEAM_CHAT
                    and not _players_ready(current)
                ):
                    raise EventCommitError(
                        "PHASE_BLOCKED: all players must complete knowledge preparation"
                    )
                # The shell performs the same checks for a friendly command
                # error, but these checks must also live here to close the
                # check-then-commit race with action coordinators.
                if current.serial_turn is not None:
                    raise EventCommitError("PHASE_BLOCKED: a serial turn is still active")
                if current.pending_resolution is not None:
                    raise EventCommitError(
                        "PHASE_BLOCKED: an unconfirmed resolution is still pending"
                    )
                for window_id, raw_window in current.action_windows.items():
                    try:
                        window = _load_action_window(raw_window)
                    except ValueError as exc:
                        raise EventCommitError(
                            f"PHASE_BLOCKED: action window {window_id!r} is malformed"
                        ) from exc
                    if window.is_open:
                        if current.phase is GamePhase.TRIGGER_ACTION and (
                            _looks_like_trigger_window(window)
                            or _is_trigger_window_state(current, window, require_bound=False)
                        ):
                            raise EventCommitError(
                                "PHASE_BLOCKED: the trigger action window is still open"
                            )
                        raise EventCommitError(
                            f"PHASE_BLOCKED: action window {window_id!r} is still open"
                        )
                for request_id, raw_request in current.action_requests.items():
                    if not isinstance(raw_request, dict):
                        raise EventCommitError(
                            f"PHASE_BLOCKED: action request {request_id!r} is malformed"
                        )
                    status = raw_request.get("status")
                    if not isinstance(status, str) or status.upper() in {
                        "OPEN",
                        "REQUESTED",
                        "SUBMITTING",
                        "IN_FLIGHT",
                        "PENDING",
                    }:
                        raise EventCommitError(
                            f"PHASE_BLOCKED: action request {request_id!r} is still active"
                        )
                try:
                    candidate = transition_phase(
                        current,
                        target_phase,
                        expected_revision=expected_revision,
                        now=commit_time,
                    )
                except (TypeError, ValueError) as exc:
                    raise EventCommitError(f"PHASE_INVALID: {exc}") from exc
            elif run_status is not None:
                if run_status is current.run_status:
                    raise EventCommitError("STATUS_INVALID: requested run status is already active")
                if run_status is RunStatus.CLOSED:
                    if current.phase is not GamePhase.FINISHED:
                        raise EventCommitError("STATUS_INVALID: CLOSED requires the FINISHED phase")
                    if current.serial_turn is not None or current.pending_resolution is not None:
                        raise EventCommitError(
                            "STATUS_INVALID: CLOSED requires no active turn or resolution"
                        )
                if current.run_status is RunStatus.CLOSED:
                    finish_rollback = (
                        operation == "FINISH_ROLLBACK"
                        and command == "finish"
                        and run_status in {RunStatus.READY, RunStatus.RUNNING}
                        and current.phase is GamePhase.FINISHED
                        and current.serial_turn is None
                        and current.pending_resolution is None
                    )
                    if not finish_rollback:
                        raise EventCommitError("STATUS_INVALID: a CLOSED game cannot be reopened")
                if current.run_status is RunStatus.FAILED and run_status is not RunStatus.FAILED:
                    raise EventCommitError("STATUS_INVALID: a FAILED game cannot be resumed")
                data = _state_data(current)
                data["run_status"] = run_status
                data["state_revision"] = current.state_revision + 1
                data["updated_at"] = commit_time
                candidate = GameState.model_validate(data)
            else:
                data = _state_data(current)
                data["state_revision"] = current.state_revision + 1
                data["updated_at"] = commit_time
                candidate = GameState.model_validate(data)

            data = _state_data(candidate)
            audits = list(data["moderator_audit"])
            audits.append(
                {
                    "operation": operation,
                    "moderator_id": "human",
                    "command": command,
                    "reason": reason[:500],
                    "base_revision": current.state_revision,
                    "committed_revision": candidate.state_revision,
                    "created_at": commit_time.isoformat(),
                }
            )
            data["moderator_audit"] = tuple(audits)
            data["updated_at"] = commit_time
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_game_start(
        self,
        *,
        assignment_plan: PlayerAssignmentPlan,
        session_refs: Mapping[int, str],
        expected_revision: int,
        now: datetime | None = None,
    ) -> GameState:
        """Atomically commit initial identities, runtime refs, and prepare phase.

        Runtime processes are started outside this lock.  This method is the
        only point that makes their successful references authoritative: if
        validation fails, the state remains exactly as it was and the caller
        can close the prepared sessions and retry with the same assignment
        seed.
        """

        if not isinstance(assignment_plan, PlayerAssignmentPlan):
            raise EventCommitError("START_INVALID: assignment plan is invalid")
        if not isinstance(session_refs, Mapping):
            raise EventCommitError("START_INVALID: session refs are invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise EventCommitError("START_INVALID: expected revision is invalid")
        commit_time = utc_now() if now is None else now
        if commit_time.tzinfo is None or commit_time.utcoffset() is None:
            raise EventCommitError("START_INVALID: timestamp must include a timezone")
        commit_time = commit_time.astimezone(UTC)

        async with self._lock:
            current = self._state
            _revision_check(current, expected_revision)
            if current.phase is not GamePhase.ASSIGNED:
                raise EventCommitError("START_INVALID: game must be in ASSIGNED phase before start")
            if current.run_status in {RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CLOSED}:
                raise EventCommitError(f"START_INVALID: game is {current.run_status.value.lower()}")
            ruleset = current.ruleset
            if ruleset is None or (
                assignment_plan.board_ref.id != ruleset.board_id
                or assignment_plan.board_ref.version != ruleset.version
            ):
                raise EventCommitError("START_INVALID: assignment plan does not match ruleset")
            if tuple(sorted(session_refs)) != assignment_plan.seats:
                raise EventCommitError("START_INVALID: session refs do not cover every seat")
            execution_identity: RuleExecutionIdentity | None = None
            ability_instances: tuple[AbilityInstanceState, ...] = ()
            initial_rule_state: tuple[RuleStateValue, ...] = ()
            if self._execution_package is not None:
                try:
                    assert self._rules is not None
                    execution_identity = self._rules.identity
                    ability_instances = build_ability_instances(
                        assignment_plan.players,
                        self._execution_package,
                    )
                    initial_rule_state = build_initial_rule_state(
                        self._execution_package,
                        ability_instances,
                        seats=assignment_plan.seats,
                    )
                except (RuleAdapterError, TypeError, ValueError) as exc:
                    raise EventCommitError(
                        f"START_INVALID: frozen execution package grants are invalid: {exc}"
                    ) from exc
            if any(
                type(seat) is not int
                or not isinstance(runtime_ref, str)
                or not runtime_ref.strip()
                or len(runtime_ref) > 128
                for seat, runtime_ref in session_refs.items()
            ):
                raise EventCommitError("START_INVALID: session refs contain an invalid value")

            players = {
                seat: player.model_copy(update={"runtime_ref": session_refs[seat]})
                for seat, player in assignment_plan.players.items()
            }
            next_revision = current.state_revision + 1
            current_events = _typed_events(current)
            next_event_id = current_events[-1].event_id + 1 if current_events else 1
            role_events = tuple(
                GameEvent.private(
                    event_id=next_event_id + offset,
                    game_id=current.game_id,
                    state_revision=next_revision,
                    round_no=current.round_no,
                    phase=GamePhase.PLAYER_PREPARE,
                    created_at=commit_time,
                    event_type=EventType.ROLE_ASSIGNMENT,
                    seat=seat,
                    payload=PrivateRolePayload(
                        role_id=players[seat].role_id,
                        faction_id=players[seat].faction_id,
                    ),
                )
                for offset, seat in enumerate(assignment_plan.seats)
            )
            _validate_new_events(current, role_events, next_revision=next_revision)
            data = _state_data(current)
            data["players"] = players
            if execution_identity is not None:
                data["execution_identity"] = execution_identity
                data["ability_instances"] = ability_instances
                data["rule_state"] = tuple(_rule_state_payload(item) for item in initial_rule_state)
            data["events"] = (*current_events, *role_events)
            data["phase"] = GamePhase.PLAYER_PREPARE
            data["run_status"] = RunStatus.RUNNING
            data["state_revision"] = next_revision
            data["updated_at"] = commit_time
            audits = list(data["moderator_audit"])
            audits.append(
                {
                    "operation": "START",
                    "moderator_id": "human",
                    "command": "start",
                    "reason": "player sessions started and roles assigned",
                    "base_revision": current.state_revision,
                    "committed_revision": next_revision,
                    "created_at": commit_time.isoformat(),
                }
            )
            data["moderator_audit"] = tuple(audits)
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_player_ready(
        self,
        *,
        seat: int,
        session_epoch: int,
        receipt_ids: Iterable[str],
        expected_revision: int,
        now: datetime | None = None,
    ) -> GameState:
        """Atomically record one seat's verified knowledge preparation.

        The caller must perform receipt verification against the live gateway
        before entering this serialized boundary.  This method binds the
        resulting opaque IDs to the current seat and epoch and rejects stale
        or duplicate submissions while holding the manager lock.  It never
        accepts a model's free-form readiness text as proof.
        """

        if type(seat) is not int or not 1 <= seat <= 64:
            raise EventCommitError("READY_INVALID: seat is invalid")
        if type(session_epoch) is not int or session_epoch < 0:
            raise EventCommitError("READY_INVALID: session epoch is invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise EventCommitError("READY_INVALID: expected revision is invalid")
        if isinstance(receipt_ids, (str, bytes)):
            raise EventCommitError("READY_INVALID: receipt IDs must be an iterable of strings")
        try:
            ids = tuple(receipt_ids)
        except TypeError as exc:
            raise EventCommitError("READY_INVALID: receipt IDs are invalid") from exc
        if not ids or any(type(value) is not str or not value.strip() for value in ids):
            raise EventCommitError("READY_INVALID: receipt IDs must be non-empty strings")
        if len(ids) != len(set(ids)):
            raise EventCommitError("READY_INVALID: receipt IDs must be distinct")
        if any(len(value) > 128 for value in ids):
            raise EventCommitError("READY_INVALID: receipt ID is too long")
        commit_time = utc_now() if now is None else now
        if commit_time.tzinfo is None or commit_time.utcoffset() is None:
            raise EventCommitError("READY_INVALID: timestamp must include a timezone")
        commit_time = commit_time.astimezone(UTC)

        async with self._lock:
            current = self._state
            _revision_check(current, expected_revision)
            if current.phase is not GamePhase.PLAYER_PREPARE:
                raise EventCommitError("READY_INVALID: game is not in PLAYER_PREPARE")
            player = current.players.get(seat)
            if player is None:
                raise EventCommitError("SEAT_NOT_ASSIGNED: ready seat is not a current player")
            if player.session_epoch != session_epoch:
                raise EventCommitError("SESSION_MISMATCH: ready uses an obsolete session epoch")
            if player.knowledge_receipt_ids:
                raise EventCommitError("READY_ALREADY_COMMITTED: seat is already ready")

            data = _state_data(current)
            players = dict(current.players)
            players[seat] = player.model_copy(update={"knowledge_receipt_ids": ids})
            data["players"] = players
            data["state_revision"] = current.state_revision + 1
            data["updated_at"] = commit_time
            audits = list(data["moderator_audit"])
            audits.append(
                {
                    "operation": "PLAYER_READY",
                    "moderator_id": "system",
                    "command": "prepare",
                    "reason": f"seat {seat} passed the knowledge readiness gate",
                    "base_revision": current.state_revision,
                    "committed_revision": current.state_revision + 1,
                    "created_at": commit_time.isoformat(),
                }
            )
            data["moderator_audit"] = tuple(audits)
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def peek_delivery(
        self,
        seat: int,
        session_epoch: int | None = None,
    ) -> tuple[GameEvent, ...]:
        """Read authorized events without changing state or its revision."""

        async with self._lock:
            player = self._state.players.get(seat)
            if player is None:
                raise EventCommitError(
                    "SEAT_NOT_ASSIGNED: delivery seat is not in the current game"
                )
            effective_epoch = player.session_epoch if session_epoch is None else session_epoch
            if effective_epoch != player.session_epoch:
                raise EventCommitError("SESSION_MISMATCH: delivery uses an obsolete session epoch")
            events = _typed_events(self._state)
            cursor = self._state.delivery_cursors.get(
                seat,
                DeliveryCursor(session_epoch=effective_epoch),
            )
            router = MessageRouter(events, {seat: cursor})
            return router.peek_delivery(seat, effective_epoch)

    async def get_action_window(self, window_id: str) -> ActionWindow:
        """Return the currently installed, phase-authoritative action window.

        The returned model is a frozen snapshot.  Callers may use it to build
        a player-facing view, but the manager still reloads it under the
        commit lock for every action-turn bind and action submission.
        """

        async with self._lock:
            raw = self._state.action_windows.get(window_id)
            if raw is None:
                raise EventCommitError("WINDOW_NOT_FOUND: action window is not installed")
            try:
                window = _load_action_window(raw)
            except ValueError as exc:
                raise EventCommitError(
                    "WINDOW_INVALID: installed action window is malformed"
                ) from exc
            if window.game_id != self._state.game_id:
                raise EventCommitError("GAME_MISMATCH: action window belongs to another game")
            if window.phase != self._state.phase:
                raise EventCommitError("PHASE_MISMATCH: action window is not active in this phase")
            return window

    async def get_active_team_chat_window(self) -> ActionWindow:
        """Return the one open team-chat window for the current phase.

        A team queue is derived from this frozen window.  The method exists as
        a read-only convenience for the scheduler; queue installation and
        every request bind still repeat the same check under the commit lock.
        """

        async with self._lock:
            return _active_team_chat_window(self._state)

    async def commit_action_window(
        self,
        window: ActionWindow,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Install one frozen action window through the serialized commit path.

        Window creation is a state mutation just like a request or a phase
        transition.  Keeping it here prevents a coordinator from installing a
        window against stale player/session facts or replacing a window that a
        different coordinator already opened.
        """

        if not isinstance(window, ActionWindow):
            raise TypeError("window must be an ActionWindow")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            state = self._state
            if window.game_id != state.game_id:
                raise EventCommitError("GAME_MISMATCH: action window belongs to another game")
            if window.phase != state.phase:
                raise EventCommitError("PHASE_MISMATCH: action window is not active in this phase")
            legacy_trigger_action = _is_trigger_window_state(state, window, require_bound=False)
            rule_occurrence = self._rule_occurrence_for_window(state, window)
            trigger_action = legacy_trigger_action or rule_occurrence is not None
            if _is_sheriff_badge_window(window):
                raise EventCommitError(
                    "BADGE_ACTION_INVALID: badge windows require the sheriff badge reducer"
                )
            if _looks_like_trigger_window(window) and not trigger_action:
                raise EventCommitError(
                    "TRIGGER_ACTION_INVALID: action window is not bound to the pending "
                    "granted ability"
                )
            existing_raw = state.action_windows.get(window.window_id)
            if existing_raw is not None:
                try:
                    existing = _load_action_window(existing_raw)
                except ValueError as exc:
                    raise EventCommitError(
                        "WINDOW_INVALID: installed action window is malformed"
                    ) from exc
                if existing == window:
                    return state
                raise EventCommitError(
                    "WINDOW_CONFLICT: window_id is already installed with different contents"
                )
            for seat in window.allowed_seats:
                player = state.players.get(seat)
                if player is None:
                    raise EventCommitError("SEAT_NOT_ASSIGNED: action window seat is unknown")
                if player.session_epoch != window.session_epoch:
                    raise EventCommitError(
                        "SESSION_MISMATCH: action window uses an obsolete session"
                    )
                if not player.alive and not trigger_action:
                    raise EventCommitError("PLAYER_DEAD: dead seats cannot enter a night window")
            if legacy_trigger_action:
                pending = state.pending_resolution
                if not isinstance(pending, dict):  # pragma: no cover - helper already checks
                    raise EventCommitError("TRIGGER_ACTION_INVALID: pending trigger is missing")
                # Bind the generated window ID to the pending resolution in
                # the same replacement as the installed window.  The first
                # commit therefore becomes the durable one-shot ownership
                # boundary and a second coordinator cannot race it.
                pending_data = dict(pending)
                pending_data["window_id"] = window.window_id
            data = _state_data(state)
            windows = dict(data["action_windows"])
            windows[window.window_id] = window.model_dump(mode="json")
            data["action_windows"] = windows
            if legacy_trigger_action:
                data["pending_resolution"] = pending_data
            timestamp = _aware_commit_time(now)
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = timestamp
            candidate = GameState.model_validate(data)
            if window.logical_window_id is not None:
                candidate = self._release_due_rule_disclosures(
                    candidate,
                    window.logical_window_id,
                    window.logical_window_id,
                    timestamp=timestamp,
                    next_revision=candidate.state_revision,
                )
            self._state = candidate
            return candidate

    async def close_action_window(
        self,
        window_id: str,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Close an installed window without changing player effects."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            state = self._state
            raw = state.action_windows.get(window_id)
            if raw is None:
                raise EventCommitError("WINDOW_NOT_FOUND: action window is not installed")
            try:
                window = _load_action_window(raw)
            except ValueError as exc:
                raise EventCommitError(
                    "WINDOW_INVALID: installed action window is malformed"
                ) from exc
            if window.closed_at is not None:
                return state
            if window.phase != state.phase and not (
                window.phase is GamePhase.NIGHT_ACTION and state.phase is GamePhase.NIGHT_RESOLVE
            ):
                raise EventCommitError("PHASE_MISMATCH: action window cannot be closed here")
            closed_at = now or utc_now()
            closed = window.model_copy(update={"closed_at": closed_at})
            data = _state_data(state)
            windows = dict(data["action_windows"])
            windows[window_id] = closed.model_dump(mode="json")
            data["action_windows"] = windows
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = closed_at
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def begin_action_turn(
        self,
        seat: int,
        session_epoch: int,
        *,
        window_id: str,
        request_id: str,
        previous_request_id: str | None = None,
        retry: bool = False,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Bind one physical runtime request to a seat atomically.

        Binding and delivery freezing are the only state changes made before
        a runtime call.  A timeout or malformed response leaves this request
        and its in-flight event batch visible to the moderator so it can be
        retried or inspected.  The action itself and delivery acknowledgement
        are still committed only by :meth:`commit_action_request`.
        """

        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise EventCommitError("REQUEST_INVALID: request_id must be non-empty and bounded")
        if previous_request_id is not None and (
            not isinstance(previous_request_id, str)
            or not previous_request_id
            or len(previous_request_id) > 128
        ):
            raise EventCommitError("REQUEST_INVALID: previous_request_id is invalid")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            state = self._state
            raw = state.action_windows.get(window_id)
            if raw is None:
                raise EventCommitError("WINDOW_NOT_FOUND: action window is not installed")
            try:
                window = _load_action_window(raw)
            except ValueError as exc:
                raise EventCommitError(
                    "WINDOW_INVALID: installed action window is malformed"
                ) from exc
            if window.game_id != state.game_id:
                raise EventCommitError("GAME_MISMATCH: action window belongs to another game")
            if window.phase != state.phase:
                raise EventCommitError("PHASE_MISMATCH: action window is not active in this phase")
            if not window.accepts_submissions:
                raise EventCommitError("WINDOW_CLOSED: action window no longer accepts requests")
            if window.session_epoch != session_epoch:
                raise EventCommitError("SESSION_MISMATCH: action window uses another session")
            if seat not in window.allowed_seats:
                raise EventCommitError("SEAT_NOT_ALLOWED: seat is not allowed in this window")
            legacy_trigger_action = _is_trigger_window_state(state, window, require_bound=True)
            rule_occurrence = self._rule_occurrence_for_window(state, window)
            trigger_action = legacy_trigger_action or rule_occurrence is not None
            badge_action = _is_sheriff_badge_window(window)
            if _looks_like_trigger_window(window) and not trigger_action:
                raise EventCommitError(
                    "TRIGGER_ACTION_INVALID: action window is not bound to the pending "
                    "granted ability"
                )
            player = state.players.get(seat)
            if player is None:
                raise EventCommitError("SEAT_NOT_ASSIGNED: action seat is not in the current game")
            if player.session_epoch != session_epoch:
                raise EventCommitError("SESSION_MISMATCH: player session is obsolete")
            if not player.alive and not trigger_action and not badge_action:
                raise EventCommitError("PLAYER_DEAD: dead players cannot receive an action turn")
            if badge_action:
                badge_error = _sheriff_badge_binding_error(state, window)
                if badge_error is not None:
                    raise EventCommitError(badge_error)
                marker = state.sheriff_badge
                if not isinstance(marker, dict) or marker.get("source_seat") != seat:
                    raise EventCommitError("BADGE_ACTION_INVALID: seat is not the pending sheriff")
            if window.allowed_role_ids and player.role_id not in window.allowed_role_ids:
                # Check the authoritative role before constructing or binding
                # a runtime request.  The scheduler must never give a
                # role-ineligible runtime even the window's visible context.
                raise EventCommitError("ROLE_NOT_ALLOWED: role is not allowed in this window")
            if request_id in state.action_requests and request_id != player.current_request_id:
                raise EventCommitError("REQUEST_ID_IN_USE: request_id is already committed")

            active = player.current_request_id
            if active == request_id and previous_request_id in (None, active):
                return state
            if active is not None:
                if not retry:
                    raise EventCommitError("TURN_IN_PROGRESS: seat already has an active request")
                if previous_request_id != active:
                    raise EventCommitError("REQUEST_EXPIRED: retry does not match active request")
            elif retry or previous_request_id is not None:
                raise EventCommitError("REQUEST_EXPIRED: no active request can be retried")

            events = _typed_events(state)
            cursor = _player_cursor(
                state,
                DeliveryAck(seat, session_epoch, request_id),
            )
            router = MessageRouter(events, {seat: cursor})
            try:
                # Freeze the exact authorized batch with this physical action
                # request while the player binding is still under this lock.
                candidate_cursor = router.prepare_ack(
                    seat,
                    session_epoch,
                    request_id=request_id,
                )
            except (DeliveryCursorError, DeliverySessionError, ValueError) as exc:
                raise EventCommitError(str(exc)) from exc

            data = _state_data(state)
            players = dict(data["players"])
            player_data = dict(players[seat])
            player_data["current_request_id"] = request_id
            players[seat] = player_data
            data["players"] = players
            cursors = dict(data["delivery_cursors"])
            cursors[seat] = candidate_cursor
            data["delivery_cursors"] = cursors
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def begin_delivery(
        self,
        seat: int,
        session_epoch: int,
        *,
        request_id: str,
        event_ids: tuple[int, ...] | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Freeze a peeked batch before a runtime turn begins.

        This records only ``in_flight_*``.  ``committed_event_id`` advances
        only in :meth:`commit_delivery_ack` after successful runtime output.
        A failed turn therefore sees the same event IDs on its retry.
        """

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            events = _typed_events(self._state)
            cursor = _player_cursor(
                self._state,
                DeliveryAck(seat, session_epoch, request_id, event_ids),
            )
            router = MessageRouter(events, {seat: cursor})
            try:
                candidate_cursor = router.prepare_ack(
                    seat,
                    session_epoch,
                    request_id=request_id,
                    event_ids=event_ids,
                )
            except (DeliveryCursorError, DeliverySessionError, ValueError) as exc:
                raise EventCommitError(str(exc)) from exc
            if candidate_cursor == cursor:
                return self._state
            data = _state_data(self._state)
            cursors = dict(data["delivery_cursors"])
            cursors[seat] = candidate_cursor
            data["delivery_cursors"] = cursors
            data["state_revision"] = self._state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def commit_events(
        self,
        events: Iterable[GameEvent],
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Append an already-authorized event batch as one state commit."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            candidate = reduce_state(
                self._state,
                StatePatch.append_events(events, expected_revision=revision, now=now),
            )
            self._state = candidate
            return candidate

    async def commit_delivery_ack(
        self,
        seat: int,
        session_epoch: int,
        *,
        request_id: str,
        event_ids: tuple[int, ...] | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Acknowledge a successful delivery under the manager lock."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            ack = DeliveryAck(seat, session_epoch, request_id, event_ids)
            candidate = reduce_state(
                self._state,
                StatePatch.event_delivery(
                    ack=ack,
                    expected_revision=revision,
                    now=now,
                ),
            )
            self._state = candidate
            return candidate

    async def commit_events_and_ack(
        self,
        events: Iterable[GameEvent],
        ack: DeliveryAck,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Append authorized events and acknowledge delivery atomically."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            candidate = reduce_state(
                self._state,
                StatePatch.event_delivery(
                    events,
                    ack,
                    expected_revision=revision,
                    now=now,
                ),
            )
            self._state = candidate
            return candidate

    async def open_vote_window(
        self,
        window: VoteWindow,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Open one observation-frozen secret vote window.

        The supplied window is treated as an untrusted description.  Its
        observation revision, eligible seats, live candidates, sessions, and
        weights are checked against the state while holding the commit lock.
        Opening a second unrelated window while one is active is rejected.
        """

        if not isinstance(window, VoteWindow):
            raise TypeError("window must be a VoteWindow")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if self._state.vote_state is not None:
                existing = _load_vote_state(self._state.vote_state)
                if existing.window == window:
                    return self._state
                if existing.status is not VoteStatus.RESOLVED:
                    raise VoteError("VOTE_WINDOW_ACTIVE", "another vote window is already active")
            _authoritative_vote_window(self._state, window)
            data = _state_data(self._state)
            data["vote_state"] = _vote_state_payload(VoteState.open(window))
            data["state_revision"] = self._state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def submit_vote(
        self,
        request: VoteRequest,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Revalidate and commit one private ballot through the manager lock."""

        if not isinstance(request, VoteRequest):
            raise TypeError("request must be a VoteRequest")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no active vote window")
            vote_state = _load_vote_state(self._state.vote_state)
            _authoritative_vote_request(self._state, vote_state, request)
            result = vote_state.submit(request)
            if result.idempotent_replay:
                return self._state
            data = _state_data(self._state)
            data["vote_state"] = _vote_state_payload(result.state)
            data["state_revision"] = self._state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def lock_vote_window(
        self,
        *,
        force: bool = False,
        reason: str = "all_votes_received",
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Lock ballot collection without exposing ballots or a tally."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no active vote window")
            vote_state = _load_vote_state(self._state.vote_state)
            locked = vote_state.lock(force=force, reason=reason)
            data = _state_data(self._state)
            data["vote_state"] = _vote_state_payload(locked)
            data["state_revision"] = self._state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def prepare_vote_tally(
        self,
        *,
        tie_resolver: TieResolver | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Build a GM-pending tally using an explicitly supplied board policy.

        ``tie_resolver`` is a trusted boundary: callers must obtain it from
        the validated, published ruleset snapshot.  The manager does not
        infer a PK, revote, or no-exile rule from the board name.  A missing
        resolver for a tied tally therefore raises ``TIE_POLICY_REQUIRED``
        and leaves the state unchanged.
        """

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no active vote window")
            vote_state = _load_vote_state(self._state.vote_state)
            tallied = vote_state.build_tally(tie_resolver=tie_resolver)
            data = _state_data(self._state)
            data["vote_state"] = _vote_state_payload(tallied)
            data["state_revision"] = self._state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def finalize_vote(
        self,
        *,
        force: bool = False,
        reason: str = "all_votes_received",
        tie_resolver: TieResolver | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Lock and prepare one tally in a single serialized commit.

        This is the normal collection-close path.  It performs no automatic
        tie choice; the resolver is an explicit trusted input from the
        published ruleset boundary.
        """

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no active vote window")
            vote_state = _load_vote_state(self._state.vote_state)
            if vote_state.status is VoteStatus.OPEN:
                vote_state = vote_state.lock(force=force, reason=reason)
            tallied = vote_state.build_tally(tie_resolver=tie_resolver)
            data = _state_data(self._state)
            data["vote_state"] = _vote_state_payload(tallied)
            data["state_revision"] = self._state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def confirm_vote_tally(
        self,
        board: BoardDefinition | None = None,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Confirm a pending tally and append exactly one public result event.

        The event contains the safe projection selected by the frozen board.
        The private ``ballots`` map remains in the authoritative snapshot
        until confirmation and is never copied wholesale into the payload.
        """

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no active vote window")
            vote_state = _load_vote_state(self._state.vote_state)
            if board is not None:
                _validate_vote_board(board, self._state)
            vote_kind: Literal["day", "day_pk"] = (
                "day_pk" if self._state.phase is GamePhase.VOTE_PK else "day"
            )
            if vote_state.status is VoteStatus.RESOLVED:
                correlation_id = vote_state.window.window_id
                existing_events = _typed_events(self._state)
                if any(
                    event.event_type is EventType.VOTE_RESULT
                    and event.correlation_id == correlation_id
                    for event in existing_events
                ):
                    return self._state
                # A legacy snapshot may contain a resolved vote without its
                # event.  Reconstruct the missing event from the confirmed
                # projection, preserving idempotent recovery semantics.
                confirmed = vote_state
            else:
                confirmed = vote_state.confirm_tally()
            result = confirmed.public_result
            if result is None:  # pragma: no cover - guarded by confirm_tally
                raise EventCommitError("VOTE_RESULT_INVALID: confirmation produced no result")
            events = _typed_events(self._state)
            event_id = max((event.event_id for event in events), default=0) + 1
            commit_revision = self._state.state_revision + 1
            timestamp = now or utc_now()
            payload = _public_vote_payload(
                confirmed,
                board=board,
                vote_kind=vote_kind,
            )
            event = GameEvent.public(
                event_id=event_id,
                game_id=self._state.game_id,
                state_revision=commit_revision,
                round_no=self._state.round_no,
                phase=self._state.phase,
                created_at=timestamp,
                event_type=EventType.VOTE_RESULT,
                eligible_seats=tuple(sorted(self._state.players)),
                payload=payload,
                correlation_id=vote_state.window.window_id,
            )
            candidate = _reduce_event_delivery(
                self._state,
                StatePatch.event_delivery(
                    (event,),
                    expected_revision=revision,
                    now=timestamp,
                ),
            )
            data = _state_data(candidate)
            data["vote_state"] = _vote_state_payload(confirmed)
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def confirm_vote_and_transition(
        self,
        board: BoardDefinition | None = None,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Confirm a tally, publish its result, and enter the next phase atomically.

        The older ``confirm_vote_tally`` plus ``commit_phase_transition`` pair
        leaves a recoverable but awkward ``RESOLVED``/vote-phase gap when the
        process stops between commits.  This method makes the result event,
        resolved vote state, and phase edge one serialized replacement.  A
        retry after a process restart completes only the missing phase edge and
        never appends a duplicate public result event.
        """

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if self._state.phase not in {GamePhase.VOTE, GamePhase.VOTE_PK}:
                raise EventCommitError("PHASE_MISMATCH: vote confirmation is outside a vote phase")
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no vote window to confirm")
            vote_state = _load_vote_state(self._state.vote_state)
            if board is not None:
                _validate_vote_board(board, self._state)

            if vote_state.status is VoteStatus.RESOLVED:
                result = vote_state.public_result
                if result is None:  # pragma: no cover - malformed state is rejected below
                    raise EventCommitError("VOTE_RESULT_INVALID: resolved vote has no result")
                target_phase = (
                    GamePhase.VOTE_PK_SPEECH
                    if result.tie_action is TieAction.PK
                    else GamePhase.DAY_RESOLVE
                )
                if self._state.phase is target_phase:
                    return self._state
                if not can_transition(self._state.phase, target_phase):
                    raise EventCommitError(
                        "PHASE_MISMATCH: resolved vote cannot enter its target phase"
                    )
                data = _state_data(self._state)
                data["phase"] = target_phase
                data["state_revision"] = self._state.state_revision + 1
                data["updated_at"] = now or utc_now()
                committed = GameState.model_validate(data)
                self._state = committed
                return committed

            if vote_state.status is not VoteStatus.WAITING_GM:
                raise VoteError("TALLY_NOT_PENDING", "a vote tally is not awaiting confirmation")

            confirmed = vote_state.confirm_tally()
            result = confirmed.public_result
            if result is None:  # pragma: no cover - guarded by confirm_tally
                raise EventCommitError("VOTE_RESULT_INVALID: confirmation produced no result")
            target_phase = (
                GamePhase.VOTE_PK_SPEECH
                if result.tie_action is TieAction.PK
                else GamePhase.DAY_RESOLVE
            )
            if not can_transition(self._state.phase, target_phase):
                raise EventCommitError(
                    "PHASE_MISMATCH: resolved vote cannot enter its target phase"
                )

            timestamp = now or utc_now()
            commit_revision = self._state.state_revision + 1
            events = _typed_events(self._state)
            event_id = max((event.event_id for event in events), default=0) + 1
            event = GameEvent.public(
                event_id=event_id,
                game_id=self._state.game_id,
                state_revision=commit_revision,
                round_no=self._state.round_no,
                phase=self._state.phase,
                created_at=timestamp,
                event_type=EventType.VOTE_RESULT,
                eligible_seats=tuple(sorted(self._state.players)),
                payload=_public_vote_payload(
                    confirmed,
                    board=board,
                    vote_kind=("day_pk" if self._state.phase is GamePhase.VOTE_PK else "day"),
                ),
                correlation_id=vote_state.window.window_id,
            )
            committed_events = _reduce_event_delivery(
                self._state,
                StatePatch.event_delivery(
                    (event,),
                    expected_revision=revision,
                    now=timestamp,
                ),
            )
            data = _state_data(committed_events)
            data["vote_state"] = _vote_state_payload(confirmed)
            data["phase"] = target_phase
            # ``_reduce_event_delivery`` already advanced the revision once;
            # changing phase and vote state is part of that same replacement.
            data["state_revision"] = committed_events.state_revision
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    @staticmethod
    def _require_sheriff_phase(
        state: GameState,
        *,
        phase: GamePhase | tuple[GamePhase, ...],
        status: SheriffElectionStatus | None = None,
    ) -> SheriffElectionState:
        """Load the election and enforce its phase/status boundary."""

        phases = (phase,) if isinstance(phase, GamePhase) else phase
        if state.phase not in phases:
            expected = ", ".join(item.value for item in phases)
            raise SheriffElectionError(
                "PHASE_NOT_ALLOWED",
                f"sheriff operation requires {expected}, current phase is {state.phase.value}",
            )
        election = _load_sheriff_election(state.sheriff_election)
        if status is not None and election.status is not status:
            raise SheriffElectionError(
                "ELECTION_STATUS_INVALID",
                "sheriff operation requires "
                f"{status.value}, current status is {election.status.value}",
            )
        return election

    async def start_sheriff_election(
        self,
        board: BoardDefinition,
        *,
        candidates: tuple[int, ...],
        speech_order: tuple[int, ...] | None = None,
        expected_speech_request_ids: Mapping[int, str] | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Install the first-day election before any death announcement.

        The election record and the phase edge are one serialized replacement.
        A player request can only add a campaign speech or ballot; this method
        is the moderator/coordinator boundary that creates the record.
        """

        if not isinstance(candidates, tuple):
            raise SheriffElectionError("CANDIDATES_INVALID", "candidates must be a tuple")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            if current.sheriff_election is not None:
                raise SheriffElectionError("ELECTION_EXISTS", "a sheriff election already exists")
            try:
                validate_sheriff_start(board, current, candidates=candidates)
                election = SheriffElectionState.start(
                    game_id=current.game_id,
                    day_no=current.day_no,
                    candidates=candidates,
                    eligible_voters=first_day_sheriff_participants(current),
                    speech_order=speech_order,
                    expected_speech_request_ids=expected_speech_request_ids,
                )
            except SheriffElectionError:
                raise
            except (TypeError, ValueError) as exc:
                raise SheriffElectionError("ELECTION_INVALID", str(exc)) from exc

            timestamp = utc_now() if now is None else now
            intermediate_data = _state_data(current)
            intermediate_data["sheriff_election"] = _sheriff_election_payload(election)
            intermediate = GameState.model_validate(intermediate_data)
            try:
                committed = transition_phase(
                    intermediate,
                    GamePhase.SHERIFF_ELECTION_SPEECH,
                    expected_revision=revision,
                    now=timestamp,
                )
            except (TypeError, ValueError) as exc:
                raise SheriffElectionError("PHASE_INVALID", str(exc)) from exc
            self._state = committed
            return committed

    async def submit_sheriff_speech(
        self,
        request: SheriffCampaignSpeechRequest,
        *,
        expected_request_id: str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Commit one candidate speech while the election speech phase is open."""

        if not isinstance(request, SheriffCampaignSpeechRequest):
            raise TypeError("request must be a SheriffCampaignSpeechRequest")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            election = self._require_sheriff_phase(
                current,
                phase=(
                    GamePhase.SHERIFF_ELECTION_SPEECH,
                    GamePhase.SHERIFF_ELECTION_PK_SPEECH,
                ),
                status=SheriffElectionStatus.SPEECH,
            )
            if request.observation_revision != current.state_revision:
                raise SheriffElectionError(
                    "REVISION_MISMATCH",
                    "speech observation_revision does not match the current state",
                )
            player = current.players.get(request.seat)
            if player is None:
                raise SheriffElectionError("SEAT_NOT_ASSIGNED", "speech seat is not assigned")
            if player.session_epoch != request.session_epoch:
                raise SheriffElectionError("SESSION_MISMATCH", "speech session is obsolete")
            if request.seat not in first_day_sheriff_participants(current):
                raise SheriffElectionError(
                    "SEAT_NOT_ELIGIBLE", "seat is outside the current sheriff election boundary"
                )
            try:
                accepted = election.submit_speech(
                    request,
                    expected_request_id=expected_request_id,
                )
            except SheriffElectionError:
                raise
            if accepted == election:
                return current
            data = _state_data(current)
            data["sheriff_election"] = _sheriff_election_payload(accepted)
            data["state_revision"] = current.state_revision + 1
            data["updated_at"] = utc_now() if now is None else now
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def open_sheriff_vote_window(
        self,
        window: VoteWindow,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Open the private election ballot and enter ``SHERIFF_ELECTION``."""

        if not isinstance(window, VoteWindow):
            raise TypeError("window must be a VoteWindow")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            election = self._require_sheriff_phase(
                current,
                phase=(
                    GamePhase.SHERIFF_ELECTION_SPEECH,
                    GamePhase.SHERIFF_ELECTION_PK_SPEECH,
                ),
                status=SheriffElectionStatus.SPEECH,
            )
            try:
                _authoritative_vote_window(
                    current,
                    window,
                    allow_first_day_sheriff_participants=True,
                )
                opened = election.open_vote(window)
            except SheriffElectionError:
                raise
            except VoteError as exc:
                raise SheriffElectionError(exc.code, str(exc)) from exc
            except (TypeError, ValueError) as exc:
                raise SheriffElectionError("VOTE_WINDOW_INVALID", str(exc)) from exc
            data = _state_data(current)
            data["sheriff_election"] = _sheriff_election_payload(opened)
            intermediate = GameState.model_validate(data)
            target_phase = (
                GamePhase.SHERIFF_ELECTION_PK
                if current.phase is GamePhase.SHERIFF_ELECTION_PK_SPEECH
                else GamePhase.SHERIFF_ELECTION
            )
            try:
                committed = transition_phase(
                    intermediate,
                    target_phase,
                    expected_revision=revision,
                    now=utc_now() if now is None else now,
                )
            except (TypeError, ValueError) as exc:
                raise SheriffElectionError("PHASE_INVALID", str(exc)) from exc
            self._state = committed
            return committed

    async def submit_sheriff_vote(
        self,
        request: VoteRequest,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Commit one private election ballot under the election capability."""

        if not isinstance(request, VoteRequest):
            raise TypeError("request must be a VoteRequest")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            election = self._require_sheriff_phase(
                current,
                phase=(GamePhase.SHERIFF_ELECTION, GamePhase.SHERIFF_ELECTION_PK),
                status=SheriffElectionStatus.VOTING,
            )
            if election.vote is None:
                raise SheriffElectionError("VOTE_NOT_OPEN", "the sheriff election vote is not open")
            try:
                _authoritative_vote_request(
                    current,
                    election.vote,
                    request,
                    allow_first_day_sheriff_participants=True,
                )
                accepted = election.submit_vote(request)
            except SheriffElectionError:
                raise
            except VoteError as exc:
                raise SheriffElectionError(exc.code, str(exc)) from exc
            if accepted == election:
                return current
            data = _state_data(current)
            data["sheriff_election"] = _sheriff_election_payload(accepted)
            data["state_revision"] = current.state_revision + 1
            data["updated_at"] = utc_now() if now is None else now
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def finalize_sheriff_election(
        self,
        board: BoardDefinition,
        *,
        force: bool = False,
        reason: str = "all_votes_received",
        tie_round: int | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Lock and tally the election, retaining unresolved ties for GM."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            election = self._require_sheriff_phase(
                current,
                phase=(GamePhase.SHERIFF_ELECTION, GamePhase.SHERIFF_ELECTION_PK),
                status=SheriffElectionStatus.VOTING,
            )
            _validate_sheriff_board(board, current)
            try:
                pending = election.finalize(
                    board=board,
                    force=force,
                    reason=reason,
                    tie_round=tie_round,
                )
            except SheriffElectionError:
                raise
            # A missing tie policy deliberately raises before this replacement,
            # leaving the ballot OPEN and the phase unchanged.
            data = _state_data(current)
            data["sheriff_election"] = _sheriff_election_payload(pending)
            data["state_revision"] = current.state_revision + 1
            data["updated_at"] = utc_now() if now is None else now
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def confirm_sheriff_election(
        self,
        board: BoardDefinition,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Apply the explicit GM decision and install the 1.5-style board weight."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            _validate_sheriff_board(board, current)
            # A completed confirmation can be retried after a caller loses
            # its response.  The event correlation is the frozen vote window
            # ID, so this recovery path never appends a duplicate result.
            if current.phase in {
                GamePhase.SHERIFF_ELECTION_PK_SPEECH,
                GamePhase.SHERIFF_TRANSFER,
            }:
                recovered = _load_sheriff_election(current.sheriff_election)
                correlation_id: str | None = None
                if (
                    current.phase is GamePhase.SHERIFF_ELECTION_PK_SPEECH
                    and recovered.status is SheriffElectionStatus.SPEECH
                    and recovered.vote_history
                ):
                    correlation_id = recovered.vote_history[-1].window.window_id
                elif (
                    current.phase is GamePhase.SHERIFF_TRANSFER
                    and recovered.status
                    in {SheriffElectionStatus.RESOLVED, SheriffElectionStatus.NO_SHERIFF}
                    and recovered.vote is not None
                ):
                    correlation_id = recovered.vote.window.window_id
                if correlation_id is not None and any(
                    event.event_type is EventType.VOTE_RESULT
                    and event.correlation_id == correlation_id
                    for event in _typed_events(current)
                ):
                    return current
            election = self._require_sheriff_phase(
                current,
                phase=(GamePhase.SHERIFF_ELECTION, GamePhase.SHERIFF_ELECTION_PK),
                status=SheriffElectionStatus.WAITING_GM,
            )
            # Confirming the first tied tally is itself the atomic edge into
            # the PK speech phase.  The tied candidates and tie_round are
            # derived from the private tally; callers cannot replace either.
            if (
                current.phase is GamePhase.SHERIFF_ELECTION
                and election.decision is not None
                and election.decision.action.name == "PK"
            ):
                try:
                    if election.vote is None:
                        raise SheriffElectionError(
                            "TALLY_INVALID", "the pending sheriff tally has no vote state"
                        )
                    confirmed_vote = election.vote.confirm_tally()
                    started_pk = election.model_copy(update={"vote": confirmed_vote}).begin_pk()
                except SheriffElectionError:
                    raise
                except VoteError as exc:
                    raise SheriffElectionError(exc.code, str(exc)) from exc
                target_phase = GamePhase.SHERIFF_ELECTION_PK_SPEECH
                if not can_transition(current.phase, target_phase):
                    raise SheriffElectionError(
                        "PHASE_INVALID", "sheriff election cannot enter PK speech"
                    )
                timestamp = now or utc_now()
                events = _typed_events(current)
                event = GameEvent.public(
                    event_id=max((item.event_id for item in events), default=0) + 1,
                    game_id=current.game_id,
                    state_revision=current.state_revision + 1,
                    round_no=current.round_no,
                    phase=current.phase,
                    created_at=timestamp,
                    event_type=EventType.VOTE_RESULT,
                    eligible_seats=tuple(sorted(current.players)),
                    payload=_public_vote_payload(
                        confirmed_vote,
                        board=board,
                        vote_kind="sheriff",
                    ),
                    correlation_id=confirmed_vote.window.window_id,
                )
                committed_events = _reduce_event_delivery(
                    current,
                    StatePatch.event_delivery(
                        (event,),
                        expected_revision=revision,
                        now=timestamp,
                    ),
                )
                data = _state_data(committed_events)
                data["sheriff_election"] = _sheriff_election_payload(started_pk)
                data["phase"] = target_phase
                data["state_revision"] = committed_events.state_revision
                data["updated_at"] = timestamp
                committed = GameState.model_validate(data)
                self._state = committed
                return committed
            try:
                confirmed = election.confirm()
            except SheriffElectionError:
                raise

            if confirmed.vote is None or confirmed.vote.public_result is None:
                raise SheriffElectionError(
                    "TALLY_INVALID", "confirmed sheriff election has no public result"
                )
            target_phase = GamePhase.SHERIFF_TRANSFER
            if not can_transition(current.phase, target_phase):
                raise SheriffElectionError(
                    "PHASE_INVALID", "sheriff election cannot enter transfer"
                )
            timestamp = now or utc_now()
            events = _typed_events(current)
            event = GameEvent.public(
                event_id=max((item.event_id for item in events), default=0) + 1,
                game_id=current.game_id,
                state_revision=current.state_revision + 1,
                round_no=current.round_no,
                phase=current.phase,
                created_at=timestamp,
                event_type=EventType.VOTE_RESULT,
                eligible_seats=tuple(sorted(current.players)),
                payload=_public_vote_payload(
                    confirmed.vote,
                    board=board,
                    vote_kind=(
                        "sheriff_pk"
                        if current.phase is GamePhase.SHERIFF_ELECTION_PK
                        else "sheriff"
                    ),
                    elected_seat=confirmed.sheriff_seat,
                ),
                correlation_id=confirmed.vote.window.window_id,
            )
            committed_events = _reduce_event_delivery(
                current,
                StatePatch.event_delivery(
                    (event,),
                    expected_revision=revision,
                    now=timestamp,
                ),
            )

            data = _state_data(committed_events)
            data["sheriff_election"] = _sheriff_election_payload(confirmed)
            data["sheriff_seat"] = confirmed.sheriff_seat
            if confirmed.sheriff_seat is not None:
                player = current.players.get(confirmed.sheriff_seat)
                if player is None:
                    raise SheriffElectionError(
                        "SEAT_NOT_ASSIGNED", "elected sheriff seat is not assigned"
                    )
                players = dict(data["players"])
                player_data = dict(players[confirmed.sheriff_seat])
                player_data["vote_weight"] = board.day_flow.sheriff.vote_weight
                players[confirmed.sheriff_seat] = player_data
                data["players"] = players
            data["phase"] = target_phase
            data["state_revision"] = committed_events.state_revision
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def complete_sheriff_transfer(
        self,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Close the explicit badge-transfer boundary and enter day speech."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            election = self._require_sheriff_phase(current, phase=GamePhase.SHERIFF_TRANSFER)
            if election.status not in {
                SheriffElectionStatus.RESOLVED,
                SheriffElectionStatus.NO_SHERIFF,
            }:
                raise SheriffElectionError("ELECTION_PENDING", "sheriff election is not confirmed")
            try:
                committed = transition_phase(
                    current,
                    GamePhase.DAY_SPEECH,
                    expected_revision=revision,
                    now=utc_now() if now is None else now,
                )
            except (TypeError, ValueError) as exc:
                raise SheriffElectionError("PHASE_INVALID", str(exc)) from exc
            self._state = committed
            return committed

    async def open_sheriff_badge_window(
        self,
        board: BoardDefinition,
        window: ActionWindow,
        *,
        source_seat: int,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Bind one death-invalidated sheriff to an explicit badge action.

        The marker and the action window are installed in one serialized
        replacement.  The elected office record remains separate from the
        first-day election so a restart cannot accidentally rewrite history.
        """

        if not isinstance(window, ActionWindow):
            raise TypeError("window must be an ActionWindow")
        if type(source_seat) is not int:
            raise SheriffElectionError("BADGE_SOURCE_INVALID", "source_seat must be an integer")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            _validate_sheriff_board(board, current)
            policy = board.day_flow.sheriff
            if current.phase not in _sheriff_badge_allowed_phases(current):
                raise SheriffElectionError(
                    "PHASE_NOT_ALLOWED", "badge choice requires a daytime boundary"
                )
            trigger_origin: tuple[str, str] | None = None
            if current.phase is GamePhase.TRIGGER_ACTION:
                if current.pending_resolution is not None:
                    raise SheriffElectionError(
                        "BADGE_PENDING", "trigger resolution must finish before badge choice"
                    )
                trigger_origin = _sheriff_badge_trigger_origin(current)
                if trigger_origin is None or trigger_origin[0] != "DAY_EXILE":
                    raise SheriffElectionError(
                        "BADGE_ORIGIN_INVALID",
                        "trigger badge choice requires a closed DAY_EXILE source",
                    )
            rule_boundary = next(
                (
                    item
                    for item in current.rule_boundaries
                    if item.is_pending
                    and item.sheriff_badge_required
                    and source_seat in item.death_seats
                ),
                None,
            )
            required_rule_badge = any(
                item.is_pending and item.sheriff_badge_required for item in current.rule_boundaries
            )
            if required_rule_badge and rule_boundary is None:
                raise SheriffElectionError(
                    "BADGE_BOUNDARY_INVALID",
                    "badge choice is not bound to the pending confirmed-death boundary",
                )
            marker = current.sheriff_badge
            if isinstance(marker, dict):
                if (
                    marker.get("status") == "COMPLETE"
                    and rule_boundary is not None
                    and marker.get("rule_boundary_id") == rule_boundary.boundary_id
                ):
                    return current
                if marker.get("status") == "OPEN" and marker.get("source_seat") != source_seat:
                    raise SheriffElectionError(
                        "BADGE_PENDING", "another badge choice is already pending"
                    )
                if (
                    marker.get("status") == "OPEN"
                    and rule_boundary is not None
                    and marker.get("rule_boundary_id") != rule_boundary.boundary_id
                ):
                    raise SheriffElectionError(
                        "BADGE_BOUNDARY_INVALID", "open badge belongs to another rule boundary"
                    )
                raw_id = marker.get("window_id")
                if (
                    marker.get("status") == "OPEN"
                    and isinstance(raw_id, str)
                    and raw_id in current.action_windows
                ):
                    return current
            if current.sheriff_seat != source_seat:
                raise SheriffElectionError(
                    "BADGE_SOURCE_INVALID", "source is not the current sheriff"
                )
            player = current.players.get(source_seat)
            if player is None or (player.alive and player.can_vote):
                raise SheriffElectionError("BADGE_NOT_TRIGGERED", "the sheriff is still eligible")
            if policy.transfer_enabled is not True:
                raise SheriffElectionError("BADGE_TRANSFER_DISABLED", "badge transfer is disabled")
            if player.alive and policy.transfer_on_resignation is not True:
                raise SheriffElectionError(
                    "BADGE_TRANSFER_DISABLED", "resignation badge transfer is disabled"
                )
            if not player.alive and policy.transfer_on_death is not True:
                raise SheriffElectionError(
                    "BADGE_TRANSFER_DISABLED", "death badge transfer is disabled"
                )
            if window.game_id != current.game_id or window.phase is not current.phase:
                raise SheriffElectionError(
                    "WINDOW_INVALID", "badge window belongs to another phase/game"
                )
            if window.session_epoch != player.session_epoch:
                raise SheriffElectionError(
                    "WINDOW_INVALID", "badge window uses an obsolete session"
                )
            if window.allowed_seats != (source_seat,):
                raise SheriffElectionError(
                    "WINDOW_INVALID", "badge window must authorize only the old sheriff"
                )
            if window.allowed_action_codes != (201, 202):
                raise SheriffElectionError(
                    "WINDOW_INVALID", "badge window must allow transfer and tear"
                )
            if window.min_actions != 1 or window.max_actions != 1 or window.allow_pass:
                raise SheriffElectionError("WINDOW_INVALID", "badge window cardinality is invalid")
            candidates = window.visible_context.get("candidate_seats")
            if not isinstance(candidates, (list, tuple)):
                raise SheriffElectionError("WINDOW_INVALID", "badge candidates are missing")
            legal = tuple(
                sorted(
                    seat
                    for seat in candidates
                    if type(seat) is int
                    and seat in current.players
                    and seat != source_seat
                    and current.players[seat].alive
                    and current.players[seat].can_vote
                )
            )
            if tuple(candidates) != legal:
                raise SheriffElectionError(
                    "WINDOW_INVALID", "badge candidates are not authoritative"
                )
            visible_context = dict(window.visible_context)
            visible_context["kind"] = "sheriff_badge"
            visible_context["source_seat"] = source_seat
            if rule_boundary is not None:
                visible_context["rule_boundary_id"] = rule_boundary.boundary_id
            if trigger_origin is not None:
                visible_context["origin"] = trigger_origin[0]
                visible_context["origin_resolution_id"] = trigger_origin[1]
            window = window.model_copy(update={"visible_context": visible_context})
            timestamp = utc_now() if now is None else now
            data = _state_data(current)
            windows = dict(data["action_windows"])
            windows[window.window_id] = window.model_dump(mode="json")
            data["action_windows"] = windows
            data["sheriff_badge"] = {
                "status": "OPEN",
                "source_seat": source_seat,
                "source_session_epoch": player.session_epoch,
                "office_seat": source_seat,
                "window_id": window.window_id,
                "observation_revision": revision,
                "candidate_seats": list(legal),
                "trigger": "death" if not player.alive else "resignation",
                "rule_boundary_id": (
                    rule_boundary.boundary_id if rule_boundary is not None else None
                ),
                "source_group_id": (
                    rule_boundary.source_group_id if rule_boundary is not None else None
                ),
                "source_batch_id": (
                    rule_boundary.source_batch_id if rule_boundary is not None else None
                ),
                "death_fact_ids": (
                    list(rule_boundary.death_fact_ids) if rule_boundary is not None else []
                ),
                "opened_at": timestamp.isoformat(),
            }
            data["state_revision"] = revision + 1
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_sheriff_badge_decision(
        self,
        board: BoardDefinition,
        *,
        request_id: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Atomically apply transfer/tear, close its window, and announce it."""

        if not isinstance(request_id, str) or not request_id:
            raise SheriffElectionError("REQUEST_INVALID", "request_id is required")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            _validate_sheriff_board(board, current)
            marker = current.sheriff_badge
            if (
                isinstance(marker, dict)
                and marker.get("status") == "COMPLETE"
                and marker.get("request_id") == request_id
            ):
                return current
            if current.phase not in _sheriff_badge_allowed_phases(current):
                raise SheriffElectionError(
                    "PHASE_NOT_ALLOWED", "badge choice requires a daytime boundary"
                )
            if not isinstance(marker, dict) or marker.get("status") != "OPEN":
                raise SheriffElectionError("BADGE_NOT_OPEN", "no sheriff badge choice is pending")
            window_id = marker.get("window_id")
            source_seat = marker.get("source_seat")
            source_epoch = marker.get("source_session_epoch")
            office_seat = marker.get("office_seat")
            if (
                not isinstance(window_id, str)
                or type(source_seat) is not int
                or type(source_epoch) is not int
                or office_seat != source_seat
                or current.sheriff_seat != source_seat
            ):
                raise SheriffElectionError("BADGE_STATE_INVALID", "badge marker is malformed")
            raw_window = current.action_windows.get(window_id)
            if raw_window is None:
                raise SheriffElectionError("WINDOW_NOT_FOUND", "badge action window is missing")
            window = _load_action_window(raw_window)
            binding_error = _sheriff_badge_binding_error(current, window)
            if binding_error is not None:
                raise SheriffElectionError("BADGE_ACTION_INVALID", binding_error)
            if window.closed_at is not None:
                raise SheriffElectionError("WINDOW_CLOSED", "badge action window is already closed")
            request = _stored_action_request(current, request_id)
            if request.window_id != window_id or request.seat != source_seat:
                raise SheriffElectionError(
                    "REQUEST_MISMATCH", "request is not for the pending badge"
                )
            player = current.players.get(source_seat)
            if player is None or player.session_epoch != source_epoch:
                raise SheriffElectionError(
                    "SESSION_MISMATCH", "badge request uses an obsolete sheriff session"
                )
            if player.current_request_id != request_id:
                raise SheriffElectionError("REQUEST_INVALID", "badge request is no longer active")
            raw_request = current.action_requests.get(request_id)
            if not isinstance(raw_request, dict) or raw_request.get("status") != "PENDING":
                raise SheriffElectionError("REQUEST_INVALID", "badge request is not pending")
            if len(request.actions) != 1:
                raise SheriffElectionError(
                    "ACTION_INVALID", "badge choice requires exactly one action"
                )
            action = request.actions[0]
            if action.action_code == 201:
                if len(action.targets) != 1:
                    raise SheriffElectionError("TARGET_INVALID", "transfer requires one target")
                target = action.targets[0]
                target_player = current.players.get(target)
                marker_candidates = marker.get("candidate_seats")
                if (
                    not isinstance(marker_candidates, (list, tuple))
                    or target not in marker_candidates
                ):
                    raise SheriffElectionError(
                        "TARGET_NOT_ALLOWED",
                        "transfer target is not in the frozen candidate set",
                    )
                if (
                    target == source_seat
                    or target_player is None
                    or not target_player.alive
                    or not target_player.can_vote
                ):
                    raise SheriffElectionError(
                        "TARGET_NOT_ALLOWED",
                        "transfer target is not an eligible living voter",
                    )
                content = f"警徽由 {source_seat} 号移交给 {target} 号。"
                new_sheriff: int | None = target
            elif action.action_code == 202:
                if action.targets:
                    raise SheriffElectionError(
                        "TARGET_INVALID", "tearing the badge takes no target"
                    )
                content = f"{source_seat} 号警长出局后撕毁警徽，本局不再有警长。"
                target = None
                new_sheriff = None
            else:
                raise SheriffElectionError(
                    "ACTION_NOT_ALLOWED", "badge choice must use action 201 or 202"
                )
            timestamp = utc_now() if now is None else now
            commit_revision = revision + 1
            data = _state_data(current)
            players = dict(data["players"])
            old_data = dict(players[source_seat])
            old_data["vote_weight"] = 1.0
            players[source_seat] = old_data
            if new_sheriff is not None:
                target_data = dict(players[new_sheriff])
                target_data["vote_weight"] = board.day_flow.sheriff.vote_weight
                players[new_sheriff] = target_data
            data["players"] = players
            data["sheriff_seat"] = new_sheriff
            closed = window.model_copy(update={"closed_at": timestamp})
            windows = dict(data["action_windows"])
            windows[window_id] = closed.model_dump(mode="json")
            data["action_windows"] = windows
            requests = dict(data["action_requests"])
            request_data = dict(requests[request_id])
            # Badge choices use the same terminal request status as every
            # other successfully committed action.  The badge-specific
            # outcome is carried in metadata so snapshot boundaries,
            # replay, and dependent action readers keep one status contract.
            request_data["status"] = "CONFIRMED"
            request_data["resolution_kind"] = "SHERIFF_BADGE"
            request_data["resolved_action_code"] = action.action_code
            request_data["resolved_target_seat"] = target
            requests[request_id] = request_data
            data["action_requests"] = requests
            actor_data = dict(players[source_seat])
            actor_data["current_request_id"] = None
            players[source_seat] = actor_data
            data["players"] = players
            # ``GameState`` freezes nested JSON arrays as tuples.  Thaw the
            # marker before persisting its completion fields back into the
            # strict JSON extension slot.
            marker = json.loads(json.dumps(marker))
            marker.update(
                {
                    "status": "COMPLETE",
                    "request_id": request_id,
                    "action_code": action.action_code,
                    "target_seat": target,
                    "completed_at": timestamp.isoformat(),
                }
            )
            data["sheriff_badge"] = marker
            events = _typed_events(current)
            event = GameEvent.public(
                event_id=max((item.event_id for item in events), default=0) + 1,
                game_id=current.game_id,
                state_revision=commit_revision,
                round_no=current.round_no,
                phase=current.phase,
                created_at=timestamp,
                event_type=EventType.ANNOUNCEMENT,
                eligible_seats=tuple(sorted(current.players)),
                payload=PublicAnnouncementPayload(content=content),
                correlation_id=f"sheriff-badge-{request_id}",
            )
            data["events"] = (*data["events"], event)
            audits = list(data["moderator_audit"])
            audits.append(
                {
                    "operation": "SHERIFF_BADGE",
                    "status": "TRANSFERRED" if new_sheriff is not None else "TORN",
                    "source_seat": source_seat,
                    "target_seat": target,
                    "request_id": request_id,
                    "rule_boundary_id": marker.get("rule_boundary_id"),
                    "source_group_id": marker.get("source_group_id"),
                    "source_batch_id": marker.get("source_batch_id"),
                    "death_fact_ids": list(
                        _frozen_string_tuple(marker.get("death_fact_ids")) or ()
                    ),
                    "base_revision": revision,
                    "committed_revision": commit_revision,
                    "created_at": timestamp.isoformat(),
                }
            )
            data["moderator_audit"] = tuple(audits)
            data["state_revision"] = commit_revision
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def complete_sheriff_badge(
        self,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Close a completed death/resignation badge window at its day edge.

        The decision and its public announcement are committed separately from
        this lifecycle edge so a moderator restart can resume after the player
        response has been accepted.  A new badge window may later overwrite
        the completed marker when the successor becomes ineligible.
        """

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            current = self._state
            if current.phase not in _sheriff_badge_allowed_phases(current):
                raise SheriffElectionError(
                    "PHASE_NOT_ALLOWED", "badge completion requires a daytime boundary"
                )
            marker = current.sheriff_badge
            if not isinstance(marker, dict) or marker.get("status") != "COMPLETE":
                raise SheriffElectionError("BADGE_PENDING", "badge decision is not complete")
            if current.phase is GamePhase.TRIGGER_ACTION:
                window_id = marker.get("window_id")
                raw_window = (
                    current.action_windows.get(window_id) if isinstance(window_id, str) else None
                )
                if raw_window is None:
                    raise SheriffElectionError("WINDOW_NOT_FOUND", "badge action window is missing")
                try:
                    badge_window = _load_action_window(raw_window)
                except (TypeError, ValueError) as exc:
                    raise SheriffElectionError(
                        "WINDOW_INVALID", "badge action window is malformed"
                    ) from exc
                if current.pending_resolution is not None:
                    raise SheriffElectionError(
                        "BADGE_PENDING", "trigger resolution is still pending"
                    )
                origin = _sheriff_badge_trigger_origin(current)
                if (
                    origin is None
                    or origin[0] != "DAY_EXILE"
                    or badge_window.visible_context.get("origin") != "DAY_EXILE"
                    or badge_window.visible_context.get("origin_resolution_id") != origin[1]
                ):
                    raise SheriffElectionError(
                        "BADGE_ACTION_INVALID",
                        "trigger badge requires a closed DAY_EXILE source",
                    )
            if current.serial_turn is not None or current.pending_resolution is not None:
                raise SheriffElectionError("BADGE_PENDING", "badge action is still active")
            for raw_window in current.action_windows.values():
                try:
                    window = _load_action_window(raw_window)
                except ValueError as exc:
                    raise SheriffElectionError(
                        "WINDOW_INVALID", "badge window is malformed"
                    ) from exc
                if _is_sheriff_badge_window(window) and window.closed_at is None:
                    raise SheriffElectionError("WINDOW_CLOSED", "badge action window is still open")
            source = marker.get("source_seat")
            if type(source) is not int:
                raise SheriffElectionError("BADGE_STATE_INVALID", "badge source is malformed")
            player = current.players.get(source)
            if player is None or player.current_request_id is not None:
                raise SheriffElectionError(
                    "REQUEST_INVALID", "badge actor still has an active request"
                )
            timestamp = utc_now() if now is None else now
            rule_boundary_id = marker.get("rule_boundary_id")
            typed_boundary: RuleBoundary | None = None
            if rule_boundary_id is not None:
                typed_boundary = next(
                    (
                        item
                        for item in current.rule_boundaries
                        if item.boundary_id == rule_boundary_id
                        and item.sheriff_badge_required
                        and item.is_pending
                    ),
                    None,
                )
                if (
                    typed_boundary is None
                    or marker.get("source_group_id") != typed_boundary.source_group_id
                    or marker.get("source_batch_id") != typed_boundary.source_batch_id
                    or _frozen_string_tuple(marker.get("death_fact_ids"))
                    != typed_boundary.death_fact_ids
                    or source not in typed_boundary.death_seats
                ):
                    raise SheriffElectionError(
                        "BADGE_BOUNDARY_INVALID",
                        "completed badge is detached from its confirmed-death boundary",
                    )
                request_id = marker.get("request_id")
                window_id = marker.get("window_id")
                action_code = marker.get("action_code")
                target_seat = marker.get("target_seat")
                raw_request = (
                    current.action_requests.get(request_id) if isinstance(request_id, str) else None
                )
                decision_status = "TRANSFERRED" if action_code == 201 else "TORN"
                request_matches = (
                    isinstance(request_id, str)
                    and bool(request_id)
                    and isinstance(window_id, str)
                    and type(action_code) is int
                    and action_code in {201, 202}
                    and isinstance(raw_request, Mapping)
                    and raw_request.get("window_id") == window_id
                    and raw_request.get("seat") == source
                    and raw_request.get("session_epoch") == marker.get("source_session_epoch")
                    and raw_request.get("status") == "CONFIRMED"
                    and raw_request.get("resolution_kind") == "SHERIFF_BADGE"
                    and raw_request.get("resolved_action_code") == action_code
                    and raw_request.get("resolved_target_seat") == target_seat
                    and (action_code != 201 or type(target_seat) is int)
                    and (action_code != 202 or target_seat is None)
                )
                raw_badge_window = current.action_windows.get(
                    window_id if isinstance(window_id, str) else ""
                )
                try:
                    completed_boundary_badge_window: ActionWindow | None = (
                        _load_action_window(raw_badge_window)
                        if raw_badge_window is not None
                        else None
                    )
                except (TypeError, ValueError) as exc:
                    raise SheriffElectionError(
                        "BADGE_BOUNDARY_INVALID", "completed badge window is malformed"
                    ) from exc
                window_matches = (
                    completed_boundary_badge_window is not None
                    and completed_boundary_badge_window.closed_at is not None
                    and completed_boundary_badge_window.game_id == current.game_id
                    and completed_boundary_badge_window.session_epoch
                    == marker.get("source_session_epoch")
                    and completed_boundary_badge_window.allowed_seats == (source,)
                    and completed_boundary_badge_window.visible_context.get("kind")
                    == "sheriff_badge"
                    and completed_boundary_badge_window.visible_context.get("source_seat") == source
                    and completed_boundary_badge_window.visible_context.get("rule_boundary_id")
                    == typed_boundary.boundary_id
                )
                decision_audited = any(
                    audit.get("operation") == "SHERIFF_BADGE"
                    and audit.get("status") == decision_status
                    and audit.get("source_seat") == source
                    and audit.get("target_seat") == target_seat
                    and audit.get("request_id") == request_id
                    and audit.get("rule_boundary_id") == typed_boundary.boundary_id
                    and audit.get("source_group_id") == typed_boundary.source_group_id
                    and audit.get("source_batch_id") == typed_boundary.source_batch_id
                    and _frozen_string_tuple(audit.get("death_fact_ids"))
                    == typed_boundary.death_fact_ids
                    for audit in current.moderator_audit
                )
                if not request_matches or not window_matches or not decision_audited:
                    raise SheriffElectionError(
                        "BADGE_BOUNDARY_INVALID",
                        "completed badge lacks a matching decision request and boundary audit",
                    )
            if current.phase is GamePhase.DAY_SPEECH:
                data = _state_data(current)
                data["state_revision"] = revision + 1
                data["updated_at"] = timestamp
                transitioned = GameState.model_validate(data)
            elif typed_boundary is not None:
                # A typed rule boundary temporarily stages host work in its
                # current phase; it must return through advance_rule_workflow
                # before the ordinary phase lifecycle moves on.
                data = _state_data(current)
                data["state_revision"] = revision + 1
                data["updated_at"] = timestamp
                transitioned = GameState.model_validate(data)
            else:
                target_phase = (
                    GamePhase.DAY_SPEECH
                    if current.phase is GamePhase.DAY_ANNOUNCE
                    else GamePhase.VICTORY_CHECK
                )
                try:
                    transitioned = transition_phase(
                        current,
                        target_phase,
                        expected_revision=revision,
                        now=timestamp,
                    )
                except (TypeError, ValueError) as exc:
                    raise SheriffElectionError("PHASE_INVALID", str(exc)) from exc
            data = _state_data(transitioned)
            if typed_boundary is not None:
                boundaries = tuple(
                    item.model_copy(
                        update={
                            "sheriff_badge_completed": True,
                            "completed_at": (
                                timestamp
                                if not item.last_words_required
                                or item.last_words_completed_seats == item.last_words_seats
                                else None
                            ),
                        }
                    )
                    if item.boundary_id == typed_boundary.boundary_id
                    else item
                    for item in current.rule_boundaries
                )
                data["rule_boundaries"] = tuple(
                    item.model_dump(mode="python") for item in boundaries
                )
            audits = list(data["moderator_audit"])
            audits.append(
                {
                    "operation": "SHERIFF_BADGE_COMPLETE",
                    "source_seat": source,
                    "target_seat": marker.get("target_seat"),
                    "action_code": marker.get("action_code"),
                    "rule_boundary_id": (
                        typed_boundary.boundary_id if typed_boundary is not None else None
                    ),
                    "source_group_id": (
                        typed_boundary.source_group_id if typed_boundary is not None else None
                    ),
                    "source_batch_id": (
                        typed_boundary.source_batch_id if typed_boundary is not None else None
                    ),
                    "death_fact_ids": (
                        list(typed_boundary.death_fact_ids) if typed_boundary is not None else []
                    ),
                    "request_id": marker.get("request_id"),
                    "base_revision": revision,
                    "committed_revision": transitioned.state_revision,
                    "created_at": timestamp.isoformat(),
                }
            )
            data["moderator_audit"] = tuple(audits)
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_day_exile(
        self,
        decision: DayExileDecision,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Commit one explicit board-derived exile result atomically.

        ``DayCoordinator`` constructs ``decision`` from the frozen board
        snapshot.  This method owns the serialized state replacement: player
        status, public announcement, GM audit, optional hunter trigger marker,
        and phase are committed together.  A vote confirmation alone never
        calls this method and therefore never changes player life state.
        """

        # Import lazily so the manager remains the dependency root for the
        # coordinator and the decision module stays a pure policy helper.
        from .day_resolution import DayExileDecision as Decision

        if not isinstance(decision, Decision):
            raise TypeError("decision must be a DayExileDecision")
        if not decision.resolution_id or len(decision.resolution_id) > 128:
            raise EventCommitError("RESOLUTION_ID_INVALID: exile resolution ID is invalid")
        if decision.next_phase not in {GamePhase.DAY_RESOLVE, GamePhase.TRIGGER_ACTION}:
            raise EventCommitError("PHASE_MISMATCH: invalid exile target phase")

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            existing = next(
                (
                    audit
                    for audit in self._state.moderator_audit
                    if isinstance(audit, dict)
                    and audit.get("operation") == "DAY_EXILE"
                    and audit.get("resolution_id") == decision.resolution_id
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.get("target_seat") != decision.target_seat
                    or existing.get("outcome_code") != decision.outcome_code
                ):
                    raise ResolutionError(
                        "IDEMPOTENCY_CONFLICT",
                        "exile resolution ID was already committed differently",
                    )
                return self._state

            if self._state.phase is not GamePhase.DAY_RESOLVE:
                raise EventCommitError("PHASE_MISMATCH: exile requires DAY_RESOLVE")
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no resolved vote to exile")
            vote_state = _load_vote_state(self._state.vote_state)
            if vote_state.status is not VoteStatus.RESOLVED:
                raise VoteError("TALLY_NOT_CONFIRMED", "confirm the vote before resolving exile")
            result = vote_state.public_result
            if result is None:
                raise EventCommitError("VOTE_RESULT_INVALID: resolved vote has no result")
            if result.eliminated_seat != decision.target_seat:
                raise ResolutionError(
                    "TARGET_MISMATCH",
                    "exile decision does not match the confirmed vote result",
                )
            if decision.window_id != vote_state.window.window_id:
                raise ResolutionError("WINDOW_MISMATCH", "exile decision uses another vote window")
            if decision.next_phase is not self._state.phase and not can_transition(
                self._state.phase, decision.next_phase
            ):
                raise EventCommitError("PHASE_MISMATCH: invalid exile phase transition")

            # ``DayExileDecision`` is an adapter value, not an authorization
            # token.  Rebuild the executable part of the result from the
            # confirmed vote and the private trigger abilities on the target
            # seat before creating any event or changing player state.  This
            # prevents a caller from reanimating a dead seat, fabricating a
            # hunter-like trigger window, or consuming an unrelated ability by
            # constructing a decision by hand.
            expected_exile: dict[str, bool | str | int | GamePhase | None]
            if decision.target_seat is None:
                expected_exile = {
                    "outcome_code": "no_exile",
                    "alive_after": None,
                    "death_cause": None,
                    "can_vote_after": None,
                    "next_phase": GamePhase.DAY_RESOLVE,
                    "trigger_action": False,
                    "trigger_ability_id": None,
                    "trigger_action_code": None,
                    "trigger_event": None,
                }
            else:
                if isinstance(decision.target_seat, bool) or not isinstance(
                    decision.target_seat, int
                ):
                    raise ResolutionError("TARGET_INVALID", "exile target seat must be an integer")
                target_player = self._state.players.get(decision.target_seat)
                if target_player is None:
                    raise EventCommitError("SEAT_NOT_ASSIGNED: exile target is unknown")
                if not target_player.alive:
                    raise ResolutionError("PLAYER_ALREADY_RESOLVED", "exile target is already dead")

                from .day_resolution import _trigger_abilities_for_event

                exile_abilities = _trigger_abilities_for_event(
                    target_player, TriggerEvent.EXILE_SELECTED
                )
                automatic = next(
                    (
                        ability
                        for ability in exile_abilities
                        if ability.trigger.mode is TriggerMode.AUTOMATIC
                    ),
                    None,
                )
                choice = next(
                    (
                        ability
                        for ability in exile_abilities
                        if ability.trigger.mode is TriggerMode.PLAYER_CHOICE
                    ),
                    None,
                )
                if automatic is None and choice is None:
                    choice = next(
                        (
                            ability
                            for ability in _trigger_abilities_for_event(
                                target_player,
                                TriggerEvent.DEATH_CONFIRMED,
                                death_cause="exiled",
                            )
                            if ability.trigger.mode is TriggerMode.PLAYER_CHOICE
                        ),
                        None,
                    )
                trigger = automatic or choice
                if trigger is not None and not trigger.trigger.once:
                    raise EventCommitError(
                        "TRIGGER_USAGE_UNSUPPORTED",
                        "trigger abilities with once=false are not supported by the state model",
                    )
                effects = set(trigger.trigger.effects) if trigger is not None else set()
                survives = TriggerEffect.SURVIVE_TRIGGER in effects
                if automatic is not None:
                    expected_exile = {
                        "outcome_code": "trigger_automatic",
                        "alive_after": True if survives else False,
                        "death_cause": None if survives else "exiled",
                        "can_vote_after": (
                            False if TriggerEffect.REMOVE_VOTE_RIGHT in effects else bool(survives)
                        ),
                        "next_phase": GamePhase.DAY_RESOLVE,
                        "trigger_action": False,
                        "trigger_ability_id": automatic.ability_id,
                        "trigger_action_code": automatic.action_code,
                        "trigger_event": automatic.trigger.event.value,
                    }
                elif choice is not None:
                    expected_exile = {
                        "outcome_code": "trigger_player_choice",
                        "alive_after": True if survives else False,
                        "death_cause": None if survives else "exiled",
                        "can_vote_after": False,
                        "next_phase": GamePhase.TRIGGER_ACTION,
                        "trigger_action": True,
                        "trigger_ability_id": choice.ability_id,
                        "trigger_action_code": choice.action_code,
                        "trigger_event": choice.trigger.event.value,
                    }
                else:
                    expected_exile = {
                        "outcome_code": "exiled",
                        "alive_after": False,
                        "death_cause": "exiled",
                        "can_vote_after": False,
                        "next_phase": GamePhase.DAY_RESOLVE,
                        "trigger_action": False,
                        "trigger_ability_id": None,
                        "trigger_action_code": None,
                        "trigger_event": None,
                    }

            for field_name, expected in expected_exile.items():
                if getattr(decision, field_name) != expected:
                    raise ResolutionError(
                        "EXILE_DECISION_MISMATCH",
                        "decision field does not match the authoritative exile outcome: "
                        f"{field_name}",
                    )

            timestamp = now or utc_now()
            commit_revision = self._state.state_revision + 1
            events = _typed_events(self._state)
            event_id = max((event.event_id for event in events), default=0) + 1
            public_event = GameEvent.public(
                event_id=event_id,
                game_id=self._state.game_id,
                state_revision=commit_revision,
                round_no=self._state.round_no,
                phase=self._state.phase,
                created_at=timestamp,
                event_type=EventType.ANNOUNCEMENT,
                eligible_seats=tuple(sorted(self._state.players)),
                payload=PublicAnnouncementPayload(content=decision.public_message),
                correlation_id=decision.resolution_id,
            )
            audit_details = dict(decision.audit_details)
            audit_details["resolution_id"] = decision.resolution_id
            audit_details["committed_revision"] = commit_revision
            gm_event = GameEvent.gm_only(
                event_id=event_id + 1,
                game_id=self._state.game_id,
                state_revision=commit_revision,
                round_no=self._state.round_no,
                phase=self._state.phase,
                created_at=timestamp,
                event_type=EventType.GM_AUDIT,
                payload=GmAuditPayload(
                    code="day_exile_committed",
                    details=cast(dict[str, JsonValue], audit_details),
                ),
                correlation_id=decision.resolution_id,
            )
            candidate = _reduce_event_delivery(
                self._state,
                StatePatch.event_delivery(
                    (public_event, gm_event),
                    expected_revision=revision,
                    now=timestamp,
                ),
            )
            data = _state_data(candidate)
            if decision.target_seat is not None:
                player = self._state.players.get(decision.target_seat)
                if player is None:
                    raise EventCommitError("SEAT_NOT_ASSIGNED: exile target is unknown")
                if decision.alive_after is False and not player.alive:
                    raise ResolutionError("PLAYER_ALREADY_RESOLVED", "exile target is already dead")
                players = dict(data["players"])
                player_data = dict(players[decision.target_seat])
                if decision.alive_after is not None:
                    player_data["alive"] = decision.alive_after
                if decision.death_cause is not None:
                    player_data["death_cause"] = decision.death_cause
                if decision.can_vote_after is not None:
                    player_data["can_vote"] = decision.can_vote_after
                if decision.trigger_ability_id is not None and not decision.trigger_action:
                    granted = []
                    found = False
                    for raw_ability in player_data.get("granted_trigger_abilities", ()):
                        item = dict(raw_ability)
                        if item.get("ability_id") == decision.trigger_ability_id:
                            if item.get("consumed") is True:
                                raise ResolutionError(
                                    "TRIGGER_ALREADY_CONSUMED",
                                    "automatic trigger ability was already consumed",
                                )
                            item["consumed"] = True
                            found = True
                        granted.append(item)
                    if not found:
                        raise ResolutionError(
                            "TRIGGER_NOT_GRANTED",
                            "automatic trigger ability is not granted to the target seat",
                        )
                    player_data["granted_trigger_abilities"] = tuple(granted)
                players[decision.target_seat] = player_data
                data["players"] = players
            if decision.trigger_action:
                if (
                    decision.trigger_ability_id is None
                    or decision.trigger_action_code is None
                    or decision.trigger_event is None
                ):
                    raise EventCommitError(
                        "TRIGGER_ACTION_INVALID: decision is missing its granted ability binding"
                    )
                data["pending_resolution"] = {
                    "operation": "DAY_EXILE",
                    "status": "TRIGGER_ACTION_REQUIRED",
                    "resolution_id": decision.resolution_id,
                    "seat": decision.target_seat,
                    "trigger_event": decision.trigger_event,
                    "ability_id": decision.trigger_ability_id,
                    "action_code": decision.trigger_action_code,
                    "death_cause": decision.death_cause,
                    "snapshot_revision": candidate.state_revision,
                }
            else:
                data["pending_resolution"] = None
            audit = dict(audit_details)
            audit["operation"] = "DAY_EXILE"
            audit["outcome_code"] = decision.outcome_code
            audit["target_seat"] = decision.target_seat
            audit["created_at"] = timestamp.isoformat()
            audits = list(data["moderator_audit"])
            audits.append(audit)
            data["moderator_audit"] = tuple(audits)
            data["phase"] = decision.next_phase
            data["state_revision"] = candidate.state_revision
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_confirmed_vote_exile(
        self,
        *,
        target_seat: int | None,
        vote_window_id: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Synthesize a host exile request from the unique confirmed vote result."""

        if not isinstance(vote_window_id, str) or not vote_window_id:
            raise EventCommitError("WINDOW_MISMATCH: vote window ID is invalid")
        if target_seat is not None and (type(target_seat) is not int or target_seat < 1):
            raise ResolutionError("TARGET_INVALID", "exile target must be an assigned seat")
        async with self._lock:
            state = self._state
            resolution_id = f"day-exile-{vote_window_id}"
            previous = next(
                (
                    item
                    for item in state.moderator_audit
                    if isinstance(item, Mapping)
                    and item.get("operation") == "DAY_EXILE"
                    and item.get("resolution_id") == resolution_id
                ),
                None,
            )
            if previous is not None:
                if (
                    previous.get("vote_window_id") != vote_window_id
                    or previous.get("target_seat") != target_seat
                    or previous.get("package_id")
                    != (self._execution_package.package_id if self._execution_package else None)
                ):
                    raise ResolutionError(
                        "IDEMPOTENCY_CONFLICT",
                        "confirmed exile result was already committed differently",
                    )
                return state
            revision = state.state_revision if expected_revision is None else expected_revision
            _revision_check(state, revision)
            if state.phase is not GamePhase.DAY_RESOLVE or state.vote_state is None:
                raise EventCommitError("PHASE_MISMATCH: confirmed exile requires DAY_RESOLVE")
            vote_state = _load_vote_state(state.vote_state)
            result = vote_state.public_result
            if (
                vote_state.status is not VoteStatus.RESOLVED
                or result is None
                or vote_state.window.window_id != vote_window_id
            ):
                raise VoteError("TALLY_NOT_CONFIRMED", "confirm the vote before resolving exile")
            if result.eliminated_seat != target_seat:
                raise ResolutionError(
                    "TARGET_MISMATCH",
                    "exile target must match the unique confirmed vote result",
                )
            timestamp = now or utc_now()
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise EventCommitError("TIMESTAMP: commit timestamp must include a timezone")
            timestamp = timestamp.astimezone(UTC)
            package = self._execution_package
            if package is None or self._rules is None:
                raise ResolutionError(
                    "RULE_PACKAGE_MISSING",
                    "host exile execution requires the game's pinned package",
                )
            candidate = state
            batch: ResolutionBatch | None = None
            request: SkillRequest | None = None
            extra_instances: tuple[AbilityInstance, ...] = ()
            if target_seat is not None:
                target = state.players.get(target_seat)
                if target is None:
                    raise EventCommitError("SEAT_NOT_ASSIGNED: exile target is unknown")
                if not target.alive:
                    raise ResolutionError("PLAYER_ALREADY_RESOLVED", "exile target is already dead")
                host_skills = tuple(
                    skill
                    for skill in package.skills
                    if skill.mode == "HOST"
                    and skill.action_code == 203
                    and GamePhase.DAY_RESOLVE.value in skill.timing
                    and not skill.grants
                )
                if len(host_skills) != 1:
                    raise ResolutionError(
                        "HOST_EXILE_UNAVAILABLE",
                        "frozen package must declare one host exile action for DAY_RESOLVE",
                    )
                skill = host_skills[0]
                request_id = (
                    "host-"
                    + hashlib.sha256(
                        f"{state.game_id}:{vote_window_id}:{package.package_id}".encode()
                    ).hexdigest()
                )
                instance_id = f"host:{skill.skill_id}"
                extra_instances = (
                    AbilityInstance(
                        ability_instance_id=instance_id,
                        skill_id=skill.skill_id,
                        actor_seat=target_seat,
                        grant_id="$host",
                    ),
                )
                request = SkillRequest(
                    request_id=request_id,
                    ability_instance_id=instance_id,
                    skill_id=skill.skill_id,
                    action_code=skill.action_code,
                    actor_seat=target_seat,
                    targets=(target_seat,),
                    origin="HOST",
                )
                group_id = f"day-resolve:{state.round_no}:{vote_window_id}"
                try:
                    batch = self._rules.plan(
                        state,
                        (request,),
                        group_id=group_id,
                        timing=GamePhase.DAY_RESOLVE.value,
                        extra_ability_instances=extra_instances,
                    )
                except (RuleAdapterError, TypeError, ValueError) as exc:
                    raise ResolutionError("RULE_PLAN_INVALID", str(exc)) from exc
                if batch.cost_updates or batch.state_updates:
                    raise ResolutionError(
                        "HOST_EXILE_UNSUPPORTED_EFFECT",
                        "host exile package may not charge player resources or write ability state",
                    )
                candidate = self._apply_rule_batch(
                    state,
                    batch,
                    (request,),
                    (),
                    (),
                    group_id=group_id,
                    timing=GamePhase.DAY_RESOLVE.value,
                    timestamp=timestamp,
                    extra_ability_instances=extra_instances,
                )

            target_after = candidate.players.get(target_seat) if target_seat is not None else None
            trigger_candidates: list[GrantedTriggerAbility] = []
            if target_after is not None:
                for triggered_ability in target_after.granted_trigger_abilities:
                    trigger_rule = triggered_ability.trigger
                    if (
                        triggered_ability.consumed
                        or trigger_rule.mode is not TriggerMode.PLAYER_CHOICE
                    ):
                        continue
                    if trigger_rule.event is TriggerEvent.EXILE_SELECTED:
                        trigger_candidates.append(triggered_ability)
                    elif (
                        trigger_rule.event is TriggerEvent.DEATH_CONFIRMED
                        and not target_after.alive
                        and target_after.death_cause in trigger_rule.allowed_death_causes
                    ):
                        trigger_candidates.append(triggered_ability)
            if len(trigger_candidates) > 1:
                raise ResolutionError(
                    "MULTIPLE_TRIGGER_ACTIONS",
                    "confirmed exile produced multiple player-choice triggers",
                )

            data = _state_data(candidate)
            current_events = _typed_events(candidate)
            next_event_id = max((event.event_id for event in current_events), default=0) + 1
            message = (
                "The confirmed vote resulted in no exile."
                if target_seat is None
                else (
                    f"Seat {target_seat} was selected by the confirmed vote."
                    if target_after is not None and target_after.alive
                    else f"Seat {target_seat} was exiled by the confirmed vote."
                )
            )
            public_event = GameEvent.public(
                event_id=next_event_id,
                game_id=state.game_id,
                state_revision=revision + 1,
                round_no=state.round_no,
                phase=state.phase,
                created_at=timestamp,
                event_type=EventType.ANNOUNCEMENT,
                eligible_seats=tuple(sorted(state.players)),
                payload=PublicAnnouncementPayload(content=message),
                correlation_id=resolution_id,
            )
            outcome_digest = batch.batch_id if batch is not None else None
            details: dict[str, JsonValue] = {
                "operation": "DAY_EXILE",
                "resolution_id": resolution_id,
                "vote_window_id": vote_window_id,
                "target_seat": target_seat,
                "outcome_code": (
                    "no_exile"
                    if target_seat is None
                    else "triggered"
                    if target_after is not None and target_after.alive
                    else "exiled"
                ),
                "package_id": package.package_id,
                "rule_batch_id": batch.batch_id if batch is not None else None,
            }
            gm_event = GameEvent.gm_only(
                event_id=next_event_id + 1,
                game_id=state.game_id,
                state_revision=revision + 1,
                round_no=state.round_no,
                phase=state.phase,
                created_at=timestamp,
                event_type=EventType.GM_AUDIT,
                payload=GmAuditPayload(code="day_exile_committed", details=details),
                correlation_id=resolution_id,
            )
            data["events"] = (*current_events, public_event, gm_event)
            audits: list[dict[str, object]] = [dict(item) for item in data["moderator_audit"]]
            audits.append(
                {
                    **details,
                    "committed_revision": revision + 1,
                    "outcome_digest": outcome_digest,
                    "created_at": timestamp.isoformat(),
                }
            )
            data["moderator_audit"] = tuple(audits)
            if trigger_candidates:
                trigger = trigger_candidates[0]
                data["phase"] = GamePhase.TRIGGER_ACTION
                data["pending_resolution"] = {
                    "operation": "DAY_EXILE",
                    "status": "TRIGGER_ACTION_REQUIRED",
                    "resolution_id": resolution_id,
                    "seat": target_seat,
                    "trigger_event": trigger.trigger.event.value,
                    "ability_id": trigger.ability_id,
                    "action_code": trigger.action_code,
                    "death_cause": target_after.death_cause if target_after else None,
                    "snapshot_revision": revision + 1,
                }
            else:
                data["phase"] = GamePhase.DAY_RESOLVE
                data["pending_resolution"] = None
            data["state_revision"] = revision + 1
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_victory_check(
        self,
        board: BoardDefinition,
        *,
        role_groups: Mapping[str, str] | None = None,
        moderator_winner: str | None = None,
        reason: str = "",
        expected_revision: int | None = None,
        now: datetime | None = None,
        snapshot_writer: SnapshotWriter | None = None,
    ) -> GameState:
        """Evaluate and commit the one authoritative victory boundary.

        Victory evaluation is pure, but its result is a lifecycle mutation and
        therefore must pass through the manager's serialized commit boundary.
        The board is evaluated while the lock is held, after the expected
        revision and all active work have been rechecked.  A pending result
        cannot be resolved by this method unless the moderator explicitly
        selects one of the evaluator's candidate sides; the manager never
        invents a priority for an ambiguous or underspecified board.
        """

        if not isinstance(reason, str):
            raise EventCommitError("VICTORY_INVALID: reason must be a string")
        if moderator_winner is not None and (
            not isinstance(moderator_winner, str) or not moderator_winner
        ):
            raise EventCommitError(
                "VICTORY_INVALID: moderator_winner must be a non-empty candidate side"
            )
        if role_groups is not None and not isinstance(role_groups, Mapping):
            raise EventCommitError("VICTORY_INVALID: role_groups must be a mapping")

        commit_time = utc_now() if now is None else now
        if commit_time.tzinfo is None or commit_time.utcoffset() is None:
            raise EventCommitError("VICTORY_INVALID: timestamp must include a timezone")
        commit_time = commit_time.astimezone(UTC)

        async with self._lock:
            state = self._state
            revision = state.state_revision if expected_revision is None else expected_revision
            _revision_check(state, revision)
            if state.phase is not GamePhase.VICTORY_CHECK:
                raise EventCommitError("PHASE_MISMATCH: victory check requires VICTORY_CHECK")
            if state.run_status in {
                RunStatus.PAUSED,
                RunStatus.FAILED,
                RunStatus.CLOSED,
            }:
                raise EventCommitError(
                    "VICTORY_BLOCKED: victory check is unavailable while game is "
                    f"{state.run_status.value}"
                )
            if state.winner is not None:
                raise EventCommitError(
                    "VICTORY_ALREADY_RECORDED: winner is already present at the boundary"
                )
            if state.serial_turn is not None:
                raise EventCommitError("VICTORY_BLOCKED: a serial turn is still active")
            if state.current_queue:
                raise EventCommitError("VICTORY_BLOCKED: a serial turn queue is still active")
            if state.pending_resolution is not None:
                raise EventCommitError(
                    "VICTORY_BLOCKED: an unconfirmed resolution is still pending"
                )
            if self._rule_work_blocks_lifecycle(state):
                raise EventCommitError(
                    "VICTORY_BLOCKED: a rule settlement, trigger, or boundary is pending"
                )

            active_request_statuses = {
                "OPEN",
                "REQUESTED",
                "SUBMITTING",
                "IN_FLIGHT",
                "PENDING",
            }
            for request_id, raw_request in state.action_requests.items():
                if not isinstance(raw_request, dict):
                    raise EventCommitError(
                        f"VICTORY_BLOCKED: action request {request_id!r} is malformed"
                    )
                status = raw_request.get("status")
                if not isinstance(status, str) or status.upper() in active_request_statuses:
                    raise EventCommitError(
                        f"VICTORY_BLOCKED: action request {request_id!r} is still active"
                    )
            for window_id, raw_window in state.action_windows.items():
                try:
                    window = _load_action_window(raw_window)
                except ValueError as exc:
                    raise EventCommitError(
                        f"VICTORY_BLOCKED: action window {window_id!r} is malformed"
                    ) from exc
                if window.is_open:
                    raise EventCommitError(
                        f"VICTORY_BLOCKED: action window {window_id!r} is still open"
                    )
            for seat, player in state.players.items():
                if player.current_request_id is not None:
                    raise EventCommitError(
                        f"VICTORY_BLOCKED: seat {seat} still has an active action request binding"
                    )
            for seat, cursor in state.delivery_cursors.items():
                if cursor.in_flight_request_id is not None or cursor.in_flight_event_ids:
                    raise EventCommitError(
                        f"VICTORY_BLOCKED: seat {seat} still has an in-flight delivery cursor"
                    )

            # This call also verifies that ``board`` is a published,
            # human-reviewed, frozen ruleset matching the game state.
            evaluation = evaluate_victory(state, board, role_groups=role_groups)
            candidate_sides = tuple(dict.fromkeys(evaluation.candidate_sides))

            if evaluation.status == "ONGOING":
                if evaluation.winner is not None or candidate_sides:
                    raise EventCommitError(
                        "VICTORY_INVALID: ONGOING evaluation contains a winner candidate"
                    )
                if moderator_winner is not None:
                    raise EventCommitError(
                        "VICTORY_INVALID: ONGOING evaluation has no moderator winner"
                    )
                selected_winner = None
                target_phase = GamePhase.NIGHT_TEAM_CHAT
            elif evaluation.status == "WINNER":
                if evaluation.winner is None or candidate_sides != (evaluation.winner,):
                    raise EventCommitError(
                        "VICTORY_INVALID: WINNER evaluation is not uniquely identified"
                    )
                if moderator_winner is not None and moderator_winner != evaluation.winner:
                    raise EventCommitError(
                        "VICTORY_INVALID: moderator winner does not match the evaluator"
                    )
                selected_winner = evaluation.winner
                target_phase = GamePhase.FINISHED
            elif evaluation.status == "PENDING_MODERATOR":
                if moderator_winner is None:
                    raise EventCommitError(
                        "VICTORY_PENDING: moderator_winner is required for a pending result"
                    )
                if not candidate_sides or moderator_winner not in candidate_sides:
                    raise EventCommitError(
                        "VICTORY_PENDING: moderator winner must be one of the explicit candidates"
                    )
                selected_winner = moderator_winner
                target_phase = GamePhase.FINISHED
            else:
                raise EventCommitError(
                    f"VICTORY_INVALID: unsupported evaluation status {evaluation.status!r}"
                )

            try:
                candidate = transition_phase(
                    state,
                    target_phase,
                    expected_revision=revision,
                    now=commit_time,
                )
            except (TypeError, ValueError) as exc:
                raise EventCommitError(f"PHASE_INVALID: {exc}") from exc

            data = _state_data(candidate)
            if selected_winner is None:
                data["winner"] = None
            else:
                selected_candidate = next(
                    candidate_item
                    for candidate_item in evaluation.candidates
                    if candidate_item.side == selected_winner
                )
                data["winner"] = {
                    "status": "WINNER",
                    "side": selected_winner,
                    "condition": selected_candidate.condition,
                    "candidates": [
                        {"side": item.side, "condition": item.condition}
                        for item in evaluation.candidates
                    ],
                    "reasons": list(evaluation.reasons),
                    "resolved_by": (
                        "moderator" if evaluation.status == "PENDING_MODERATOR" else "evaluator"
                    ),
                    "base_revision": revision,
                    "committed_revision": candidate.state_revision,
                    "created_at": commit_time.isoformat(),
                }

            audits = list(data["moderator_audit"])
            audits.append(
                {
                    "operation": "VICTORY_CHECK",
                    "status": evaluation.status,
                    "candidates": [
                        {"side": item.side, "condition": item.condition}
                        for item in evaluation.candidates
                    ],
                    "reasons": list(evaluation.reasons),
                    "winner": selected_winner,
                    "moderator_winner": moderator_winner,
                    "board_id": board.board_id,
                    "board_version": board.version,
                    "reason": reason[:500],
                    "base_revision": revision,
                    "committed_revision": candidate.state_revision,
                    "created_at": commit_time.isoformat(),
                }
            )
            data["moderator_audit"] = tuple(audits)
            data["updated_at"] = commit_time
            candidate_state = GameState.model_validate(data)

            # A complete-cycle snapshot is part of this boundary.  The
            # callback runs before replacing ``self._state`` while the commit
            # lock is held, so a failed write leaves VICTORY_CHECK intact and
            # the moderator can retry the same command.  The writer is given
            # the reduced candidate, never the pre-check state.
            snapshot_reference: Mapping[str, JsonValue] | None = None
            if snapshot_writer is not None:
                try:
                    snapshot_reference = await snapshot_writer(candidate_state)
                except Exception as exc:
                    raise EventCommitError(
                        "VICTORY_SNAPSHOT_FAILED: completed victory boundary "
                        "could not be durably snapshotted"
                    ) from exc
                if not isinstance(snapshot_reference, Mapping):
                    raise EventCommitError(
                        "VICTORY_SNAPSHOT_FAILED: snapshot writer returned an invalid reference"
                    )
                required_reference = {
                    "snapshot_id",
                    "snapshot_revision",
                    "state_revision",
                    "created_at",
                    "manifest_sha256",
                }
                if set(snapshot_reference) != required_reference:
                    raise EventCommitError(
                        "VICTORY_SNAPSHOT_FAILED: snapshot reference fields are invalid"
                    )
                snapshot_id = snapshot_reference.get("snapshot_id")
                snapshot_revision = snapshot_reference.get("snapshot_revision")
                snapshot_state_revision = snapshot_reference.get("state_revision")
                created_at = snapshot_reference.get("created_at")
                manifest_sha256 = snapshot_reference.get("manifest_sha256")
                if (
                    not isinstance(snapshot_id, str)
                    or not snapshot_id
                    or type(snapshot_revision) is not int
                    or snapshot_revision < 0
                    or type(snapshot_state_revision) is not int
                    or snapshot_state_revision != candidate_state.state_revision
                    or not isinstance(created_at, str)
                    or not created_at
                    or not isinstance(manifest_sha256, str)
                    or not manifest_sha256
                ):
                    raise EventCommitError(
                        "VICTORY_SNAPSHOT_FAILED: snapshot reference values are invalid"
                    )
                candidate_data = _state_data(candidate_state)
                candidate_data["last_snapshot"] = dict(snapshot_reference)
                candidate_state = GameState.model_validate(candidate_data)

            committed = candidate_state
            self._state = committed
            return committed

    async def commit_night_victory_check(
        self,
        board: BoardDefinition,
        *,
        role_groups: Mapping[str, str] | None = None,
        reason: str = "night boundary victory check",
        expected_revision: int | None = None,
        now: datetime | None = None,
        snapshot_writer: SnapshotWriter | None = None,
    ) -> GameState:
        """Resolve a unique winner directly after a completed night.

        This is a deliberately narrow terminal boundary.  It accepts only a
        clean ``DAY_ANNOUNCE`` state produced by night resolution, and only a
        uniquely evaluated winner.  An ongoing board is returned unchanged so
        the normal daytime flow remains mandatory; ambiguous results are
        routed to the ordinary ``VICTORY_CHECK`` path instead of being
        silently selected here.
        """

        if not isinstance(board, BoardDefinition):
            raise EventCommitError("VICTORY_INVALID: board is invalid")
        timestamp = utc_now() if now is None else now
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise EventCommitError("VICTORY_INVALID: timestamp must include a timezone")
        timestamp = timestamp.astimezone(UTC)
        async with self._lock:
            state = self._state
            revision = state.state_revision if expected_revision is None else expected_revision
            _revision_check(state, revision)
            if state.phase is not GamePhase.DAY_ANNOUNCE:
                raise EventCommitError("PHASE_MISMATCH: night victory check requires DAY_ANNOUNCE")
            if state.pending_resolution is not None or state.serial_turn is not None:
                raise EventCommitError("VICTORY_BLOCKED: an action is still pending")
            if self._rule_work_blocks_lifecycle(state):
                raise EventCommitError(
                    "VICTORY_BLOCKED: a rule settlement, trigger, or boundary is pending"
                )
            if state.winner is not None:
                raise EventCommitError("VICTORY_ALREADY_RECORDED: winner is already present")
            evaluation = evaluate_victory(state, board, role_groups=role_groups)
            candidates = tuple(dict.fromkeys(evaluation.candidate_sides))
            if evaluation.status == "ONGOING":
                if evaluation.winner is not None or candidates:
                    raise EventCommitError("VICTORY_INVALID: ongoing evaluation has a candidate")
                return state
            # Ambiguous board outcomes continue through the normal daytime
            # boundary where the moderator may choose a candidate explicitly.
            if evaluation.status != "WINNER":
                return state
            if evaluation.winner is None or candidates != (evaluation.winner,):
                raise EventCommitError(
                    "VICTORY_INVALID: winner boundary is not uniquely identified"
                )
            selected = next(
                item for item in evaluation.candidates if item.side == evaluation.winner
            )
            data = _state_data(state)
            commit_revision = revision + 1
            data["phase"] = GamePhase.FINISHED
            data["winner"] = {
                "status": "WINNER",
                "side": selected.side,
                "condition": selected.condition,
                "candidates": [
                    {"side": item.side, "condition": item.condition}
                    for item in evaluation.candidates
                ],
                "reasons": list(evaluation.reasons),
                "resolved_by": "night_boundary",
                "base_revision": revision,
                "committed_revision": commit_revision,
                "created_at": timestamp.isoformat(),
            }
            audits = list(data["moderator_audit"])
            audits.append(
                {
                    "operation": "NIGHT_VICTORY_CHECK",
                    "status": evaluation.status,
                    "winner": selected.side,
                    "candidates": [
                        {"side": item.side, "condition": item.condition}
                        for item in evaluation.candidates
                    ],
                    "reasons": list(evaluation.reasons),
                    "board_id": board.board_id,
                    "board_version": board.version,
                    "reason": reason[:500],
                    "base_revision": revision,
                    "committed_revision": commit_revision,
                    "created_at": timestamp.isoformat(),
                }
            )
            data["moderator_audit"] = tuple(audits)
            data["state_revision"] = commit_revision
            data["updated_at"] = timestamp
            candidate_state = GameState.model_validate(data)
            if snapshot_writer is not None:
                try:
                    reference = await snapshot_writer(candidate_state)
                except Exception as exc:
                    raise EventCommitError(
                        "VICTORY_SNAPSHOT_FAILED: night winner could not be snapshotted"
                    ) from exc
                required = {
                    "snapshot_id",
                    "snapshot_revision",
                    "state_revision",
                    "created_at",
                    "manifest_sha256",
                }
                if not isinstance(reference, Mapping) or set(reference) != required:
                    raise EventCommitError("VICTORY_SNAPSHOT_FAILED: invalid snapshot reference")
                if reference.get("state_revision") != candidate_state.state_revision:
                    raise EventCommitError("VICTORY_SNAPSHOT_FAILED: snapshot revision mismatch")
                snapshot_data = _state_data(candidate_state)
                snapshot_data["last_snapshot"] = dict(reference)
                candidate_state = GameState.model_validate(snapshot_data)
            self._state = candidate_state
            return candidate_state

    async def vote_observation(self, seat: int) -> PlayerVoteObservation:
        """Return a seat-scoped vote projection without revealing ballots."""

        async with self._lock:
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no active vote window")
            if seat not in self._state.players:
                raise VoteError("SEAT_NOT_ASSIGNED", "seat is not assigned in the game")
            return _load_vote_state(self._state.vote_state).player_observation(seat)

    # Vote-specific names used by adapters and the moderator shell.
    open_vote = open_vote_window
    submit_ballot = submit_vote
    lock_vote = lock_vote_window
    build_vote_tally = prepare_vote_tally
    confirm_vote = confirm_vote_tally
    confirm_vote_tally_and_transition = confirm_vote_and_transition

    async def set_serial_turn_queue(
        self,
        queue: tuple[int, ...],
        *,
        phase: GamePhase = GamePhase.DAY_SPEECH,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Freeze the queue used by the serialized speech scheduler.

        The queue is game state, so installing it uses the same revision
        boundary as every other lifecycle mutation.  An active turn cannot
        be replaced underneath a runtime.
        """

        if not queue:
            raise EventCommitError("TURN_QUEUE_EMPTY: a serial turn queue must contain a seat")
        if len(set(queue)) != len(queue):
            raise EventCommitError("TURN_QUEUE_INVALID: queue seats must be unique")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if any(seat not in self._state.players for seat in queue):
                raise EventCommitError("SEAT_NOT_ASSIGNED: queue contains an unknown seat")
            if phase in {
                GamePhase.SHERIFF_ELECTION_SPEECH,
                GamePhase.SHERIFF_ELECTION_PK_SPEECH,
            }:
                remaining = _sheriff_serial_speech_queue(self._state, phase)
                if tuple(queue) != remaining:
                    raise EventCommitError(
                        "SHERIFF_QUEUE_INVALID: queue must equal the frozen order of "
                        "unspoken election candidates"
                    )
            if phase is GamePhase.NIGHT_TEAM_CHAT:
                window = _active_team_chat_window(self._state)
                unauthorized = tuple(seat for seat in queue if seat not in window.allowed_seats)
                if unauthorized:
                    raise EventCommitError(
                        "TEAM_SEAT_NOT_AUTHORIZED: queue contains seats outside the "
                        "active team window"
                    )
            if self._state.serial_turn is not None:
                raise EventCommitError("TURN_IN_PROGRESS: cannot replace an active serial turn")
            if self._state.current_queue == queue:
                return self._state
            data = _state_data(self._state)
            data["current_queue"] = queue
            data["state_revision"] = self._state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def sheriff_speech_queue(self, phase: GamePhase) -> tuple[int, ...]:
        """Return the current frozen queue of unspoken sheriff candidates."""

        async with self._lock:
            return _sheriff_serial_speech_queue(self._state, phase)

    async def begin_serial_speech_turn(
        self,
        seat: int,
        session_epoch: int,
        *,
        request_id: str,
        logical_request_id: str,
        attempt_no: int = 1,
        event_ids: tuple[int, ...] | None = None,
        retry: bool = False,
        phase: GamePhase = GamePhase.DAY_SPEECH,
        rule_boundary_id: str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Bind one physical speech request and freeze its input events.

        Only this method advances the delivery cursor to ``in_flight``.  The
        cursor is not acknowledged until :meth:`commit_serial_speech` gets a
        schema-valid response, which leaves the same event IDs available for a
        retry after a runtime failure.
        """

        if not request_id or not logical_request_id:
            raise EventCommitError("REQUEST_INVALID: request IDs must not be empty")
        if rule_boundary_id is not None and not rule_boundary_id:
            raise EventCommitError("BOUNDARY_INVALID: rule boundary ID must not be empty")
        if attempt_no < 1:
            raise EventCommitError("REQUEST_INVALID: attempt_no must be positive")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            state = self._state
            if state.run_status is RunStatus.PAUSED:
                raise EventCommitError("GAME_PAUSED: serial speech is paused")
            if state.phase is not phase:
                raise EventCommitError(f"PHASE_MISMATCH: serial speech requires {phase.value}")
            if rule_boundary_id is not None:
                boundary = next(
                    (
                        item
                        for item in state.rule_boundaries
                        if item.boundary_id == rule_boundary_id and item.is_pending
                    ),
                    None,
                )
                pending_seats = (
                    tuple(
                        item
                        for item in boundary.last_words_seats
                        if item not in boundary.last_words_completed_seats
                    )
                    if boundary is not None
                    else ()
                )
                if boundary is None or seat not in pending_seats or pending_seats[0] != seat:
                    raise EventCommitError(
                        "BOUNDARY_NOT_PENDING: seat is not the pending boundary speech head"
                    )
            else:
                if any(item.is_pending for item in state.rule_boundaries):
                    raise EventCommitError(
                        "BOUNDARY_REQUIRED: pending rule boundary must bind serial speech"
                    )
                if state.current_queue is None or not state.current_queue:
                    raise EventCommitError("TURN_QUEUE_EMPTY: no serial speech turn is pending")
                if state.current_queue[0] != seat:
                    raise EventCommitError("TURN_NOT_AT_HEAD: only the queue head may speak")
            if phase is GamePhase.NIGHT_TEAM_CHAT:
                _validate_team_chat_seat(state, seat)
            elif phase in {
                GamePhase.SHERIFF_ELECTION_SPEECH,
                GamePhase.SHERIFF_ELECTION_PK_SPEECH,
            }:
                if seat not in first_day_sheriff_participants(state):
                    raise EventCommitError(
                        "SHERIFF_SEAT_NOT_AUTHORIZED: seat is outside the current "
                        "first-day election boundary"
                    )
                if state.current_queue is None:
                    raise EventCommitError("SHERIFF_QUEUE_INVALID: active queue is missing")
                remaining = _sheriff_serial_speech_queue(state, phase)
                if tuple(state.current_queue) != remaining:
                    raise EventCommitError(
                        "SHERIFF_QUEUE_INVALID: active queue does not match the frozen "
                        "order of unspoken election candidates"
                    )
                if seat not in remaining:
                    raise EventCommitError(
                        "SHERIFF_SEAT_NOT_AUTHORIZED: seat has already spoken or is not "
                        "in the frozen candidate order"
                    )
            player = state.players.get(seat)
            if player is None:
                raise EventCommitError(
                    "SEAT_NOT_ASSIGNED: delivery seat is not in the current game"
                )
            if player.session_epoch != session_epoch:
                raise EventCommitError("SESSION_MISMATCH: delivery uses an obsolete session epoch")

            previous = state.serial_turn
            if previous is not None:
                same_request = (
                    previous.request_id == request_id
                    and previous.logical_request_id == logical_request_id
                    and previous.attempt_no == attempt_no
                    and previous.seat == seat
                    and previous.rule_boundary_id == rule_boundary_id
                )
                if same_request:
                    return state
                if not retry:
                    raise EventCommitError("TURN_IN_PROGRESS: another physical request is active")
                if (
                    previous.seat != seat
                    or previous.logical_request_id != logical_request_id
                    or previous.attempt_no != attempt_no - 1
                    or previous.rule_boundary_id != rule_boundary_id
                ):
                    raise EventCommitError("REQUEST_EXPIRED: retry does not match the active turn")
                event_ids = previous.event_ids
            elif retry or attempt_no != 1:
                raise EventCommitError("REQUEST_EXPIRED: no active turn can be retried")

            events = _typed_events(state)
            cursor = _player_cursor(
                state,
                DeliveryAck(seat, session_epoch, request_id, event_ids),
            )
            router = MessageRouter(events, {seat: cursor})
            try:
                candidate_cursor = router.prepare_ack(
                    seat,
                    session_epoch,
                    request_id=request_id,
                    event_ids=event_ids,
                )
            except (DeliveryCursorError, DeliverySessionError, ValueError) as exc:
                raise EventCommitError(str(exc)) from exc

            data = _state_data(state)
            cursors = dict(data["delivery_cursors"])
            cursors[seat] = candidate_cursor
            data["delivery_cursors"] = cursors
            data["serial_turn"] = SerialTurnBinding(
                seat=seat,
                session_epoch=session_epoch,
                request_id=request_id,
                logical_request_id=logical_request_id,
                attempt_no=attempt_no,
                event_ids=candidate_cursor.in_flight_event_ids,
                rule_boundary_id=rule_boundary_id,
            )
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
            self._state = candidate
            return candidate

    async def commit_serial_speech(
        self,
        seat: int,
        session_epoch: int,
        *,
        request_id: str,
        logical_request_id: str,
        attempt_no: int,
        text: str,
        phase: GamePhase = GamePhase.DAY_SPEECH,
        rule_boundary_id: str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Commit a validated public speech, delivery ack, and queue pop once."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            state = self._state
            if state.run_status is RunStatus.PAUSED:
                raise EventCommitError("GAME_PAUSED: serial speech is paused")
            if state.phase is not phase:
                raise EventCommitError(f"PHASE_MISMATCH: serial speech requires {phase.value}")
            if rule_boundary_id is not None and not rule_boundary_id:
                raise EventCommitError("BOUNDARY_INVALID: rule boundary ID must not be empty")
            boundary: RuleBoundary | None = None
            if rule_boundary_id is not None:
                boundary = next(
                    (
                        item
                        for item in state.rule_boundaries
                        if item.boundary_id == rule_boundary_id and item.is_pending
                    ),
                    None,
                )
                pending_seats = (
                    tuple(
                        item
                        for item in boundary.last_words_seats
                        if item not in boundary.last_words_completed_seats
                    )
                    if boundary is not None
                    else ()
                )
                if boundary is None or seat not in pending_seats or pending_seats[0] != seat:
                    raise EventCommitError(
                        "BOUNDARY_NOT_PENDING: seat is not the pending boundary speech head"
                    )
            elif any(item.is_pending for item in state.rule_boundaries):
                raise EventCommitError(
                    "BOUNDARY_REQUIRED: pending rule boundary must bind serial speech"
                )
            elif state.current_queue is None or not state.current_queue:
                raise EventCommitError("TURN_QUEUE_EMPTY: no serial speech turn is pending")
            elif state.current_queue[0] != seat:
                raise EventCommitError("TURN_NOT_AT_HEAD: only the queue head may speak")
            team_window = (
                _validate_team_chat_seat(state, seat)
                if phase is GamePhase.NIGHT_TEAM_CHAT
                else None
            )
            if boundary is not None and team_window is not None:
                raise EventCommitError(
                    "BOUNDARY_SPEECH_INVALID: rule-boundary last words must be public speech"
                )
            active = state.serial_turn
            if active is None or (
                active.seat != seat
                or active.session_epoch != session_epoch
                or active.request_id != request_id
                or active.logical_request_id != logical_request_id
                or active.attempt_no != attempt_no
                or active.rule_boundary_id != rule_boundary_id
            ):
                raise EventCommitError("REQUEST_EXPIRED: speech response is stale or superseded")
            player = state.players.get(seat)
            if player is None:
                raise EventCommitError("SEAT_NOT_ASSIGNED: speaker is not in the current game")
            if player.session_epoch != session_epoch:
                raise EventCommitError("SESSION_MISMATCH: speech response uses an obsolete session")
            if not isinstance(text, str) or not text.strip() or len(text) > 8_000:
                raise EventCommitError("SCHEMA_INVALID: speech text is blank or too long")
            cursor = state.delivery_cursors.get(seat)
            if cursor is None or cursor.in_flight_request_id != request_id:
                raise EventCommitError("REQUEST_EXPIRED: speech delivery is no longer active")

            events = _typed_events(state)
            event_id = max((event.event_id for event in events), default=0) + 1
            commit_revision = state.state_revision + 1
            timestamp = now or utc_now()
            if team_window is None:
                event = GameEvent.public(
                    event_id=event_id,
                    game_id=state.game_id,
                    state_revision=commit_revision,
                    round_no=state.round_no,
                    phase=phase,
                    created_at=timestamp,
                    event_type=EventType.SPEECH,
                    eligible_seats=tuple(sorted(state.players)),
                    actor_seat=seat,
                    correlation_id=logical_request_id,
                    payload=PublicSpeechPayload(speaker_seat=seat, content=text),
                )
            else:
                event = GameEvent.team(
                    event_id=event_id,
                    game_id=state.game_id,
                    state_revision=commit_revision,
                    round_no=state.round_no,
                    phase=phase,
                    created_at=timestamp,
                    event_type=EventType.TEAM_SPEECH,
                    authorized_seats=team_window.allowed_seats,
                    actor_seat=seat,
                    correlation_id=logical_request_id,
                    payload=TeamSpeechPayload(speaker_seat=seat, content=text),
                )
            candidate = _reduce_event_delivery(
                state,
                StatePatch.event_delivery(
                    (event,),
                    DeliveryAck(
                        seat,
                        session_epoch,
                        request_id,
                        cursor.in_flight_event_ids,
                    ),
                    expected_revision=revision,
                    now=timestamp,
                ),
            )
            data = _state_data(candidate)
            if rule_boundary_id is not None:
                data["last_serial_turn"] = None
            elif phase is GamePhase.DAY_SPEECH:
                data["last_serial_turn"] = active.model_copy(
                    update={"event_ids": tuple(sorted((*active.event_ids, event.event_id)))}
                ).model_dump(mode="python")
            else:
                data["last_serial_turn"] = None
            if rule_boundary_id is None:
                current_queue = state.current_queue
                if current_queue is None:
                    raise EventCommitError("TURN_QUEUE_EMPTY: no serial speech turn is pending")
                data["current_queue"] = tuple(current_queue[1:])
            else:
                assert boundary is not None
                completed_seats = tuple(
                    item
                    for item in boundary.last_words_seats
                    if item in boundary.last_words_completed_seats or item == seat
                )
                updated_boundary = boundary.model_copy(
                    update={
                        "last_words_completed_seats": completed_seats,
                        "completed_at": timestamp
                        if (
                            len(completed_seats) == len(boundary.last_words_seats)
                            and (
                                not boundary.sheriff_badge_required
                                or boundary.sheriff_badge_completed
                            )
                        )
                        else None,
                    }
                )
                data["rule_boundaries"] = tuple(
                    updated_boundary.model_dump(mode="python")
                    if item.boundary_id == boundary.boundary_id
                    else item.model_dump(mode="python")
                    for item in state.rule_boundaries
                )
                audits = list(data["moderator_audit"])
                audits.append(
                    {
                        "operation": "RULE_BOUNDARY_LAST_WORDS_SPEECH_COMPLETE",
                        "boundary_id": boundary.boundary_id,
                        "source_group_id": boundary.source_group_id,
                        "source_batch_id": boundary.source_batch_id,
                        "death_fact_ids": list(boundary.death_fact_ids),
                        "seat": seat,
                        "session_epoch": active.session_epoch,
                        "request_id": active.request_id,
                        "logical_request_id": active.logical_request_id,
                        "attempt_no": active.attempt_no,
                        "phase": event.phase.value,
                        "speech_event_id": event.event_id,
                        "speech_event_revision": event.state_revision,
                        "base_revision": revision,
                        "committed_revision": event.state_revision,
                        "created_at": timestamp.isoformat(),
                    }
                )
                data["moderator_audit"] = tuple(audits)
            data["serial_turn"] = None
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_serial_sheriff_speech(
        self,
        seat: int,
        session_epoch: int,
        *,
        request_id: str,
        logical_request_id: str,
        attempt_no: int,
        text: str,
        phase: GamePhase,
        rule_boundary_id: str | None = None,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Commit one sheriff campaign speech with its public event atomically.

        This is intentionally a separate path from ordinary public speech:
        the accepted text also belongs in the private election record.  The
        event, delivery acknowledgement, election update, queue pop, and
        request binding are all validated under the same manager lock.
        """

        if phase not in {
            GamePhase.SHERIFF_ELECTION_SPEECH,
            GamePhase.SHERIFF_ELECTION_PK_SPEECH,
        }:
            raise EventCommitError(
                "PHASE_INVALID: sheriff serial speech requires a sheriff speech phase"
            )
        if rule_boundary_id is not None:
            raise EventCommitError(
                "BOUNDARY_INVALID: sheriff campaign speech cannot use a rule death boundary"
            )
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            state = self._state
            if state.run_status is RunStatus.PAUSED:
                raise EventCommitError("GAME_PAUSED: serial speech is paused")
            if state.phase is not phase:
                raise EventCommitError(f"PHASE_MISMATCH: serial speech requires {phase.value}")
            if state.current_queue is None or not state.current_queue:
                raise EventCommitError("TURN_QUEUE_EMPTY: no serial speech turn is pending")
            if state.current_queue[0] != seat:
                raise EventCommitError("TURN_NOT_AT_HEAD: only the queue head may speak")
            remaining = _sheriff_serial_speech_queue(state, phase)
            if seat not in first_day_sheriff_participants(state):
                raise EventCommitError(
                    "SHERIFF_SEAT_NOT_AUTHORIZED: seat is outside the current "
                    "first-day election boundary"
                )
            if tuple(state.current_queue) != remaining:
                raise EventCommitError(
                    "SHERIFF_QUEUE_INVALID: active queue does not match the frozen "
                    "order of unspoken election candidates"
                )
            if seat not in remaining:
                raise EventCommitError(
                    "SHERIFF_SEAT_NOT_AUTHORIZED: seat has already spoken or is not "
                    "in the frozen candidate order"
                )
            active = state.serial_turn
            if active is None or (
                active.seat != seat
                or active.session_epoch != session_epoch
                or active.request_id != request_id
                or active.logical_request_id != logical_request_id
                or active.attempt_no != attempt_no
            ):
                raise EventCommitError("REQUEST_EXPIRED: speech response is stale or superseded")
            player = state.players.get(seat)
            if player is None:
                raise EventCommitError("SEAT_NOT_ASSIGNED: speaker is not in the current game")
            if player.session_epoch != session_epoch:
                raise EventCommitError("SESSION_MISMATCH: speech response uses an obsolete session")
            if not isinstance(text, str) or not text.strip() or len(text) > 8_000:
                raise EventCommitError("SCHEMA_INVALID: speech text is blank or too long")
            cursor = state.delivery_cursors.get(seat)
            if cursor is None or cursor.in_flight_request_id != request_id:
                raise EventCommitError("REQUEST_EXPIRED: speech delivery is no longer active")

            election = _load_sheriff_election(state.sheriff_election)
            campaign_request = SheriffCampaignSpeechRequest(
                request_id=request_id,
                game_id=state.game_id,
                day_no=election.day_no,
                seat=seat,
                session_epoch=session_epoch,
                observation_revision=state.state_revision,
                text=text,
            )
            try:
                accepted = election.submit_speech(campaign_request)
            except SheriffElectionError as exc:
                raise EventCommitError(str(exc)) from exc
            if accepted == election:
                raise EventCommitError(
                    "IDEMPOTENCY_CONFLICT: election already contains this campaign speech"
                )

            events = _typed_events(state)
            event_id = max((event.event_id for event in events), default=0) + 1
            commit_revision = state.state_revision + 1
            timestamp = now or utc_now()
            event = GameEvent.public(
                event_id=event_id,
                game_id=state.game_id,
                state_revision=commit_revision,
                round_no=state.round_no,
                phase=phase,
                created_at=timestamp,
                event_type=EventType.SPEECH,
                eligible_seats=tuple(sorted(state.players)),
                actor_seat=seat,
                correlation_id=logical_request_id,
                payload=PublicSpeechPayload(speaker_seat=seat, content=text),
            )
            candidate = _reduce_event_delivery(
                state,
                StatePatch.event_delivery(
                    (event,),
                    DeliveryAck(
                        seat,
                        session_epoch,
                        request_id,
                        cursor.in_flight_event_ids,
                    ),
                    expected_revision=revision,
                    now=timestamp,
                ),
            )
            data = _state_data(candidate)
            data["sheriff_election"] = _sheriff_election_payload(accepted)
            data["current_queue"] = tuple(state.current_queue[1:])
            data["serial_turn"] = None
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    # Names used by the scheduler and by adapters that model ack as the
    # commit operation are intentionally short aliases.
    append_events = commit_events
    ack_delivery = commit_delivery_ack
    commit_delivery = commit_delivery_ack
    commit_event_delivery = commit_events_and_ack

    async def commit_phase_transition(
        self,
        target: GamePhase,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Validate and commit one explicit lifecycle edge."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            current = self._state
            unsettled_successor = self._is_unsettled_group_successor(current, target)
            if self._rule_work_blocks_lifecycle(current) and not unsettled_successor:
                raise EventCommitError(
                    "PHASE_BLOCKED: a rule settlement, trigger, or boundary is pending"
                )
            timestamp = _aware_commit_time(now)
            if unsettled_successor and target is current.phase:
                data = _state_data(current)
                data["state_revision"] = revision + 1
                data["updated_at"] = timestamp
                candidate = GameState.model_validate(data)
            else:
                candidate = reduce_state(
                    current,
                    StatePatch.phase_transition(
                        target,
                        expected_revision=revision,
                        now=timestamp,
                    ),
                )
            # Persist discrete expiry crossings as part of the same phase
            # transition. Adapter-side checks also handle older restored
            # snapshots, but deleting the rows here prevents them from ever
            # reappearing after the next NIGHT_TEAM_CHAT hook has passed.
            if (
                candidate.phase is GamePhase.NIGHT_TEAM_CHAT
                or candidate.round_no > current.round_no
            ):
                data = _state_data(candidate)
                data["rule_state"] = tuple(
                    _rule_state_payload(value)
                    for value in candidate.rule_state
                    if not (
                        value.expires_at_round is not None
                        and value.expires_at_round <= candidate.round_no
                        and (
                            value.expiry_policy == "ROUND_END"
                            or (
                                value.expiry_policy == "NEXT_NIGHT_START"
                                and candidate.phase is GamePhase.NIGHT_TEAM_CHAT
                            )
                        )
                    )
                )
                data["rule_relations"] = tuple(
                    value.model_dump(mode="python")
                    for value in candidate.rule_relations
                    if not (
                        value.expires_at_round is not None
                        and value.expires_at_round <= candidate.round_no
                        and (
                            value.expiry_policy == "ROUND_END"
                            or (
                                value.expiry_policy == "NEXT_NIGHT_START"
                                and candidate.phase is GamePhase.NIGHT_TEAM_CHAT
                            )
                        )
                    )
                )
                candidate = GameState.model_validate(data)
            candidate = self._release_due_rule_disclosures(
                candidate,
                target.value,
                None,
                timestamp=timestamp,
                next_revision=candidate.state_revision,
            )
            self._state = candidate
            return candidate

    async def commit_action_request(
        self,
        request: ActionRequest,
        context: ActionValidationContext,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Revalidate and record one pending action request atomically.

        The method records only the validated request and window submission
        bookkeeping.  It deliberately does not consume resources, kill a
        player, create a resolution, or otherwise execute a skill.
        """

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            window = self._window_for_request(self._state, request)
            if not window.accepts_submissions:
                raise ActionValidationError(
                    "WINDOW_CLOSED", "action window no longer accepts new requests"
                )
            if _looks_like_trigger_window(window) and not (
                _is_trigger_window_state(self._state, window, require_bound=True)
                or self._rule_occurrence_for_window(self._state, window) is not None
            ):
                raise ActionValidationError(
                    "TRIGGER_ACTION_INVALID",
                    "action window is not bound to the pending granted ability",
                )
            authoritative_context = self._context_for_request(self._state, request, context, window)
            validated = validate_action_request(
                request,
                window,
                authoritative_context,
                registry=self._registry,
                now=now,
            )
            if validated.idempotent_replay:
                cursor = self._state.delivery_cursors.get(request.seat)
                if cursor is None or cursor.in_flight_request_id != request.request_id:
                    return self._state
            cursor = self._state.delivery_cursors.get(request.seat)
            delivery_ack = None
            if cursor is not None and cursor.in_flight_request_id == request.request_id:
                delivery_ack = DeliveryAck(
                    request.seat,
                    request.session_epoch,
                    request.request_id,
                    cursor.in_flight_event_ids,
                )
            # Request submission and successful delivery acknowledgement are
            # reduced into one candidate before replacing the manager state.
            candidate = _reduce_action_request(
                self._state,
                StatePatch.action_request(
                    validated,
                    expected_revision=revision,
                    now=now,
                ),
                delivery_ack=delivery_ack,
            )
            self._state = candidate
            return candidate

    async def commit_action_resolution(
        self,
        resolution: ActionResolution,
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Commit one moderator resolution and all of its effects atomically.

        The resolution is checked against the request, window, session, and
        current revision while holding the manager lock.  The reducer first
        preflights every target and resource transition, then replaces the
        complete state once; a rejected entry therefore cannot partially
        consume a skill or change a player's status.
        """

        if not isinstance(resolution, ActionResolution):
            raise TypeError("resolution must be an ActionResolution")
        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            candidate = _reduce_action_resolution(
                self._state,
                resolution,
                registry=self._registry,
                expected_revision=revision,
                now=now,
            )
            self._state = candidate
            return candidate

    async def commit_action_resolutions(
        self,
        resolutions: tuple[ActionResolution, ...],
        *,
        use_rules_engine: bool = False,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Commit a complete night bundle as one state replacement.

        Each individual resolution is preflighted against a local candidate.
        The manager publishes that candidate only after every request passes,
        and advances the authoritative revision once for the whole bundle.
        This is the transaction boundary used by ``NIGHT_RESOLVE``.
        """

        if not isinstance(resolutions, tuple) or not resolutions:
            raise TypeError("resolutions must be a non-empty tuple")
        if any(not isinstance(item, ActionResolution) for item in resolutions):
            raise TypeError("resolutions must contain ActionResolution values")
        if type(use_rules_engine) is not bool:
            raise TypeError("use_rules_engine must be a bool")
        ids = tuple(item.resolution_id for item in resolutions)
        if len(ids) != len(set(ids)):
            raise ResolutionError("DUPLICATE_RESOLUTION", "resolution_id values must be unique")
        async with self._lock:
            if use_rules_engine:
                state = self._state
                if self._rules is None or self._execution_package is None:
                    raise ResolutionError(
                        "RULE_PACKAGE_MISSING",
                        "game has no pinned execution package",
                    )
                request_ids = tuple(sorted(item.request_id for item in resolutions))
                replay = next(
                    (
                        receipt
                        for receipt in state.rule_receipts
                        if receipt.timing == GamePhase.TRIGGER_ACTION.value
                        and tuple(sorted(receipt.request_ids)) == request_ids
                        and receipt.package_id == self._execution_package.package_id
                    ),
                    None,
                )
                if replay is not None:
                    stored_by_request = {
                        payload.get("request_id"): payload
                        for payload in state.resolutions
                        if isinstance(payload, dict)
                    }
                    for item in resolutions:
                        payload = stored_by_request.get(item.request_id)
                        if payload is None:
                            raise ResolutionError(
                                "IDEMPOTENCY_CONFLICT",
                                "rule trigger receipt has no matching stored acknowledgement",
                            )
                        try:
                            stored = ActionResolution.model_validate(payload)
                        except ValueError as exc:
                            raise ResolutionError(
                                "RESOLUTION_INVALID",
                                "stored trigger acknowledgement is malformed",
                            ) from exc
                        if stored != item:
                            raise ResolutionError(
                                "IDEMPOTENCY_CONFLICT",
                                "trigger request was already committed with a different "
                                "acknowledgement",
                            )
                    return state

                initial_revision = (
                    state.state_revision if expected_revision is None else expected_revision
                )
                _revision_check(state, initial_revision)
                if state.phase is not GamePhase.TRIGGER_ACTION:
                    raise ResolutionError(
                        "PHASE_MISMATCH",
                        "rule-backed action resolutions require TRIGGER_ACTION",
                    )
                window_ids = {item.window_id for item in resolutions}
                if len(window_ids) != 1:
                    raise ResolutionError(
                        "WINDOW_MISMATCH",
                        "rule-backed trigger acknowledgements must share one window",
                    )
                window_id = next(iter(window_ids))
                raw_window = state.action_windows.get(window_id)
                if raw_window is None:
                    raise ResolutionError("WINDOW_NOT_FOUND", "trigger window is not installed")
                try:
                    window = _load_action_window(raw_window)
                except (TypeError, ValueError) as exc:
                    raise ResolutionError("WINDOW_INVALID", "trigger window is malformed") from exc
                if window.closed_at is not None or not _is_trigger_window_state(
                    state, window, require_bound=True
                ):
                    raise ResolutionError(
                        "TRIGGER_ACTION_INVALID",
                        "trigger window is closed or no longer bound to its granted ability",
                    )
                if any(item.base_revision != initial_revision for item in resolutions):
                    raise ResolutionError(
                        "REVISION_MISMATCH",
                        "all trigger acknowledgements must use the same observation revision",
                    )
                pending_ids = {
                    request_id
                    for request_id, payload in state.action_requests.items()
                    if isinstance(payload, Mapping)
                    and payload.get("window_id") == window_id
                    and payload.get("status") == "PENDING"
                }
                supplied_ids = {item.request_id for item in resolutions}
                if pending_ids != supplied_ids:
                    raise ResolutionError(
                        "RESOLUTION_INCOMPLETE",
                        "trigger resolution must acknowledge every pending request in its window",
                    )
                requests, bindings, group_id = self._rule_group_requests(
                    state,
                    pending_ids,
                    timing=GamePhase.TRIGGER_ACTION.value,
                )
                stored_requests = {
                    item.request_id: _stored_action_request(state, item.request_id)
                    for item in bindings
                }
                for resolution in resolutions:
                    stored_request = stored_requests.get(resolution.request_id)
                    if (
                        stored_request is None
                        or resolution.game_id != state.game_id
                        or resolution.window_id != window_id
                        or resolution.session_epoch != stored_request.session_epoch
                        or resolution.status is not ResolutionStatus.CONFIRMED
                        or len(resolution.actions) != len(stored_request.actions)
                    ):
                        raise ResolutionError(
                            "RULE_ENVELOPE_INVALID",
                            "executable trigger resolutions must be neutral acknowledgements "
                            "for current requests",
                        )
                    for index, (entry, requested_action) in enumerate(
                        zip(resolution.actions, stored_request.actions, strict=True)
                    ):
                        if (
                            entry.action_index != index
                            or entry.requested_action != requested_action
                            or entry.disposition is not ActionDisposition.CONFIRMED
                            or entry.resolved_action is not None
                            or entry.resource_cost != 0
                            or entry.effects
                        ):
                            raise ResolutionError(
                                "RULE_ENVELOPE_INVALID",
                                "executable trigger acknowledgements cannot override "
                                "interpreter effects",
                            )
                try:
                    batch = self._rules.plan(
                        state,
                        requests,
                        group_id=group_id,
                        timing=GamePhase.TRIGGER_ACTION.value,
                    )
                except (RuleAdapterError, TypeError, ValueError) as exc:
                    raise ResolutionError("RULE_PLAN_INVALID", str(exc)) from exc
                timestamp = now or utc_now()
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise EventCommitError("TIMESTAMP: commit timestamp must include a timezone")
                timestamp = timestamp.astimezone(UTC)
                candidate = self._apply_rule_batch(
                    state,
                    batch,
                    requests,
                    bindings,
                    resolutions,
                    group_id=group_id,
                    timing=GamePhase.TRIGGER_ACTION.value,
                    timestamp=timestamp,
                )
                data = _state_data(candidate)
                closed_window = window.model_copy(update={"closed_at": timestamp})
                windows = dict(data["action_windows"])
                windows[window_id] = closed_window.model_dump(mode="json")
                data["action_windows"] = windows
                data["pending_resolution"] = None
                data["state_revision"] = initial_revision + 1
                data["updated_at"] = timestamp
                committed = GameState.model_validate(data)
                self._state = committed
                return committed

            initial_revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, initial_revision)
            if any(item.base_revision != initial_revision for item in resolutions):
                raise ResolutionError(
                    "REVISION_MISMATCH",
                    "all night resolutions must use the same observation revision",
                )
            existing_payloads = {
                payload.get("resolution_id"): payload
                for payload in self._state.resolutions
                if isinstance(payload, dict)
            }
            if all(item.resolution_id in existing_payloads for item in resolutions):
                for item in resolutions:
                    try:
                        existing_payload = existing_payloads[item.resolution_id]
                        existing = ActionResolution.model_validate(existing_payload)
                    except ValueError as exc:
                        raise ResolutionError(
                            "RESOLUTION_INVALID", "stored resolution is malformed"
                        ) from exc
                    if existing != item:
                        raise ResolutionError(
                            "IDEMPOTENCY_CONFLICT",
                            "resolution_id was already committed differently",
                        )
                return self._state
            if any(item.resolution_id in existing_payloads for item in resolutions):
                raise ResolutionError(
                    "RESOLUTION_ALREADY_RESOLVED",
                    "a batch cannot mix already committed and new resolutions",
                )
            staged_actor_seats = _batch_live_actor_seats(self._state, resolutions)
            candidate = self._state
            for item in resolutions:
                staged = item.model_copy(update={"base_revision": candidate.state_revision})
                candidate = _reduce_action_resolution(
                    candidate,
                    staged,
                    registry=self._registry,
                    expected_revision=candidate.state_revision,
                    now=now,
                    staged_actor_seats=staged_actor_seats,
                    validation_state=self._state,
                )

            # The reducers above use temporary revisions to validate each
            # staged request. Normalize their persisted audit records to the
            # single externally visible commit revision before publication.
            data = _state_data(candidate)
            resolution_ids = set(ids)
            normalized_resolutions: list[dict[str, object]] = []
            for raw_resolution_payload in data["resolutions"]:
                if not isinstance(raw_resolution_payload, dict):
                    raise ResolutionError("RESOLUTION_INVALID", "stored resolution is malformed")
                normalized_resolution = cast(dict[str, object], dict(raw_resolution_payload))
                if normalized_resolution.get("resolution_id") in resolution_ids:
                    normalized_resolution["base_revision"] = initial_revision
                normalized_resolutions.append(normalized_resolution)
            data["resolutions"] = tuple(normalized_resolutions)
            normalized_audits: list[dict[str, object]] = []
            for raw_audit_payload in data["moderator_audit"]:
                if not isinstance(raw_audit_payload, dict):
                    raise ResolutionError("AUDIT_INVALID", "stored moderator audit is malformed")
                normalized_audit = cast(dict[str, object], dict(raw_audit_payload))
                if (
                    normalized_audit.get("operation") == "ACTION_RESOLUTION"
                    and normalized_audit.get("resolution_id") in resolution_ids
                ):
                    normalized_audit["base_revision"] = initial_revision
                    normalized_audit["committed_revision"] = initial_revision + 1
                normalized_audits.append(normalized_audit)
            data["moderator_audit"] = tuple(normalized_audits)
            timestamp = now or utc_now()
            data["state_revision"] = initial_revision + 1
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def commit_night_resolution(
        self,
        resolutions: tuple[ActionResolution, ...],
        *,
        action_window_id: str,
        resolve_window_id: str,
        board: BoardDefinition | None = None,
        use_rules_engine: bool = False,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Resolve a night, close both windows, and enter day atomically.

        The ordinary resolution API intentionally stops after applying effects
        because other callers may need a different lifecycle boundary.  The
        night coordinator needs a stronger transaction: an effect must never
        be visible as confirmed while the resolve window or phase still says
        that the night is active.
        """

        if not isinstance(resolutions, tuple) or not resolutions:
            raise TypeError("resolutions must be a non-empty tuple")
        if any(not isinstance(item, ActionResolution) for item in resolutions):
            raise TypeError("resolutions must contain ActionResolution values")
        ids = tuple(item.resolution_id for item in resolutions)
        if len(ids) != len(set(ids)):
            raise ResolutionError("DUPLICATE_RESOLUTION", "resolution_id values must be unique")
        if any(item.window_id != action_window_id for item in resolutions):
            raise ResolutionError(
                "WINDOW_MISMATCH", "night resolutions must use the active action window"
            )

        async with self._lock:
            if use_rules_engine:
                requested_ids = tuple(sorted(item.request_id for item in resolutions))
                replay = next(
                    (
                        item
                        for item in self._state.rule_receipts
                        if item.timing == GamePhase.NIGHT_ACTION.value
                        and tuple(sorted(item.request_ids)) == requested_ids
                        and item.package_id
                        == (self._execution_package.package_id if self._execution_package else "")
                    ),
                    None,
                )
                if replay is not None:
                    stored = {
                        payload.get("resolution_id"): payload
                        for payload in self._state.resolutions
                        if isinstance(payload, dict) and payload.get("request_id") in requested_ids
                    }
                    if len(stored) == len(resolutions) and all(
                        stored.get(item.resolution_id) is not None
                        and ActionResolution.model_validate(stored[item.resolution_id]) == item
                        for item in resolutions
                    ):
                        return self._state
                    raise ResolutionError(
                        "IDEMPOTENCY_CONFLICT",
                        "rule batch request IDs were committed with different acknowledgements",
                    )
            initial_revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, initial_revision)
            state = self._state
            if state.phase is not GamePhase.NIGHT_RESOLVE:
                raise EventCommitError("PHASE_MISMATCH: night resolution requires NIGHT_RESOLVE")
            raw_action = state.action_windows.get(action_window_id)
            raw_resolve = state.action_windows.get(resolve_window_id)
            if raw_action is None or raw_resolve is None:
                raise EventCommitError("WINDOW_NOT_FOUND: night windows are not installed")
            try:
                action_window = _load_action_window(raw_action)
                resolve_window = _load_action_window(raw_resolve)
            except ValueError as exc:
                raise EventCommitError("WINDOW_INVALID: night window is malformed") from exc
            if action_window.phase is not GamePhase.NIGHT_ACTION:
                raise EventCommitError("PHASE_MISMATCH: action window is not a night action")
            if action_window.closed_at is not None:
                raise EventCommitError("WINDOW_CLOSED: action window is already closed")
            if resolve_window.phase is not GamePhase.NIGHT_RESOLVE:
                raise EventCommitError("PHASE_MISMATCH: resolve window is not a night resolve")
            if resolve_window.closed_at is not None:
                raise EventCommitError("WINDOW_CLOSED: resolve window is already closed")
            if any(item.base_revision != initial_revision for item in resolutions):
                raise ResolutionError(
                    "REVISION_MISMATCH",
                    "all night resolutions must use the same observation revision",
                )

            pending = {
                request_id
                for request_id, payload in state.action_requests.items()
                if isinstance(payload, dict)
                and payload.get("window_id") == action_window_id
                and payload.get("status") == "PENDING"
            }
            supplied = {item.request_id for item in resolutions}
            if pending != supplied:
                raise ResolutionError(
                    "RESOLUTION_INCOMPLETE",
                    "resolution must include exactly every pending night request",
                )

            existing_payloads = {
                payload.get("resolution_id"): payload
                for payload in state.resolutions
                if isinstance(payload, dict)
            }
            if all(item.resolution_id in existing_payloads for item in resolutions):
                for item in resolutions:
                    try:
                        existing = ActionResolution.model_validate(
                            existing_payloads[item.resolution_id]
                        )
                    except ValueError as exc:
                        raise ResolutionError(
                            "RESOLUTION_INVALID", "stored resolution is malformed"
                        ) from exc
                    if existing != item:
                        raise ResolutionError(
                            "IDEMPOTENCY_CONFLICT",
                            "resolution_id was already committed differently",
                        )
                # A caller retrying a previously completed batch is safe only
                # if the lifecycle boundary is still open.  Apply the final
                # close/transition below without replaying effects.
                candidate = state
            elif any(item.resolution_id in existing_payloads for item in resolutions):
                raise ResolutionError(
                    "RESOLUTION_ALREADY_RESOLVED",
                    "a batch cannot mix already committed and new resolutions",
                )
            else:
                oracle_candidate = state
                if not use_rules_engine:
                    staged_actor_seats = _batch_live_actor_seats(state, resolutions)
                    for item in resolutions:
                        staged = item.model_copy(
                            update={"base_revision": oracle_candidate.state_revision}
                        )
                        oracle_candidate = _reduce_action_resolution(
                            oracle_candidate,
                            staged,
                            registry=self._registry,
                            expected_revision=oracle_candidate.state_revision,
                            now=now,
                            staged_actor_seats=staged_actor_seats,
                            validation_state=state,
                        )
                if use_rules_engine:
                    requests, bindings, group_id = self._rule_group_requests(
                        state,
                        pending,
                        timing=GamePhase.NIGHT_ACTION.value,
                    )
                    requests_by_external: dict[str, ActionRequest] = {
                        item.request_id: _stored_action_request(state, item.request_id)
                        for item in bindings
                    }
                    for resolution in resolutions:
                        stored_request: ActionRequest | None = requests_by_external.get(
                            resolution.request_id
                        )
                        if (
                            stored_request is None
                            or resolution.game_id != state.game_id
                            or resolution.window_id != action_window_id
                            or resolution.session_epoch != stored_request.session_epoch
                            or resolution.base_revision != initial_revision
                            or resolution.status is not ResolutionStatus.CONFIRMED
                            or len(resolution.actions) != len(stored_request.actions)
                        ):
                            raise ResolutionError(
                                "RULE_ENVELOPE_INVALID",
                                "executable resolutions must be neutral acknowledgements "
                                "for current requests",
                            )
                        for index, (entry, requested_action) in enumerate(
                            zip(resolution.actions, stored_request.actions, strict=True)
                        ):
                            if (
                                entry.action_index != index
                                or entry.requested_action != requested_action
                                or entry.disposition is not ActionDisposition.CONFIRMED
                                or entry.resolved_action is not None
                                or entry.resource_cost != 0
                                or entry.effects
                            ):
                                raise ResolutionError(
                                    "RULE_ENVELOPE_INVALID",
                                    "executable resolutions cannot override interpreter effects",
                                )
                    if self._rules is None:
                        raise ResolutionError(
                            "RULE_PACKAGE_MISSING", "game has no pinned execution package"
                        )
                    try:
                        batch = self._rules.plan(
                            state,
                            requests,
                            group_id=group_id,
                            timing=GamePhase.NIGHT_ACTION.value,
                        )
                    except (RuleAdapterError, TypeError, ValueError) as exc:
                        raise ResolutionError("RULE_PLAN_INVALID", str(exc)) from exc
                    timestamp = now or utc_now()
                    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                        raise EventCommitError(
                            "TIMESTAMP: commit timestamp must include a timezone"
                        )
                    candidate = self._apply_rule_batch(
                        state,
                        batch,
                        requests,
                        bindings,
                        resolutions,
                        group_id=group_id,
                        timing=GamePhase.NIGHT_ACTION.value,
                        timestamp=timestamp.astimezone(UTC),
                    )
                else:
                    candidate = oracle_candidate

            data = _state_data(candidate)
            timestamp = now or utc_now()
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise EventCommitError("TIMESTAMP: commit timestamp must include a timezone")
            timestamp = timestamp.astimezone(UTC)
            private_events = (
                ()
                if use_rules_engine
                else _night_private_result_events(
                    state,
                    resolutions,
                    registry=self._registry,
                    next_revision=initial_revision + 1,
                    now=timestamp,
                )
            )
            if private_events:
                _validate_new_events(state, private_events, next_revision=initial_revision + 1)
                data["events"] = (*_typed_events(state), *private_events)
            windows = dict(data["action_windows"])
            if use_rules_engine:
                closed_action = action_window.model_copy(update={"closed_at": timestamp})
                windows[action_window_id] = closed_action.model_dump(mode="json")
            closed_resolve = resolve_window.model_copy(update={"closed_at": timestamp})
            windows[resolve_window_id] = closed_resolve.model_dump(mode="json")
            data["action_windows"] = windows

            # Normalize the staged reducers to one externally visible commit.
            # The lifecycle edge is part of that same replacement, so a
            # failed validation above leaves the original state untouched.
            resolution_ids = set(ids)
            normalized_resolutions: list[dict[str, object]] = []
            for payload in data["resolutions"]:
                if isinstance(payload, dict) and payload.get("resolution_id") in resolution_ids:
                    payload = dict(payload)
                    payload["base_revision"] = initial_revision
                normalized_resolutions.append(payload)
            data["resolutions"] = tuple(normalized_resolutions)
            normalized_audits: list[dict[str, object]] = []
            for payload in data["moderator_audit"]:
                if (
                    isinstance(payload, dict)
                    and payload.get("operation") == "ACTION_RESOLUTION"
                    and payload.get("resolution_id") in resolution_ids
                ):
                    payload = dict(payload)
                    payload["base_revision"] = initial_revision
                    payload["committed_revision"] = initial_revision + 1
                normalized_audits.append(payload)
            data["moderator_audit"] = tuple(normalized_audits)
            # A death-trigger ability is discovered from the fully reduced
            # candidate, while the published state is still all-or-nothing.
            # This keeps the ordinary night effects and the trigger binding
            # in one transaction: a malformed or ambiguous trigger leaves
            # the original NIGHT_RESOLVE state untouched.
            trigger_candidates = _night_death_trigger_candidates(
                state,
                candidate,
                resolutions,
                action_window_id=action_window_id,
            )
            for _seat, ability, _death_cause, _resolution_id in trigger_candidates:
                if ability.trigger.mode is not TriggerMode.PLAYER_CHOICE:
                    raise ResolutionError(
                        "TRIGGER_AUTOMATIC_UNSUPPORTED",
                        "automatic death triggers are not supported at NIGHT_RESOLVE",
                    )
            if len(trigger_candidates) > 1:
                raise ResolutionError(
                    "MULTIPLE_TRIGGER_ACTIONS",
                    "one night resolution cannot queue multiple death-trigger actions",
                )

            data["phase"] = (
                GamePhase.TRIGGER_ACTION if trigger_candidates else GamePhase.DAY_ANNOUNCE
            )
            data["day_no"] = candidate.day_no + (0 if trigger_candidates else 1)
            if trigger_candidates:
                seat, ability, death_cause, resolution_id = trigger_candidates[0]
                data["pending_resolution"] = {
                    "operation": "NIGHT_RESOLUTION",
                    "status": "TRIGGER_ACTION_REQUIRED",
                    "resolution_id": resolution_id,
                    "seat": seat,
                    "trigger_event": ability.trigger.event.value,
                    "ability_id": ability.ability_id,
                    "action_code": ability.action_code,
                    "death_cause": death_cause,
                    "snapshot_revision": initial_revision + 1,
                }
            else:
                data["pending_resolution"] = None
            data["state_revision"] = initial_revision + 1
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    async def finalize_night_resolution(
        self,
        *,
        action_window_id: str,
        resolve_window_id: str,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Recover an old partial night commit at one atomic boundary."""

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            state = self._state
            if state.phase is not GamePhase.NIGHT_RESOLVE:
                raise EventCommitError("PHASE_MISMATCH: night resolution requires NIGHT_RESOLVE")
            raw_action = state.action_windows.get(action_window_id)
            raw_resolve = state.action_windows.get(resolve_window_id)
            if raw_action is None or raw_resolve is None:
                raise EventCommitError("WINDOW_NOT_FOUND: night windows are not installed")
            action_window = _load_action_window(raw_action)
            resolve_window = _load_action_window(raw_resolve)
            if action_window.closed_at is None:
                raise EventCommitError("WINDOW_OPEN: effects must be committed before recovery")
            if resolve_window.closed_at is not None:
                raise EventCommitError("WINDOW_CLOSED: resolve window is already closed")
            timestamp = now or utc_now()
            data = _state_data(state)
            windows = dict(data["action_windows"])
            windows[resolve_window_id] = resolve_window.model_copy(
                update={"closed_at": timestamp}
            ).model_dump(mode="json")
            data["action_windows"] = windows
            resolved_night: list[ActionResolution] = []
            for payload in state.resolutions:
                if not isinstance(payload, dict) or payload.get("window_id") != action_window_id:
                    continue
                try:
                    resolved_night.append(ActionResolution.model_validate(payload))
                except ValueError as exc:
                    raise EventCommitError(
                        "RESOLUTION_INVALID: stored night resolution is malformed"
                    ) from exc
            trigger_candidates = _night_death_trigger_candidates(
                None,
                state,
                tuple(resolved_night),
                action_window_id=action_window_id,
            )
            for _seat, ability, _death_cause, _resolution_id in trigger_candidates:
                if ability.trigger.mode is not TriggerMode.PLAYER_CHOICE:
                    raise ResolutionError(
                        "TRIGGER_AUTOMATIC_UNSUPPORTED",
                        "automatic death triggers are not supported at NIGHT_RESOLVE",
                    )
            if len(trigger_candidates) > 1:
                raise ResolutionError(
                    "MULTIPLE_TRIGGER_ACTIONS",
                    "one night resolution cannot queue multiple death-trigger actions",
                )
            data["phase"] = (
                GamePhase.TRIGGER_ACTION if trigger_candidates else GamePhase.DAY_ANNOUNCE
            )
            data["day_no"] = state.day_no + (0 if trigger_candidates else 1)
            if trigger_candidates:
                seat, ability, death_cause, resolution_id = trigger_candidates[0]
                data["pending_resolution"] = {
                    "operation": "NIGHT_RESOLUTION",
                    "status": "TRIGGER_ACTION_REQUIRED",
                    "resolution_id": resolution_id,
                    "seat": seat,
                    "trigger_event": ability.trigger.event.value,
                    "ability_id": ability.ability_id,
                    "action_code": ability.action_code,
                    "death_cause": death_cause,
                    "snapshot_revision": state.state_revision + 1,
                }
            else:
                data["pending_resolution"] = None
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = timestamp
            committed = GameState.model_validate(data)
            self._state = committed
            return committed

    # Short aliases keep the public coordinator vocabulary aligned with the
    # architecture document while retaining explicit commit_* entry points.
    transition_phase = commit_phase_transition
    submit_action_request = commit_action_request
    commit_resolution = commit_action_resolution
    resolve_action = commit_action_resolution
    confirm_day_exile = commit_day_exile
    commit_exile_resolution = commit_day_exile
    open_sheriff_vote = open_sheriff_vote_window
    submit_sheriff_ballot = submit_sheriff_vote
    finalize_sheriff = finalize_sheriff_election
    confirm_sheriff = confirm_sheriff_election
    finish_sheriff_transfer = complete_sheriff_transfer
    finish_sheriff_badge = complete_sheriff_badge

    @staticmethod
    def _window_for_request(state: GameState, request: ActionRequest) -> ActionWindow:
        raw = state.action_windows.get(request.window_id)
        if raw is None:
            raise ActionValidationError(
                "WINDOW_NOT_FOUND", "request.window_id is not present in current state"
            )
        try:
            window = _load_action_window(raw)
        except ValueError as exc:
            raise ActionValidationError(
                "WINDOW_INVALID", "current action window is not a valid frozen window"
            ) from exc
        if window.game_id != state.game_id:
            raise ActionValidationError(
                "GAME_MISMATCH", "action window does not belong to the current game"
            )
        resolving_night_action = (
            window.phase is GamePhase.NIGHT_ACTION
            and state.phase is GamePhase.NIGHT_RESOLVE
            and request.phase is GamePhase.NIGHT_ACTION
        )
        if window.phase != state.phase and not resolving_night_action:
            raise ActionValidationError(
                "PHASE_MISMATCH", "action window is not active in the current phase"
            )
        return window

    def _context_for_request(
        self,
        state: GameState,
        request: ActionRequest,
        context: ActionValidationContext,
        window: ActionWindow,
    ) -> ActionValidationContext:
        if context.game_id != state.game_id:
            raise ActionValidationError(
                "GAME_MISMATCH", "validation context does not belong to the current game"
            )
        if context.session_epoch != window.session_epoch:
            raise ActionValidationError(
                "SESSION_MISMATCH", "validation context is from an obsolete session"
            )

        player = state.players.get(request.seat)
        if player is None:
            raise ActionValidationError(
                "SEAT_NOT_ASSIGNED", "action seat is not assigned in the current state"
            )
        if player.session_epoch != request.session_epoch:
            raise ActionValidationError(
                "SESSION_MISMATCH", "player session_epoch does not match request"
            )
        if player.current_request_id != request.request_id:
            raise ActionValidationError(
                "REQUEST_MISMATCH", "request_id is not the player's active request"
            )

        # These facts belong to the authoritative player record.  Context
        # callers may provide role rules and target sets, but cannot elevate
        # a dead/unassigned seat or invent resources/current requests.  The
        # only dead-seat exception is a currently bound granted trigger;
        # ``validate_action_request`` still applies all ordinary target and
        # action checks to that request.
        trigger_binding = _pending_trigger_ability(state, window, require_bound=True)
        rule_occurrence = self._rule_occurrence_for_window(state, window)
        trigger_action = trigger_binding is not None or rule_occurrence is not None
        badge_action = _is_sheriff_badge_window(window)
        trigger_ability = trigger_binding[1] if trigger_binding is not None else None
        authorized_codes = context.authorized_action_codes
        if trigger_ability is not None:
            authorized_codes = (
                (trigger_ability.action_code, 299)
                if trigger_ability.trigger.allow_pass
                else (trigger_ability.action_code,)
            )
        if rule_occurrence is not None:
            skill = self._workflow_skill(rule_occurrence)
            allow_pass = bool(
                window.allow_pass
                and any(
                    action.action_code == skill.action_code and action.allow_pass
                    for action in cast(ExecutionPackage, self._execution_package).actions
                )
            )
            authorized_codes = (skill.action_code, 299) if allow_pass else (skill.action_code,)
        updates: dict[str, object] = {
            "active_request_id": player.current_request_id,
            "session_epoch": player.session_epoch,
            "player_alive": player.alive or trigger_action or badge_action,
            "role_id": player.role_id,
            "skill_resources": dict(player.skill_resources),
            "alive_seats": tuple(seat for seat, item in state.players.items() if item.alive),
        }
        if window.phase is GamePhase.NIGHT_ACTION and not trigger_action:
            if self._execution_package is not None and self._rules is not None:
                observation = self._rules.observation(
                    state,
                    group_id=f"context:{state.round_no}:{window.window_id}",
                    timing=window.phase.value,
                )
                observed_players = {item.seat: item for item in observation.players}
                eligible_instances = self._rule_skill_instances(
                    state,
                    player.seat,
                    window.phase.value,
                    allowed_codes=set(window.allowed_action_codes),
                    logical_window_id=window.logical_window_id,
                )
                completed_skill_ids = self._rule_completed_skill_ids(state, window)
                eligible_instances = tuple(
                    (instance, skill)
                    for instance, skill in eligible_instances
                    if set(skill.after_skills).issubset(completed_skill_ids)
                )
                grant_codes = {skill.action_code for _instance, skill in eligible_instances}
                pass_candidates = [
                    (instance, skill)
                    for instance, skill in eligible_instances
                    if skill.action_code != 299
                    and any(
                        action.action_code == skill.action_code and action.allow_pass
                        for action in self._execution_package.actions
                    )
                ]
                pass_action = next(
                    (
                        action
                        for action in self._execution_package.actions
                        if action.action_code == 299
                    ),
                    None,
                )
                pass_allowed = (
                    window.allow_pass
                    and bool(eligible_instances)
                    and len(pass_candidates) == len(eligible_instances)
                    and pass_action is not None
                    and pass_action.allow_pass
                )
                authorized_codes = tuple(
                    code
                    for code in window.allowed_action_codes
                    if code in grant_codes or (code == 299 and pass_allowed)
                )
                rule_target_sets: dict[int, tuple[int, ...]] = {}
                for instance, skill in eligible_instances:
                    skill_values = {
                        item.key: item.value
                        for item in observation.skill_state
                        if item.ability_instance_id == instance.ability_instance_id
                    }
                    context_values = {
                        "actor": observed_players[player.seat],
                        "observation": observation,
                        "skill_state": skill_values,
                        "request_targets": (),
                    }
                    selected = set(select_seats(skill.targets.selector, context_values))
                    prior = set(rule_target_sets.get(skill.action_code, ()))
                    rule_target_sets[skill.action_code] = tuple(sorted(prior | selected))
                for action_code, supplied in context.eligible_targets_by_action.items():
                    if action_code in rule_target_sets:
                        rule_target_sets[action_code] = tuple(
                            seat for seat in rule_target_sets[action_code] if seat in supplied
                        )
                updates["authorized_action_codes"] = authorized_codes
                updates["eligible_targets_by_action"] = rule_target_sets
                updates["current_kill_target_seat"] = None
                return context.model_copy(update=updates)

            # Night action authorization is rebuilt from the seat's immutable
            # setup grant.  The caller may narrow target context, but cannot
            # add an action code or widen the grant's target universe.
            active_legacy_abilities = _active_night_abilities(player, window, self._registry)
            grant_codes = {ability.action_code for ability in active_legacy_abilities}
            authorized_codes = tuple(
                code
                for code in window.allowed_action_codes
                if code in grant_codes or (code == 299 and window.allow_pass)
            )
            updates["authorized_action_codes"] = authorized_codes
            legacy_target_sets: dict[int, tuple[int, ...]] = {}
            kill_target = _current_kill_target(state, window.window_id, self._registry)
            for ability in active_legacy_abilities:
                derived_targets: set[int] = set(
                    _grant_target_seats(state, player, ability, self._registry)
                )
                try:
                    definition = self._registry.get(ability.action_code)
                except KeyError:
                    definition = None
                if definition is not None and definition.target_policy == "current_kill_not_self":
                    derived_targets = {kill_target} if kill_target is not None else set()
                target_supplied = context.eligible_targets_by_action.get(ability.action_code)
                if target_supplied is not None:
                    derived_targets.intersection_update(target_supplied)
                legacy_target_sets[ability.action_code] = tuple(sorted(derived_targets))
            # Preserve coordinator supplied target restrictions for actions
            # without an active grant only in the trigger/non-night paths.
            updates["eligible_targets_by_action"] = legacy_target_sets
            updates["current_kill_target_seat"] = kill_target
        else:
            updates["authorized_action_codes"] = authorized_codes
        if badge_action:
            badge_error = _sheriff_badge_binding_error(state, window)
            if badge_error is not None:
                raise ActionValidationError("BADGE_ACTION_INVALID", badge_error)
            candidates = window.visible_context.get("candidate_seats")
            if isinstance(candidates, (list, tuple)):
                updates["eligible_targets_by_action"] = {
                    201: tuple(
                        item
                        for item in candidates
                        if isinstance(item, int) and not isinstance(item, bool)
                    )
                }
        if trigger_ability is not None:
            candidates = window.visible_context.get("candidate_seats")
            if isinstance(candidates, (list, tuple)):
                target_sets = dict(context.eligible_targets_by_action)
                target_sets[trigger_ability.action_code] = tuple(
                    item
                    for item in candidates
                    if isinstance(item, int) and not isinstance(item, bool)
                )
                updates["eligible_targets_by_action"] = target_sets
        if rule_occurrence is not None:
            skill = self._workflow_skill(rule_occurrence)
            updates["eligible_targets_by_action"] = {
                skill.action_code: self._trigger_target_seats(state, rule_occurrence, skill)
            }

        pending_ids = tuple(state.action_requests)
        fingerprints: dict[str, str] = {}
        counts: dict[int, int] = {}
        for request_id, payload in state.action_requests.items():
            fingerprint = payload.get("request_fingerprint")
            if isinstance(fingerprint, str):
                fingerprints[request_id] = fingerprint
            seat = payload.get("seat")
            window_id = payload.get("window_id")
            if isinstance(seat, int) and window_id == request.window_id:
                counts[seat] = counts.get(seat, 0) + 1
        updates["submitted_request_ids"] = tuple(
            dict.fromkeys((*context.submitted_request_ids, *pending_ids))
        )
        merged_fingerprints = dict(context.submitted_request_fingerprints)
        merged_fingerprints.update(fingerprints)
        updates["submitted_request_fingerprints"] = merged_fingerprints
        merged_counts = dict(context.submitted_counts_by_seat)
        for seat, count in counts.items():
            merged_counts[seat] = max(merged_counts.get(seat, 0), count)
        updates["submitted_counts_by_seat"] = merged_counts
        return context.model_copy(update=updates)

    def _rule_skill_instances(
        self,
        state: GameState,
        seat: int,
        timing: str,
        *,
        allowed_codes: set[int] | None = None,
        trigger_only: bool = False,
        logical_window_id: str | None = None,
    ) -> tuple[tuple[AbilityInstanceState, SkillSpec], ...]:
        """Return active package skills for one actor and frozen timing."""

        package = self._execution_package
        if package is None:
            return ()
        skills = {item.skill_id: item for item in package.skills}
        actor = state.players.get(seat)
        if actor is None:
            return ()
        result: list[tuple[AbilityInstanceState, SkillSpec]] = []
        for instance in state.ability_instances:
            skill = skills.get(instance.skill_id)
            if (
                instance.actor_seat != seat
                or not instance.enabled
                or instance.consumed
                or skill is None
                or skill.mode != "PLAYER"
                or skill.action_code == 299
                or timing not in skill.timing
                or (allowed_codes is not None and skill.action_code not in allowed_codes)
                or (skill.window_ids and logical_window_id not in skill.window_ids)
                or (trigger_only and instance.grant_kind != "TRIGGER")
                or (not trigger_only and instance.grant_kind != "ACTIVE")
                or instance.grant_id not in {grant.grant_id for grant in skill.grants}
            ):
                continue
            if self._rule_instance_capacity_reason(state, instance, skill) is not None:
                continue
            result.append((instance, skill))
        return tuple(result)

    @staticmethod
    def _rule_instance_capacity_reason(
        state: GameState,
        instance: AbilityInstanceState,
        skill: SkillSpec,
    ) -> str | None:
        """Return why a frozen instance cannot pay or use this skill now."""

        actor = state.players.get(instance.actor_seat)
        if actor is None:
            return "actor_missing"
        usage = skill.usage
        prior_uses = sum(
            1
            for entry in state.rule_ledger
            for use in entry.history_updates
            if use.ability_instance_id == instance.ability_instance_id
            and use.skill_id == skill.skill_id
            and (usage.scope == "GAME" or use.round_number == state.round_no)
            and (not use.passed or usage.pass_updates_history)
        )
        if usage.max_uses is not None and prior_uses >= usage.max_uses:
            return "usage_limit_reached"
        required: dict[str, int] = {}
        for cost in usage.costs:
            required[cost.resource_id] = required.get(cost.resource_id, 0) + cost.amount
        if any(
            actor.skill_resources.get(resource_id, 0) < amount
            for resource_id, amount in required.items()
        ):
            return "insufficient_resource"
        return None

    def _rule_completed_skill_ids(
        self,
        state: GameState,
        window: ActionWindow,
    ) -> set[str]:
        """Resolve accepted current-window requests to their frozen skill IDs."""

        package = self._execution_package
        if package is None:
            return set()
        action_specs = {item.action_code: item for item in package.actions}
        candidate_skills: dict[str, SkillSpec] = {}
        for request_id, payload in state.action_requests.items():
            if (
                not isinstance(payload, Mapping)
                or payload.get("request_id", request_id) != request_id
                or payload.get("window_id") != window.window_id
                or payload.get("status") not in {"PENDING", "CONFIRMED"}
            ):
                continue
            seat = payload.get("seat")
            session_epoch = payload.get("session_epoch")
            if type(seat) is not int:
                continue
            player = state.players.get(seat)
            if (
                player is None
                or session_epoch != window.session_epoch
                or player.session_epoch != window.session_epoch
            ):
                continue
            raw_actions = payload.get("actions")
            if not isinstance(raw_actions, (list, tuple)):
                continue
            instances = tuple(
                item
                for item in state.ability_instances
                if item.actor_seat == seat
                and item.action_code in window.allowed_action_codes
                and item.grant_kind
                == ("TRIGGER" if window.phase is GamePhase.TRIGGER_ACTION else "ACTIVE")
            )
            skills_by_instance = {
                item.ability_instance_id: next(
                    (
                        skill
                        for skill in package.skills
                        if skill.skill_id == item.skill_id
                        and skill.action_code == item.action_code
                        and window.phase.value in skill.timing
                        and (not skill.window_ids or window.logical_window_id in skill.window_ids)
                        and skill.mode == "PLAYER"
                    ),
                    None,
                )
                for item in instances
            }
            active = tuple(
                (instance, skill)
                for instance in instances
                if (skill := skills_by_instance[instance.ability_instance_id]) is not None
            )
            for raw_action in raw_actions:
                if not isinstance(raw_action, Mapping):
                    continue
                action_code = raw_action.get("action_code")
                if type(action_code) is not int:
                    continue
                if action_code == 299:
                    pass_action = action_specs.get(299)
                    if pass_action is not None and pass_action.allow_pass:
                        for _instance, skill in active:
                            action_spec = action_specs.get(skill.action_code)
                            if action_spec is not None and action_spec.allow_pass:
                                candidate_skills[skill.skill_id] = skill
                    continue
                matching = [
                    skill for _instance, skill in active if skill.action_code == action_code
                ]
                if len(matching) == 1:
                    candidate_skills[matching[0].skill_id] = matching[0]

        completed: set[str] = set()
        while True:
            newly_completed = {
                skill_id
                for skill_id, skill in candidate_skills.items()
                if skill_id not in completed and set(skill.after_skills).issubset(completed)
            }
            if not newly_completed:
                break
            completed.update(newly_completed)
        return completed


__all__ = [
    "DeliveryAck",
    "EventCommitError",
    "GameManager",
    "RevisionConflict",
    "ResolutionError",
    "StatePatch",
    "StatePatchError",
    "reduce_state",
]
