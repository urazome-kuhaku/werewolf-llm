"""Cross-check published role abilities against the runtime action registry.

Role documents own the role-specific authorization and target bounds, while
``config/actions.yaml`` owns the board-independent wire action definitions.
This module is deliberately a small validation boundary so publication can
check the two contracts without making the knowledge models depend on the
game manager or reducer.
"""

from __future__ import annotations

from collections.abc import Mapping

from werewolf.game.actions import ActionRegistry

from .role import AbilityDefinition, RoleDefinition, TargetKind, TriggerMode, TriggerType


class RoleActionContractError(ValueError):
    """Raised when a role ability cannot be represented by the action registry."""


def validate_role_action_contract(
    roles: Mapping[str, RoleDefinition],
    registry: ActionRegistry,
) -> None:
    """Validate executable abilities in every effective role.

    Automatic passive abilities use ``action_code=0`` as a reducer-only
    marker and therefore do not need a registry entry.  Active abilities and
    player-choice trigger abilities are submitted over the action protocol and
    must agree with the registry on action identity, target cardinality, and
    resource identity.
    """

    for role_id, role in sorted(roles.items()):
        for ability in role.abilities:
            trigger_mode = None if ability.trigger is None else ability.trigger.mode
            if not _requires_registry_entry(ability.trigger_type, trigger_mode):
                continue
            _validate_ability(role_id, ability.ability_id, ability, registry)


def _requires_registry_entry(
    trigger_type: TriggerType,
    trigger_mode: TriggerMode | None,
) -> bool:
    """Return whether an ability can produce a player-submitted action."""

    return trigger_type is TriggerType.ACTIVE or trigger_mode is TriggerMode.PLAYER_CHOICE


def _validate_ability(
    role_id: str,
    ability_id: str,
    ability: AbilityDefinition,
    registry: ActionRegistry,
) -> None:
    action_code = ability.action_code
    try:
        action = registry.get(action_code)
    except KeyError as exc:
        raise RoleActionContractError(
            f"role {role_id!r} ability {ability_id!r} declares unknown action_code {action_code}"
        ) from exc

    target_rule = ability.target_rule
    expected_count = action.target_count
    if target_rule.min_targets != expected_count or target_rule.max_targets != expected_count:
        raise RoleActionContractError(
            f"role {role_id!r} ability {ability_id!r} target bounds "
            f"({target_rule.min_targets}, {target_rule.max_targets}) do not match "
            f"action {action_code} target_count {expected_count}"
        )
    if (action.target_policy == "none") != (target_rule.kind is TargetKind.NONE):
        raise RoleActionContractError(
            f"role {role_id!r} ability {ability_id!r} target kind "
            f"{target_rule.kind.value!r} is incompatible with action "
            f"{action_code} target_policy {action.target_policy!r}"
        )

    declared_resource = None if ability.resource is None else ability.resource.resource_id
    if declared_resource != action.resource_id:
        raise RoleActionContractError(
            f"role {role_id!r} ability {ability_id!r} resource_id "
            f"{declared_resource!r} does not match action {action_code} "
            f"resource_id {action.resource_id!r}"
        )


__all__ = ["RoleActionContractError", "validate_role_action_contract"]
