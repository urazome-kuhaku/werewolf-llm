"""Explicit, board-agnostic phase transition table and pure reducer."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from werewolf.domain.enums import GamePhase

from .events import GameEvent
from .state import GameState, utc_now


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """One edge in the fixed lifecycle graph.

    Guards that depend on a board snapshot, open action windows, or a
    moderator decision are intentionally left to the future coordinator.
    """

    source: GamePhase
    target: GamePhase


_TRANSITION_EDGES = (
    # Setup and first night.
    PhaseTransition(GamePhase.CREATED, GamePhase.RULESET_READY),
    PhaseTransition(GamePhase.RULESET_READY, GamePhase.ASSIGNED),
    PhaseTransition(GamePhase.ASSIGNED, GamePhase.PLAYER_PREPARE),
    PhaseTransition(GamePhase.PLAYER_PREPARE, GamePhase.NIGHT_TEAM_CHAT),
    PhaseTransition(GamePhase.NIGHT_TEAM_CHAT, GamePhase.NIGHT_ACTION),
    PhaseTransition(GamePhase.NIGHT_ACTION, GamePhase.NIGHT_RESOLVE),
    PhaseTransition(GamePhase.NIGHT_RESOLVE, GamePhase.DAY_ANNOUNCE),
    # Sheriff election is an explicit optional branch of day announcement.
    PhaseTransition(GamePhase.DAY_ANNOUNCE, GamePhase.DAY_SPEECH),
    PhaseTransition(GamePhase.DAY_ANNOUNCE, GamePhase.SHERIFF_ELECTION_SPEECH),
    PhaseTransition(GamePhase.SHERIFF_ELECTION_SPEECH, GamePhase.SHERIFF_ELECTION),
    PhaseTransition(GamePhase.SHERIFF_ELECTION, GamePhase.SHERIFF_TRANSFER),
    PhaseTransition(GamePhase.SHERIFF_ELECTION, GamePhase.SHERIFF_ELECTION_PK_SPEECH),
    PhaseTransition(GamePhase.SHERIFF_ELECTION_PK_SPEECH, GamePhase.SHERIFF_ELECTION_PK),
    PhaseTransition(GamePhase.SHERIFF_ELECTION_PK, GamePhase.SHERIFF_TRANSFER),
    PhaseTransition(GamePhase.SHERIFF_TRANSFER, GamePhase.DAY_SPEECH),
    # Day discussion, voting, and optional tie-break.
    PhaseTransition(GamePhase.DAY_SPEECH, GamePhase.VOTE),
    PhaseTransition(GamePhase.VOTE, GamePhase.DAY_RESOLVE),
    PhaseTransition(GamePhase.VOTE, GamePhase.VOTE_PK_SPEECH),
    PhaseTransition(GamePhase.VOTE_PK_SPEECH, GamePhase.VOTE_PK),
    PhaseTransition(GamePhase.VOTE_PK, GamePhase.DAY_RESOLVE),
    # Resolution and the next loop.
    PhaseTransition(GamePhase.DAY_RESOLVE, GamePhase.VICTORY_CHECK),
    PhaseTransition(GamePhase.DAY_RESOLVE, GamePhase.TRIGGER_ACTION),
    PhaseTransition(GamePhase.TRIGGER_ACTION, GamePhase.DAY_ANNOUNCE),
    PhaseTransition(GamePhase.TRIGGER_ACTION, GamePhase.VICTORY_CHECK),
    PhaseTransition(GamePhase.VICTORY_CHECK, GamePhase.FINISHED),
    PhaseTransition(GamePhase.VICTORY_CHECK, GamePhase.NIGHT_TEAM_CHAT),
)

ALLOWED_PHASE_TRANSITIONS: Mapping[GamePhase, frozenset[GamePhase]] = MappingProxyType(
    {
        phase: frozenset(edge.target for edge in _TRANSITION_EDGES if edge.source is phase)
        for phase in GamePhase
    }
)


class InvalidTransition(ValueError):
    """Raised when a requested phase edge is absent from the explicit table."""

    def __init__(self, source: GamePhase, target: object) -> None:
        allowed = sorted(phase.value for phase in ALLOWED_PHASE_TRANSITIONS[source])
        self.source = source
        self.target = target
        self.allowed = tuple(allowed)
        allowed_text = ", ".join(allowed) if allowed else "none"
        target_text = target.value if isinstance(target, GamePhase) else repr(target)
        super().__init__(
            f"cannot transition from {source.value} to {target_text}; allowed: {allowed_text}"
        )


def can_transition(source: GamePhase, target: GamePhase) -> bool:
    """Return whether ``source -> target`` is in the fixed lifecycle table."""

    return target in ALLOWED_PHASE_TRANSITIONS[source]


def transition_phase(
    state: GameState,
    target: GamePhase,
    *,
    now: datetime | None = None,
    expected_revision: int | None = None,
) -> GameState:
    """Return a new state with one validated phase transition.

    The old state is never mutated.  The candidate is serialized back through
    Pydantic before it is returned, so a failed validation cannot expose a
    partially updated object.  ``state_revision`` increases exactly once.
    """

    if not isinstance(state, GameState):
        raise TypeError("state must be a GameState")
    if not isinstance(target, GamePhase):
        try:
            target = GamePhase(target)
        except (TypeError, ValueError) as exc:
            raise InvalidTransition(state.phase, target) from exc
    if expected_revision is not None and expected_revision != state.state_revision:
        raise ValueError(
            f"state revision mismatch: expected {expected_revision}, actual {state.state_revision}"
        )
    if not can_transition(state.phase, target):
        raise InvalidTransition(state.phase, target)
    if (
        state.phase is GamePhase.TRIGGER_ACTION
        and target
        in {
            GamePhase.DAY_ANNOUNCE,
            GamePhase.VICTORY_CHECK,
        }
        and state.pending_resolution is not None
    ):
        raise InvalidTransition(state.phase, target)

    transition_time = utc_now() if now is None else now
    data = state.model_dump(mode="python", warnings=False)
    # Extension fields in GameState are recursively frozen JSON containers.
    # A phase-only replacement must thaw those fields before strict Pydantic
    # validation, otherwise an already installed ActionWindow prevents a
    # perfectly valid phase transition.
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
        "vote_state",
        "sheriff_election",
        "sheriff_badge",
    ):
        data[field_name] = json.loads(json.dumps(data[field_name]))
    for field_name in ("resolutions", "knowledge_receipts", "moderator_audit"):
        data[field_name] = tuple(json.loads(json.dumps(data[field_name])))
    data["phase"] = target
    # A night-to-day edge starts the numbered day.  The complete cycle ends
    # when victory is checked, so entering that boundary advances the round
    # before either FINISHED or the next NIGHT_TEAM_CHAT edge is committed.
    if target is GamePhase.DAY_ANNOUNCE and state.phase in {
        GamePhase.NIGHT_RESOLVE,
        GamePhase.TRIGGER_ACTION,
    }:
        data["day_no"] = state.day_no + 1
    if target is GamePhase.VICTORY_CHECK and state.phase in {
        GamePhase.DAY_RESOLVE,
        GamePhase.TRIGGER_ACTION,
    }:
        data["round_no"] = state.round_no + 1
    data["state_revision"] = state.state_revision + 1
    data["updated_at"] = transition_time
    return GameState.model_validate(data)


__all__ = [
    "ALLOWED_PHASE_TRANSITIONS",
    "InvalidTransition",
    "PhaseTransition",
    "can_transition",
    "transition_phase",
]
