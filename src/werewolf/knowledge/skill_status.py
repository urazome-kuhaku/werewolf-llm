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
    execution_package: object | None = None,
    action_registry: object | None = None,
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

    compiled_abilities = _compiled_ability_projection(
        state,
        seat=seat,
        execution_package=execution_package,
    )
    if compiled_abilities is None:
        abilities = [
            _active_ability(ability) for ability in getattr(player, "granted_abilities", ())
        ]
        abilities.extend(
            _trigger_ability(ability)
            for ability in getattr(player, "granted_trigger_abilities", ())
        )
        active_abilities = tuple(getattr(player, "granted_abilities", ()))
        trigger_abilities = tuple(getattr(player, "granted_trigger_abilities", ()))
    else:
        abilities = compiled_abilities
        active_abilities = tuple(
            ability for ability in compiled_abilities if ability.get("kind") == "ACTIVE"
        )
        trigger_abilities = tuple(
            ability for ability in compiled_abilities if ability.get("kind") == "TRIGGER"
        )

    windows = _open_windows(
        getattr(state, "action_windows", {}),
        current_phase=getattr(state.phase, "value", state.phase),
        pending_resolution=getattr(state, "pending_resolution", None),
        game_id=game_id,
        seat=seat,
        active_abilities=active_abilities,
        trigger_abilities=trigger_abilities,
        resources=getattr(player, "skill_resources", {}),
        session_epoch=session_epoch,
        players=players,
        sheriff_seat=getattr(state, "sheriff_seat", None),
        sheriff_badge=getattr(state, "sheriff_badge", None),
        sheriff_election=getattr(state, "sheriff_election", None),
        action_registry=action_registry,
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
        "actions": _action_registry_projection(action_registry),
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


def _compiled_ability_projection(
    state: Any,
    *,
    seat: int,
    execution_package: object | None,
) -> list[dict[str, object]] | None:
    """Project only frozen package skills instantiated for this seat.

    The state stores actor-resolved ability instances; executable contracts are
    joined by skill ID against the package frozen with the game. Runtime status
    never consults the live source tree or a process-global registry.
    """

    instances = getattr(state, "ability_instances", None)
    if not isinstance(instances, (list, tuple)) or not instances:
        return None
    if execution_package is None:
        raise SkillStatusSessionMismatch("frozen execution package is unavailable")
    skills_raw = _field(execution_package, "skills", ())
    if not isinstance(skills_raw, (list, tuple)):
        raise SkillStatusSessionMismatch("frozen execution package has no skill registry")
    skills: dict[str, dict[str, object]] = {}
    for value in skills_raw:
        skill = _model_mapping(value)
        if skill is None:
            continue
        skill_id = skill.get("skill_id")
        if isinstance(skill_id, str):
            skills[skill_id] = skill

    result: list[dict[str, object]] = []
    current_round = getattr(state, "round_no", 0)
    for instance_value in instances:
        instance = _model_mapping(instance_value)
        if instance is None or instance.get("actor_seat") != seat:
            continue
        skill_id = instance.get("skill_id")
        if not isinstance(skill_id, str) or skill_id not in skills:
            raise SkillStatusSessionMismatch("frozen ability instance has no package skill")
        skill = skills[skill_id]
        action_code = instance.get("action_code", skill.get("action_code"))
        if type(action_code) is not int or action_code <= 0:
            raise SkillStatusSessionMismatch("frozen ability instance has an invalid action code")
        grant_kind = instance.get("grant_kind", "ACTIVE")
        if grant_kind not in {"ACTIVE", "TRIGGER"}:
            raise SkillStatusSessionMismatch("frozen ability instance has an invalid grant kind")
        targets = _model_mapping(skill.get("targets")) or {}
        usage = _model_mapping(skill.get("usage")) or {}
        costs = usage.get("costs", ())
        cost_records = (
            [_model_mapping(value) for value in costs if _model_mapping(value) is not None]
            if isinstance(costs, (list, tuple))
            else []
        )
        timing = skill.get("timing", ())
        timing_values = list(timing) if isinstance(timing, (list, tuple)) else []
        parameters_raw = skill.get("parameters", ())
        parameters = (
            [_model_mapping(value) for value in parameters_raw if _model_mapping(value) is not None]
            if isinstance(parameters_raw, (list, tuple))
            else []
        )
        instance_id = instance.get("ability_instance_id")
        history = (
            _private_ability_history(state, instance_id, actor_seat=seat)
            if isinstance(instance_id, str)
            else []
        )
        usage_scope = usage.get("scope", "GAME")
        uses_consumed = instance.get("uses_consumed", 0)
        if usage_scope == "ROUND" and type(current_round) is int:
            pass_updates_history = usage.get("pass_updates_history") is True
            uses_consumed = sum(
                1
                for record in history
                if record.get("round_number") == current_round
                and (
                    record.get("disposition") == "ACCEPTED"
                    or (pass_updates_history and record.get("disposition") == "PASSED")
                )
            )
        maximum_uses = usage.get("max_uses")
        consumed = (instance.get("consumed", False) is True and usage_scope != "ROUND") or (
            type(uses_consumed) is int
            and type(maximum_uses) is int
            and uses_consumed >= maximum_uses
        )
        record: dict[str, object] = {
            "skill_id": skill_id,
            "ability_id": instance.get("ability_instance_id", skill_id),
            "ability_instance_id": instance.get("ability_instance_id"),
            "grant_id": instance.get("grant_id"),
            "action_code": action_code,
            "kind": grant_kind,
            "timing": timing_values,
            "allowed_phases": timing_values,
            "target_rule": targets,
            "parameters": parameters,
            "uses_consumed": uses_consumed,
            "usage_limit": {
                "max_uses": maximum_uses,
                "scope": usage_scope,
            },
            "pass_updates_history": usage.get("pass_updates_history", False),
            "charge_on_pass": usage.get("charge_on_pass", False),
            "costs": cost_records,
            "consumed": consumed,
            "enabled": instance.get("enabled", True),
            "history": history,
        }
        if isinstance(instance_id, str):
            record["state"] = _private_ability_state(state, instance_id)
        if grant_kind == "TRIGGER":
            record["trigger"] = {
                "events": timing_values,
                "allow_pass": _execution_action_allows_pass(execution_package, action_code),
            }
        result.append(record)
    result.sort(key=_ability_sort_key)
    return result


def _ability_sort_key(item: Mapping[str, object]) -> tuple[int, str, str]:
    code = item.get("action_code")
    return (
        code if type(code) is int else 0,
        str(item.get("skill_id", "")),
        str(item.get("ability_instance_id", "")),
    )


def _execution_action_allows_pass(execution_package: object, action_code: int) -> bool:
    actions = _field(execution_package, "actions", ())
    if not isinstance(actions, (list, tuple)):
        return False
    return any(
        _field(action, "action_code") == action_code and _field(action, "allow_pass", False) is True
        for action in actions
    )


def _private_ability_state(state: Any, instance_id: str) -> dict[str, object]:
    projected: dict[str, object] = {}
    values = getattr(state, "rule_state", ())
    if not isinstance(values, (list, tuple)):
        return projected
    for value in values:
        item = _model_mapping(value)
        if item is None or item.get("scope") != "ABILITY" or item.get("scope_id") != instance_id:
            continue
        key = item.get("key")
        if isinstance(key, str):
            projected[key] = item.get("value")
    return projected


def _private_ability_history(
    state: Any,
    instance_id: str,
    *,
    actor_seat: int,
) -> list[dict[str, object]]:
    """Project only the current seat's records for this exact ability instance."""

    result: list[dict[str, object]] = []
    entries = getattr(state, "rule_ledger", ())
    if not isinstance(entries, (list, tuple)):
        return result
    for entry_value in entries:
        entry = _model_mapping(entry_value)
        if entry is None:
            continue
        records = entry.get("history_updates", ())
        if not isinstance(records, (list, tuple)):
            continue
        for record_value in records:
            record = _model_mapping(record_value)
            if (
                record is None
                or record.get("ability_instance_id") != instance_id
                or record.get("actor_seat") != actor_seat
            ):
                continue
            projected = {
                key: record[key]
                for key in (
                    "record_id",
                    "request_id",
                    "ability_instance_id",
                    "skill_id",
                    "action_code",
                    "actor_seat",
                    "round_number",
                    "targets",
                    "passed",
                    "successful",
                    "disposition",
                )
                if key in record
            }
            targets = projected.get("targets")
            if isinstance(targets, (list, tuple)) and len(targets) == 1:
                projected["target_seat"] = targets[0]
            result.append(projected)
    return result[-1024:]


def _action_registry_projection(action_registry: object | None) -> list[dict[str, object]]:
    if action_registry is None:
        return []
    actions = _field(action_registry, "actions", ())
    if not isinstance(actions, (list, tuple)):
        return []
    projected: list[dict[str, object]] = []
    for value in actions:
        action = _model_mapping(value)
        if action is None:
            continue
        code = action.get("action_code")
        if type(code) is not int or code <= 0:
            continue
        name = action.get("action_name")
        item: dict[str, object] = {"action_code": code}
        if isinstance(name, str):
            item["action_name"] = name
            item["is_pass"] = name == "PASS"
        target_count = action.get("target_count")
        if type(target_count) is int and target_count >= 0:
            item["target_count"] = target_count
        policy = action.get("target_policy")
        if isinstance(policy, str):
            item["target_policy"] = policy
            item["target_rule"] = {
                "kind": "NONE" if policy == "none" else "PLAYER",
                "min_targets": target_count if type(target_count) is int else 0,
                "max_targets": target_count if type(target_count) is int else 0,
                "allow_self": policy == "candidate",
            }
        projected.append(item)
    projected.sort(key=_registry_action_sort_key)
    return projected


def _registry_action_sort_key(item: Mapping[str, object]) -> int:
    code = item.get("action_code")
    return code if type(code) is int else 0


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _model_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else None
    return None


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
    action_registry: object | None,
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
            action_registry=action_registry,
        )
        if badge_candidates is not None:
            visible_codes = _badge_action_codes(raw, action_registry)
            if visible_codes is None:
                continue
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
    action_registry: object | None,
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
    if _badge_action_codes(raw_window, action_registry) is None:
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


def _badge_action_codes(
    raw_window: Mapping[str, Any],
    action_registry: object | None,
) -> list[int] | None:
    raw_codes = raw_window.get("allowed_action_codes")
    if not isinstance(raw_codes, (list, tuple)):
        return None
    if any(type(code) is not int or code <= 0 for code in raw_codes):
        return None
    codes = list(raw_codes)
    if len(codes) != 2 or len(set(codes)) != 2:
        return None
    if action_registry is None:
        # Old snapshots predate the frozen package registry. Their badge
        # contract remains explicitly bounded to the existing common codes.
        return codes if codes == [201, 202] else None
    registered = {
        action.get("action_code")
        for value in _registry_actions(action_registry)
        if (action := _model_mapping(value)) is not None
    }
    return codes if all(code in registered for code in codes) else None


def _registry_actions(action_registry: object) -> tuple[object, ...]:
    actions = _field(action_registry, "actions", ())
    return tuple(actions) if isinstance(actions, (list, tuple)) else ()


def _usable_action_codes(
    phase: str,
    *,
    active_abilities: tuple[Any, ...],
    resources: Mapping[str, int],
) -> set[int]:
    codes: set[int] = set()
    for ability in active_abilities:
        usage_limit = _field(ability, "usage_limit")
        max_uses = _field(usage_limit, "max_uses") if usage_limit is not None else None
        uses_consumed = _field(ability, "uses_consumed", 0)
        if type(max_uses) is int and type(uses_consumed) is int and uses_consumed >= max_uses:
            continue
        resource = _field(ability, "resource")
        resource_id = _field(resource, "resource_id") if resource is not None else None
        cost = _field(resource, "cost_per_use") if resource is not None else None
        if (
            isinstance(resource_id, str)
            and type(cost) is int
            and resources.get(resource_id, 0) < cost
        ):
            continue
        phases = _field(ability, "allowed_phases", ())
        if isinstance(phases, (list, tuple)) and phase in {
            getattr(item, "value", item) for item in phases
        }:
            action_code = _field(ability, "action_code")
            if type(action_code) is int and action_code > 0:
                codes.add(action_code)
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
    ability_id = pending_resolution.get(
        "ability_instance_id",
        pending_resolution.get("ability_id", pending_resolution.get("skill_id")),
    )
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

    matches: list[Any] = []
    for ability in trigger_abilities:
        trigger = _field(ability, "trigger", {})
        event = _field(trigger, "event")
        if getattr(event, "value", event) != trigger_event:
            continue
        ids = {
            _field(ability, "ability_id"),
            _field(ability, "ability_instance_id"),
            _field(ability, "skill_id"),
        }
        if (
            _field(ability, "consumed", False)
            or _field(ability, "action_code") != action_code
            or ability_id not in ids
        ):
            continue
        matches.append(ability)
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
            ("ability_instance_id", ability_id),
            ("skill_id", ability_id),
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
