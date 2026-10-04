"""Finite, deterministic selection over observation records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from werewolf.rules.models import DomainFact, PlayerObservation, RuleObservation, SelectorExpr
from werewolf.rules.predicates import evaluate_expr, evaluate_predicate

_MAX_PLAYERS = 64
_MAX_GROUP_TARGETS = 16
_MAX_HISTORY_ITEMS = 10_000


def _sort_key(item: object) -> tuple[str, str]:
    if isinstance(item, PlayerObservation):
        return ("player", f"{item.seat:03d}")
    if isinstance(item, DomainFact):
        return ("fact", item.fact_id)
    mapping = getattr(item, "__dict__", None)
    if isinstance(mapping, dict):
        identity = mapping.get("record_id", mapping.get("request_id", mapping.get("fact_id", "")))
        return (type(item).__name__, str(identity))
    return (type(item).__name__, repr(item))


def _source_items(selector: SelectorExpr, context: Mapping[str, object]) -> tuple[object, ...]:
    observation = context.get("observation")
    if not isinstance(observation, RuleObservation):
        raise ValueError("selector requires a RuleObservation")
    if selector.source == "players":
        result: Sequence[object] = observation.players
    elif selector.source == "request_targets":
        result = context.get("request_targets", ())  # type: ignore[assignment]
    elif selector.source == "ledger":
        result = observation.ledger
    elif selector.source == "facts":
        result = observation.facts
    else:  # pragma: no cover - constrained by the model
        raise ValueError("unsupported selector source")
    items = tuple(sorted(result, key=_sort_key))
    limit = {
        "players": _MAX_PLAYERS,
        "request_targets": _MAX_GROUP_TARGETS,
        "ledger": _MAX_HISTORY_ITEMS,
        "facts": _MAX_HISTORY_ITEMS,
    }[selector.source]
    if len(items) > limit:
        raise ValueError("selector input exceeds its finite execution budget")
    return items


def select_items(
    selector: SelectorExpr,
    context: Mapping[str, object],
    *,
    _budget: list[int] | None = None,
    _depth: int = 0,
) -> tuple[object, ...]:
    """Return selected items, optionally mapped, in stable source order."""

    budget = [0] if _budget is None else _budget
    selected: list[object] = []
    for item in _source_items(selector, context):
        nested = dict(context)
        nested["item"] = item
        if not evaluate_predicate(selector.where, nested, _budget=budget, _depth=_depth):
            continue
        if selector.map is None:
            selected.append(item)
        else:
            selected.append(evaluate_expr(selector.map, nested, _budget=budget, _depth=_depth))
    if selector.distinct:
        unique: list[object] = []
        seen: set[tuple[type[object], object]] = set()
        for item in selected:
            try:
                marker = (type(item), item)
                hash(marker)
            except TypeError:
                marker = (type(item), repr(item))
            if marker not in seen:
                seen.add(marker)
                unique.append(item)
        selected = unique
    if len(selected) > _MAX_HISTORY_ITEMS:
        raise ValueError("selector output exceeds its finite execution budget")
    return tuple(selected)


def select_seats(selector: SelectorExpr, context: Mapping[str, object]) -> tuple[int, ...]:
    """Select player seats using only the bounded selector language."""

    values = select_items(selector, context)
    seats: list[int] = []
    for value in values:
        if isinstance(value, PlayerObservation):
            seats.append(value.seat)
        elif isinstance(value, int) and not isinstance(value, bool):
            seats.append(value)
        else:
            raise ValueError("seat selector must resolve to player records or seat numbers")
    if len(seats) != len(set(seats)):
        raise ValueError("seat selector produced duplicate seats")
    return tuple(seats)


__all__ = ["select_items", "select_seats"]
