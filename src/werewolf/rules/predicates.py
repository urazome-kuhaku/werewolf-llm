"""Typed evaluation for the bounded rule expression language."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from werewolf.rules.models import (
    BooleanExpr,
    CompareExpr,
    CountExpr,
    DomainFact,
    Expr,
    LiteralExpr,
    MapExpr,
    PlayerObservation,
    RefExpr,
    SelectorExpr,
    SkillSpec,
    StateDeclaration,
    ValueType,
)

_MAX_NODES = 512
_MAX_DEPTH = 32


def _member(item: object, name: str) -> object:
    """Read only a field from the finite rule-visible record vocabulary."""

    mapping: Mapping[str, object] | None = None
    if isinstance(item, Mapping):
        mapping = item
    elif isinstance(item, PlayerObservation):
        mapping = {
            "seat": item.seat,
            "alive": item.alive,
            "role_id": item.role_id,
            "faction_id": item.faction_id,
            "victory_group_id": item.victory_group_id,
            "chat_group_ids": item.chat_group_ids,
            "attributes": item.attributes,
            "resources": item.resources,
        }
    elif isinstance(item, DomainFact):
        mapping = {
            "fact_id": item.fact_id,
            "fact_type": item.fact_type,
            "source_rule_id": item.source_rule_id,
            "source_request_id": item.source_request_id,
            "actor_seat": item.actor_seat,
            "target_seat": item.target_seat,
            "tags": item.tags,
            "data": item.data,
        }
    else:
        mapping = getattr(item, "__dict__", None)
    if mapping is None:
        raise ValueError("expression reference is unavailable")
    if name.startswith("attribute:"):
        key = name.removeprefix("attribute:")
        attrs = mapping.get("attributes")
        if not isinstance(attrs, Mapping) or key not in attrs:
            return None
        return attrs[key]
    if name.startswith("resource:"):
        key = name.removeprefix("resource:")
        resources = mapping.get("resources")
        if not isinstance(resources, Mapping) or key not in resources:
            return None
        return resources[key]
    if name.startswith("parameter:"):
        key = name.removeprefix("parameter:")
        values = mapping.get("parameters")
        if not isinstance(values, Mapping) or key not in values:
            return None
        return values[key]
    if name.startswith("data:"):
        key = name.removeprefix("data:")
        values = mapping.get("data")
        if not isinstance(values, Mapping) or key not in values:
            return None
        return values[key]
    if name not in mapping:
        raise ValueError(f"unknown expression field: {name}")
    return mapping[name]


def _resolve_ref(expression: RefExpr, context: Mapping[str, object]) -> object:
    source = expression.source
    name = expression.name
    if source == "actor":
        allowed = {"seat", "alive", "role_id", "faction_id", "victory_group_id"}
        if name not in allowed and not name.startswith("resource:"):
            raise ValueError(f"actor reference is not allowed: {name}")
        actor = context.get("actor")
        return None if actor is None else _member(actor, name)
    if source == "target":
        allowed = {"seat", "alive", "role_id", "faction_id", "victory_group_id"}
        if name not in allowed and not name.startswith("resource:"):
            raise ValueError(f"target reference is not allowed: {name}")
        target = context.get("target")
        return None if target is None else _member(target, name)
    if source == "request":
        allowed = {"action_code", "passed", "target_count", "actor_seat"}
        if name not in allowed and not name.startswith("parameter:"):
            raise ValueError(f"request reference is not allowed: {name}")
        return _member(context.get("request"), name)
    if source == "observation":
        allowed = {"round_number", "group_id", "timing", "revision"}
        if name not in allowed:
            raise ValueError(f"observation reference is not allowed: {name}")
        return _member(context.get("observation"), name)
    if source == "skill_state":
        state = context.get("skill_state")
        if not isinstance(state, Mapping) or name not in state:
            return None
        return state[name]
    if source == "item":
        allowed = {
            "seat",
            "alive",
            "role_id",
            "faction_id",
            "victory_group_id",
            "actor_seat",
            "target_seat",
            "fact_type",
            "tags",
            "round_number",
            "skill_id",
            "action_code",
            "passed",
            "successful",
            "effect_type",
            "death_cause",
            "deceased",
            "cause_effect_ids",
        }
        if name not in allowed:
            raise ValueError(f"item reference is not allowed: {name}")
        return _member(context.get("item"), name)
    raise ValueError(f"unknown expression source: {source}")


def resolve_ref(expression: RefExpr, context: Mapping[str, object]) -> object:
    """Resolve an explicitly named reference from an evaluation context."""

    return _resolve_ref(expression, context)


def _compatible(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        return isinstance(right, (int, float)) and not isinstance(right, bool)
    return isinstance(left, str) and isinstance(right, str)


def _strict_eq(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if type(left) is not type(right):
        return False
    return left == right


def _evaluate_comparison(operation: str, left: object, right: object) -> bool:
    if operation == "eq":
        return _strict_eq(left, right)
    if operation == "ne":
        return not _strict_eq(left, right)
    if operation in {"lt", "le", "gt", "ge"}:
        if left is None or right is None or not _compatible(left, right):
            raise ValueError("ordered comparison requires compatible non-null values")
        if operation == "lt":
            return bool(left < right)  # type: ignore[operator]
        if operation == "le":
            return bool(left <= right)  # type: ignore[operator]
        if operation == "gt":
            return bool(left > right)  # type: ignore[operator]
        return bool(left >= right)  # type: ignore[operator]
    if operation == "in":
        if right is None or not isinstance(right, (str, tuple, list, set, frozenset, dict)):
            raise ValueError("in comparison requires a finite collection")
        if isinstance(right, (tuple, list, set, frozenset)):
            return any(_strict_eq(left, item) for item in right)
        return left in right
    if operation == "contains":
        if left is None or not isinstance(left, (str, tuple, list, set, frozenset, dict)):
            raise ValueError("contains comparison requires a finite collection")
        if isinstance(left, (tuple, list, set, frozenset)):
            return any(_strict_eq(right, item) for item in left)
        return right in left
    raise ValueError(f"unknown comparison operator: {operation}")


def evaluate_expr(
    expression: Expr,
    context: Mapping[str, object],
    *,
    _budget: list[int] | None = None,
    _depth: int = 0,
) -> object:
    """Evaluate an expression with a strict node/depth budget."""

    budget = [0] if _budget is None else _budget

    def visit(expr: Expr, current: Mapping[str, object], depth: int) -> object:
        budget[0] += 1
        if budget[0] > _MAX_NODES or depth > _MAX_DEPTH:
            raise ValueError("expression exceeds the execution budget")
        if isinstance(expr, LiteralExpr):
            return expr.value
        if isinstance(expr, RefExpr):
            return _resolve_ref(expr, current)
        if isinstance(expr, CompareExpr):
            left = visit(expr.left, current, depth + 1)
            right = visit(expr.right, current, depth + 1)
            return _evaluate_comparison(expr.op, left, right)
        if isinstance(expr, BooleanExpr):
            if expr.op == "not":
                if len(expr.values) != 1:
                    raise ValueError("not requires exactly one expression")
                return not bool(visit(expr.values[0], current, depth + 1))
            if not expr.values:
                raise ValueError("and/or requires at least one expression")
            if expr.op == "and":
                return all(bool(visit(value, current, depth + 1)) for value in expr.values)
            return any(bool(visit(value, current, depth + 1)) for value in expr.values)
        if isinstance(expr, CountExpr):
            from werewolf.rules.selectors import select_items

            return len(select_items(expr.selector, current, _budget=budget, _depth=depth + 1))
        if isinstance(expr, MapExpr):
            from werewolf.rules.selectors import select_items

            mapped: list[object] = []
            for item in select_items(expr.selector, current, _budget=budget, _depth=depth + 1):
                nested = dict(current)
                nested["item"] = item
                mapped.append(visit(expr.value, nested, depth + 1))
            return tuple(mapped)
        raise ValueError("unsupported expression node")

    return visit(expression, context, _depth)


def evaluate_predicate(
    expression: Expr | None,
    context: Mapping[str, object],
    *,
    _budget: list[int] | None = None,
    _depth: int = 0,
) -> bool:
    if expression is None:
        return True
    value = evaluate_expr(expression, context, _budget=_budget, _depth=_depth)
    if not isinstance(value, bool):
        raise ValueError("predicate expression must evaluate to bool")
    return value


def _literal_type(value: object) -> ValueType:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    if isinstance(value, (tuple, list)) and value:
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            return "seat_list"
        if all(isinstance(item, str) for item in value):
            return "str_list"
    return "json"


def _literal_matches_type(value: object, value_type: ValueType) -> bool:
    if value_type == "json":
        return True
    if value_type == "null":
        return value is None
    if value is None:
        return value_type in {"nullable_seat", "nullable_str"}
    if value_type == "bool":
        return isinstance(value, bool)
    if value_type in {"int", "seat", "nullable_seat"}:
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        return value_type != "seat" or 1 <= value <= 64
    if value_type in {"str", "nullable_str"}:
        return isinstance(value, str)
    if value_type == "seat_list":
        return isinstance(value, (tuple, list)) and all(
            isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= 64
            for item in value
        )
    if value_type == "str_list":
        return isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value)
    return False


def _assignable(actual: ValueType, expected: ValueType) -> bool:
    if expected == "json" or actual == "json":
        return True
    if expected == actual:
        return True
    if expected == "nullable_seat" and actual in {"seat", "null"}:
        return True
    if expected == "nullable_str" and actual in {"str", "null"}:
        return True
    if expected == "seat" and actual == "int":
        return True
    if expected == "int" and actual == "seat":
        return True
    return False


def infer_expr_type(
    expression: Expr,
    *,
    skill: SkillSpec,
    state_declarations: Sequence[StateDeclaration],
    item_source: str | None = None,
) -> ValueType:
    """Type-check expression references against the frozen package schema."""

    if isinstance(expression, LiteralExpr):
        inferred = expression.value_type or _literal_type(expression.value)
        if not _literal_matches_type(expression.value, inferred):
            raise ValueError("literal does not match its declared value_type")
        return inferred
    if isinstance(expression, RefExpr):
        field = expression.name
        source = expression.source
        known: dict[tuple[str, str], ValueType] = {
            ("actor", "seat"): "seat",
            ("actor", "alive"): "bool",
            ("actor", "role_id"): "nullable_str",
            ("actor", "faction_id"): "nullable_str",
            ("actor", "victory_group_id"): "nullable_str",
            ("target", "seat"): "seat",
            ("target", "alive"): "bool",
            ("target", "role_id"): "nullable_str",
            ("target", "faction_id"): "nullable_str",
            ("target", "victory_group_id"): "nullable_str",
            ("request", "action_code"): "int",
            ("request", "passed"): "bool",
            ("request", "target_count"): "int",
            ("request", "actor_seat"): "seat",
            ("observation", "round_number"): "int",
            ("observation", "group_id"): "str",
            ("observation", "timing"): "str",
            ("observation", "revision"): "int",
            ("item", "seat"): "seat",
            ("item", "alive"): "bool",
            ("item", "role_id"): "nullable_str",
            ("item", "faction_id"): "nullable_str",
            ("item", "victory_group_id"): "nullable_str",
            ("item", "actor_seat"): "seat",
            ("item", "target_seat"): "nullable_seat",
            ("item", "fact_type"): "str",
            ("item", "tags"): "str_list",
            ("item", "round_number"): "int",
            ("item", "skill_id"): "str",
            ("item", "action_code"): "int",
            ("item", "passed"): "bool",
            ("item", "successful"): "bool",
            ("item", "effect_type"): "str",
            ("item", "death_cause"): "nullable_str",
            ("item", "deceased"): "bool",
            ("item", "cause_effect_ids"): "str_list",
        }
        if source == "skill_state":
            state_declaration = next(
                (
                    item
                    for item in state_declarations
                    if item.skill_id == skill.skill_id and item.key == field
                ),
                None,
            )
            if state_declaration is None:
                raise ValueError(f"undeclared skill state reference: {skill.skill_id}.{field}")
            return state_declaration.value_type
        if source == "item":
            item_fields: dict[str, dict[str, ValueType]] = {
                "players": {
                    "seat": "seat",
                    "alive": "bool",
                    "role_id": "nullable_str",
                    "faction_id": "nullable_str",
                    "victory_group_id": "nullable_str",
                    "chat_group_ids": "str_list",
                },
                "request_targets": {
                    "seat": "seat",
                    "alive": "bool",
                    "role_id": "nullable_str",
                    "faction_id": "nullable_str",
                    "victory_group_id": "nullable_str",
                    "chat_group_ids": "str_list",
                },
                "ledger": {
                    "actor_seat": "seat",
                    "round_number": "int",
                    "skill_id": "str",
                    "action_code": "int",
                    "passed": "bool",
                    "successful": "bool",
                },
                "facts": {
                    "fact_type": "str",
                    "actor_seat": "nullable_seat",
                    "target_seat": "nullable_seat",
                    "tags": "str_list",
                },
                "effect_intent": {
                    "actor_seat": "seat",
                    "target_seat": "nullable_seat",
                    "skill_id": "str",
                    "effect_type": "str",
                    "tags": "str_list",
                },
                "post_death": {
                    "seat": "seat",
                    "alive": "bool",
                    "death_cause": "nullable_str",
                    "tags": "str_list",
                    "deceased": "bool",
                },
                "disclosure": {
                    "target_seat": "seat",
                    "effect_type": "str",
                    "death_cause": "nullable_str",
                    "tags": "str_list",
                    "deceased": "bool",
                },
            }
            if item_source is None:
                raise ValueError(
                    "unknown or untyped reference: item field is unavailable outside "
                    "a selector or interaction"
                )
            try:
                return item_fields[item_source][field]
            except KeyError as exc:
                raise ValueError(
                    f"unknown or untyped reference: item.{field} is not available in {item_source}"
                ) from exc
        if field.startswith("parameter:") and source == "request":
            parameter = next((item for item in skill.parameters if item.name == field[10:]), None)
            if parameter is None:
                raise ValueError(f"undeclared request parameter: {field[10:]}")
            return parameter.value_type
        if field.startswith("resource:") and source in {"actor", "target"}:
            resource_id = field.removeprefix("resource:")
            if resource_id not in {item.resource_id for item in skill.usage.costs}:
                raise ValueError(f"undeclared resource reference: {resource_id}")
            return "int"
        try:
            return known[(source, field)]
        except KeyError as exc:
            raise ValueError(f"unknown or untyped reference: {source}.{field}") from exc
    if isinstance(expression, BooleanExpr):
        if expression.op == "not" and len(expression.values) != 1:
            raise ValueError("not requires exactly one expression")
        if expression.op in {"and", "or"} and not expression.values:
            raise ValueError("and/or requires at least one expression")
        if any(
            infer_expr_type(
                value,
                skill=skill,
                state_declarations=state_declarations,
                item_source=item_source,
            )
            != "bool"
            for value in expression.values
        ):
            raise ValueError("boolean operators require bool operands")
        return "bool"
    if isinstance(expression, CompareExpr):
        left = infer_expr_type(
            expression.left,
            skill=skill,
            state_declarations=state_declarations,
            item_source=item_source,
        )
        right = infer_expr_type(
            expression.right,
            skill=skill,
            state_declarations=state_declarations,
            item_source=item_source,
        )
        if expression.op in {"eq", "ne"}:
            aliases = {
                frozenset(("seat", "int")),
                frozenset(("nullable_seat", "seat")),
                frozenset(("nullable_str", "str")),
            }
            if (
                left != "json"
                and right != "json"
                and left != right
                and "null" not in {left, right}
                and frozenset((left, right)) not in aliases
            ):
                raise ValueError("equality comparison requires compatible types")
            return "bool"
        if expression.op in {"lt", "le", "gt", "ge"}:
            if left not in {"int", "str", "seat"} or right != left:
                raise ValueError("ordered comparisons require matching non-null scalar types")
            return "bool"
        if expression.op == "in":
            if right not in {"seat_list", "str_list", "json"}:
                raise ValueError("in requires a finite collection on the right")
            if right == "seat_list" and left not in {"seat", "int", "json"}:
                raise ValueError("in requires a seat value for a seat collection")
            if right == "str_list" and left not in {"str", "json"}:
                raise ValueError("in requires a string value for a string collection")
            return "bool"
        if left not in {"seat_list", "str_list", "json"}:
            raise ValueError("contains requires a finite collection on the left")
        if left == "seat_list" and right not in {"seat", "int", "json"}:
            raise ValueError("contains requires a seat value for a seat collection")
        if left == "str_list" and right not in {"str", "json"}:
            raise ValueError("contains requires a string value for a string collection")
        return "bool"
    if isinstance(expression, CountExpr):
        _validate_selector(expression.selector, skill, state_declarations)
        return "int"
    if isinstance(expression, MapExpr):
        _validate_selector(expression.selector, skill, state_declarations)
        map_item_source = expression.selector.source if expression.selector.map is None else None
        item_type = infer_expr_type(
            expression.value,
            skill=skill,
            state_declarations=state_declarations,
            item_source=map_item_source,
        )
        if item_type == "seat":
            return "seat_list"
        if item_type == "str":
            return "str_list"
        return "json"
    raise ValueError("unsupported expression type")


def _validate_selector(
    selector: SelectorExpr,
    skill: SkillSpec,
    state_declarations: Sequence[StateDeclaration],
) -> None:
    if selector.where is not None:
        value_type = infer_expr_type(
            selector.where,
            skill=skill,
            state_declarations=state_declarations,
            item_source=selector.source,
        )
        if value_type != "bool":
            raise ValueError("selector where expression must be bool")
    if selector.map is not None:
        infer_expr_type(
            selector.map,
            skill=skill,
            state_declarations=state_declarations,
            item_source=selector.source,
        )


def validate_package_expressions(package: object) -> None:
    """Validate every declared reference and expression type in a package."""

    from werewolf.rules.models import ExecutionPackage

    if not isinstance(package, ExecutionPackage):
        raise TypeError("package must be an ExecutionPackage")
    state_keys = [(item.skill_id, item.key) for item in package.state_declarations]
    if len(state_keys) != len(set(state_keys)):
        raise ValueError("skill state declarations must be unique")
    action_codes = [item.action_code for item in package.actions]
    if len(action_codes) != len(set(action_codes)):
        raise ValueError("action codes must be unique in an execution package")
    skill_ids = [item.skill_id for item in package.skills]
    if len(skill_ids) != len(set(skill_ids)):
        raise ValueError("skill ids must be unique in an execution package")
    if len({item.interaction_id for item in package.interactions}) != len(package.interactions):
        raise ValueError("interaction ids must be unique")
    for declaration in package.state_declarations:
        if not _literal_matches_type(declaration.initial, declaration.value_type):
            raise ValueError(
                f"initial value for {declaration.skill_id}.{declaration.key} has the wrong type"
            )
    for skill in package.skills:
        if skill.action_code not in action_codes:
            raise ValueError(f"skill {skill.skill_id} references an undeclared action code")
        if skill.mode == "PLAYER" and not skill.grants:
            raise ValueError(f"player skill {skill.skill_id} requires at least one declared grant")
        if skill.mode == "HOST" and skill.grants:
            raise ValueError(f"host skill {skill.skill_id} cannot grant player abilities")
        if skill.targets.min_targets > skill.targets.max_targets:
            raise ValueError(f"skill {skill.skill_id} has invalid target bounds")
        if skill.targets.max_targets > 16:
            raise ValueError("target bound exceeds the rule-language limit")
        _validate_selector(skill.targets.selector, skill, package.state_declarations)
        if skill.condition is not None:
            if (
                infer_expr_type(
                    skill.condition,
                    skill=skill,
                    state_declarations=package.state_declarations,
                )
                != "bool"
            ):
                raise ValueError(f"skill {skill.skill_id} condition must be bool")
        parameter_names = [item.name for item in skill.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError(f"skill {skill.skill_id} has duplicate parameter names")
        for effect in skill.effects:
            if (
                effect.condition is not None
                and infer_expr_type(
                    effect.condition, skill=skill, state_declarations=package.state_declarations
                )
                != "bool"
            ):
                raise ValueError(f"effect {effect.effect_id} condition must be bool")
            target_type = None
            if effect.target is not None:
                target_type = infer_expr_type(
                    effect.target, skill=skill, state_declarations=package.state_declarations
                )
                if target_type not in {"seat", "nullable_seat", "json"}:
                    raise ValueError(f"effect {effect.effect_id} target must be a seat reference")
            value_type = None
            if effect.value is not None:
                value_type = infer_expr_type(
                    effect.value, skill=skill, state_declarations=package.state_declarations
                )
            if effect.effect_type == "CONSUME_ABILITY":
                if effect.target is None or target_type != "seat":
                    raise ValueError(f"effect {effect.effect_id} target must be a seat")
                if effect.value is None:
                    raise ValueError(f"effect {effect.effect_id} requires an ability ID value")
                if value_type != "str":
                    raise ValueError(f"effect {effect.effect_id} ability ID value must be str")
            if effect.effect_type == "STATE_SET":
                if effect.state_key is None:
                    raise ValueError(f"effect {effect.effect_id} requires state_key")
                state_declaration = next(
                    (
                        item
                        for item in package.state_declarations
                        if item.skill_id == skill.skill_id and item.key == effect.state_key
                    ),
                    None,
                )
                if state_declaration is None:
                    raise ValueError(f"effect {effect.effect_id} writes undeclared state")
                value_expression = effect.value
                if value_expression is None:
                    raise ValueError(f"effect {effect.effect_id} requires a value")
                actual_type = infer_expr_type(
                    value_expression,
                    skill=skill,
                    state_declarations=package.state_declarations,
                )
                if not _assignable(actual_type, state_declaration.value_type):
                    raise ValueError(
                        f"effect {effect.effect_id} value does not match declared state type"
                    )
            if effect.effect_type == "FACT" and effect.fact_type is None:
                raise ValueError(f"effect {effect.effect_id} requires fact_type")
            if effect.effect_type == "DAMAGE" and effect.target is None:
                raise ValueError(f"damage effect {effect.effect_id} requires a target")
            vote_value = effect.value
            if effect.effect_type == "SET_CAN_VOTE" and vote_value is None:
                raise ValueError(f"effect {effect.effect_id} requires a boolean value")
            if (
                effect.effect_type == "SET_CAN_VOTE"
                and vote_value is not None
                and infer_expr_type(
                    vote_value,
                    skill=skill,
                    state_declarations=package.state_declarations,
                )
                != "bool"
            ):
                raise ValueError(f"effect {effect.effect_id} vote value must be bool")
            for target in effect.authorized_targets:
                if infer_expr_type(
                    target, skill=skill, state_declarations=package.state_declarations
                ) not in {"seat", "json"}:
                    raise ValueError(f"effect {effect.effect_id} has invalid authorized target")
        for disclosure in skill.disclosures:
            if (
                disclosure.condition is not None
                and infer_expr_type(
                    disclosure.condition, skill=skill, state_declarations=package.state_declarations
                )
                != "bool"
            ):
                raise ValueError(f"disclosure {disclosure.disclosure_id} condition must be bool")
            if disclosure.audience == "SEATS" and disclosure.recipients is None:
                raise ValueError(
                    f"disclosure {disclosure.disclosure_id} requires recipient selector"
                )
            if disclosure.recipients is not None:
                _validate_selector(disclosure.recipients, skill, package.state_declarations)
            for value in disclosure.values.values():
                infer_expr_type(value, skill=skill, state_declarations=package.state_declarations)
        for effect in skill.pass_effects:
            if effect.effect_type not in {"STATE_SET", "FACT"}:
                raise ValueError("PASS effects may only update declared state or emit facts")
            if (
                effect.condition is not None
                and infer_expr_type(
                    effect.condition, skill=skill, state_declarations=package.state_declarations
                )
                != "bool"
            ):
                raise ValueError(f"PASS effect {effect.effect_id} condition must be bool")
            if effect.effect_type == "STATE_SET":
                if effect.state_key is None or effect.value is None:
                    raise ValueError("PASS state update requires a declared key and value")
                state_declaration = next(
                    (
                        item
                        for item in package.state_declarations
                        if item.skill_id == skill.skill_id and item.key == effect.state_key
                    ),
                    None,
                )
                if state_declaration is None:
                    raise ValueError("PASS state update writes undeclared state")
                value_expression = effect.value
                if value_expression is None:
                    raise ValueError("PASS state update requires a declared key and value")
                actual_type = infer_expr_type(
                    value_expression,
                    skill=skill,
                    state_declarations=package.state_declarations,
                )
                if not _assignable(actual_type, state_declaration.value_type):
                    raise ValueError("PASS state update value does not match declared state type")
            if effect.effect_type == "FACT" and effect.fact_type is None:
                raise ValueError("PASS fact effect requires fact_type")
    for interaction in package.interactions:
        if interaction.when is not None:
            # Interactions use the same finite expression language.  A synthetic
            # package-wide context is represented by the first skill when present.
            item_context = (
                "post_death" if interaction.rule_type == "EMIT_POST_DEATH_FACT" else "effect_intent"
            )
            if (
                package.skills
                and infer_expr_type(
                    interaction.when,
                    skill=package.skills[0],
                    state_declarations=package.state_declarations,
                    item_source=item_context,
                )
                != "bool"
            ):
                raise ValueError(f"interaction {interaction.interaction_id} condition must be bool")
        if package.skills:
            skill = package.skills[0]
            for effect in interaction.effects:
                if (
                    effect.condition is not None
                    and infer_expr_type(
                        effect.condition,
                        skill=skill,
                        state_declarations=package.state_declarations,
                        item_source="effect_intent",
                    )
                    != "bool"
                ):
                    raise ValueError(
                        f"interaction effect {effect.effect_id} condition must be bool"
                    )
                target_type = None
                if effect.target is not None:
                    target_type = infer_expr_type(
                        effect.target,
                        skill=skill,
                        state_declarations=package.state_declarations,
                        item_source="effect_intent",
                    )
                value_type = None
                if effect.value is not None:
                    value_type = infer_expr_type(
                        effect.value,
                        skill=skill,
                        state_declarations=package.state_declarations,
                        item_source="effect_intent",
                    )
                if effect.effect_type == "CONSUME_ABILITY":
                    if effect.target is None or target_type != "seat":
                        raise ValueError(f"effect {effect.effect_id} target must be a seat")
                    if effect.value is None:
                        raise ValueError(f"effect {effect.effect_id} requires an ability ID value")
                    if value_type != "str":
                        raise ValueError(f"effect {effect.effect_id} ability ID value must be str")
            for disclosure in interaction.disclosures:
                if (
                    disclosure.condition is not None
                    and infer_expr_type(
                        disclosure.condition,
                        skill=skill,
                        state_declarations=package.state_declarations,
                        item_source="disclosure",
                    )
                    != "bool"
                ):
                    raise ValueError(
                        f"interaction disclosure {disclosure.disclosure_id} condition must be bool"
                    )
                for value in disclosure.values.values():
                    infer_expr_type(
                        value,
                        skill=skill,
                        state_declarations=package.state_declarations,
                        item_source="disclosure",
                    )
    if any(
        effect.effect_type == "DAMAGE"
        for skill in package.skills
        for effect in (*skill.effects, *skill.pass_effects)
    ) and not any(rule.rule_type == "CONFIRM_DEATH" for rule in package.interactions):
        raise ValueError("packages with damage must declare at least one CONFIRM_DEATH interaction")


validate_package = validate_package_expressions


__all__ = [
    "evaluate_expr",
    "evaluate_predicate",
    "infer_expr_type",
    "resolve_ref",
    "validate_package",
    "validate_package_expressions",
]
