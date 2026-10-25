"""Pure first-day sheriff participation projections.

The first-day sheriff election is a special public boundary: it happens
before the moderator announces the deaths from the first night.  A seat that
was killed by that night may therefore still participate in the election,
but only when the death can be proved by the frozen night resolution.  This
module deliberately uses structural access instead of importing game models
so knowledge projections can apply the same masking rule without creating an
import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SHERIFF_PHASES = frozenset(
    {
        "DAY_ANNOUNCE",
        "SHERIFF_ELECTION_SPEECH",
        "SHERIFF_ELECTION",
        "SHERIFF_ELECTION_PK_SPEECH",
        "SHERIFF_ELECTION_PK",
        "SHERIFF_TRANSFER",
    }
)


def _value(value: object) -> object:
    return getattr(value, "value", value)


def _mapping_value(raw: object, key: str) -> object:
    if isinstance(raw, Mapping):
        return raw.get(key)
    return getattr(raw, key, None)


def is_first_day_sheriff_boundary(state: Any) -> bool:
    """Return whether ``state`` is still inside the pre-announcement election."""

    if (
        getattr(state, "day_no", None) != 1
        or getattr(state, "round_no", None) != 0
        or _value(getattr(state, "phase", None)) not in _SHERIFF_PHASES
    ):
        return False
    return not has_first_day_announcement(state)


def has_first_day_announcement(state: Any) -> bool:
    """Return whether the current first-day death announcement was committed."""

    expected_correlation = (
        f"day-announce-r{getattr(state, 'round_no', -1)}-d{getattr(state, 'day_no', -1)}"
    )
    events = getattr(state, "events", ())
    for event in events:
        if _value(_mapping_value(event, "event_type")) != "announcement":
            continue
        correlation = _mapping_value(event, "correlation_id")
        if correlation == expected_correlation:
            return True
        # Legacy snapshots may not retain the correlation ID.  At day 1 an
        # announcement event for the current round is the same boundary.
        if (
            correlation is None
            and _mapping_value(event, "round_no") == 0
            and _value(_mapping_value(event, "phase")) == "DAY_ANNOUNCE"
        ):
            return True
    return False


def _schedule_requires_first_day_election(execution_package: object | None) -> bool:
    schedule = _mapping_value(execution_package, "night_boundary_schedule")
    if schedule is None:
        return False
    prerequisites = _mapping_value(schedule, "prerequisites")
    return isinstance(prerequisites, (list, tuple)) and (
        "FIRST_DAY_SHERIFF_ELECTION" in prerequisites
    )


def _rows(raw: object) -> tuple[object, ...]:
    return tuple(raw) if isinstance(raw, (tuple, list)) else ()


def _verified_rule_receipt(state: Any, ledger: object, package_id: str) -> bool:
    batch_id = _mapping_value(ledger, "batch_id")
    receipts = tuple(
        receipt
        for receipt in _rows(getattr(state, "rule_receipts", ()))
        if _mapping_value(receipt, "batch_id") == batch_id
    )
    if len(receipts) != 1:
        return False
    receipt = receipts[0]
    read_revision = _mapping_value(ledger, "read_revision")
    committed_revision = _mapping_value(ledger, "committed_revision")
    fields = (
        "package_id",
        "group_id",
        "timing",
        "read_revision",
        "committed_revision",
        "request_ids",
        "outcome_digest",
    )
    return (
        _mapping_value(ledger, "package_id") == package_id
        and all(
            _mapping_value(receipt, field) == _mapping_value(ledger, field)
            for field in fields
        )
        and type(read_revision) is int
        and type(committed_revision) is int
        and type(getattr(state, "state_revision", None)) is int
        and committed_revision == read_revision + 1
        and committed_revision <= state.state_revision
    )


def _night_group_has_terminal_topology(
    state: Any,
    execution_package: object,
    *,
    group_id: str,
    point: object,
    metadata: tuple[tuple[str, str, int], ...],
) -> bool:
    """Prove the source group is closed and includes the frozen final night window."""

    settlement_groups = _mapping_value(execution_package, "window_settlement_groups")
    if not isinstance(settlement_groups, Mapping):
        return False

    def frozen_group_id(window_id: str) -> str:
        if settlement_groups:
            return f"night:0:{settlement_groups.get(window_id)}"
        return "night:0"

    expected_rows = tuple(
        (window_id, phase, order)
        for window_id, phase, order in metadata
        if frozen_group_id(window_id) == group_id
    )
    if not expected_rows or frozen_group_id(metadata[-1][0]) != group_id:
        return False

    windows = _group_windows(state, group_id)
    if not windows or any(_mapping_value(row, "closed_at") is None for _, row in windows):
        return False
    by_logical_id: dict[str, list[tuple[str, object]]] = {}
    for physical_id, row in windows:
        logical_id = _mapping_value(row, "logical_window_id")
        if not isinstance(logical_id, str):
            return False
        by_logical_id.setdefault(logical_id, []).append((physical_id, row))
    if set(by_logical_id) != {window_id for window_id, _phase, _order in expected_rows}:
        return False
    if any(len(rows) != 1 for rows in by_logical_id.values()):
        return False

    metadata_by_id = {
        window_id: (index, phase, order)
        for index, (window_id, phase, order) in enumerate(metadata)
    }
    for window_id, phase, _order in expected_rows:
        index, _metadata_phase, _metadata_order = metadata_by_id[window_id]
        physical_id, row = by_logical_id[window_id][0]
        expected_next = metadata[index + 1][0] if index + 1 < len(metadata) else None
        if (
            physical_id != _mapping_value(row, "window_id")
            or _value(_mapping_value(row, "phase")) != phase
            or _mapping_value(row, "next_window_id") != expected_next
        ):
            return False

    # The boundary may retain the group anchor as its physical source.  Bind
    # that anchor to the proven chain, while the chain itself establishes that
    # the final frozen night window has closed.
    point_window_id = _mapping_value(point, "window_id")
    if not isinstance(point_window_id, str):
        return False
    point_match = next(
        ((physical_id, row) for physical_id, row in windows if physical_id == point_window_id),
        None,
    )
    if point_match is None:
        return False
    _physical_id, point_window = point_match
    point_logical_id = _mapping_value(point, "logical_window_id")
    return point_logical_id is None or point_logical_id == _mapping_value(
        point_window, "next_window_id"
    )


def _metadata_with_phases(
    execution_package: object,
) -> tuple[tuple[str, str, int], ...]:
    result: list[tuple[str, str, int]] = []
    for row in _rows(_mapping_value(execution_package, "window_metadata")):
        window_id = _mapping_value(row, "window_id")
        phase = _value(_mapping_value(row, "phase"))
        order = _mapping_value(row, "order")
        if (
            isinstance(window_id, str)
            and isinstance(phase, str)
            and phase in {"NIGHT_TEAM_CHAT", "NIGHT_ACTION", "NIGHT_RESOLVE"}
            and type(order) is int
        ):
            result.append((window_id, phase, order))
    return tuple(sorted(result, key=lambda item: item[2]))


def _group_windows(state: Any, group_id: str) -> tuple[tuple[str, object], ...]:
    windows = getattr(state, "action_windows", {})
    if not isinstance(windows, Mapping):
        return ()
    matched: list[tuple[str, object]] = []
    for key, raw in windows.items():
        if not isinstance(key, str) or not isinstance(raw, Mapping):
            continue
        raw_group = raw.get("settlement_group_id") or raw.get("window_id") or key
        if raw_group == group_id:
            matched.append((key, raw))
    return tuple(matched)


def _physical_request_internal_ids(state: Any, ledger: object) -> set[str] | None:
    request_ids = _mapping_value(ledger, "request_ids")
    group_id = _mapping_value(ledger, "group_id")
    timing = _value(_mapping_value(ledger, "timing"))
    requests = getattr(state, "action_requests", {})
    windows = getattr(state, "action_windows", {})
    if (
        not isinstance(request_ids, (tuple, list))
        or not request_ids
        or not isinstance(requests, Mapping)
        or not isinstance(windows, Mapping)
        or not isinstance(group_id, str)
    ):
        return None
    internal_ids: set[str] = set()
    for request_id in request_ids:
        raw_request = requests.get(request_id)
        if not isinstance(raw_request, Mapping):
            return None
        window_id = raw_request.get("window_id")
        raw_window = windows.get(window_id)
        if (
            raw_request.get("status") != "CONFIRMED"
            or raw_request.get("rule_receipt_id") != _mapping_value(ledger, "batch_id")
            or raw_request.get("rule_group_id") != group_id
            or not isinstance(window_id, str)
            or not isinstance(raw_window, Mapping)
            or _value(raw_window.get("phase")) != timing
            or raw_window.get("closed_at") is None
            or (raw_window.get("settlement_group_id") or window_id) != group_id
        ):
            return None
        dispositions = raw_request.get("rule_dispositions")
        if not isinstance(dispositions, (tuple, list)) or not dispositions:
            return None
        for disposition in dispositions:
            if not isinstance(disposition, Mapping):
                return None
            internal_id = disposition.get("skill_request_id")
            if not isinstance(internal_id, str) or not internal_id or internal_id in internal_ids:
                return None
            internal_ids.add(internal_id)
    return internal_ids


def _valid_death_fact(fact: object, players: Mapping[object, object]) -> int | None:
    if _value(_mapping_value(fact, "fact_type")) != "DEATH_CONFIRMED":
        return None
    seat = _mapping_value(fact, "target_seat")
    cause = _mapping_value(fact, "death_cause")
    player = players.get(seat)
    if (
        type(seat) is not int
        or player is None
        or getattr(player, "alive", True) is not False
        or not isinstance(cause, str)
        or not cause
        or getattr(player, "death_cause", None) != cause
    ):
        return None
    data = _mapping_value(fact, "data")
    if not isinstance(data, Mapping):
        return None
    causal = data.get("source_request_ids")
    if (
        not isinstance(causal, (tuple, list))
        or not causal
        or any(not isinstance(item, str) or not item for item in causal)
        or len(set(causal)) != len(causal)
    ):
        return None
    if not isinstance(data.get("cause_effect_ids"), (tuple, list)):
        return None
    sources = data.get("sources")
    if not isinstance(sources, (tuple, list)) or not sources:
        return None
    recorded_requests: set[str] = set()
    recorded_effects: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            return None
        request_id = source.get("source_request_id")
        effect_id = source.get("effect_id")
        if (
            not isinstance(request_id, str)
            or request_id not in causal
            or not isinstance(effect_id, str)
            or not effect_id
        ):
            return None
        recorded_requests.add(request_id)
        recorded_effects.add(effect_id)
    if recorded_requests != set(causal) or recorded_effects != set(
        data.get("cause_effect_ids", ())
    ):
        return None
    direct = _mapping_value(fact, "source_request_id")
    if direct is not None and direct not in causal:
        return None
    return seat


def _strict_first_night_death_seats(state: Any, execution_package: object) -> tuple[int, ...]:
    package_id = _mapping_value(execution_package, "package_id")
    identity = getattr(state, "execution_identity", None)
    if (
        not isinstance(package_id, str)
        or not package_id
        or _mapping_value(identity, "package_id") != package_id
    ):
        return ()
    players = getattr(state, "players", {})
    boundaries = _rows(getattr(state, "rule_boundaries", ()))
    ledgers = _rows(getattr(state, "rule_ledger", ()))
    if not isinstance(players, Mapping):
        return ()
    metadata = _metadata_with_phases(execution_package)
    if not metadata:
        return ()
    deaths: set[int] = set()
    verified_boundary_ids: set[str] = set()

    def verify_fact_source(
        *, fact_id: str, batch_id: str, expected_group_id: str, depth: int, seen: frozenset[str]
    ) -> bool:
        if depth > 64 or fact_id in seen:
            return False
        candidates = tuple(
            ledger
            for ledger in ledgers
            if _mapping_value(ledger, "batch_id") == batch_id
            and _mapping_value(ledger, "group_id") == expected_group_id
            and _mapping_value(ledger, "round_no") == 0
            and _mapping_value(ledger, "package_id") == package_id
            and any(
                _mapping_value(fact, "fact_id") == fact_id
                for fact in _rows(_mapping_value(ledger, "facts"))
            )
        )
        if len(candidates) != 1:
            return False
        ledger = candidates[0]
        timing = _value(_mapping_value(ledger, "timing"))
        read_revision = _mapping_value(ledger, "read_revision")
        committed_revision = _mapping_value(ledger, "committed_revision")
        if (
            type(read_revision) is not int
            or type(committed_revision) is not int
            or committed_revision != read_revision + 1
            or not _verified_rule_receipt(state, ledger, package_id)
        ):
            return False
        raw_facts = tuple(
            fact
            for fact in _rows(_mapping_value(ledger, "facts"))
            if _mapping_value(fact, "fact_id") == fact_id
        )
        if len(raw_facts) != 1:
            return False
        fact = raw_facts[0]
        if _valid_death_fact(fact, players) is None:
            return False
        fact_data = _mapping_value(fact, "data")
        raw_causal_ids = _mapping_value(fact_data, "source_request_ids")
        if not isinstance(raw_causal_ids, (tuple, list)):
            return False
        causal_ids = set(raw_causal_ids)

        night_phases = {"NIGHT_TEAM_CHAT", "NIGHT_ACTION", "NIGHT_RESOLVE"}
        if timing in night_phases:
            group = _group_windows(state, expected_group_id)
            if not group or any(_mapping_value(row, "closed_at") is None for _, row in group):
                return False
            source_boundaries = tuple(
                boundary
                for boundary in boundaries
                if _mapping_value(boundary, "source_batch_id") == batch_id
                and _mapping_value(boundary, "source_group_id") == expected_group_id
                and fact_id in _rows(_mapping_value(boundary, "death_fact_ids"))
            )
            if not source_boundaries:
                return False
            terminal = False
            for boundary in source_boundaries:
                point = _mapping_value(boundary, "return_point")
                if (
                    _value(_mapping_value(point, "phase")) == "DAY_ANNOUNCE"
                    and _mapping_value(point, "day_no") == 0
                    and _night_group_has_terminal_topology(
                        state,
                        execution_package,
                        group_id=expected_group_id,
                        point=point,
                        metadata=metadata,
                    )
                ):
                    terminal = True
            internal_ids = _physical_request_internal_ids(state, ledger)
            if not terminal or internal_ids is None or not causal_ids.issubset(internal_ids):
                return False
            receipt = next(
                receipt
                for receipt in _rows(getattr(state, "rule_receipts", ()))
                if _mapping_value(receipt, "batch_id") == batch_id
            )
            return not _rows(_mapping_value(receipt, "occurrence_ids"))

        if timing != "TRIGGER_ACTION":
            return False
        receipts = tuple(
            receipt
            for receipt in _rows(getattr(state, "rule_receipts", ()))
            if _mapping_value(receipt, "batch_id") == batch_id
        )
        if len(receipts) != 1:
            return False
        receipt = receipts[0]
        occurrence_ids = _rows(_mapping_value(receipt, "occurrence_ids"))
        if len(occurrence_ids) != 1 or not isinstance(occurrence_ids[0], str):
            return False
        occurrence_id = occurrence_ids[0]
        expected_prefix = "automatic-" if not _mapping_value(ledger, "request_ids") else "trigger-"
        if expected_group_id != f"{expected_prefix}{occurrence_id}":
            return False
        occurrences = tuple(
            occurrence
            for occurrence in _rows(getattr(state, "rule_trigger_queue", ()))
            if _mapping_value(occurrence, "occurrence_id") == occurrence_id
        )
        if len(occurrences) != 1:
            return False
        occurrence = occurrences[0]
        mode = _mapping_value(occurrence, "mode")
        if (
            _mapping_value(occurrence, "status") != "COMPLETED"
            or (mode == "AUTOMATIC") != (expected_prefix == "automatic-")
        ):
            return False
        request_ids = _rows(_mapping_value(ledger, "request_ids"))
        if mode == "AUTOMATIC":
            if request_ids:
                return False
            history = _rows(_mapping_value(ledger, "history_updates"))
            if not any(
                _mapping_value(row, "request_id") == f"automatic-{occurrence_id}"
                and _mapping_value(row, "ability_instance_id")
                == _mapping_value(occurrence, "ability_instance_id")
                and _mapping_value(row, "skill_id") == _mapping_value(occurrence, "skill_id")
                and _mapping_value(row, "actor_seat") == _mapping_value(occurrence, "actor_seat")
                for row in history
            ):
                return False
            if causal_ids != {f"automatic-{occurrence_id}"}:
                return False
        else:
            internal_ids = _physical_request_internal_ids(state, ledger)
            occurrence_window_id = _mapping_value(occurrence, "window_id")
            action_windows = getattr(state, "action_windows", {})
            action_requests = getattr(state, "action_requests", {})
            trigger_window = (
                action_windows.get(occurrence_window_id)
                if isinstance(action_windows, Mapping)
                and isinstance(occurrence_window_id, str)
                else None
            )
            trigger_request = (
                action_requests.get(request_ids[0])
                if len(request_ids) == 1
                and isinstance(request_ids[0], str)
                and isinstance(action_requests, Mapping)
                else None
            )
            trigger_context = _mapping_value(trigger_window, "visible_context")
            occurrence_request_id = _mapping_value(occurrence, "request_id")
            if (
                len(request_ids) != 1
                or internal_ids is None
                or not causal_ids.issubset(internal_ids)
                or not isinstance(occurrence_window_id, str)
                or not isinstance(trigger_window, Mapping)
                or _value(_mapping_value(trigger_window, "phase")) != "TRIGGER_ACTION"
                or _mapping_value(trigger_window, "closed_at") is None
                or _mapping_value(trigger_window, "settlement_group_id") != expected_group_id
                or _mapping_value(trigger_context, "rule_occurrence_id") != occurrence_id
                or _mapping_value(trigger_context, "rule_source_fact_id")
                != _mapping_value(occurrence, "source_fact_id")
                or _mapping_value(trigger_context, "rule_actor_seat")
                != _mapping_value(occurrence, "actor_seat")
                or _mapping_value(trigger_context, "rule_ability_instance_id")
                != _mapping_value(occurrence, "ability_instance_id")
                or _mapping_value(trigger_context, "rule_skill_id")
                != _mapping_value(occurrence, "skill_id")
                or not isinstance(trigger_request, Mapping)
                or _mapping_value(trigger_request, "window_id") != occurrence_window_id
                or _mapping_value(trigger_request, "seat")
                != _mapping_value(occurrence, "actor_seat")
                or (occurrence_request_id is not None and occurrence_request_id not in request_ids)
            ):
                return False
        source_fact_id = _mapping_value(occurrence, "source_fact_id")
        source_batch_id = _mapping_value(occurrence, "source_batch_id")
        source = tuple(
            item
            for item in ledgers
            if _mapping_value(item, "batch_id") == source_batch_id
            and any(
                _mapping_value(source_fact, "fact_id") == source_fact_id
                for source_fact in _rows(_mapping_value(item, "facts"))
            )
        )
        if len(source) != 1:
            return False
        if not isinstance(source_fact_id, str) or not isinstance(source_batch_id, str):
            return False
        source_group_id = _mapping_value(source[0], "group_id")
        if not isinstance(source_group_id, str):
            return False
        return verify_fact_source(
            fact_id=source_fact_id,
            batch_id=source_batch_id,
            expected_group_id=source_group_id,
            depth=depth + 1,
            seen=seen | {fact_id},
        )

    for boundary in boundaries:
        point = _mapping_value(boundary, "return_point")
        if (
            _value(_mapping_value(point, "phase")) != "DAY_ANNOUNCE"
            or _mapping_value(point, "day_no") != 0
        ):
            continue
        group_id = _mapping_value(boundary, "source_group_id")
        batch_id = _mapping_value(boundary, "source_batch_id")
        fact_ids = _rows(_mapping_value(boundary, "death_fact_ids"))
        seats = _rows(_mapping_value(boundary, "death_seats"))
        if not isinstance(group_id, str) or not isinstance(batch_id, str) or not fact_ids:
            continue
        fact_seats: set[int] = set()
        valid = True
        for fact_id in fact_ids:
            if not isinstance(fact_id, str) or not verify_fact_source(
                fact_id=fact_id,
                batch_id=batch_id,
                expected_group_id=group_id,
                depth=0,
                seen=frozenset(),
            ):
                valid = False
                break
            ledger = next(
                item
                for item in ledgers
                if _mapping_value(item, "batch_id") == batch_id
                and _mapping_value(item, "group_id") == group_id
            )
            fact = next(
                item
                for item in _rows(_mapping_value(ledger, "facts"))
                if _mapping_value(item, "fact_id") == fact_id
            )
            seat = _valid_death_fact(fact, players)
            if seat is None:
                valid = False
                break
            fact_seats.add(seat)
        if valid and fact_seats == set(seats):
            deaths.update(fact_seats)
            boundary_id = _mapping_value(boundary, "boundary_id")
            if isinstance(boundary_id, str):
                verified_boundary_ids.add(boundary_id)
    if not deaths:
        return ()

    death_fact_ids = {
        _mapping_value(fact, "fact_id")
        for ledger in ledgers
        for fact in _rows(_mapping_value(ledger, "facts"))
        if _valid_death_fact(fact, players) in deaths
    }
    for occurrence in _rows(getattr(state, "rule_trigger_queue", ())):
        if _mapping_value(occurrence, "source_fact_id") in death_fact_ids and (
            _mapping_value(occurrence, "status") != "COMPLETED"
        ):
            return ()
    cursor = getattr(state, "rule_workflow_cursor", None)
    cursor_status = _mapping_value(cursor, "status")
    if cursor_status in {"COLLECTING", "WAITING_CHOICE", "ERROR"}:
        return ()
    if _mapping_value(cursor, "active_occurrence_id") is not None:
        return ()
    pending_boundaries = tuple(
        boundary
        for boundary in boundaries
        if _mapping_value(boundary, "is_pending") is True
    )
    pending_boundary_ids = {
        boundary_id
        for boundary in pending_boundaries
        if isinstance(boundary_id := _mapping_value(boundary, "boundary_id"), str)
    }
    if pending_boundary_ids and not pending_boundary_ids.issubset(verified_boundary_ids):
        return ()
    if cursor_status in {"DRAINING", "WAITING_BOUNDARY"}:
        if _mapping_value(cursor, "pending_boundary_id") not in pending_boundary_ids:
            return ()
    elif pending_boundary_ids:
        return ()
    return tuple(sorted(deaths))


def first_night_death_seats(
    state: Any,
    *,
    execution_package: object | None = None,
) -> tuple[int, ...]:
    """Return only first-night deaths with current resolution provenance.

    A plain ``alive=False`` flag is intentionally insufficient.  The seat
    must be dead now, and the same current-round resolution must contain both
    ``SET_ALIVE=false`` and ``SET_DEATH_CAUSE`` effects.  Trigger resolutions
    are accepted only when their frozen context points back to that original
    night resolution.
    """

    if getattr(state, "day_no", None) != 1 or getattr(state, "round_no", None) != 0:
        return ()
    if _schedule_requires_first_day_election(execution_package):
        return _strict_first_night_death_seats(state, execution_package)
    resolutions = getattr(state, "resolutions", ())
    windows = getattr(state, "action_windows", {})
    players = getattr(state, "players", {})
    if not isinstance(windows, Mapping) or not isinstance(players, Mapping):
        return ()

    night_resolution_ids: set[str] = set()
    night_window_ids: set[str] = set()
    deaths: set[int] = set()

    def collect(raw_resolution: object) -> None:
        actions = _mapping_value(raw_resolution, "actions")
        if not isinstance(actions, (list, tuple)):
            return
        for action in actions:
            disposition = _value(_mapping_value(action, "disposition"))
            if disposition is not None and disposition not in {"CONFIRMED", "OVERRIDDEN"}:
                continue
            effects = _mapping_value(action, "effects")
            if not isinstance(effects, (list, tuple)):
                continue
            killed: set[int] = set()
            causes: dict[int, str] = {}
            for effect in effects:
                target = _mapping_value(effect, "target_seat")
                if type(target) is not int:
                    continue
                effect_type = _mapping_value(effect, "effect_type")
                if effect_type == "SET_ALIVE" and _mapping_value(effect, "value") is False:
                    killed.add(target)
                elif effect_type == "SET_DEATH_CAUSE":
                    cause = _mapping_value(effect, "value")
                    if isinstance(cause, str) and cause:
                        causes[target] = cause
            for seat in killed:
                player = players.get(seat)
                if (
                    player is not None
                    and getattr(player, "alive", True) is False
                    and causes.get(seat) is not None
                    and getattr(player, "death_cause", None) == causes[seat]
                ):
                    deaths.add(seat)

    for raw_resolution in resolutions:
        status = _value(_mapping_value(raw_resolution, "status"))
        if status is not None and status not in {"CONFIRMED", "OVERRIDDEN"}:
            continue
        resolution_id = _mapping_value(raw_resolution, "resolution_id")
        window_id = _mapping_value(raw_resolution, "window_id")
        if not isinstance(resolution_id, str) or not isinstance(window_id, str):
            continue
        raw_window = windows.get(window_id)
        phase = _value(_mapping_value(raw_window, "phase"))
        visible = _mapping_value(raw_window, "visible_context")
        if (
            phase == "NIGHT_ACTION"
            and isinstance(visible, Mapping)
            and visible.get("night_round") == 0
        ):
            night_resolution_ids.add(resolution_id)
            night_window_ids.add(window_id)
            collect(raw_resolution)

    if not night_resolution_ids:
        return ()

    for raw_resolution in resolutions:
        status = _value(_mapping_value(raw_resolution, "status"))
        if status is not None and status not in {"CONFIRMED", "OVERRIDDEN"}:
            continue
        window_id = _mapping_value(raw_resolution, "window_id")
        if not isinstance(window_id, str) or window_id in night_window_ids:
            continue
        raw_window = windows.get(window_id)
        visible = _mapping_value(raw_window, "visible_context")
        if (
            _value(_mapping_value(raw_window, "phase")) == "TRIGGER_ACTION"
            and isinstance(visible, Mapping)
            and visible.get("operation") == "NIGHT_RESOLUTION"
            and visible.get("resolution_id") in night_resolution_ids
        ):
            collect(raw_resolution)

    return tuple(sorted(deaths))


def first_day_sheriff_participants(
    state: Any,
    *,
    execution_package: object | None = None,
) -> tuple[int, ...]:
    """Return the authoritative voter/candidate set for first-day election."""

    players = getattr(state, "players", {})
    if not isinstance(players, Mapping):
        return ()
    eligible = {
        seat
        for seat, player in players.items()
        if type(seat) is int
        and getattr(player, "alive", False)
        and getattr(player, "can_vote", False)
    }
    if is_first_day_sheriff_boundary(state):
        eligible.update(
            first_night_death_seats(state, execution_package=execution_package)
        )
    return tuple(sorted(eligible))


def should_mask_unannounced_first_night_death(
    state: Any,
    seat: int,
    *,
    execution_package: object | None = None,
) -> bool:
    """Return whether a private status projection must hide this death."""

    return is_first_day_sheriff_boundary(state) and seat in first_night_death_seats(
        state, execution_package=execution_package
    )


__all__ = [
    "first_day_sheriff_participants",
    "first_night_death_seats",
    "has_first_day_announcement",
    "is_first_day_sheriff_boundary",
    "should_mask_unannounced_first_night_death",
]
