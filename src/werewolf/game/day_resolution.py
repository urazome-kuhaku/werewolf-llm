"""Board driven decisions for the daytime exile boundary.

The vote collector only establishes a public tally.  This module translates
that tally into an explicit moderator decision using the already frozen board
and role binding fields.  It deliberately does not mutate game state; the
manager owns the serialized commit of the resulting decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.role import TargetKind, TriggerEffect, TriggerEvent, TriggerMode

from .actions import ActionWindow
from .state import GameState, GrantedTriggerAbility
from .voting import PublicVoteResult


@dataclass(frozen=True, slots=True)
class DayExileDecision:
    """One fully resolved, moderator-confirmed daytime result."""

    window_id: str
    resolution_id: str
    target_seat: int | None
    outcome_code: str
    alive_after: bool | None
    death_cause: str | None
    can_vote_after: bool | None
    next_phase: GamePhase
    public_message: str
    reveal_role: bool
    trigger_action: bool
    audit_details: Mapping[str, object]
    trigger_ability_id: str | None = None
    trigger_action_code: int | None = None
    trigger_event: str | None = None


def _trigger_abilities_for_event(
    player: object,
    event: TriggerEvent,
    *,
    death_cause: str | None = None,
) -> tuple[GrantedTriggerAbility, ...]:
    """Select unconsumed abilities from the private assigned-seat state.

    The board resolver intentionally does not inspect role IDs or board prose.
    Assignment has already copied the executable trigger contracts onto the
    seat, so this is the only source used to decide whether a special window
    exists.
    """

    abilities = getattr(player, "granted_trigger_abilities", ())
    selected: list[GrantedTriggerAbility] = []
    for ability in abilities:
        trigger = ability.trigger
        if ability.consumed or trigger.event is not event:
            continue
        if death_cause is not None and death_cause not in trigger.allowed_death_causes:
            continue
        selected.append(ability)
    return tuple(selected)


def _automatic_effects(ability: GrantedTriggerAbility) -> dict[str, bool]:
    """Project the closed trigger effects onto the day decision fields."""

    effects = set(ability.trigger.effects)
    return {
        "reveal_role": TriggerEffect.REVEAL_ROLE in effects,
        "survive": TriggerEffect.SURVIVE_TRIGGER in effects,
        "remove_vote_right": TriggerEffect.REMOVE_VOTE_RIGHT in effects,
        "open_player_action": TriggerEffect.OPEN_PLAYER_ACTION in effects,
    }


def _candidate_seats(
    state: GameState,
    seat: int,
    ability: GrantedTriggerAbility,
) -> tuple[int, ...]:
    """Build a conservative candidate list from the typed target contract."""

    rule = ability.target_rule
    if rule.kind is TargetKind.NONE:
        return ()
    candidates = sorted(
        other_seat
        for other_seat, other in state.players.items()
        if (other.alive or rule.allow_dead) and (rule.allow_self or other_seat != seat)
    )
    return tuple(candidates)


def build_day_exile_decision_for_window(
    board: BoardDefinition,
    state: GameState,
    vote_result: PublicVoteResult,
    *,
    window_id: str,
) -> DayExileDecision:
    """Build the decision and bind it to the resolved vote window."""

    resolution_id = f"day-exile-{window_id}"
    target = vote_result.eliminated_seat
    if target is None:
        return DayExileDecision(
            window_id=window_id,
            resolution_id=resolution_id,
            target_seat=None,
            outcome_code="no_exile",
            alive_after=None,
            death_cause=None,
            can_vote_after=None,
            next_phase=GamePhase.DAY_RESOLVE,
            public_message="本轮无人被放逐。",
            reveal_role=False,
            trigger_action=False,
            audit_details={
                "vote_window_id": window_id,
                "target_seat": None,
                "outcome_code": "no_exile",
            },
        )

    player = state.players.get(target)
    if player is None:
        raise ValueError("vote result names an unknown exile seat")
    exile_abilities = _trigger_abilities_for_event(player, TriggerEvent.EXILE_SELECTED)
    automatic = next(
        (ability for ability in exile_abilities if ability.trigger.mode is TriggerMode.AUTOMATIC),
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
    # Once the exile result is confirmed, death-trigger abilities match the
    # resulting cause.  They are intentionally checked only after the
    # pre-death EXILE_SELECTED automatic boundary has had its chance to act.
    if automatic is None and choice is None:
        death_choice = next(
            (
                ability
                for ability in _trigger_abilities_for_event(
                    player,
                    TriggerEvent.DEATH_CONFIRMED,
                    death_cause="exiled",
                )
                if ability.trigger.mode is TriggerMode.PLAYER_CHOICE
            ),
            None,
        )
        choice = death_choice
    if automatic is not None:
        automatic_effects = _automatic_effects(automatic)
        return DayExileDecision(
            window_id=window_id,
            resolution_id=resolution_id,
            target_seat=target,
            outcome_code="trigger_automatic",
            alive_after=True if automatic_effects.get("survive") else False,
            death_cause=None if automatic_effects.get("survive") else "exiled",
            can_vote_after=(
                False
                if automatic_effects.get("survive") and automatic_effects.get("remove_vote_right")
                else bool(automatic_effects.get("survive"))
            ),
            next_phase=GamePhase.DAY_RESOLVE,
            public_message=(
                f"座位 {target} 被投票放逐，触发角色能力，继续留在场上。"
                if automatic_effects.get("survive")
                else f"座位 {target} 被投票放逐。"
            ),
            reveal_role=automatic_effects.get("reveal_role", False),
            trigger_action=False,
            audit_details={
                "vote_window_id": window_id,
                "target_seat": target,
                "ability_id": automatic.ability_id,
                "action_code": automatic.action_code,
                "trigger_event": automatic.trigger.event.value,
                "outcome_code": "trigger_automatic",
            },
            trigger_ability_id=automatic.ability_id,
            trigger_action_code=automatic.action_code,
            trigger_event=automatic.trigger.event.value,
        )

    # An EXILE_SELECTED choice is still a generic trigger window.  The target
    # is considered exiled unless the trigger's effects explicitly preserve it;
    # effects that are applied before the window are represented in the same
    # decision so the pending marker is atomic.
    trigger = choice
    trigger_effects = _automatic_effects(trigger) if trigger is not None else {}
    trigger_action = trigger is not None and trigger_effects.get("open_player_action", False)
    reveal_role = board.identity_reveal.reveal_on_exile or trigger_effects.get("reveal_role", False)
    role_suffix = "，身份已公开" if reveal_role else ""
    message = f"座位 {target} 被投票放逐{role_suffix}。"
    trigger_ability_id = trigger.ability_id if trigger is not None else None
    trigger_action_code = trigger.action_code if trigger is not None else None
    trigger_event = trigger.trigger.event.value if trigger is not None else None
    return DayExileDecision(
        window_id=window_id,
        resolution_id=resolution_id,
        target_seat=target,
        outcome_code="trigger_player_choice" if trigger_action else "exiled",
        alive_after=True if trigger_effects.get("survive") else False,
        death_cause=None if trigger_effects.get("survive") else "exiled",
        can_vote_after=False if trigger_effects.get("remove_vote_right") else False,
        next_phase=GamePhase.TRIGGER_ACTION if trigger_action else GamePhase.DAY_RESOLVE,
        public_message=message,
        reveal_role=reveal_role,
        trigger_action=trigger_action,
        audit_details={
            "vote_window_id": window_id,
            "target_seat": target,
            "ability_id": trigger_ability_id,
            "action_code": trigger_action_code,
            "trigger_event": trigger_event,
            "outcome_code": "trigger_player_choice" if trigger_action else "exiled",
            "trigger_action": trigger_action,
        },
        trigger_ability_id=trigger_ability_id if trigger_action else None,
        trigger_action_code=trigger_action_code if trigger_action else None,
        trigger_event=trigger_event if trigger_action else None,
    )


def build_trigger_action_window(
    board: BoardDefinition,
    state: GameState,
    *,
    resolution_id: str | None = None,
) -> ActionWindow:
    """Build an action window for the pending, ability-driven trigger.

    A death trigger is deliberately separate from an ordinary skill window:
    the actor is already dead, and the pending moderator resolution is the
    authority that permits this exception.  The manager binds the returned
    window to that pending resolution before accepting any request.
    """

    if state.phase is not GamePhase.TRIGGER_ACTION:
        raise ValueError("trigger action window requires TRIGGER_ACTION")
    pending = state.pending_resolution
    if not isinstance(pending, dict) or pending.get("status") != "TRIGGER_ACTION_REQUIRED":
        raise ValueError("no hunter trigger resolution is pending")
    ability_id = pending.get("ability_id")
    if not isinstance(ability_id, str) or not ability_id:
        raise ValueError("pending trigger has no ability ID")
    pending_resolution_id = pending.get("resolution_id")
    if not isinstance(pending_resolution_id, str) or not pending_resolution_id:
        raise ValueError("hunter trigger resolution has no resolution ID")
    if resolution_id is not None and resolution_id != pending_resolution_id:
        raise ValueError("hunter trigger resolution ID does not match pending resolution")
    seat = pending.get("seat")
    if isinstance(seat, bool) or not isinstance(seat, int):
        raise ValueError("hunter trigger resolution has no valid seat")
    player = state.players.get(seat)
    if player is None:
        raise ValueError("pending trigger seat is unknown")
    abilities = [
        item
        for item in player.granted_trigger_abilities
        if item.ability_id == ability_id and not item.consumed
    ]
    if len(abilities) != 1:
        raise ValueError("pending trigger ability is not granted or already consumed")
    ability = abilities[0]
    trigger = ability.trigger
    event_value = pending.get("trigger_event")
    if event_value != trigger.event.value:
        raise ValueError("pending trigger event does not match the granted ability")
    cause = pending.get("death_cause")
    if trigger.event is TriggerEvent.DEATH_CONFIRMED:
        if player.alive or player.death_cause is None or player.death_cause != cause:
            raise ValueError("death trigger requires a dead seat with the matching cause")
        if cause not in trigger.allowed_death_causes:
            raise ValueError("death cause is not eligible for the granted ability")
    window_id = f"{pending_resolution_id}-trigger-{ability_id}"
    if len(window_id) > 128:
        window_id = f"trigger-{seat}-{pending_resolution_id[-60:]}-{ability_id[-40:]}"
    candidates = _candidate_seats(state, seat, ability)
    allowed_codes = (ability.action_code, 299) if trigger.allow_pass else (ability.action_code,)
    return ActionWindow(
        window_id=window_id,
        game_id=state.game_id,
        session_epoch=player.session_epoch,
        phase=GamePhase.TRIGGER_ACTION,
        allowed_seats=(seat,),
        allowed_role_ids=(),
        allowed_action_codes=allowed_codes,
        min_actions=1,
        max_actions=1,
        allow_pass=trigger.allow_pass,
        opened_at=state.updated_at,
        visible_context={
            "trigger_event": trigger.event.value,
            "ability_id": ability.ability_id,
            "action_code": ability.action_code,
            "resolution_id": pending_resolution_id,
            "death_cause": cause,
            "snapshot_revision": pending.get("snapshot_revision"),
            # ``visible_context`` is persisted as JSON.  Keep the wire form
            # list shaped here; the manager will round-trip it back to a
            # tuple when it reloads the frozen window.
            "candidate_seats": list(candidates),
        },
    )


# Compatibility spelling for adapters that still call the old constructor.
# The implementation is completely driven by the pending ability contract.
build_hunter_trigger_window = build_trigger_action_window


__all__ = [
    "DayExileDecision",
    "build_trigger_action_window",
    "build_hunter_trigger_window",
    "build_day_exile_decision_for_window",
]
