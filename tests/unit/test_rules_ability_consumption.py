"""Explicit ability-consumption effects in the generic rules language."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from werewolf.knowledge.package_loader import KnowledgePackage, KnowledgePackageLoader
from werewolf.rules.compat import compile_legacy_execution
from werewolf.rules.interpreter import RuleInterpreter
from werewolf.rules.models import (
    AbilityGrant,
    AbilityInstance,
    ActionSpec,
    CompareExpr,
    EffectSpec,
    ExecutionPackage,
    InteractionRule,
    LiteralExpr,
    PlayerObservation,
    RefExpr,
    RuleObservation,
    SelectorExpr,
    SkillRequest,
    SkillSpec,
    TargetPolicy,
    UsagePolicy,
)
from werewolf.rules.predicates import validate_package_expressions

PROJECT_ROOT = Path(__file__).parents[2]


def _literal(value: object) -> LiteralExpr:
    return LiteralExpr.model_validate({"op": "literal", "value": value})


def _ref(source: str, name: str) -> RefExpr:
    return RefExpr.model_validate({"op": "ref", "source": source, "name": name})


def _skill() -> SkillSpec:
    return SkillSpec(
        skill_id="exile",
        action_code=1,
        grants=(
            AbilityGrant(
                grant_id="exile-grant",
                actor_selector=SelectorExpr(source="players", map=_ref("item", "seat")),
            ),
        ),
        timing=("DAY_RESOLVE",),
        targets=TargetPolicy(
            min_targets=1,
            max_targets=1,
            selector=SelectorExpr(source="players", map=_ref("item", "seat")),
        ),
        usage=UsagePolicy(),
        effects=(
            EffectSpec(
                effect_id="exile-damage",
                effect_type="DAMAGE",
                target=_ref("target", "seat"),
                tags=("exiled",),
            ),
        ),
    )


def _interaction_effect(effect_id: str, *, target: RefExpr | None = None, value: object):
    return EffectSpec(
        effect_id=effect_id,
        effect_type="CONSUME_ABILITY",
        target=target or _ref("target", "seat"),
        value=value,
    )


def _package(consumption_effects: tuple[EffectSpec, ...]) -> ExecutionPackage:
    skill = _skill()
    return ExecutionPackage(
        board_id="ability-consumption-test",
        board_version="1.0.0",
        actions=(ActionSpec(action_code=1, action_id="EXILE"),),
        skills=(skill,),
        interactions=(
            InteractionRule(
                interaction_id="replace-exile",
                rule_type="REPLACE_DEATH",
                damage_tags=("exiled",),
                when=CompareExpr(
                    op="eq",
                    left=_ref("target", "role_id"),
                    right=_literal("idiot"),
                ),
                effects=consumption_effects,
            ),
            InteractionRule(
                interaction_id="confirm-exile",
                rule_type="CONFIRM_DEATH",
                damage_tags=("exiled",),
                death_cause="exile",
            ),
        ),
    )


def _observation(package: ExecutionPackage) -> RuleObservation:
    skill = package.skills[0]
    return RuleObservation(
        board_id=package.board_id,
        board_version=package.board_version,
        revision=4,
        round_number=1,
        timing="DAY_RESOLVE",
        group_id="exile-window",
        players=(
            PlayerObservation(seat=1, role_id="voter", faction_id="good"),
            PlayerObservation(seat=2, role_id="idiot", faction_id="good"),
        ),
        ability_instances=(
            AbilityInstance(
                ability_instance_id="exile-instance",
                skill_id=skill.skill_id,
                actor_seat=1,
                grant_id=skill.grants[0].grant_id,
            ),
        ),
    )


def _request() -> SkillRequest:
    return SkillRequest(
        request_id="exile-request",
        ability_instance_id="exile-instance",
        skill_id="exile",
        action_code=1,
        actor_seat=1,
        targets=(2,),
    )


def test_interaction_emits_typed_ability_consumption_for_authorized_target() -> None:
    package = _package(
        (
            _interaction_effect(
                "consume-reveal",
                value=_literal("reveal_on_exile"),
            ),
        )
    )

    batch = RuleInterpreter().plan(package, _observation(package), (_request(),))

    effects = [item for item in batch.effects if item.effect_type == "CONSUME_ABILITY"]
    assert len(effects) == 1
    assert effects[0].target_seat == 2
    assert effects[0].value == "reveal_on_exile"
    assert effects[0].applied is True
    assert effects[0].source_rule_id == "replace-exile"


@pytest.mark.parametrize(
    ("effect", "message"),
    [
        (
            _interaction_effect("bad-value", value=_literal(17)),
            "ability ID value must be str",
        ),
        (
            EffectSpec(
                effect_id="missing-target",
                effect_type="CONSUME_ABILITY",
                value=_literal("reveal_on_exile"),
            ),
            "target must be a seat",
        ),
    ],
)
def test_compiler_rejects_invalid_ability_consumption_declarations(
    effect: EffectSpec, message: str
) -> None:
    package = _package((effect,))

    with pytest.raises(ValueError, match=message):
        validate_package_expressions(package)


def test_interaction_cannot_consume_an_ability_from_an_unauthorized_seat() -> None:
    package = _package(
        (
            _interaction_effect(
                "consume-actor",
                target=_ref("actor", "seat"),
                value=_literal("reveal_on_exile"),
            ),
        )
    )

    with pytest.raises(ValueError, match="not authorized by its source"):
        RuleInterpreter().plan(package, _observation(package), (_request(),))


def test_duplicate_consumption_of_the_same_seat_and_ability_is_rejected() -> None:
    package = _package(
        (
            _interaction_effect("consume-first", value=_literal("reveal_on_exile")),
            _interaction_effect("consume-second", value=_literal("reveal_on_exile")),
        )
    )

    with pytest.raises(ValueError, match="duplicate CONSUME_ABILITY"):
        RuleInterpreter().plan(package, _observation(package), (_request(),))


def _classic_execution() -> ExecutionPackage:
    async def load_package() -> KnowledgePackage:
        loader = KnowledgePackageLoader(PROJECT_ROOT / "vault" / "published")
        return await loader.load("classic_12_seer_witch_hunter_idiot@1.0.0")

    return compile_legacy_execution(asyncio.run(load_package())).execution


def test_classic_consumes_the_pinned_trigger_id_and_changes_new_execution_digest() -> None:
    execution = _classic_execution()
    replacement = next(
        item
        for item in execution.interactions
        if item.interaction_id == "classic_idiot_exile_replacement"
    )
    consumption = next(
        item for item in replacement.effects if item.effect_type == "CONSUME_ABILITY"
    )
    assert consumption.target == _ref("target", "seat")
    assert consumption.value == _literal("reveal_on_exile")

    previous_effects = tuple(
        item for item in replacement.effects if item.effect_type != "CONSUME_ABILITY"
    )
    previous_replacement = replacement.model_copy(update={"effects": previous_effects})
    previous_interactions = tuple(
        previous_replacement if item is replacement else item for item in execution.interactions
    )
    previous_execution = execution.model_copy(update={"interactions": previous_interactions})
    assert previous_execution.package_id != execution.package_id


def test_classic_wolf_target_selector_uses_runtime_team_id() -> None:
    execution = _classic_execution()
    wolf = next(item for item in execution.skills if item.skill_id == "wolf_kill")
    observation = RuleObservation(
        board_id=execution.board_id,
        board_version=execution.board_version,
        revision=1,
        round_number=1,
        timing="NIGHT_ACTION",
        group_id="wolf-chat",
        players=(
            PlayerObservation(
                seat=1,
                role_id="wolf",
                faction_id="wolf",
                chat_group_ids=("wolf",),
            ),
            PlayerObservation(seat=2, role_id="villager", faction_id="good"),
        ),
        ability_instances=(
            AbilityInstance(
                ability_instance_id="wolf-instance",
                skill_id=wolf.skill_id,
                actor_seat=1,
                grant_id=wolf.grants[0].grant_id,
            ),
        ),
    )
    request = SkillRequest(
        request_id="wolf-request",
        ability_instance_id="wolf-instance",
        skill_id=wolf.skill_id,
        action_code=wolf.action_code,
        actor_seat=1,
        targets=(2,),
    )

    batch = RuleInterpreter().plan(execution, observation, (request,))

    assert batch.dispositions[0].status == "ACCEPTED"
