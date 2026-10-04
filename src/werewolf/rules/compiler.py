"""Compile one board's explicit YAML executable definition."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.package_loader import KnowledgePackage
from werewolf.knowledge.refs import VersionedRef
from werewolf.rules.predicates import validate_package_expressions

from .models import ExecutionPackage
from .registry import (
    ActionRegistryCompilationError,
    merge_action_registries,
    validate_execution_actions,
)
from .scheduler import SkillDependencyError, skill_dependency_ranks

if TYPE_CHECKING:
    from werewolf.game.actions import ActionRegistry


class ExecutionCompilerError(ValueError):
    """Raised when executable rules are malformed or exceed supported capability."""


@dataclass(frozen=True, slots=True)
class CompiledExecution:
    """Typed executable definition and its frozen merged action registry."""

    execution: ExecutionPackage
    action_registry: ActionRegistry
    source: str
    source_sha256: str


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _error(message: str) -> ExecutionCompilerError:
    return ExecutionCompilerError(message)


def compile_execution_definition(package: KnowledgePackage) -> CompiledExecution | None:
    """Compile a package-local executable definition or explicit legacy mapping.

    A missing declaration is accepted only by the compatibility compiler for
    the audited, currently published classic board. Every other package must
    carry an ``execution.yaml`` beside its pinned board document.
    """

    if package.execution_definition is None:
        if package.board_ref != VersionedRef(
            id="classic_12_seer_witch_hunter_idiot",
            version="1.0.0",
        ):
            return None
        from .compat import compile_legacy_execution

        return compile_legacy_execution(package)

    raw = cast(Mapping[str, object], _canonicalize(package.execution_definition))
    expected = {"schema_version", "execution", "action_definitions"}
    if set(raw) != expected:
        missing = sorted(expected - set(raw))
        extra = sorted(set(raw) - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise _error("execution.yaml fields are invalid: " + "; ".join(detail))
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
        raise _error("execution.yaml schema_version is unsupported")

    try:
        # Import lazily: importing werewolf.game.actions executes game package
        # initialization, which depends on the knowledge service/compiler.
        from werewolf.game.actions import ActionDefinition, load_action_registry

        execution = ExecutionPackage.model_validate(raw["execution"])
        additions_raw = raw["action_definitions"]
        if not isinstance(additions_raw, Sequence) or isinstance(additions_raw, str | bytes):
            raise _error("execution.yaml action_definitions must be a list")
        additions = tuple(ActionDefinition.model_validate(item) for item in additions_raw)
        merged = merge_action_registries(load_action_registry(), additions)
        validate_execution_package(execution, merged)
    except ExecutionCompilerError:
        raise
    except (ActionRegistryCompilationError, TypeError, ValueError, ValidationError) as exc:
        raise _error(f"execution.yaml is invalid: {exc}") from exc

    if execution.board_id != package.board_ref.id:
        raise _error("execution.yaml board_id disagrees with the pinned board")
    if execution.board_version != package.board_ref.version:
        raise _error("execution.yaml board_version disagrees with the pinned board")
    _validate_package_literals(execution, package)

    canonical_source = _canonical_json(_canonicalize(raw))
    return CompiledExecution(
        execution=execution,
        action_registry=merged,
        source="declared",
        source_sha256=hashlib.sha256(canonical_source.encode("utf-8")).hexdigest(),
    )


def _canonicalize(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    return value


def validate_execution_package(
    execution: ExecutionPackage,
    registry: ActionRegistry,
    *,
    role_ids: set[str] | None = None,
) -> None:
    """Validate executable references and their frozen action contract.

    This check runs both when compiling source YAML and when restoring the
    detached artifact.  It intentionally validates only identifiers and
    contracts; skill names remain arbitrary package data.
    """

    from werewolf.game.actions import ActionRegistry

    if not isinstance(registry, ActionRegistry):
        raise TypeError("registry must be an ActionRegistry")
    if not isinstance(execution, ExecutionPackage):
        raise TypeError("execution must be an ExecutionPackage")

    validate_execution_actions(execution, registry)
    validate_package_expressions(execution)

    skill_ids = [skill.skill_id for skill in execution.skills]
    if len(skill_ids) != len(set(skill_ids)):
        raise _error("execution skill IDs must be unique")
    try:
        skill_dependency_ranks(execution.skills)
    except SkillDependencyError as exc:
        raise _error(f"skill dependencies are invalid: {exc}") from exc
    action_codes = [skill.action_code for skill in execution.skills]
    if len(action_codes) != len(set(action_codes)):
        raise _error("an action code may be bound to only one skill in an execution package")
    interaction_ids = [rule.interaction_id for rule in execution.interactions]
    if len(interaction_ids) != len(set(interaction_ids)):
        raise _error("execution interaction IDs must be unique")
    state_keys = [(item.skill_id, item.key) for item in execution.state_declarations]
    if len(state_keys) != len(set(state_keys)):
        raise _error("execution state declarations must be unique per skill")
    grant_ids = [grant.grant_id for skill in execution.skills for grant in skill.grants]
    if len(grant_ids) != len(set(grant_ids)):
        raise _error("ability grant IDs must be unique in an execution package")
    effect_ids = [
        effect.effect_id
        for skill in execution.skills
        for effect in (*skill.effects, *skill.pass_effects)
    ] + [
        effect.effect_id for interaction in execution.interactions for effect in interaction.effects
    ]
    if len(effect_ids) != len(set(effect_ids)):
        raise _error("effect IDs must be unique in an execution package")
    disclosure_ids = [
        disclosure.disclosure_id for skill in execution.skills for disclosure in skill.disclosures
    ] + [
        disclosure.disclosure_id
        for interaction in execution.interactions
        for disclosure in interaction.disclosures
    ]
    if len(disclosure_ids) != len(set(disclosure_ids)):
        raise _error("disclosure IDs must be unique in an execution package")

    known_skills = set(skill_ids)
    for declaration in execution.state_declarations:
        if declaration.skill_id not in known_skills:
            raise _error(f"state declaration references unknown skill {declaration.skill_id!r}")
    declared_state = {(item.skill_id, item.key) for item in execution.state_declarations}
    action_specs = {item.action_code: item for item in execution.actions}
    for skill in execution.skills:
        if skill.mode == "PLAYER" and not skill.grants:
            raise _error(f"skill {skill.skill_id!r} has no ability grants")
        if skill.mode == "HOST" and skill.grants:
            raise _error(f"host skill {skill.skill_id!r} must not grant player abilities")
        if skill.mode == "AUTOMATIC":
            raise _error("automatic skill mode is unsupported in language version 1")
        if not skill.timing:
            raise _error(f"skill {skill.skill_id!r} must declare at least one timing")
        if len(skill.timing) != len(set(skill.timing)):
            raise _error(f"skill {skill.skill_id!r} timing values must be unique")
        unsupported_timing = sorted(
            timing for timing in skill.timing if timing not in GamePhase._value2member_map_
        )
        if unsupported_timing:
            raise _error(
                f"skill {skill.skill_id!r} declares unsupported timing values: "
                + ", ".join(unsupported_timing)
            )
        action = registry.get(skill.action_code)
        action_spec = action_specs[skill.action_code]
        if not skill.targets.min_targets <= action.target_count <= skill.targets.max_targets:
            raise _error(
                f"skill {skill.skill_id!r} target bounds disagree with action "
                f"{action.action_name!r} target_count"
            )
        if not action_spec.allow_pass and (
            skill.pass_effects or skill.usage.pass_records or skill.usage.pass_updates_history
        ):
            raise _error(
                f"skill {skill.skill_id!r} declares PASS behavior but its action "
                "does not allow PASS"
            )
        for effect in skill.effects:
            if (
                effect.state_key is not None
                and (skill.skill_id, effect.state_key) not in declared_state
            ):
                raise _error(
                    f"skill {skill.skill_id!r} effect references undeclared state "
                    f"{effect.state_key!r}"
                )

    if role_ids is not None:
        _validate_execution_role_literals(execution, role_ids)


def _validate_package_literals(execution: ExecutionPackage, package: KnowledgePackage) -> None:
    """Bind declared role/skill references to this exact dependency closure."""

    role_ids = set(package.roles)
    _validate_execution_role_literals(execution, role_ids)


def _validate_execution_role_literals(execution: ExecutionPackage, role_ids: set[str]) -> None:
    """Reject role literals that point outside the frozen board dependency set."""

    skill_ids = {skill.skill_id for skill in execution.skills}

    def visit(value: object) -> None:
        from .models import BooleanExpr, CompareExpr, CountExpr, LiteralExpr, MapExpr, RefExpr

        if isinstance(value, CompareExpr):
            for reference, literal in ((value.left, value.right), (value.right, value.left)):
                if isinstance(reference, RefExpr) and isinstance(literal, LiteralExpr):
                    if reference.name == "role_id" and isinstance(literal.value, str):
                        if literal.value not in role_ids:
                            raise _error(
                                f"execution references role not bound to board: {literal.value!r}"
                            )
                    if reference.name == "skill_id" and isinstance(literal.value, str):
                        if literal.value not in skill_ids:
                            raise _error(f"execution references unknown skill: {literal.value!r}")
            visit(value.left)
            visit(value.right)
        elif isinstance(value, BooleanExpr):
            for child in value.values:
                visit(child)
        elif isinstance(value, CountExpr):
            visit(value.selector)
        elif isinstance(value, MapExpr):
            visit(value.selector)
            visit(value.value)
        elif isinstance(value, RefExpr | LiteralExpr):
            return
        elif isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, (tuple, list)):
            for child in value:
                visit(child)
        elif isinstance(value, BaseModel):
            for field_name in type(value).model_fields:
                visit(getattr(value, field_name))

    visit(execution)


def execution_artifact(compiled: CompiledExecution) -> dict[str, object]:
    """Return canonical JSON-shaped artifacts stored with a compiled package."""

    return {
        "action_registry": cast(
            dict[str, object], compiled.action_registry.model_dump(mode="json")
        ),
        "execution": cast(dict[str, object], compiled.execution.model_dump(mode="json")),
        "source": compiled.source,
        "source_sha256": compiled.source_sha256,
    }


__all__ = [
    "CompiledExecution",
    "ExecutionCompilerError",
    "compile_execution_definition",
    "execution_artifact",
]
