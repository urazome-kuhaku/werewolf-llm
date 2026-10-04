"""Explicit, version-pinned executable mapping for supported schema-1 boards.

This is the only module that knows the legacy role IDs and action codes. New
or changed boards must provide ``execution.yaml`` beside their published
board definition instead of extending game-specific handlers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING

from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.package_loader import KnowledgePackage
from werewolf.knowledge.role import (
    AbilityDefinition,
    RoleDefinition,
    TargetKind,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerType,
)

from .compiler import CompiledExecution, ExecutionCompilerError, validate_execution_package
from .models import (
    AbilityGrant,
    ActionSpec,
    BooleanExpr,
    CompareExpr,
    CostSpec,
    CountExpr,
    DisclosureSpec,
    EffectSpec,
    ExecutionPackage,
    Expr,
    InteractionRule,
    LiteralExpr,
    RefExpr,
    SelectorExpr,
    SkillSpec,
    TargetPolicy,
    UsagePolicy,
)

if TYPE_CHECKING:
    from werewolf.game.actions import ActionRegistry


CLASSIC_BOARD_ID = "classic_12_seer_witch_hunter_idiot"
CLASSIC_BOARD_VERSION = "1.0.0"
CLASSIC_COMPATIBILITY_ID = "classic-12-audited-2026-10-03"


def _classic_registry() -> ActionRegistry:
    # Keep game package initialization out of the knowledge compiler's import
    # path; legacy compilation only needs the immutable action model here.
    from werewolf.game.actions import ActionDefinition, ActionRegistry

    definitions = (
        ActionDefinition(
            action_code=101,
            action_name="WOLF_KILL",
            target_policy="alive_non_authorized_wolf",
            target_count=1,
        ),
        ActionDefinition(
            action_code=102,
            action_name="SEER_INSPECT",
            target_policy="other_alive",
            target_count=1,
        ),
        ActionDefinition(
            action_code=103,
            action_name="WITCH_POISON",
            target_policy="board_eligible",
            target_count=1,
            resource_id="witch_poison",
        ),
        ActionDefinition(
            action_code=104,
            action_name="WITCH_HEAL",
            target_policy="current_kill_not_self",
            target_count=1,
            resource_id="witch_heal",
        ),
        ActionDefinition(
            action_code=105,
            action_name="HUNTER_SHOOT",
            target_policy="other_alive",
            target_count=1,
        ),
        ActionDefinition(
            action_code=203,
            action_name="EXILE_RESOLVE",
            target_policy="candidate",
            target_count=1,
        ),
        ActionDefinition(
            action_code=106,
            action_name="GUARD_PROTECT",
            target_policy="board_eligible",
            target_count=1,
        ),
        ActionDefinition(
            action_code=107,
            action_name="SELF_SACRIFICE_TAKE",
            target_policy="self_and_other_alive",
            target_count=2,
        ),
        ActionDefinition(
            action_code=201,
            action_name="VOTE",
            target_policy="candidate",
            target_count=1,
        ),
        ActionDefinition(
            action_code=202, action_name="ABSTAIN", target_policy="none", target_count=0
        ),
        ActionDefinition(action_code=299, action_name="PASS", target_policy="none", target_count=0),
    )
    return ActionRegistry(actions=definitions)


def _ref(source: str, name: str) -> RefExpr:
    return RefExpr.model_validate({"op": "ref", "source": source, "name": name})


def _literal(value: object) -> LiteralExpr:
    return LiteralExpr.model_validate({"op": "literal", "value": value})


def _eq(left: Expr, right: Expr) -> CompareExpr:
    return CompareExpr.model_validate({"op": "eq", "left": left, "right": right})


def _contains(left: Expr, right: Expr) -> CompareExpr:
    return CompareExpr.model_validate({"op": "contains", "left": left, "right": right})


def _and(*values: Expr) -> BooleanExpr:
    return BooleanExpr.model_validate({"op": "and", "values": values})


def _or(*values: Expr) -> BooleanExpr:
    return BooleanExpr.model_validate({"op": "or", "values": values})


def _role_selector(role_id: str) -> SelectorExpr:
    return SelectorExpr(
        op="select",
        source="players",
        where=_eq(_ref("item", "role_id"), _literal(role_id)),
        map=_ref("item", "seat"),
    )


def _all_players_selector() -> SelectorExpr:
    return SelectorExpr(
        op="select",
        source="players",
        where=_eq(_ref("item", "alive"), _literal(True)),
        map=_ref("item", "seat"),
    )


def _grant(role_id: str, *, grant_id: str | None = None) -> AbilityGrant:
    return AbilityGrant(
        grant_id=grant_id or f"role:{role_id}",
        actor_selector=_role_selector(role_id),
    )


def _actor_target() -> Expr:
    return _ref("actor", "seat")


def _request_target() -> Expr:
    return _ref("target", "seat")


def _living_other_targets() -> SelectorExpr:
    return SelectorExpr(
        op="select",
        source="players",
        where=_and(
            _eq(_ref("item", "alive"), _literal(True)),
            CompareExpr(op="ne", left=_ref("item", "seat"), right=_ref("actor", "seat")),
        ),
        map=_ref("item", "seat"),
    )


def _target_policy(selector: SelectorExpr) -> TargetPolicy:
    return TargetPolicy(min_targets=1, max_targets=1, selector=selector, allow_self=False)


def _exile_target() -> TargetPolicy:
    return TargetPolicy(
        min_targets=1,
        max_targets=1,
        selector=_all_players_selector(),
        allow_self=True,
    )


def _effect(
    effect_id: str,
    effect_type: str,
    *,
    target: object | None = None,
    tags: tuple[str, ...] = (),
    fact_type: str | None = None,
    value: object | None = None,
) -> EffectSpec:
    payload: dict[str, object] = {
        "effect_id": effect_id,
        "effect_type": effect_type,
        "target": target,
        "tags": tags,
    }
    if fact_type is not None:
        payload["fact_type"] = fact_type
    if value is not None:
        payload["value"] = value
    return EffectSpec.model_validate(payload)


def _ability_by_code(
    roles: Mapping[str, RoleDefinition], role_id: str, action_code: int
) -> AbilityDefinition:
    role = roles[role_id]
    for ability in role.abilities:
        if ability.action_code == action_code:
            return ability
    raise ExecutionCompilerError(
        f"legacy compatibility package is missing {role_id!r} action {action_code}"
    )


def _validate_audited_board(
    board: BoardDefinition,
    roles: Mapping[str, RoleDefinition],
) -> None:
    if board.board_id != CLASSIC_BOARD_ID or board.version != CLASSIC_BOARD_VERSION:
        raise ExecutionCompilerError(
            "no executable declaration or explicit compatibility mapping exists for "
            f"{board.board_id}@{board.version}"
        )
    expected_counts = {
        "wolf": 4,
        "villager": 4,
        "seer": 1,
        "witch": 1,
        "hunter": 1,
        "idiot": 1,
    }
    actual_counts = {binding.role_ref.id: binding.count for binding in board.role_bindings}
    if actual_counts != expected_counts:
        raise ExecutionCompilerError("published classic board role composition changed")
    if set(roles) != set(expected_counts):
        raise ExecutionCompilerError("published classic board role dependency set changed")

    witch_rules = dict(
        next(item for item in board.role_bindings if item.role_ref.id == "witch").effective_rules
    )
    expected_witch_rules = {
        "can_self_heal": False,
        "potions_per_night": 1,
        "heal_potion_count": 1,
        "poison_potion_count": 1,
        "knows_wolf_target": True,
    }
    if witch_rules != expected_witch_rules:
        raise ExecutionCompilerError("published classic witch rules changed")
    if not board.knife_rule.plan_confirmation_required:
        raise ExecutionCompilerError("published classic wolf plan confirmation is not enabled")
    if board.knife_rule.selection_mode != "consensus":
        raise ExecutionCompilerError("published classic wolf selection mode changed")
    if board.knife_rule.target_visibility != "wolf_team":
        raise ExecutionCompilerError("published classic wolf target visibility changed")

    expected_interactions = {
        "hunter-death-trigger",
        "idiot-exile",
        "witch-poison-hunter",
    }
    if {reference.id for reference in board.interaction_refs} != expected_interactions:
        raise ExecutionCompilerError("published classic interaction dependencies changed")
    _validate_legacy_abilities(roles)


def _validate_legacy_abilities(roles: Mapping[str, RoleDefinition]) -> None:
    """Pin each compatibility conversion to the effective 1.0.0 role rules."""

    expected = {
        ("wolf", 101): ("kill", TriggerType.ACTIVE, "NIGHT_ACTION"),
        ("seer", 102): ("inspect", TriggerType.ACTIVE, "NIGHT_ACTION"),
        ("witch", 103): ("poison", TriggerType.ACTIVE, "NIGHT_ACTION"),
        ("witch", 104): ("heal", TriggerType.ACTIVE, "NIGHT_ACTION"),
        ("hunter", 105): ("shoot", TriggerType.DEATH_TRIGGER, "TRIGGER_ACTION"),
        ("idiot", 0): ("reveal_on_exile", TriggerType.PASSIVE, "DAY_RESOLVE"),
    }
    abilities: dict[tuple[str, int], AbilityDefinition] = {}
    for role_id, role in roles.items():
        for ability in role.abilities:
            abilities[(role_id, ability.action_code)] = ability
    if set(abilities) != set(expected):
        raise ExecutionCompilerError("published classic role abilities changed")
    for key, (ability_id, trigger_type, timing) in expected.items():
        role_id = key[0]
        ability = abilities[key]
        if (
            ability.ability_id != ability_id
            or ability.trigger_type is not trigger_type
            or ability.timing.value != timing
            or (
                ability.target_rule.kind
                is not (TargetKind.NONE if role_id == "idiot" else TargetKind.PLAYER)
            )
        ):
            raise ExecutionCompilerError("published classic role ability contract changed")
        if role_id == "idiot":
            trigger = ability.trigger
            if (
                trigger is None
                or trigger.event is not TriggerEvent.EXILE_SELECTED
                or trigger.mode is not TriggerMode.AUTOMATIC
                or set(trigger.effects)
                != {
                    TriggerEffect.REVEAL_ROLE,
                    TriggerEffect.SURVIVE_TRIGGER,
                    TriggerEffect.REMOVE_VOTE_RIGHT,
                    TriggerEffect.RETAIN_SPEECH,
                }
            ):
                raise ExecutionCompilerError("published classic idiot replacement changed")
        elif role_id == "hunter":
            trigger = ability.trigger
            if (
                trigger is None
                or trigger.event is not TriggerEvent.DEATH_CONFIRMED
                or trigger.mode is not TriggerMode.PLAYER_CHOICE
                or set(trigger.allowed_death_causes) != {"wolf_kill", "exiled"}
                or not trigger.allow_pass
            ):
                raise ExecutionCompilerError("published classic hunter trigger changed")
        elif role_id == "witch":
            expected_resource = "witch_poison" if ability.action_code == 103 else "witch_heal"
            if (
                ability.resource is None
                or ability.resource.resource_id != expected_resource
                or ability.resource.initial_amount != 1
                or ability.resource.cost_per_use != 1
                or ability.target_rule.allow_self
            ):
                raise ExecutionCompilerError("published classic witch resources changed")
        elif ability.target_rule.kind is not TargetKind.PLAYER:
            raise ExecutionCompilerError("published classic active target contract changed")


def _one_potion_per_night_condition() -> Expr:
    same_round = _eq(_ref("item", "round_number"), _ref("observation", "round_number"))
    is_witch_potion = CompareExpr(
        op="in",
        left=_ref("item", "skill_id"),
        right=_literal(["witch_heal", "witch_poison"]),
    )
    uses = CountExpr(
        selector=SelectorExpr(
            op="select",
            source="ledger",
            where=_and(same_round, is_witch_potion),
            map=_ref("item", "skill_id"),
        )
    )
    return _eq(uses, _literal(0))


def _fact_for_actor(cause: str) -> CountExpr:
    return CountExpr(
        selector=SelectorExpr(
            op="select",
            source="facts",
            where=_and(
                _eq(_ref("item", "fact_type"), _literal("death_confirmed")),
                _eq(_ref("item", "target_seat"), _ref("actor", "seat")),
                _contains(_ref("item", "tags"), _literal(cause)),
            ),
            map=_ref("item", "target_seat"),
        )
    )


def _hunter_condition() -> Expr:
    return _or(
        CompareExpr(op="gt", left=_fact_for_actor("wolf_kill"), right=_literal(0)),
        CompareExpr(op="gt", left=_fact_for_actor("exiled"), right=_literal(0)),
    )


def _exile_on_idiot_condition() -> Expr:
    already_revealed = CountExpr(
        selector=SelectorExpr(
            op="select",
            source="facts",
            where=_and(
                _eq(_ref("item", "fact_type"), _literal("idiot_revealed")),
                _eq(_ref("item", "target_seat"), _ref("target", "seat")),
            ),
            map=_ref("item", "target_seat"),
        )
    )
    return _and(
        _contains(_ref("item", "tags"), _literal("exiled")),
        _eq(_ref("target", "role_id"), _literal("idiot")),
        _eq(already_revealed, _literal(0)),
    )


def compile_legacy_execution(package: KnowledgePackage) -> CompiledExecution:
    """Compile the audited classic schema-1 source package into a typed plan."""

    roles = {role_id: document.model for role_id, document in package.roles.items()}
    return compile_legacy_execution_from_models(package.board.model, roles)


def compile_legacy_execution_from_models(
    board: BoardDefinition,
    roles: Mapping[str, RoleDefinition],
) -> CompiledExecution:
    """Restore the same pinned compatibility mapping from a detached package."""

    _validate_audited_board(board, roles)
    for role_id, action_code in (
        ("wolf", 101),
        ("seer", 102),
        ("witch", 103),
        ("witch", 104),
        ("hunter", 105),
    ):
        _ability_by_code(roles, role_id, action_code)

    wolf_attack = _effect(
        "wolf_damage",
        "DAMAGE",
        target=_request_target(),
        tags=("wolf_attack",),
        fact_type="wolf_attack_proposed",
    )
    seer_result = _effect(
        "seer_inspection",
        "FACT",
        target=_request_target(),
        fact_type="inspection_result",
        value=_ref("target", "faction_id"),
    )
    heal_effect = _effect(
        "witch_heal",
        "HEAL",
        target=_request_target(),
        tags=("witch_heal",),
    )
    poison_effect = _effect(
        "witch_poison",
        "DAMAGE",
        target=_request_target(),
        tags=("witch_poison",),
    )
    hunter_shot = _effect(
        "hunter_shot",
        "DAMAGE",
        target=_request_target(),
        tags=("hunter_shot",),
    )

    skills = (
        SkillSpec(
            skill_id="wolf_kill",
            action_code=101,
            coordination_scope="CHAT_GROUP",
            grants=(_grant("wolf", grant_id="wolf_team_shared"),),
            timing=("NIGHT_ACTION",),
            targets=_target_policy(
                SelectorExpr(
                    op="select",
                    source="players",
                    where=_and(
                        _eq(_ref("item", "alive"), _literal(True)),
                        _eq(_ref("item", "faction_id"), _literal("good")),
                    ),
                    map=_ref("item", "seat"),
                )
            ),
            usage=UsagePolicy(max_uses=1, scope="ROUND"),
            effects=(wolf_attack,),
            disclosures=(
                DisclosureSpec(
                    disclosure_id="wolf_target_to_witch",
                    audience="SEATS",
                    values={"target_seat": _ref("target", "seat")},
                    condition=_eq(_ref("request", "passed"), _literal(False)),
                    recipients=SelectorExpr(
                        op="select",
                        source="players",
                        where=_eq(_ref("item", "role_id"), _literal("witch")),
                        map=_ref("item", "seat"),
                    ),
                    hook="NIGHT_ACTION",
                    event_type="wolf_attack_proposed",
                ),
                DisclosureSpec(
                    disclosure_id="wolf_pass_notice_to_witch",
                    audience="SEATS",
                    values={},
                    condition=_eq(_ref("request", "passed"), _literal(True)),
                    recipients=SelectorExpr(
                        op="select",
                        source="players",
                        where=_eq(_ref("item", "role_id"), _literal("witch")),
                        map=_ref("item", "seat"),
                    ),
                    hook="NIGHT_ACTION",
                    event_type="wolf_attack_none",
                ),
            ),
        ),
        SkillSpec(
            skill_id="seer_inspect",
            action_code=102,
            grants=(_grant("seer", grant_id="seer_inspect"),),
            timing=("NIGHT_ACTION",),
            targets=_target_policy(_living_other_targets()),
            usage=UsagePolicy(max_uses=None),
            effects=(seer_result,),
            disclosures=(
                DisclosureSpec(
                    disclosure_id="seer_private_result",
                    audience="SELF",
                    values={
                        "target_seat": _ref("target", "seat"),
                        "faction_id": _ref("target", "faction_id"),
                    },
                    hook="NIGHT_RESOLVE",
                    event_type="inspection_result",
                ),
            ),
        ),
        SkillSpec(
            skill_id="witch_heal",
            action_code=104,
            after_skills=("wolf_kill",),
            grants=(_grant("witch", grant_id="witch_heal"),),
            timing=("NIGHT_ACTION",),
            condition=_one_potion_per_night_condition(),
            targets=_target_policy(
                SelectorExpr(
                    op="select",
                    source="facts",
                    where=_and(
                        _eq(_ref("item", "fact_type"), _literal("wolf_attack_proposed")),
                        CompareExpr(
                            op="ne",
                            left=_ref("item", "target_seat"),
                            right=_ref("actor", "seat"),
                        ),
                    ),
                    map=_ref("item", "target_seat"),
                )
            ),
            usage=UsagePolicy(
                max_uses=1,
                scope="GAME",
                costs=(CostSpec(resource_id="witch_heal", amount=1),),
            ),
            effects=(heal_effect,),
        ),
        SkillSpec(
            skill_id="witch_poison",
            action_code=103,
            after_skills=("wolf_kill",),
            grants=(_grant("witch", grant_id="witch_poison"),),
            timing=("NIGHT_ACTION",),
            condition=_one_potion_per_night_condition(),
            targets=_target_policy(_living_other_targets()),
            usage=UsagePolicy(
                max_uses=1,
                scope="GAME",
                costs=(CostSpec(resource_id="witch_poison", amount=1),),
            ),
            effects=(poison_effect,),
        ),
        SkillSpec(
            skill_id="hunter_shoot",
            action_code=105,
            grants=(_grant("hunter", grant_id="hunter_shoot"),),
            timing=("TRIGGER_ACTION",),
            condition=_hunter_condition(),
            targets=_target_policy(_living_other_targets()),
            usage=UsagePolicy(max_uses=1, scope="GAME", pass_records=True),
            effects=(hunter_shot,),
        ),
        SkillSpec(
            skill_id="exile_resolution",
            action_code=203,
            mode="HOST",
            grants=(),
            timing=("DAY_RESOLVE",),
            targets=_exile_target(),
            usage=UsagePolicy(scope="ROUND"),
            effects=(
                _effect(
                    "exile_damage",
                    "DAMAGE",
                    target=_request_target(),
                    tags=("exiled",),
                    fact_type="exile_selected",
                ),
            ),
        ),
        SkillSpec(
            skill_id="pass",
            action_code=299,
            grants=(AbilityGrant(grant_id="generic_pass", actor_selector=_all_players_selector()),),
            timing=("NIGHT_ACTION", "TRIGGER_ACTION"),
            targets=TargetPolicy(
                min_targets=0,
                max_targets=0,
                selector=SelectorExpr(
                    op="select",
                    source="request_targets",
                    map=_ref("item", "seat"),
                ),
                allow_self=False,
            ),
            usage=UsagePolicy(scope="ROUND", pass_records=True),
        ),
    )

    interactions = (
        InteractionRule(
            interaction_id="classic_witch_heal_blocks_wolf_attack",
            rule_type="CANCEL_DAMAGE_HEAL",
            priority=100,
            damage_tags=("wolf_attack",),
            counter_tags=("witch_heal",),
        ),
        InteractionRule(
            interaction_id="classic_idiot_exile_replacement",
            rule_type="REPLACE_DEATH",
            priority=100,
            when=_exile_on_idiot_condition(),
            damage_tags=("exiled",),
            effects=(
                _effect(
                    "idiot_loses_vote_right",
                    "SET_CAN_VOTE",
                    target=_ref("target", "seat"),
                    value=_literal(False),
                ),
                _effect(
                    "idiot_consumes_reveal_on_exile",
                    "CONSUME_ABILITY",
                    target=_ref("target", "seat"),
                    value=_literal("reveal_on_exile"),
                ),
                _effect(
                    "idiot_reveal_fact",
                    "FACT",
                    target=_ref("target", "seat"),
                    fact_type="idiot_revealed",
                    value=_ref("target", "role_id"),
                ),
            ),
            disclosures=(
                DisclosureSpec(
                    disclosure_id="idiot_reveal_public",
                    audience="ALL",
                    values={"role_id": _literal("idiot")},
                    hook="DAY_RESOLVE",
                    event_type="idiot_revealed",
                ),
            ),
        ),
        InteractionRule(
            interaction_id="classic_confirm_wolf_death",
            rule_type="CONFIRM_DEATH",
            priority=100,
            damage_tags=("wolf_attack",),
            death_cause="wolf_kill",
        ),
        InteractionRule(
            interaction_id="classic_confirm_poison_death",
            rule_type="CONFIRM_DEATH",
            priority=100,
            damage_tags=("witch_poison",),
            death_cause="witch_poison",
        ),
        InteractionRule(
            interaction_id="classic_confirm_hunter_death",
            rule_type="CONFIRM_DEATH",
            priority=100,
            damage_tags=("hunter_shot",),
            death_cause="hunter_shot",
        ),
        InteractionRule(
            interaction_id="classic_confirm_exile_death",
            rule_type="CONFIRM_DEATH",
            priority=100,
            damage_tags=("exiled",),
            death_cause="exiled",
        ),
        InteractionRule(
            interaction_id="classic_final_death_fact",
            rule_type="EMIT_POST_DEATH_FACT",
            priority=0,
            fact_type="death_confirmed",
        ),
    )

    state_declarations = ()
    execution = ExecutionPackage(
        board_id=CLASSIC_BOARD_ID,
        board_version=CLASSIC_BOARD_VERSION,
        actions=(
            ActionSpec(action_code=101, action_id="WOLF_KILL", allow_pass=True),
            ActionSpec(action_code=102, action_id="SEER_INSPECT", allow_pass=True),
            ActionSpec(action_code=103, action_id="WITCH_POISON", allow_pass=True),
            ActionSpec(action_code=104, action_id="WITCH_HEAL", allow_pass=True),
            ActionSpec(action_code=105, action_id="HUNTER_SHOOT", allow_pass=True),
            ActionSpec(action_code=203, action_id="EXILE_RESOLVE"),
            ActionSpec(action_code=299, action_id="PASS", allow_pass=True),
        ),
        skills=skills,
        state_declarations=state_declarations,
        interactions=interactions,
    )
    registry = _classic_registry()
    validate_execution_package(execution, registry)

    source = f"compat:{CLASSIC_COMPATIBILITY_ID}"
    source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return CompiledExecution(
        execution=execution,
        action_registry=registry,
        source=source,
        source_sha256=source_digest,
    )


__all__ = [
    "CLASSIC_COMPATIBILITY_ID",
    "compile_legacy_execution",
    "compile_legacy_execution_from_models",
]
