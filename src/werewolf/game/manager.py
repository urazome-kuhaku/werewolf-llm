"""Serialized authoritative state commits for one game.

The manager is deliberately small: board specific rules still arrive through
the frozen action window and validation context.  It owns the only replacement
of the in-memory :class:`~werewolf.game.state.GameState`; model, HTTP, and
filesystem work belongs outside this module and outside its lock.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from pydantic import JsonValue

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.preview import experimental_preview_enabled
from werewolf.knowledge.role import TargetKind, TriggerEffect, TriggerEvent, TriggerMode

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
    PrivateRolePayload,
    PrivateSeerResultPayload,
    PublicAnnouncementPayload,
    PublicSpeechPayload,
    PublicVoteResultPayload,
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
from .setup import PlayerAssignmentPlan
from .sheriff import (
    SheriffCampaignSpeechRequest,
    SheriffElectionError,
    SheriffElectionState,
    SheriffElectionStatus,
    validate_sheriff_start,
)
from .sheriff_eligibility import first_day_sheriff_participants, is_first_day_sheriff_boundary
from .state import (
    GameState,
    GrantedAbility,
    GrantedTriggerAbility,
    PlayerState,
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
        if window.phase is GamePhase.NIGHT_TEAM_CHAT and window.closed_at is None:
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
    # before a replacement model is validated; typed event records stay in
    # Python form so their protocol models are preserved.
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
    if state.vote_state is not None:
        data["vote_state"] = json.loads(json.dumps(state.vote_state))
    return data


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
    for key in ("status", "validated_at", "request_fingerprint", "idempotent_replay"):
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
    """Return protocol events and reject legacy records for new commits."""

    legacy = tuple(event for event in state.events if not isinstance(event, GameEvent))
    if legacy:
        raise EventCommitError(
            "event delivery requires a typed event log; migrate the legacy JSON snapshot first"
        )
    return tuple(event for event in state.events if isinstance(event, GameEvent))


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

    def __init__(self, state: GameState, *, registry: ActionRegistry) -> None:
        if not isinstance(state, GameState):
            raise TypeError("state must be a GameState")
        self._state = state
        self._registry = registry
        self._lock = asyncio.Lock()

    @property
    def state(self) -> GameState:
        """Return the current immutable state reference."""

        return self._state

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
            trigger_action = _is_trigger_window_state(state, window, require_bound=False)
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
            if trigger_action:
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
            if trigger_action:
                data["pending_resolution"] = pending_data
            data["state_revision"] = state.state_revision + 1
            data["updated_at"] = now or utc_now()
            candidate = GameState.model_validate(data)
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
            if not window.is_open:
                raise EventCommitError("WINDOW_CLOSED: action window is closed")
            if window.session_epoch != session_epoch:
                raise EventCommitError("SESSION_MISMATCH: action window uses another session")
            if seat not in window.allowed_seats:
                raise EventCommitError("SEAT_NOT_ALLOWED: seat is not allowed in this window")
            trigger_action = _is_trigger_window_state(state, window, require_bound=True)
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
        *,
        expected_revision: int | None = None,
        now: datetime | None = None,
    ) -> GameState:
        """Confirm a pending tally and append exactly one public result event.

        The event contains only the safe tally projection.  The private
        ``ballots`` map remains in the authoritative snapshot and is never
        copied into the public payload.
        """

        async with self._lock:
            revision = (
                self._state.state_revision if expected_revision is None else expected_revision
            )
            _revision_check(self._state, revision)
            if self._state.vote_state is None:
                raise VoteError("VOTE_WINDOW_NOT_OPEN", "there is no active vote window")
            vote_state = _load_vote_state(self._state.vote_state)
            confirmed = vote_state.confirm_tally()
            result = confirmed.public_result
            if result is None:  # pragma: no cover - guarded by confirm_tally
                raise EventCommitError("VOTE_RESULT_INVALID: confirmation produced no result")
            events = _typed_events(self._state)
            event_id = max((event.event_id for event in events), default=0) + 1
            commit_revision = self._state.state_revision + 1
            timestamp = now or utc_now()
            payload = PublicVoteResultPayload(
                eliminated_seat=result.eliminated_seat,
                tally=dict(result.tally.counts),
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
                payload=PublicVoteResultPayload(
                    eliminated_seat=result.eliminated_seat,
                    tally=dict(result.tally.counts),
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
            election = self._require_sheriff_phase(
                current,
                phase=(GamePhase.SHERIFF_ELECTION, GamePhase.SHERIFF_ELECTION_PK),
                status=SheriffElectionStatus.WAITING_GM,
            )
            _validate_sheriff_board(board, current)
            # Confirming the first tied tally is itself the atomic edge into
            # the PK speech phase.  The tied candidates and tie_round are
            # derived from the private tally; callers cannot replace either.
            if (
                current.phase is GamePhase.SHERIFF_ELECTION
                and election.decision is not None
                and election.decision.action.name == "PK"
            ):
                try:
                    started_pk = election.begin_pk()
                except SheriffElectionError:
                    raise
                data = _state_data(current)
                data["sheriff_election"] = _sheriff_election_payload(started_pk)
                intermediate = GameState.model_validate(data)
                try:
                    committed = transition_phase(
                        intermediate,
                        GamePhase.SHERIFF_ELECTION_PK_SPEECH,
                        expected_revision=revision,
                        now=utc_now() if now is None else now,
                    )
                except (TypeError, ValueError) as exc:
                    raise SheriffElectionError("PHASE_INVALID", str(exc)) from exc
                self._state = committed
                return committed
            try:
                confirmed = election.confirm()
            except SheriffElectionError:
                raise

            data = _state_data(current)
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
            intermediate = GameState.model_validate(data)
            try:
                committed = transition_phase(
                    intermediate,
                    GamePhase.SHERIFF_TRANSFER,
                    expected_revision=revision,
                    now=utc_now() if now is None else now,
                )
            except (TypeError, ValueError) as exc:
                raise SheriffElectionError("PHASE_INVALID", str(exc)) from exc
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
            marker = current.sheriff_badge
            if isinstance(marker, dict):
                if marker.get("status") == "OPEN" and marker.get("source_seat") != source_seat:
                    raise SheriffElectionError(
                        "BADGE_PENDING", "another badge choice is already pending"
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
            if current.phase is GamePhase.DAY_SPEECH:
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
            audits = list(data["moderator_audit"])
            audits.append(
                {
                    "operation": "SHERIFF_BADGE_COMPLETE",
                    "source_seat": source,
                    "target_seat": marker.get("target_seat"),
                    "action_code": marker.get("action_code"),
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
            if state.current_queue is None or not state.current_queue:
                raise EventCommitError("TURN_QUEUE_EMPTY: no serial speech turn is pending")
            if state.phase is not phase:
                raise EventCommitError(f"PHASE_MISMATCH: serial speech requires {phase.value}")
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
                )
                if same_request:
                    return state
                if not retry:
                    raise EventCommitError("TURN_IN_PROGRESS: another physical request is active")
                if (
                    previous.seat != seat
                    or previous.logical_request_id != logical_request_id
                    or previous.attempt_no != attempt_no - 1
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
            if state.current_queue is None or not state.current_queue:
                raise EventCommitError("TURN_QUEUE_EMPTY: no serial speech turn is pending")
            if state.current_queue[0] != seat:
                raise EventCommitError("TURN_NOT_AT_HEAD: only the queue head may speak")
            team_window = (
                _validate_team_chat_seat(state, seat)
                if phase is GamePhase.NIGHT_TEAM_CHAT
                else None
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
            data["current_queue"] = tuple(state.current_queue[1:])
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
            candidate = reduce_state(
                self._state,
                StatePatch.phase_transition(
                    target,
                    expected_revision=revision,
                    now=now,
                ),
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
            if _looks_like_trigger_window(window) and not _is_trigger_window_state(
                self._state, window, require_bound=True
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
        ids = tuple(item.resolution_id for item in resolutions)
        if len(ids) != len(set(ids)):
            raise ResolutionError("DUPLICATE_RESOLUTION", "resolution_id values must be unique")
        async with self._lock:
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
                staged_actor_seats = _batch_live_actor_seats(state, resolutions)
                candidate = state
                for item in resolutions:
                    staged = item.model_copy(update={"base_revision": candidate.state_revision})
                    candidate = _reduce_action_resolution(
                        candidate,
                        staged,
                        registry=self._registry,
                        expected_revision=candidate.state_revision,
                        now=now,
                        staged_actor_seats=staged_actor_seats,
                        validation_state=state,
                    )

            data = _state_data(candidate)
            timestamp = now or utc_now()
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise EventCommitError("TIMESTAMP: commit timestamp must include a timezone")
            timestamp = timestamp.astimezone(UTC)
            private_events = _night_private_result_events(
                state,
                resolutions,
                registry=self._registry,
                next_revision=initial_revision + 1,
                now=timestamp,
            )
            if private_events:
                _validate_new_events(state, private_events, next_revision=initial_revision + 1)
                data["events"] = (*_typed_events(state), *private_events)
            windows = dict(data["action_windows"])
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
        if window.phase != state.phase:
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
        trigger_action = trigger_binding is not None
        badge_action = _is_sheriff_badge_window(window)
        trigger_ability = trigger_binding[1] if trigger_binding is not None else None
        authorized_codes = context.authorized_action_codes
        if trigger_ability is not None:
            authorized_codes = (
                (trigger_ability.action_code, 299)
                if trigger_ability.trigger.allow_pass
                else (trigger_ability.action_code,)
            )
        updates: dict[str, object] = {
            "active_request_id": player.current_request_id,
            "session_epoch": player.session_epoch,
            "player_alive": player.alive or trigger_action or badge_action,
            "role_id": player.role_id,
            "skill_resources": dict(player.skill_resources),
            "alive_seats": tuple(seat for seat, item in state.players.items() if item.alive),
        }
        if window.phase is GamePhase.NIGHT_ACTION and not trigger_action:
            # Night action authorization is rebuilt from the seat's immutable
            # setup grant.  The caller may narrow target context, but cannot
            # add an action code or widen the grant's target universe.
            active_abilities = _active_night_abilities(player, window, self._registry)
            grant_codes = {ability.action_code for ability in active_abilities}
            authorized_codes = tuple(
                code
                for code in window.allowed_action_codes
                if code in grant_codes or (code == 299 and window.allow_pass)
            )
            updates["authorized_action_codes"] = authorized_codes
            target_sets: dict[int, tuple[int, ...]] = {}
            kill_target = _current_kill_target(state, window.window_id, self._registry)
            for ability in active_abilities:
                derived = set(_grant_target_seats(state, player, ability, self._registry))
                try:
                    definition = self._registry.get(ability.action_code)
                except KeyError:
                    definition = None
                if definition is not None and definition.target_policy == "current_kill_not_self":
                    derived = {kill_target} if kill_target is not None else set()
                supplied = context.eligible_targets_by_action.get(ability.action_code)
                if supplied is not None:
                    derived.intersection_update(supplied)
                target_sets[ability.action_code] = tuple(sorted(derived))
            # Preserve coordinator supplied target restrictions for actions
            # without an active grant only in the trigger/non-night paths.
            updates["eligible_targets_by_action"] = target_sets
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
