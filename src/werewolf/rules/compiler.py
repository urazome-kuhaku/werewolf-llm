"""Compile one board's explicit YAML executable definition."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.package_loader import KnowledgePackage
from werewolf.knowledge.refs import VersionedRef
from werewolf.rules.predicates import validate_package_expressions

from .models import BoundaryPolicy, ExecutionPackage, ExecutionWindow, PlayerFieldValues
from .registry import (
    ActionRegistryCompilationError,
    merge_action_registries,
    validate_execution_actions,
)
from .scheduler import SkillDependencyError, skill_dependency_ranks

if TYPE_CHECKING:
    from werewolf.game.actions import ActionRegistry
    from werewolf.knowledge.board import BoardDefinition


class ExecutionCompilerError(ValueError):
    """Raised when executable rules are malformed or exceed supported capability."""


@dataclass(frozen=True, slots=True)
class CompiledExecution:
    """Typed executable definition and its frozen merged action registry."""

    execution: ExecutionPackage
    action_registry: ActionRegistry
    source: str
    source_sha256: str


_LEGACY_WINDOW_TOPOLOGY = (
    "NIGHT_TEAM_CHAT",
    "NIGHT_ACTION",
    "NIGHT_RESOLVE",
)
_DAY_SPEECH_HOOKS = {"DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"}


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


def execution_windows_from_board(board: BoardDefinition) -> tuple[ExecutionWindow, ...]:
    """Return the compact, ordered scheduling contract from one frozen board."""

    return tuple(
        ExecutionWindow(
            window_id=window.window_id,
            order=window.order,
            phase=cast(
                Literal["NIGHT_TEAM_CHAT", "NIGHT_ACTION", "NIGHT_RESOLVE"],
                window.phase.value,
            ),
            depends_on=tuple(window.depends_on),
        )
        for window in sorted(board.night_windows, key=lambda item: item.order)
    )


def _requires_frozen_window_metadata(windows: Sequence[ExecutionWindow]) -> bool:
    if tuple(item.phase for item in windows) != _LEGACY_WINDOW_TOPOLOGY:
        return True
    if len(windows) != 3:
        return True
    return tuple(tuple(item.depends_on) for item in windows) != (
        (),
        (windows[0].window_id,),
        (windows[1].window_id,),
    )


def _has_window_disclosure_hook(
    execution: ExecutionPackage,
    window_ids: set[str],
) -> bool:
    return any(
        disclosure.hook in window_ids or disclosure.hook in _DAY_SPEECH_HOOKS
        for skill in execution.skills
        for disclosure in skill.disclosures
    ) or any(
        disclosure.hook in window_ids or disclosure.hook in _DAY_SPEECH_HOOKS
        for interaction in execution.interactions
        for disclosure in interaction.disclosures
    )


def boundary_policy_from_board(board: BoardDefinition) -> BoundaryPolicy:
    """Return the compact death and sheriff boundary contract from a frozen board."""

    words = board.day_flow.last_words
    sheriff = board.day_flow.sheriff
    return BoundaryPolicy(
        last_words_enabled=words.enabled,
        eligible_death_causes=tuple(sorted(words.eligible_death_causes)),
        before_reveal=words.before_reveal,
        night_death_policy=words.night_death_policy,
        day_death_policy=words.day_death_policy,
        sheriff_enabled=sheriff.enabled,
        badge_transfer_enabled=sheriff.transfer_enabled,
        badge_transfer_on_death=sheriff.transfer_on_death,
    )


def player_field_values_from_board(
    board: BoardDefinition,
    *,
    role_ids: Sequence[str],
    chat_group_ids: Sequence[str],
) -> PlayerFieldValues:
    """Derive the closed identity vocabularies from frozen board dependencies."""

    return PlayerFieldValues(
        role_ids=tuple(sorted(set(role_ids))),
        faction_ids=tuple(sorted(board.factions)),
        victory_group_ids=tuple(
            sorted(set(board.factions).union(board.victory.role_groups.values()))
        ),
        chat_group_ids=tuple(sorted(set(chat_group_ids))),
    )


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

        execution_raw = raw["execution"]
        if not isinstance(execution_raw, Mapping):
            raise _error("execution.yaml execution must be a mapping")
        execution_input = dict(execution_raw)
        if "player_field_values" in execution_input:
            if execution_input["player_field_values"] is not None:
                raise _error("player_field_values is derived from the frozen knowledge package")
            del execution_input["player_field_values"]
        if "window_metadata" in execution_input:
            window_metadata_input = execution_input["window_metadata"]
            if not isinstance(window_metadata_input, (list, tuple)) or window_metadata_input:
                raise _error("window_metadata is derived from the frozen board definition")
            del execution_input["window_metadata"]
        if "boundary_policy" in execution_input:
            if execution_input["boundary_policy"] is not None:
                raise _error("boundary_policy is derived from the frozen board definition")
            del execution_input["boundary_policy"]
        execution = ExecutionPackage.model_validate(execution_input)
        additions_raw = raw["action_definitions"]
        if not isinstance(additions_raw, Sequence) or isinstance(additions_raw, str | bytes):
            raise _error("execution.yaml action_definitions must be a list")
        additions = tuple(ActionDefinition.model_validate(item) for item in additions_raw)
        merged = merge_action_registries(load_action_registry(), additions)
    except ExecutionCompilerError:
        raise
    except (ActionRegistryCompilationError, TypeError, ValueError, ValidationError) as exc:
        raise _error(f"execution.yaml is invalid: {exc}") from exc

    if execution.board_id != package.board_ref.id:
        raise _error("execution.yaml board_id disagrees with the pinned board")
    if execution.board_version != package.board_ref.version:
        raise _error("execution.yaml board_version disagrees with the pinned board")
    window_metadata = execution_windows_from_board(package.board.model)
    window_ids = {window.window_id for window in window_metadata}
    has_player_field_changes = any(
        effect.effect_type == "PLAYER_FIELD_SET"
        for skill in execution.skills
        for effect in (*skill.effects, *skill.pass_effects)
    )
    if has_player_field_changes:
        board = package.board.model
        chat_group_ids = {
            role.model.team
            for role in package.roles.values()
            if getattr(
                role.model.team_visibility.channel, "value", role.model.team_visibility.channel
            )
            == "TEAM"
        }
        player_field_values = player_field_values_from_board(
            board,
            role_ids=tuple(package.roles),
            chat_group_ids=tuple(chat_group_ids),
        )
        execution = execution.model_copy(update={"player_field_values": player_field_values})
    has_window_features = (
        _requires_frozen_window_metadata(window_metadata)
        or bool(execution.window_settlement_groups)
        or _has_window_disclosure_hook(execution, window_ids)
        or any(
            skill.window_ids
            or skill.hook_ids
            or skill.trigger is not None
            or any(effect.effect_type == "FLOW" for effect in (*skill.effects, *skill.pass_effects))
            for skill in execution.skills
        )
    )
    boundary_policy: BoundaryPolicy | None = None
    if has_window_features:
        boundary_policy = boundary_policy_from_board(package.board.model)
        execution = execution.model_copy(
            update={
                "window_metadata": window_metadata,
                "boundary_policy": boundary_policy,
            }
        )
    validate_execution_package(
        execution,
        merged,
        available_windows=window_metadata,
        expected_boundary_policy=(boundary_policy if has_window_features else None),
        expected_player_field_values=(player_field_values if has_player_field_changes else None),
    )
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
    available_windows: Sequence[ExecutionWindow] | None = None,
    expected_boundary_policy: BoundaryPolicy | None = None,
    expected_player_field_values: PlayerFieldValues | None = None,
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

    try:
        validate_execution_actions(execution, registry)
        validate_package_expressions(execution)
    except ExecutionCompilerError:
        raise
    except (ActionRegistryCompilationError, TypeError, ValueError) as exc:
        raise _error(str(exc)) from exc
    window_ids = {item.window_id for item in available_windows or ()}
    supported_disclosure_hooks = (
        {phase.value for phase in GamePhase} | _DAY_SPEECH_HOOKS | window_ids
    )
    disclosures = tuple(
        disclosure for skill in execution.skills for disclosure in skill.disclosures
    ) + tuple(
        disclosure
        for interaction in execution.interactions
        for disclosure in interaction.disclosures
    )
    for disclosure in disclosures:
        if disclosure.hook is not None and disclosure.hook not in supported_disclosure_hooks:
            raise _error(f"disclosure {disclosure.disclosure_id!r} has an unsupported hook")
    has_player_field_changes = any(
        effect.effect_type == "PLAYER_FIELD_SET"
        for skill in execution.skills
        for effect in (*skill.effects, *skill.pass_effects)
    )
    if has_player_field_changes:
        if execution.player_field_values is None:
            raise _error("PLAYER_FIELD_SET requires frozen player field values")
        if (
            expected_player_field_values is not None
            and execution.player_field_values != expected_player_field_values
        ):
            raise _error("execution player field values disagree with frozen dependencies")
    elif execution.player_field_values is not None:
        raise _error("player field values require a PLAYER_FIELD_SET effect")
    if available_windows is not None:
        available_window_ids = tuple(item.window_id for item in available_windows)
        window_ids = set(available_window_ids)
        if len(window_ids) != len(available_window_ids):
            raise _error("frozen board window IDs must be unique")
        orders = tuple(item.order for item in available_windows)
        if orders != tuple(range(1, len(available_windows) + 1)):
            raise _error("frozen board window order must be contiguous starting at 1")
        position = {item.window_id: item.order for item in available_windows}
        for window in available_windows:
            for dependency in window.depends_on:
                if dependency not in position:
                    raise _error(
                        f"frozen board window {window.window_id!r} has an unknown dependency"
                    )
                if position[dependency] >= window.order:
                    raise _error(
                        f"frozen board window {window.window_id!r} has a cyclic or forward "
                        "dependency"
                    )
        for skill in execution.skills:
            unknown = sorted(set(skill.window_ids) - window_ids)
            if unknown:
                raise _error(
                    f"skill {skill.skill_id!r} references unknown board windows: "
                    + ", ".join(unknown)
                )
    elif (
        any(
            skill.window_ids
            or skill.hook_ids
            or skill.trigger is not None
            or any(effect.effect_type == "FLOW" for effect in (*skill.effects, *skill.pass_effects))
            for skill in execution.skills
        )
        or execution.window_settlement_groups
    ):
        raise _error("window bindings require the frozen board window context")
    requires_frozen_window_metadata = (
        available_windows is not None and _requires_frozen_window_metadata(available_windows)
    )
    if available_windows is not None and execution.window_metadata:
        if tuple(execution.window_metadata) != tuple(available_windows):
            raise _error("execution window metadata disagrees with the frozen board")
    elif available_windows is not None and (
        requires_frozen_window_metadata
        or (
            any(
                skill.window_ids
                or skill.hook_ids
                or skill.trigger is not None
                or any(
                    effect.effect_type == "FLOW" for effect in (*skill.effects, *skill.pass_effects)
                )
                for skill in execution.skills
            )
            or execution.window_settlement_groups
            or _has_window_disclosure_hook(execution, window_ids)
        )
    ):
        raise _error("B window features require frozen execution window metadata")
    has_window_features = (
        requires_frozen_window_metadata
        or bool(execution.window_settlement_groups)
        or _has_window_disclosure_hook(execution, window_ids)
        or any(
            skill.window_ids
            or skill.hook_ids
            or skill.trigger is not None
            or any(effect.effect_type == "FLOW" for effect in (*skill.effects, *skill.pass_effects))
            for skill in execution.skills
        )
    )
    if has_window_features and execution.boundary_policy is None:
        raise _error("B window features require a frozen boundary policy")
    if (
        expected_boundary_policy is not None
        and execution.boundary_policy != expected_boundary_policy
    ):
        raise _error("execution boundary policy disagrees with the frozen board")
    if expected_boundary_policy is None and execution.boundary_policy is not None:
        raise _error("execution boundary policy requires the frozen board context")
    if execution.window_settlement_groups:
        if available_windows is None:
            raise _error("window settlement groups require the frozen board window context")
        available_window_ids = tuple(item.window_id for item in available_windows)
        mapped_windows = set(execution.window_settlement_groups)
        if mapped_windows != set(available_window_ids):
            missing = sorted(set(available_window_ids) - mapped_windows)
            unknown = sorted(mapped_windows - set(available_window_ids))
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unknown:
                details.append("unknown " + ", ".join(unknown))
            raise _error("window settlement groups must cover board windows: " + "; ".join(details))
        if any(not group_id for group_id in execution.window_settlement_groups.values()):
            raise _error("window settlement group IDs must be non-empty")
        closed_groups: set[str] = set()
        previous_group: str | None = None
        for window_id in available_window_ids:
            group_id = execution.window_settlement_groups[window_id]
            if group_id != previous_group:
                if group_id in closed_groups:
                    raise _error("windows in one settlement group must be contiguous")
                if previous_group is not None:
                    closed_groups.add(previous_group)
                previous_group = group_id

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
