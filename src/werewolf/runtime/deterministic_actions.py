"""Deterministic, data-driven action choices for local player harnesses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .player_runtime import Action, ActionResponse, TurnRequest


def build_deterministic_action_response(
    request: TurnRequest,
    skill_status: Mapping[str, Any],
) -> ActionResponse:
    """Choose a stable action using only this request and the seat's status.

    The selector does not interpret role, skill, or action names. Skill target
    counts and parameter schemas come from ``get_skill_status``; target values
    come from the already-authorized request window and its visible context.
    The host still validates the returned response before committing it.
    """

    window = request.action_window
    if window is None:
        raise ValueError("action request did not include an action window")
    abilities = _mapping_items(skill_status.get("abilities"))
    known_ability_codes = {
        code
        for ability in abilities
        if (code := _positive_int(ability.get("action_code"))) is not None
    }
    by_code = {
        code: ability
        for ability in abilities
        if (code := _positive_int(ability.get("action_code"))) is not None
        and _ability_available(ability, skill_status)
    }
    actions = _mapping_items(skill_status.get("actions"))
    action_specs = {
        code: action
        for action in actions
        if (code := _positive_int(action.get("action_code"))) is not None
    }

    context = window.visible_context
    ordered_codes = _ordered_action_codes(
        window.allowed_action_codes,
        by_code,
        action_specs,
        window.allow_pass,
        unavailable_ability_codes=known_ability_codes - set(by_code),
    )
    action_count = max(1, window.min_actions)
    actions_out: list[Action] = []
    used_codes: set[int] = set()
    cursor = 0
    while len(actions_out) < action_count:
        if not ordered_codes:
            raise ValueError("no authorized action is available to the deterministic runtime")
        if cursor >= len(ordered_codes):
            if not window.allow_duplicate_action_codes:
                break
            cursor = 0
        selected_code = ordered_codes[cursor]
        cursor += 1
        if selected_code in used_codes and not window.allow_duplicate_action_codes:
            continue
        contract = _contract_for(selected_code, by_code, action_specs, context)
        is_pass = _action_is_pass(action_specs.get(selected_code, {})) or _action_is_pass(contract)
        if is_pass:
            targets = []
            parameters = {}
        else:
            candidates = _candidate_seats(request, context, selected_code)
            min_targets, max_targets, allow_self, selector = _target_contract(
                contract, action_code=selected_code
            )
            raw_skill_state = contract.get("state")
            targets = _select_targets(
                request,
                candidates,
                count=min_targets,
                allow_self=allow_self,
                selector=selector,
                context=context,
                skill_state=raw_skill_state if isinstance(raw_skill_state, Mapping) else {},
                history=_mapping_items(contract.get("history")),
            )
            if min_targets > len(targets):
                continue
            targets = targets[:max_targets]
            parameters = dict(_parameters_for(request, contract, selected_code))
        actions_out.append(
            Action(
                action_code=selected_code,
                targets=list(targets),
                parameters=parameters,
            )
        )
        used_codes.add(selected_code)
    if len(actions_out) < action_count:
        raise ValueError("action window requires more distinct authorized actions")
    return ActionResponse(request_id=request.request_id, actions=actions_out)


def _ordered_action_codes(
    allowed: Sequence[int],
    abilities: Mapping[int, Mapping[str, Any]],
    action_specs: Mapping[int, Mapping[str, Any]],
    allow_pass: bool,
    *,
    unavailable_ability_codes: set[int] | None = None,
) -> list[int]:
    unavailable = unavailable_ability_codes or set()
    executable = [
        code
        for code in allowed
        if code in abilities
        and not _action_is_pass(action_specs.get(code, {}))
        and not _action_is_pass(abilities[code])
    ]
    other = [
        code
        for code in allowed
        if code not in abilities
        and code not in unavailable
        and not _action_is_pass(action_specs.get(code, {}))
        and not _action_is_pass(abilities.get(code, {}))
    ]
    passing = [code for code in allowed if _pass_code((code,), abilities, action_specs, allow_pass)]
    return [*executable, *other, *passing]


def _pass_code(
    allowed: Sequence[int],
    abilities: Mapping[int, Mapping[str, Any]],
    action_specs: Mapping[int, Mapping[str, Any]],
    allow_pass: bool,
) -> int | None:
    if not allow_pass:
        return None
    return next(
        (
            code
            for code in allowed
            if _action_is_pass(action_specs.get(code, {}))
            or _action_is_pass(abilities.get(code, {}))
        ),
        None,
    )


def _action_is_pass(action: Mapping[str, Any]) -> bool:
    return (
        action.get("is_pass") is True
        or action.get("action_id") == "pass"
        or action.get("action_name") == "PASS"
    )


def _contract_for(
    action_code: int,
    abilities: Mapping[int, Mapping[str, Any]],
    actions: Mapping[int, Mapping[str, Any]],
    context: Mapping[str, Any],
) -> Mapping[str, Any]:
    skill = abilities.get(action_code)
    action = actions.get(action_code)
    if action is not None and _action_is_pass(action):
        return action
    if skill is not None:
        return skill
    if action is not None:
        return action
    contracts = context.get("action_contracts")
    if isinstance(contracts, Mapping):
        contract = contracts.get(str(action_code), contracts.get(action_code))
        if isinstance(contract, Mapping):
            return contract
    return {}


def _ability_available(ability: Mapping[str, Any], status: Mapping[str, Any]) -> bool:
    if any(ability.get(key) is False for key in ("enabled", "available", "usable")):
        return False
    if ability.get("consumed") is True:
        return False
    usage = ability.get("usage_limit")
    used = ability.get("uses_consumed")
    if isinstance(usage, Mapping):
        maximum = usage.get("max_uses")
        if type(used) is int and type(maximum) is int and used >= maximum:
            return False
    balances = status.get("resources")
    costs = ability.get("costs")
    if isinstance(costs, (list, tuple)) and isinstance(balances, Mapping):
        for cost in costs:
            if not isinstance(cost, Mapping):
                continue
            resource_id = cost.get("resource_id")
            amount = cost.get("amount")
            if (
                isinstance(resource_id, str)
                and type(amount) is int
                and balances.get(resource_id, 0) < amount
            ):
                return False
    resource = ability.get("resource")
    if isinstance(resource, Mapping) and isinstance(balances, Mapping):
        resource_id = resource.get("resource_id")
        cost = resource.get("cost_per_use")
        if (
            isinstance(resource_id, str)
            and type(cost) is int
            and balances.get(resource_id, 0) < cost
        ):
            return False
    return True


def _target_contract(
    ability: Mapping[str, Any], *, action_code: int
) -> tuple[int, int, bool, object | None]:
    target = ability.get("target_rule")
    if not isinstance(target, Mapping):
        target = ability.get("targets")
    if not isinstance(target, Mapping):
        target = {}
    kind = target.get("kind")
    minimum = _nonnegative_int(target.get("min_targets"))
    maximum = _nonnegative_int(target.get("max_targets"))
    if minimum is None:
        minimum = _nonnegative_int(target.get("min"))
    if maximum is None:
        maximum = _nonnegative_int(target.get("max"))
    count = _nonnegative_int(target.get("target_count"))
    if count is None:
        count = _nonnegative_int(ability.get("target_count"))
    if count is not None:
        minimum = count if minimum is None else minimum
        maximum = count if maximum is None else maximum
    if kind in {"NONE", "none"} or _action_is_pass(ability):
        minimum = maximum = 0
    if minimum is None and kind not in {"NONE", "none"}:
        minimum = 1
    minimum = 0 if minimum is None else minimum
    maximum = minimum if maximum is None else maximum
    if maximum < minimum:
        maximum = minimum
    selector = target.get("selector")
    allow_self = target.get("allow_self") is True if "allow_self" in target else not bool(target)
    return minimum, maximum, allow_self, selector


def _candidate_seats(
    request: TurnRequest,
    context: Mapping[str, Any],
    action_code: int,
) -> list[int]:
    window = request.action_window
    if window is None:
        return []
    action_targets = context.get("targets_by_action")
    raw: object = None
    explicit = False
    if isinstance(action_targets, Mapping):
        raw = action_targets.get(str(action_code), action_targets.get(action_code))
        explicit = raw is not None
    if not explicit:
        raw = context.get("candidate_seats")
    candidates = raw if isinstance(raw, (list, tuple)) else window.candidate_seats
    allowed = set(window.candidate_seats)
    result: list[int] = []
    for seat in candidates:
        if type(seat) is int and seat in allowed and seat not in result:
            result.append(seat)
    return result


def _select_targets(
    request: TurnRequest,
    candidates: Sequence[int],
    *,
    count: int,
    allow_self: bool,
    selector: object | None,
    context: Mapping[str, Any],
    skill_state: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> list[int]:
    if count == 0:
        return []
    visible: list[int] = list(candidates)
    selected = _select_from_contract(
        request,
        selector,
        visible,
        context,
        skill_state=skill_state,
        history=history,
    )
    if selected is not None:
        selected_set = set(selected)
        visible = [seat for seat in visible if seat in selected_set]
    self_seat = request.observation.payload.get("seat")
    if not allow_self and type(self_seat) is int:
        visible = [seat for seat in visible if seat != self_seat]
    preferred = _window_target_facts(request, visible)
    remaining = [seat for seat in visible if seat not in preferred]
    if remaining:
        raw_round = request.observation.payload.get("round_no", 0)
        round_no = raw_round if type(raw_round) is int and raw_round >= 0 else 0
        seat = self_seat if type(self_seat) is int else 0
        offset = (round_no + seat) % len(remaining)
        remaining = remaining[offset:] + remaining[:offset]
    ordered = preferred + remaining
    return ordered[:count]


def _select_from_contract(
    request: TurnRequest,
    selector: object | None,
    candidates: Sequence[int],
    context: Mapping[str, Any],
    *,
    skill_state: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> list[int] | None:
    """Evaluate a frozen selector over facts and public candidates in this request.

    Missing private player attributes stay unknown; the chooser never reads the
    host state or substitutes a package-name-specific rule.
    """

    if not isinstance(selector, Mapping) or selector.get("op") != "select":
        return None
    source = selector.get("source")
    rows: list[Mapping[str, Any]]
    if source == "facts":
        window_id = request.action_window.window_id if request.action_window is not None else None
        current_round = request.observation.payload.get("round_no")
        if current_round is None:
            current_round = request.observation.payload.get("night_round")
        rows = []
        for event in request.observation.events:
            event_window = event.payload.get("window_id")
            if event_window is not None and event_window != window_id:
                continue
            event_round = event.payload.get("round_number")
            if event_round is None:
                event_round = event.payload.get("night_round")
            if event_round is None:
                event_round = event.payload.get("round_no")
            if (
                type(current_round) is int
                and type(event_round) is int
                and current_round != event_round
            ):
                continue
            rows.append(_event_fact(event.event_type, event.payload))
    elif source == "players":
        raw_players = context.get("player_facts")
        if isinstance(raw_players, (list, tuple)):
            rows = [
                dict(item)
                for item in raw_players
                if isinstance(item, Mapping) and type(item.get("seat")) is int
            ]
        else:
            rows = [{"seat": seat, "alive": True} for seat in candidates]
    elif source == "ledger":
        rows = list(history)
    elif source == "request_targets":
        rows = []
    else:
        return None
    actor = request.observation.payload.get("seat")
    chosen: list[int] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        candidate_seat = item.get("seat", item.get("target_seat"))
        if type(candidate_seat) is not int:
            continue
        mapped = (
            _eval_rule_expr(
                selector.get("map"),
                actor=actor,
                item=item,
                skill_state=skill_state,
                observation=request.observation.payload,
            )
            if selector.get("map") is not None
            else candidate_seat
        )
        if type(mapped) is int and mapped not in candidates:
            continue
        if type(mapped) is int:
            candidate_seat = mapped
        if candidate_seat not in candidates:
            continue
        condition = selector.get("where")
        matches = (
            _eval_rule_expr(
                condition,
                actor=actor,
                item=item,
                skill_state=skill_state,
                observation=request.observation.payload,
            )
            if condition is not None
            else True
        )
        # A player cannot evaluate hidden target attributes. Keep that seat in
        # the public candidate union and let the authoritative host validate
        # the action; only an explicit public/private-to-this-seat false
        # condition narrows the deterministic choice.
        if matches is False:
            continue
        if candidate_seat not in chosen:
            chosen.append(candidate_seat)
    return chosen


def _event_fact(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(payload)
    row.setdefault("fact_type", event_type)
    return row


def _eval_rule_expr(
    expression: object,
    *,
    actor: object,
    item: Mapping[str, Any],
    skill_state: Mapping[str, Any] | None = None,
    observation: Mapping[str, Any] | None = None,
) -> Any:
    if not isinstance(expression, Mapping):
        return _MISSING
    op = expression.get("op")
    if op == "literal":
        return expression.get("value", _MISSING)
    if op == "ref":
        source, name = expression.get("source"), expression.get("name")
        if source == "actor" and name == "seat":
            return actor if type(actor) is int else _MISSING
        if source == "actor" and name == "alive":
            return True
        if source == "target" and name in {"seat", "alive"}:
            value = item.get("seat" if name == "seat" else "alive", _MISSING)
            return value
        if source == "item" and isinstance(name, str):
            return item.get(name, _MISSING)
        if source == "skill_state" and isinstance(name, str):
            state = skill_state or {}
            return state.get(name, _MISSING)
        if source == "observation" and isinstance(name, str):
            values = observation or {}
            aliases = {"round_number": "round_no"}
            return values.get(aliases.get(name, name), _MISSING)
        if source == "request" and name == "action_code":
            return item.get("action_code", _MISSING)
        return _MISSING
    if op in {"and", "or", "not"}:
        values = expression.get("values", ())
        if op == "not":
            result = (
                _eval_rule_expr(
                    values[0],
                    actor=actor,
                    item=item,
                    skill_state=skill_state,
                    observation=observation,
                )
                if values
                else _MISSING
            )
            return not result if type(result) is bool else _MISSING
        evaluated = [
            _eval_rule_expr(
                value,
                actor=actor,
                item=item,
                skill_state=skill_state,
                observation=observation,
            )
            for value in values
        ]
        if op == "and":
            if any(value is False for value in evaluated):
                return False
            return True if all(value is True for value in evaluated) else _MISSING
        if any(value is True for value in evaluated):
            return True
        return False if all(value is False for value in evaluated) else _MISSING
    if op in {"eq", "ne", "lt", "le", "gt", "ge", "in", "contains"}:
        left = _eval_rule_expr(
            expression.get("left"),
            actor=actor,
            item=item,
            skill_state=skill_state,
            observation=observation,
        )
        right = _eval_rule_expr(
            expression.get("right"),
            actor=actor,
            item=item,
            skill_state=skill_state,
            observation=observation,
        )
        if left is _MISSING or right is _MISSING:
            return _MISSING
        try:
            if op == "eq":
                return left == right
            if op == "ne":
                return left != right
            if op == "lt":
                return left < right
            if op == "le":
                return left <= right
            if op == "gt":
                return left > right
            if op == "ge":
                return left >= right
            if op == "in":
                return left in right
            if op == "contains":
                return right in left
        except TypeError:
            return _MISSING
    return _MISSING


def _window_target_facts(request: TurnRequest, candidates: Sequence[int]) -> list[int]:
    """Prioritize a target fact delivered for this exact physical window."""

    window = request.action_window
    if window is None:
        return []
    candidate_set = set(candidates)
    current_round = request.observation.payload.get("night_round")
    if current_round is None:
        current_round = request.observation.payload.get("round_no")
    result: list[int] = []
    for event in request.observation.events:
        event_window = event.payload.get("window_id")
        if event_window != window.window_id:
            continue
        event_round = event.payload.get("night_round")
        if event_round is None:
            event_round = event.payload.get("round_no")
        if type(current_round) is int and type(event_round) is int and current_round != event_round:
            continue
        target = event.payload.get("target_seat")
        if type(target) is int and target in candidate_set and target not in result:
            result.append(target)
    return result


def _parameters_for(
    request: TurnRequest,
    ability: Mapping[str, Any],
    action_code: int,
) -> dict[str, Any]:
    context = request.action_window.visible_context if request.action_window is not None else {}
    by_action = context.get("parameters_by_action")
    if isinstance(by_action, Mapping):
        supplied = by_action.get(str(action_code))
        if isinstance(supplied, Mapping):
            return dict(supplied)
    supplied = context.get("parameters")
    if isinstance(supplied, Mapping):
        return dict(supplied)
    schema = ability.get("parameters")
    if isinstance(schema, (list, tuple)):
        result: dict[str, Any] = {}
        for raw in schema:
            if not isinstance(raw, Mapping):
                continue
            name = raw.get("name")
            if not isinstance(name, str):
                continue
            choices = raw.get("choices")
            if isinstance(choices, (list, tuple)) and choices:
                result[name] = choices[0]
                continue
            kind = raw.get("value_type")
            if kind == "seat":
                actor = request.observation.payload.get("seat")
                if type(actor) is int:
                    result[name] = actor
            elif kind in {"nullable_seat", "nullable_str", "null"}:
                result[name] = None
            elif kind == "bool":
                result[name] = False
            elif kind == "int":
                result[name] = 0
            elif kind in {"str", "str_list"}:
                result[name] = "" if kind == "str" else []
            elif kind == "seat_list":
                result[name] = []
            elif kind == "json":
                result[name] = {}
            elif raw.get("required", True) is True:
                raise ValueError(f"required action parameter {name!r} has no deterministic value")
        return result
    if not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties", schema)
    if not isinstance(properties, Mapping):
        return {}
    required = schema.get("required", ())
    required_names = set(required) if isinstance(required, (list, tuple)) else set()
    result = {}
    for key, raw in properties.items():
        if not isinstance(key, str) or not isinstance(raw, Mapping):
            continue
        context = request.action_window.visible_context if request.action_window is not None else {}
        context_key = raw.get("context_key", raw.get("source"))
        if isinstance(context_key, str) and context_key in context:
            value = context[context_key]
        else:
            value = _deterministic_parameter_value(raw)
        if value is not _MISSING:
            result[key] = value
        elif key in required_names:
            raise ValueError(
                f"required action parameter {key!r} has no visible deterministic value"
            )
    return result


_MISSING = object()


def _deterministic_parameter_value(schema: Mapping[str, Any]) -> Any:
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        return enum[0]
    kind = schema.get("type")
    if kind == "boolean":
        return False
    if kind == "integer":
        minimum = schema.get("minimum", 0)
        return minimum if type(minimum) in {int, float} else 0
    if kind == "number":
        minimum = schema.get("minimum", 0)
        return minimum if type(minimum) in {int, float} else 0
    if kind == "string":
        minimum = _nonnegative_int(schema.get("minLength")) or 0
        return "x" * minimum
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return _MISSING


def _mapping_items(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


__all__ = ["build_deterministic_action_response"]
