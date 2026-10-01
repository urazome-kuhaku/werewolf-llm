"""Deterministic, ruleset-backed role assignment for a new game.

This module only prepares a typed assignment plan.  It does not mutate a
``GameState`` and it does not emit role or faction events; the authoritative
game manager owns that one-time commit.  Every role and count comes from the
frozen board and compiled knowledge package, so callers cannot provide an
ad-hoc identity list through this API.

Resource initialization deliberately uses only the typed role contract:
``ability.resource.resource_id`` is initialized to ``ability.usage_limit``'s
explicit ``max_uses`` value.  A role without that explicit limit receives no
resource entry (zero is the safe default).  Board-specific prose in
``effective_rules`` is not interpreted here; a future typed resource override
belongs in the published knowledge schema before it can affect setup.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final

from pydantic import ValidationError

from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiler import CompiledKnowledgePackage
from werewolf.knowledge.preview import experimental_preview_enabled
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.role import Faction, RoleDefinition, TriggerType

from .state import GrantedAbility, GrantedTriggerAbility, PlayerState

_REVIEWER_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "anonymous",
        "auto",
        "automated",
        "none",
        "null",
        "n-a",
        "na",
        "pending",
        "pending-human-review",
        "pending-review",
        "system",
        "tbd",
        "todo",
        "unknown",
        "unspecified",
    }
)


class RoleAssignmentError(ValueError):
    """Raised when a board/package/seat set cannot produce a safe assignment."""


@dataclass(frozen=True, slots=True)
class PlayerAssignmentPlan:
    """One immutable assignment result ready for an atomic game-state commit."""

    board_ref: VersionedRef
    seed: int
    seats: tuple[int, ...]
    players: Mapping[int, PlayerState]

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("assignment seed must be an integer")
        if tuple(sorted(self.seats)) != self.seats:
            raise ValueError("assignment seats must be sorted")
        if len(set(self.seats)) != len(self.seats):
            raise ValueError("assignment seats must be unique")
        if tuple(self.players) != self.seats:
            raise ValueError("assignment players must cover the supplied seats exactly")
        if any(player.seat != seat for seat, player in self.players.items()):
            raise ValueError("assignment player keys must match PlayerState.seat")
        object.__setattr__(self, "players", MappingProxyType(dict(self.players)))

    @property
    def player_states(self) -> tuple[PlayerState, ...]:
        """Return player states in deterministic ascending-seat order."""

        return tuple(self.players[seat] for seat in self.seats)

    @property
    def assignments(self) -> Mapping[int, PlayerState]:
        """Compatibility spelling for the one-time commit payload."""

        return self.players


def _reviewer_is_human(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = "-".join(value.split()).casefold().replace("_", "-")
    return bool(value.strip()) and normalized not in _REVIEWER_PLACEHOLDERS


def _validate_published_reviewed(document: object, *, label: str) -> None:
    if getattr(document, "status", None) != "published":
        raise RoleAssignmentError(f"{label} must be published")
    if (
        not _reviewer_is_human(getattr(document, "reviewed_by", None))
        and not experimental_preview_enabled()
    ):
        raise RoleAssignmentError(f"{label} must have a human reviewer")
    if not isinstance(getattr(document, "reviewed_at", None), date):
        raise RoleAssignmentError(f"{label} must have a review date")


def _normalise_seats(seats: Iterable[int]) -> tuple[int, ...]:
    if isinstance(seats, (str, bytes)):
        raise TypeError("seats must be an iterable of integer seat numbers")
    try:
        values = tuple(seats)
    except TypeError as exc:
        raise TypeError("seats must be an iterable of integer seat numbers") from exc
    if any(type(seat) is not int or not 1 <= seat <= 64 for seat in values):
        raise RoleAssignmentError("seats must contain integer values from 1 through 64")
    if len(set(values)) != len(values):
        raise RoleAssignmentError("seats must be unique")
    return tuple(sorted(values))


def _initial_resources(role: RoleDefinition) -> dict[str, int]:
    """Derive safe initial balances from explicit, typed ability limits."""

    balances: dict[str, int] = {}
    for ability in role.abilities:
        resource = ability.resource
        usage_limit = ability.usage_limit
        if resource is None or usage_limit is None or usage_limit.max_uses is None:
            continue
        resource_id = resource.resource_id
        amount = usage_limit.max_uses
        previous = balances.get(resource_id)
        if previous is not None and previous != amount:
            raise RoleAssignmentError(
                f"role {role.role_id!r} declares conflicting max_uses for resource {resource_id!r}"
            )
        balances[resource_id] = amount
    return dict(sorted(balances.items()))


def _initial_trigger_abilities(role: RoleDefinition) -> tuple[GrantedTriggerAbility, ...]:
    """Copy executable trigger contracts into each assigned seat's state.

    Trigger eligibility is declared by the published ability itself.  In
    particular, this function does not infer special behavior from a role ID;
    role aliases or different role IDs can therefore share the same trigger
    contract.  The state copy is immutable and starts unconsumed for every
    one-shot trigger.
    """

    granted: list[GrantedTriggerAbility] = []
    seen_ids: set[str] = set()
    for ability in role.abilities:
        if ability.ability_id in seen_ids:
            raise RoleAssignmentError(
                f"role {role.role_id!r} declares duplicate ability ID {ability.ability_id!r}"
            )
        seen_ids.add(ability.ability_id)
        trigger = ability.trigger
        if trigger is None:
            continue
        granted.append(
            GrantedTriggerAbility(
                ability_id=ability.ability_id,
                action_code=ability.action_code,
                trigger=trigger,
                target_rule=ability.target_rule,
            )
        )
    return tuple(granted)


def _initial_active_abilities(role: RoleDefinition) -> tuple[GrantedAbility, ...]:
    """Copy active executable contracts into each assigned seat's state.

    Only abilities explicitly marked ``ACTIVE`` in the frozen role definition
    are copied.  The role ID is never consulted, so a board can add an active
    ability to a new role without changing setup code.  Usage and resource
    definitions are copied as immutable knowledge contracts; mutable balances
    remain in ``PlayerState.skill_resources``.
    """

    granted: list[GrantedAbility] = []
    for ability in role.abilities:
        if getattr(ability, "trigger_type", None) is not TriggerType.ACTIVE:
            continue
        try:
            granted.append(
                GrantedAbility(
                    ability_id=ability.ability_id,
                    action_code=ability.action_code,
                    timing=ability.timing,
                    allowed_phases=tuple(ability.allowed_phases),
                    target_rule=ability.target_rule,
                    usage_limit=ability.usage_limit,
                    resource=ability.resource,
                )
            )
        except (AttributeError, ValidationError) as exc:
            raise RoleAssignmentError(
                f"role {role.role_id!r} declares an invalid active ability contract"
            ) from exc
    return tuple(granted)


def build_role_assignment_plan(
    board: BoardDefinition,
    package: CompiledKnowledgePackage,
    seed: int,
    seats: Iterable[int],
) -> PlayerAssignmentPlan:
    """Build a reproducible seat-to-player plan from frozen published rules.

    ``seed`` affects only the deterministic shuffle.  Role IDs, counts,
    factions, and resources are read from the board/package and cannot be
    supplied by a caller.
    """

    if not isinstance(board, BoardDefinition):
        raise TypeError("board must be a BoardDefinition")
    if not isinstance(package, CompiledKnowledgePackage):
        raise TypeError("package must be a CompiledKnowledgePackage")
    if type(seed) is not int:
        raise TypeError("seed must be an integer")

    _validate_published_reviewed(board, label="board")
    if package.board_ref != board.board_ref:
        raise RoleAssignmentError("compiled package does not match the board reference")

    seat_values = _normalise_seats(seats)
    if len(seat_values) != board.seat_count:
        raise RoleAssignmentError(
            f"board requires {board.seat_count} seats, received {len(seat_values)}"
        )

    bindings = tuple(board.role_bindings)
    binding_ids = tuple(binding.role_ref.id for binding in bindings)
    if len(set(binding_ids)) != len(binding_ids):
        raise RoleAssignmentError("board role bindings must have unique role IDs")
    if sum(binding.count for binding in bindings) != len(seat_values):
        raise RoleAssignmentError("role binding counts must equal the seat count")

    profiles = package.effective_roles
    if set(profiles) != set(binding_ids):
        raise RoleAssignmentError("compiled package effective roles do not match the board")

    role_records: list[
        tuple[
            str,
            str,
            dict[str, int],
            tuple[GrantedAbility, ...],
            tuple[GrantedTriggerAbility, ...],
        ]
    ] = []
    faction_counts: dict[str, int] = {faction_id: 0 for faction_id in board.factions}

    for binding in bindings:
        role_id = binding.role_ref.id
        profile = profiles.get(role_id)
        if profile is None or profile.role_ref != binding.role_ref:
            raise RoleAssignmentError(
                f"compiled role profile does not match board binding {binding.role_ref.format()}"
            )
        if profile.count != binding.count:
            raise RoleAssignmentError(f"compiled count for role {role_id!r} does not match board")
        role = profile.base_role
        if not isinstance(role, RoleDefinition):
            raise RoleAssignmentError(f"effective role {role_id!r} is not a RoleDefinition")
        _validate_published_reviewed(role, label=f"role {role_id!r}")
        if role.role_id != role_id or role.version != binding.role_ref.version:
            raise RoleAssignmentError(f"role definition {role_id!r} does not match its pinned ref")
        if role.board_compatibility and board.board_ref not in role.board_compatibility:
            raise RoleAssignmentError(f"role {role_id!r} is not compatible with this board")
        if not isinstance(role.faction, Faction):
            raise RoleAssignmentError(f"role {role_id!r} has an invalid faction")
        if not isinstance(role.team, str) or role.team not in board.factions:
            raise RoleAssignmentError(f"role {role_id!r} team {role.team!r} is not a board faction")

        ability_ids = tuple(ability.ability_id for ability in role.abilities)
        if len(set(ability_ids)) != len(ability_ids):
            raise RoleAssignmentError(f"role {role_id!r} declares duplicate ability IDs")

        faction_counts[role.team] += binding.count
        resources = _initial_resources(role)
        active_abilities = _initial_active_abilities(role)
        trigger_abilities = _initial_trigger_abilities(role)
        role_records.extend(
            (role_id, role.team, resources.copy(), active_abilities, trigger_abilities)
            for _ in range(binding.count)
        )

    if faction_counts != dict(board.factions):
        raise RoleAssignmentError(
            "role definitions do not account for the board faction counts: "
            f"expected {dict(board.factions)!r}, got {faction_counts!r}"
        )

    random_source = random.Random(seed)
    random_source.shuffle(role_records)
    players = {
        seat: PlayerState(
            seat=seat,
            role_id=role_id,
            faction_id=faction_id,
            skill_resources=resources,
            granted_abilities=active_abilities,
            granted_trigger_abilities=trigger_abilities,
        )
        for seat, (role_id, faction_id, resources, active_abilities, trigger_abilities) in zip(
            seat_values, role_records, strict=True
        )
    }
    return PlayerAssignmentPlan(
        board_ref=board.board_ref,
        seed=seed,
        seats=seat_values,
        players=players,
    )


# Short aliases keep the pure setup boundary discoverable to callers without
# creating alternate implementations or accepting a caller-provided roster.
build_assignment_plan = build_role_assignment_plan
create_role_assignment_plan = build_role_assignment_plan


__all__ = [
    "PlayerAssignmentPlan",
    "RoleAssignmentError",
    "build_assignment_plan",
    "build_role_assignment_plan",
    "create_role_assignment_plan",
]
