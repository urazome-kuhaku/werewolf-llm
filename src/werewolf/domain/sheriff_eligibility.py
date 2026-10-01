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


def first_night_death_seats(state: Any) -> tuple[int, ...]:
    """Return only first-night deaths with current resolution provenance.

    A plain ``alive=False`` flag is intentionally insufficient.  The seat
    must be dead now, and the same current-round resolution must contain both
    ``SET_ALIVE=false`` and ``SET_DEATH_CAUSE`` effects.  Trigger resolutions
    are accepted only when their frozen context points back to that original
    night resolution.
    """

    if getattr(state, "day_no", None) != 1 or getattr(state, "round_no", None) != 0:
        return ()
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


def first_day_sheriff_participants(state: Any) -> tuple[int, ...]:
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
        eligible.update(first_night_death_seats(state))
    return tuple(sorted(eligible))


def should_mask_unannounced_first_night_death(state: Any, seat: int) -> bool:
    """Return whether a private status projection must hide this death."""

    return is_first_day_sheriff_boundary(state) and seat in first_night_death_seats(state)


__all__ = [
    "first_day_sheriff_participants",
    "first_night_death_seats",
    "has_first_day_announcement",
    "is_first_day_sheriff_boundary",
    "should_mask_unannounced_first_night_death",
]
