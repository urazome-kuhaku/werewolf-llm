"""Frozen action-registry composition for executable rulesets.

The game protocol keeps the established :class:`ActionRegistry` model. A
compiled rules package carries the complete merged registry so running games
never read ``config/actions.yaml`` again.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from werewolf.game.actions import ActionDefinition, ActionRegistry

    from .models import ExecutionPackage


class ActionRegistryCompilationError(ValueError):
    """Raised when package actions cannot be bound to one frozen registry."""


def merge_action_registries(
    base: ActionRegistry,
    package_actions: Iterable[ActionDefinition] = (),
) -> ActionRegistry:
    """Merge package action definitions with a frozen host registry.

    Identical declarations are harmless and collapse to one entry. A package
    cannot redefine an existing code or name with different semantics.
    """

    from werewolf.game.actions import ActionRegistry

    by_code = {action.action_code: action for action in base.actions}
    by_name = {action.action_name: action for action in base.actions}
    for action in package_actions:
        existing_code = by_code.get(action.action_code)
        existing_name = by_name.get(action.action_name)
        if existing_code is not None and existing_code != action:
            raise ActionRegistryCompilationError(
                f"action code {action.action_code} conflicts with the host registry"
            )
        if existing_name is not None and existing_name != action:
            raise ActionRegistryCompilationError(
                f"action ID {action.action_name!r} conflicts with the host registry"
            )
        by_code[action.action_code] = action
        by_name[action.action_name] = action
    return ActionRegistry(actions=tuple(by_code[code] for code in sorted(by_code)))


def validate_execution_actions(
    execution: ExecutionPackage,
    registry: ActionRegistry,
) -> None:
    """Require every executable action reference to match one registry row."""

    specs = tuple(execution.actions)
    codes = [item.action_code for item in specs]
    action_ids = [item.action_id for item in specs]
    if len(codes) != len(set(codes)):
        raise ActionRegistryCompilationError("execution action codes must be unique")
    if len(action_ids) != len(set(action_ids)):
        raise ActionRegistryCompilationError("execution action IDs must be unique")

    for spec in specs:
        try:
            definition = registry.get(spec.action_code)
        except KeyError as exc:
            raise ActionRegistryCompilationError(
                f"execution action {spec.action_id!r} uses unregistered code {spec.action_code}"
            ) from exc
        if definition.action_name != spec.action_id:
            raise ActionRegistryCompilationError(
                f"execution action {spec.action_id!r} disagrees with registry code "
                f"{spec.action_code} ({definition.action_name})"
            )

    declared_codes = set(codes)
    for skill in execution.skills:
        if skill.action_code and skill.action_code not in declared_codes:
            raise ActionRegistryCompilationError(
                f"skill {skill.skill_id!r} references undeclared action code {skill.action_code}"
            )


__all__ = [
    "ActionRegistryCompilationError",
    "merge_action_registries",
    "validate_execution_actions",
]
