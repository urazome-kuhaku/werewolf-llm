"""Private-seat projection for the Pi skill status query.

The authoritative game state is deliberately not a player-facing payload.  This
module copies only the grants, counters, resources, and open action windows
belonging to one authenticated seat.  It also validates the binding against
the current state before creating the projection so a token from an old game or
session epoch cannot read a newer state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from werewolf.domain.sheriff_eligibility import should_mask_unannounced_first_night_death


class SkillStatusSessionMismatch(ValueError):
    """Raised when a token binding no longer matches the game state."""


def project_skill_status(
    state: Any,
    *,
    game_id: str,
    snapshot_id: str,
    seat: int,
    session_epoch: int,
) -> dict[str, object]:
    """Return a seat-only skill projection after validating its binding."""

    if state is None:
        raise SkillStatusSessionMismatch("game state is unavailable")
    state_game_id = getattr(state, "game_id", None)
    ruleset = getattr(state, "ruleset", None)
    if (
        state_game_id != game_id
        or ruleset is None
        or getattr(ruleset, "snapshot_id", None) != snapshot_id
    ):
        raise SkillStatusSessionMismatch("game binding is obsolete")
    raw_players = getattr(state, "players", None)
    if not isinstance(raw_players, Mapping):
        raise SkillStatusSessionMismatch("player session is obsolete")
    players: Mapping[Any, Any] = raw_players
    player = players.get(seat)
    if player is None or getattr(player, "session_epoch", None) != session_epoch:
        raise SkillStatusSessionMismatch("player session is obsolete")

    abilities: list[dict[str, object]] = []
    for ability in getattr(player, "granted_abilities", ()):
        abilities.append(_active_ability(ability))
    for ability in getattr(player, "granted_trigger_abilities", ()):
        abilities.append(_trigger_ability(ability))

    windows = _open_windows(
        getattr(state, "action_windows", {}),
        current_phase=getattr(state.phase, "value", state.phase),
        pending_resolution=getattr(state, "pending_resolution", None),
        game_id=game_id,
        seat=seat,
        active_abilities=getattr(player, "granted_abilities", ()),
        trigger_abilities=getattr(player, "granted_trigger_abilities", ()),
        resources=getattr(player, "skill_resources", {}),
        session_epoch=session_epoch,
        players=players,
        sheriff_seat=getattr(state, "sheriff_seat", None),
        sheriff_badge=getattr(state, "sheriff_badge", None),
        sheriff_election=getattr(state, "sheriff_election", None),
    )

    mask_first_night_death = should_mask_unannounced_first_night_death(state, seat)
    return {
        "game_id": state_game_id,
        "phase": getattr(state.phase, "value", state.phase),
        "round_no": getattr(state, "round_no", 0),
        "day_no": getattr(state, "day_no", 0),
        "seat": getattr(player, "seat", seat),
        "session_epoch": getattr(player, "session_epoch", session_epoch),
        "role_id": getattr(player, "role_id", None),
        # Before the first-day sheriff election the moderator has not yet
        # announced night deaths.  The seat may participate in that election,
        # but its private skill query must not reveal its own hidden death.
        "alive": True if mask_first_night_death else getattr(player, "alive", False),
        "death_cause": None if mask_first_night_death else getattr(player, "death_cause", None),
        "abilities": abilities,
        "resources": dict(getattr(player, "skill_resources", {})),
        "windows": windows,
    }


def _active_ability(ability: Any) -> dict[str, object]:
    usage_limit = ability.usage_limit
    max_uses = None if usage_limit is None else usage_limit.max_uses
    consumed = max_uses is not None and ability.uses_consumed >= max_uses
    return {
        "ability_id": ability.ability_id,
        "kind": "ACTIVE",
        "action_code": ability.action_code,
        "timing": ability.timing.value,
        "allowed_phases": [phase.value for phase in ability.allowed_phases],
        "target_rule": _target_rule(ability.target_rule),
        "uses_consumed": ability.uses_consumed,
        "usage_limit": _usage_limit(usage_limit),
        "resource": _resource(ability.resource),
        "consumed": consumed,
    }


def _trigger_ability(ability: Any) -> dict[str, object]:
    trigger = ability.trigger
    return {
        "ability_id": ability.ability_id,
        "kind": "TRIGGER",
        "action_code": ability.action_code,
        "trigger": {
            "event": trigger.event.value,
            "allowed_death_causes": list(trigger.allowed_death_causes),
            "mode": trigger.mode.value,
            "effects": [effect.value for effect in trigger.effects],
            "allow_pass": trigger.allow_pass,
            "once": trigger.once,
        },
        "target_rule": _target_rule(ability.target_rule),
        "consumed": ability.consumed,
    }


def _target_rule(rule: Any) -> dict[str, object]:
    return {
        "kind": rule.kind.value,
        "min_targets": rule.min_targets,
        "max_targets": rule.max_targets,
        "allow_self": rule.allow_self,
        "allow_dead": rule.allow_dead,
    }


def _usage_limit(limit: Any) -> dict[str, object] | None:
    if limit is None:
        return None
    return {
        "max_uses": limit.max_uses,
        "uses_per_round": limit.uses_per_round,
    }


def _resource(resource: Any) -> dict[str, object] | None:
    if resource is None:
        return None
    return {
        "resource_id": resource.resource_id,
        "cost_per_use": resource.cost_per_use,
    }


def _open_windows(
    raw_windows: Mapping[str, Mapping[str, Any]],
    *,
    current_phase: object,
    pending_resolution: object,
    game_id: str,
    seat: int,
    active_abilities: tuple[Any, ...],
    trigger_abilities: tuple[Any, ...],
    resources: Mapping[str, int],
    session_epoch: int,
    players: Mapping[Any, Any],
    sheriff_seat: object,
    sheriff_badge: object,
    sheriff_election: object,
) -> list[dict[str, object]]:
    """Project only current, actionable windows for this authenticated seat.

    ``action_windows`` is an append-only snapshot field.  A window can remain
    open in that mapping after the coordinator has moved to another phase, so
    it must be matched against the current phase before it is exposed.  A
    trigger window has an additional authority source: the pending resolution
    must bind the requested seat, ability, action code, and physical window.
    """

    result: list[dict[str, object]] = []
    current_phase_value = getattr(current_phase, "value", current_phase)
    if not isinstance(current_phase_value, str):
        return result
    for key, raw in raw_windows.items():
        if not isinstance(raw, Mapping) or raw.get("closed_at") is not None:
            continue
        if not bool(raw.get("dependencies_satisfied", True)):
            continue
        allowed_seats = raw.get("allowed_seats")
        if not isinstance(allowed_seats, (list, tuple)) or seat not in allowed_seats:
            continue
        phase = raw.get("phase")
        phase_value = getattr(phase, "value", phase)
        if phase_value != current_phase_value or not isinstance(phase_value, str):
            continue
        window_session_epoch = raw.get("session_epoch")
        if type(window_session_epoch) is not int or window_session_epoch != session_epoch:
            continue
        badge_candidates = _bound_badge_candidates(
            raw,
            key=key,
            expected_game_id=game_id,
            seat=seat,
            session_epoch=session_epoch,
            current_phase_value=current_phase_value,
            sheriff_seat=sheriff_seat,
            sheriff_badge=sheriff_badge,
            sheriff_election=sheriff_election,
            players=players,
        )
        if badge_candidates is not None:
            visible_codes = [201, 202]
            window_id = raw.get("window_id", key)
            if not isinstance(window_id, str) or not window_id:
                window_id = key
            result.append(
                {
                    "window_id": window_id,
                    "phase": phase_value,
                    "kind": "SHERIFF_BADGE",
                    "allowed_action_codes": visible_codes,
                    "allow_pass": False,
                    "candidate_seats": list(badge_candidates),
                    "dependencies_satisfied": bool(raw.get("dependencies_satisfied", True)),
                }
            )
            continue
        bound_trigger = None
        if phase_value == "TRIGGER_ACTION":
            bound_trigger = _bound_trigger_ability(
                pending_resolution,
                seat=seat,
                trigger_abilities=trigger_abilities,
                raw_window=raw,
                key=key,
            )
            if bound_trigger is None:
                continue
            action_codes = {int(bound_trigger.action_code)}
        else:
            action_codes = _usable_action_codes(
                phase_value,
                active_abilities=active_abilities,
                resources=resources,
            )
        raw_codes = raw.get("allowed_action_codes", ())
        if not isinstance(raw_codes, (list, tuple)):
            continue
        visible_codes = sorted(
            {
                int(code)
                for code in raw_codes
                if isinstance(code, int) and not isinstance(code, bool) and code in action_codes
            }
        )
        allow_pass = bool(raw.get("allow_pass", False))
        if bound_trigger is not None:
            allow_pass = allow_pass and bool(bound_trigger.trigger.allow_pass)
        allow_pass = allow_pass and 299 in raw_codes and bool(visible_codes)
        if allow_pass and 299 not in visible_codes:
            visible_codes.append(299)
            visible_codes.sort()
        if not visible_codes:
            continue
        window_id = raw.get("window_id", key)
        if not isinstance(window_id, str) or not window_id:
            window_id = key
        result.append(
            {
                "window_id": window_id,
                "phase": phase_value,
                "allowed_action_codes": visible_codes,
                "allow_pass": allow_pass,
                "dependencies_satisfied": bool(raw.get("dependencies_satisfied", True)),
            }
        )
    result.sort(key=lambda item: str(item["window_id"]))
    return result


def _bound_badge_candidates(
    raw_window: Mapping[str, Any],
    *,
    key: str,
    expected_game_id: str,
    seat: int,
    session_epoch: int,
    current_phase_value: str,
    sheriff_seat: object,
    sheriff_badge: object,
    sheriff_election: object,
    players: Mapping[Any, Any],
) -> tuple[int, ...] | None:
    """Return frozen badge targets only for the currently held office.

    Badge windows share the daytime trigger phase but do not come from a role
    grant.  The durable marker and the installed window must agree on every
    identity and target field before this private projection exposes codes
    201/202 to a Pi seat.
    """

    visible_context = raw_window.get("visible_context")
    if not isinstance(visible_context, Mapping) or visible_context.get("kind") != "sheriff_badge":
        return None
    if not isinstance(sheriff_badge, Mapping) or sheriff_badge.get("status") != "OPEN":
        return None
    source = sheriff_badge.get("source_seat")
    marker_epoch = sheriff_badge.get("source_session_epoch")
    marker_window_id = sheriff_badge.get("window_id")
    office = sheriff_badge.get("office_seat")
    source_player = players.get(source) if type(source) is int else None
    if (
        type(source) is not int
        or source != seat
        or sheriff_seat != seat
        or type(office) is not int
        or office != source
        or type(marker_epoch) is not int
        or marker_epoch != session_epoch
        or marker_window_id != raw_window.get("window_id", key)
        or raw_window.get("game_id") != expected_game_id
        or type(raw_window.get("session_epoch")) is not int
        or raw_window.get("session_epoch") != session_epoch
        or visible_context.get("source_seat") != source
        or (
            current_phase_value not in {"DAY_ANNOUNCE", "DAY_RESOLVE", "TRIGGER_ACTION"}
            and not (current_phase_value == "DAY_SPEECH" and sheriff_election is not None)
        )
        or source_player is None
        or (getattr(source_player, "alive", False) and getattr(source_player, "can_vote", False))
    ):
        return None
    if raw_window.get("allowed_seats") not in ([seat], (seat,)):
        return None
    if raw_window.get("allowed_action_codes") not in ([201, 202], (201, 202)):
        return None
    if raw_window.get("min_actions", 1) != 1 or raw_window.get("max_actions", 1) != 1:
        return None
    if bool(raw_window.get("allow_pass", False)):
        return None
    if current_phase_value != getattr(raw_window.get("phase"), "value", raw_window.get("phase")):
        return None
    marker_candidates = sheriff_badge.get("candidate_seats")
    context_candidates = visible_context.get("candidate_seats")
    if not isinstance(marker_candidates, (list, tuple)) or not isinstance(
        context_candidates, (list, tuple)
    ):
        return None
    try:
        frozen = tuple(marker_candidates)
        contextual = tuple(context_candidates)
    except TypeError:
        return None
    if frozen != contextual or frozen != tuple(sorted(frozen)) or len(set(frozen)) != len(frozen):
        return None
    for candidate in frozen:
        if type(candidate) is not int or candidate == seat:
            return None
        target = players.get(candidate)
        if (
            target is None
            or not getattr(target, "alive", False)
            or not getattr(target, "can_vote", False)
        ):
            return None
    return frozen


def _usable_action_codes(
    phase: str,
    *,
    active_abilities: tuple[Any, ...],
    resources: Mapping[str, int],
) -> set[int]:
    codes: set[int] = set()
    for ability in active_abilities:
        usage_limit = getattr(ability, "usage_limit", None)
        max_uses = None if usage_limit is None else usage_limit.max_uses
        if max_uses is not None and ability.uses_consumed >= max_uses:
            continue
        resource = getattr(ability, "resource", None)
        if resource is not None and resources.get(resource.resource_id, 0) < resource.cost_per_use:
            continue
        phases = getattr(ability, "allowed_phases", ())
        if phase in {getattr(item, "value", item) for item in phases}:
            codes.add(int(ability.action_code))
    return codes


def _bound_trigger_ability(
    pending_resolution: object,
    *,
    seat: int,
    trigger_abilities: tuple[Any, ...],
    raw_window: Mapping[str, Any],
    key: str,
) -> Any | None:
    """Return the one unconsumed trigger bound by the current resolution.

    Trigger abilities are not ordinary phase abilities.  Merely having a
    trigger grant must never make every stale or forged ``TRIGGER_ACTION``
    window visible.  The pending resolution and the installed window must
    agree on the seat, ability, action code, event, and resolution identity.
    """

    if not isinstance(pending_resolution, Mapping):
        return None
    if pending_resolution.get("status") != "TRIGGER_ACTION_REQUIRED":
        return None
    pending_seat = pending_resolution.get("seat")
    ability_id = pending_resolution.get("ability_id")
    action_code = pending_resolution.get("action_code")
    trigger_event = pending_resolution.get("trigger_event")
    resolution_id = pending_resolution.get("resolution_id")
    if (
        isinstance(pending_seat, bool)
        or pending_seat != seat
        or not isinstance(ability_id, str)
        or not ability_id
        or isinstance(action_code, bool)
        or not isinstance(action_code, int)
        or action_code <= 0
        or not isinstance(trigger_event, str)
        or not trigger_event
        or not isinstance(resolution_id, str)
        or not resolution_id
    ):
        return None

    matches = [
        ability
        for ability in trigger_abilities
        if (
            not bool(getattr(ability, "consumed", False))
            and getattr(ability, "ability_id", None) == ability_id
            and getattr(ability, "action_code", None) == action_code
            and getattr(getattr(ability, "trigger", None), "event", None) == trigger_event
        )
    ]
    if len(matches) != 1:
        return None
    ability = matches[0]

    window_id = raw_window.get("window_id", key)
    if not isinstance(window_id, str) or not window_id:
        window_id = key
    expected_window_id = pending_resolution.get("window_id")
    if not isinstance(expected_window_id, str) or not expected_window_id:
        expected_window_id = _derived_trigger_window_id(
            resolution_id,
            seat=seat,
            ability_id=ability_id,
        )
    if window_id != expected_window_id:
        return None

    visible_context = raw_window.get("visible_context", {})
    if visible_context is not None:
        if not isinstance(visible_context, Mapping):
            return None
        for field, expected in (
            ("resolution_id", resolution_id),
            ("ability_id", ability_id),
            ("action_code", action_code),
            ("trigger_event", trigger_event),
        ):
            actual = visible_context.get(field)
            if actual is not None and actual != expected:
                return None
    return ability


def _derived_trigger_window_id(resolution_id: str, *, seat: int, ability_id: str) -> str:
    window_id = f"{resolution_id}-trigger-{ability_id}"
    if len(window_id) > 128:
        window_id = f"trigger-{seat}-{resolution_id[-60:]}-{ability_id[-40:]}"
    return window_id


__all__ = ["SkillStatusSessionMismatch", "project_skill_status"]
