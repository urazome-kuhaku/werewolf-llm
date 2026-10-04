"""Behavioral checks for the generic, data-driven rules kernel."""

from __future__ import annotations

import warnings
from typing import cast

import pytest
from pydantic import TypeAdapter

from werewolf.rules.interpreter import RuleInterpreter
from werewolf.rules.models import (
    AbilityGrant,
    AbilityInstance,
    ActionSpec,
    BooleanExpr,
    CompareExpr,
    CostSpec,
    CountExpr,
    DisclosureSpec,
    DomainFact,
    EffectSpec,
    ExecutionPackage,
    Expr,
    InteractionRule,
    LiteralExpr,
    MapExpr,
    ParameterSpec,
    PlayerObservation,
    RefExpr,
    RuleObservation,
    SelectorExpr,
    SkillRequest,
    SkillSpec,
    SkillUseRecord,
    StateDeclaration,
    TargetPolicy,
    UsagePolicy,
)
from werewolf.rules.predicates import (
    evaluate_expr,
    infer_expr_type,
    validate_package_expressions,
)
from werewolf.rules.selectors import select_seats

BOARD_ID = "rules-kernel-test"
BOARD_VERSION = "1.0.0"


def _literal(value: object) -> LiteralExpr:
    return LiteralExpr.model_validate({"op": "literal", "value": value})


def _ref(source: str, name: str) -> RefExpr:
    return RefExpr.model_validate({"op": "ref", "source": source, "name": name})


def _compare(op: str, left: Expr, right: Expr) -> CompareExpr:
    return CompareExpr.model_validate({"op": op, "left": left, "right": right})


def _players(*, alive_only: bool = False) -> SelectorExpr:
    return SelectorExpr(
        source="players",
        where=(_compare("eq", _ref("item", "alive"), _literal(True)) if alive_only else None),
        map=_ref("item", "seat"),
    )


def _skill(
    skill_id: str,
    action_code: int,
    *,
    effects: tuple[EffectSpec, ...] = (),
    pass_effects: tuple[EffectSpec, ...] = (),
    condition: Expr | None = None,
    usage: UsagePolicy | None = None,
    disclosures: tuple[DisclosureSpec, ...] = (),
    min_targets: int = 0,
    max_targets: int = 1,
    parameters: tuple[ParameterSpec, ...] = (),
) -> SkillSpec:
    return SkillSpec(
        skill_id=skill_id,
        action_code=action_code,
        grants=(
            AbilityGrant(grant_id=f"{skill_id}-grant", actor_selector=_players(alive_only=True)),
        ),
        timing=(),
        condition=condition,
        targets=TargetPolicy(
            min_targets=min_targets,
            max_targets=max_targets,
            selector=_players(alive_only=True),
            allow_self=True,
        ),
        usage=usage or UsagePolicy(),
        effects=effects,
        pass_effects=pass_effects,
        disclosures=disclosures,
        parameters=parameters,
    )


def _package(
    skills: tuple[SkillSpec, ...],
    *,
    interactions: tuple[InteractionRule, ...] = (),
    state_declarations: tuple[StateDeclaration, ...] = (),
) -> ExecutionPackage:
    return ExecutionPackage(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        actions=tuple(
            ActionSpec(
                action_code=skill.action_code,
                action_id=f"ACTION_{skill.action_code}",
                allow_pass=True,
            )
            for skill in skills
        ),
        skills=skills,
        state_declarations=state_declarations,
        interactions=interactions,
    )


def _observation(
    skills: tuple[SkillSpec, ...],
    *,
    players: tuple[PlayerObservation, ...] | None = None,
    facts: tuple[DomainFact, ...] = (),
    ledger: tuple[SkillUseRecord, ...] = (),
    resources: dict[int, dict[str, int]] | None = None,
    revision: int = 7,
    round_number: int = 2,
    timing: str = "test.window",
) -> RuleObservation:
    if players is None:
        resources = resources or {}
        players = tuple(
            PlayerObservation(
                seat=seat,
                alive=True,
                role_id="wolf" if seat == 1 else "villager",
                faction_id="WOLF" if seat == 1 else "GOOD",
                chat_group_ids=("wolves",) if seat in {1, 2} else (),
                resources=resources.get(seat, {}),
            )
            for seat in range(1, 9)
        )
    instances = tuple(
        AbilityInstance(
            ability_instance_id=f"ability-{skill.action_code}-{seat}",
            skill_id=skill.skill_id,
            actor_seat=seat,
            grant_id=skill.grants[0].grant_id,
        )
        for skill in skills
        for seat in range(1, 9)
    )
    return RuleObservation(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        revision=revision,
        round_number=round_number,
        players=players,
        facts=facts,
        ledger=ledger,
        ability_instances=instances,
        group_id="simultaneous-group",
        timing=timing,
    )


def _request(
    skill: SkillSpec, *, actor: int, targets: tuple[int, ...], request_id: str
) -> SkillRequest:
    return SkillRequest(
        request_id=request_id,
        ability_instance_id=f"ability-{skill.action_code}-{actor}",
        skill_id=skill.skill_id,
        action_code=skill.action_code,
        actor_seat=actor,
        targets=targets,
    )


def _damage_skill(skill_id: str, action_code: int, cause_tag: str) -> SkillSpec:
    return _skill(
        skill_id,
        action_code,
        min_targets=1,
        effects=(
            EffectSpec(
                effect_id=f"{skill_id}-damage",
                effect_type="DAMAGE",
                target=_ref("target", "seat"),
                tags=(cause_tag,),
            ),
        ),
    )


def test_typed_ast_parses_evaluates_maps_and_checks_item_source() -> None:
    raw: object = {
        "op": "contains",
        "left": {
            "op": "map",
            "selector": {
                "op": "select",
                "source": "players",
                "where": {
                    "op": "eq",
                    "left": {"op": "ref", "source": "item", "name": "alive"},
                    "right": {"op": "literal", "value": True},
                },
            },
            "value": {"op": "ref", "source": "item", "name": "seat"},
        },
        "right": {"op": "literal", "value": 3},
    }
    expression = TypeAdapter(Expr).validate_python(raw)
    skill = _skill("selection", 1, condition=expression)
    package = _package((skill,))
    observation = _observation(
        (skill,),
        players=(
            PlayerObservation(seat=1, alive=True),
            PlayerObservation(seat=2, alive=False),
            PlayerObservation(seat=3, alive=True),
        ),
    )

    assert isinstance(expression, CompareExpr)
    assert evaluate_expr(expression, {"observation": observation}) is True
    assert (
        infer_expr_type(
            cast(MapExpr, expression.left),
            skill=skill,
            state_declarations=package.state_declarations,
        )
        == "seat_list"
    )
    assert select_seats(_players(alive_only=True), {"observation": observation}) == (1, 3)
    validate_package_expressions(package)


def test_typed_ast_rejects_unknown_and_wrong_source_references() -> None:
    unknown = _skill(
        "unknown-reference",
        1,
        condition=_compare("eq", _ref("target", "attribute:secret"), _literal(True)),
    )
    with pytest.raises(ValueError, match="unknown or untyped reference"):
        validate_package_expressions(_package((unknown,)))

    wrong_item = _skill(
        "wrong-item-source",
        2,
        condition=CompareExpr(
            op="eq",
            left=MapExpr(
                selector=SelectorExpr(
                    source="facts",
                    map=_ref("item", "seat"),
                ),
                value=_ref("item", "seat"),
            ),
            right=_literal([]),
        ),
    )
    with pytest.raises(ValueError, match="unknown or untyped reference"):
        validate_package_expressions(_package((wrong_item,)))

    mismatched = _skill(
        "type-mismatch",
        3,
        condition=_compare("eq", _ref("actor", "seat"), _ref("actor", "alive")),
    )
    with pytest.raises(ValueError, match="equality comparison requires compatible types"):
        validate_package_expressions(_package((mismatched,)))


def test_evaluator_shares_the_budget_across_comparisons_and_selectors() -> None:
    expression: Expr = _literal(True)
    for _ in range(40):
        expression = CompareExpr(op="eq", left=expression, right=_literal(True))

    with pytest.raises(ValueError, match="execution budget"):
        evaluate_expr(expression, {})

    nested_selector_expr: Expr = _literal(True)
    for _ in range(40):
        nested_selector_expr = BooleanExpr(op="not", values=(nested_selector_expr,))
    selector = SelectorExpr(source="players", where=nested_selector_expr)
    observation = RuleObservation(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        revision=0,
        round_number=0,
        players=(PlayerObservation(seat=1),),
    )
    with pytest.raises(ValueError, match="execution budget"):
        select_seats(selector, {"observation": observation})


def test_observation_sequences_are_immutable_and_json_round_trip_without_warnings() -> None:
    observation = RuleObservation(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        revision=1,
        round_number=1,
        players=(
            PlayerObservation(
                seat=1,
                attributes={"labels": ["first", "second"], "details": {"rank": 2}},
            ),
        ),
    )
    with pytest.raises(TypeError):
        observation.players[0].attributes["labels"].append("third")  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        observation.players[0].attributes["details"]["rank"] = 4  # type: ignore[index]

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        restored = RuleObservation.model_validate_json(observation.model_dump_json())
        restored.model_dump(mode="json")

    assert not captured
    assert restored == observation
    assert isinstance(restored.players, tuple)
    assert isinstance(restored.facts, tuple)


def test_package_hash_omits_implicit_and_future_empty_defaults() -> None:
    skill = _skill("hash-stability", 1)
    package = _package((skill,))
    explicit_defaults = ExecutionPackage.model_validate(package.model_dump(mode="json"))

    class ExtendedPackage(ExecutionPackage):
        future_empty_default: tuple[str, ...] = ()

    extended = ExtendedPackage.model_validate(package.model_dump(mode="json"))

    assert explicit_defaults.package_id == package.package_id
    assert extended.package_id == package.package_id
    assert package.model_dump(mode="json")["state_declarations"] == []
    assert "state_declarations" not in package.model_dump(mode="json", exclude_defaults=True)
    assert package.model_copy(update={"board_version": "2.0.0"}).package_id != package.package_id


@pytest.mark.parametrize(
    ("policy", "expected_cost"),
    [("ON_SUCCESS", True), ("ON_EFFECT", False)],
)
def test_legal_blocked_action_cost_semantics_are_distinct(
    policy: str,
    expected_cost: bool,
) -> None:
    attack = _damage_skill("attack", 1, "wolf_attack")
    attack = attack.model_copy(
        update={
            "usage": UsagePolicy(
                costs=(CostSpec(resource_id="dose", amount=1),),
                cost_policy=policy,
            )
        }
    )
    protection = _skill(
        "protection",
        2,
        min_targets=1,
        effects=(
            EffectSpec(
                effect_id="protection-effect",
                effect_type="PROTECTION",
                target=_ref("target", "seat"),
                tags=("wolf_attack",),
            ),
        ),
    )
    package = _package(
        (attack, protection),
        interactions=(
            InteractionRule(
                interaction_id="block-wolf-attack",
                rule_type="BLOCK_DAMAGE",
                damage_tags=("wolf_attack",),
                counter_tags=("wolf_attack",),
            ),
            InteractionRule(
                interaction_id="confirm-wolf-death",
                rule_type="CONFIRM_DEATH",
                damage_tags=("wolf_attack",),
                death_cause="wolf_attack",
            ),
        ),
    )
    observation = _observation((attack, protection), resources={1: {"dose": 1}})

    batch = RuleInterpreter().plan(
        package,
        observation,
        (
            _request(attack, actor=1, targets=(5,), request_id="attack-1"),
            _request(protection, actor=3, targets=(5,), request_id="guard-1"),
        ),
    )

    attack_history = next(item for item in batch.history_updates if item.request_id == "attack-1")
    assert attack_history.successful is True
    assert not any(
        effect.applied for effect in batch.effects if effect.source_request_id == "attack-1"
    )
    assert bool(batch.cost_updates) is expected_cost
    assert observation.players[0].resources["dose"] == 1
    assert observation.ledger == ()


def test_group_cost_oversubscription_rejects_every_contender_without_updates() -> None:
    first = _skill(
        "first-cost",
        1,
        usage=UsagePolicy(costs=(CostSpec(resource_id="charge", amount=1),)),
    )
    second = _skill(
        "second-cost",
        2,
        usage=UsagePolicy(costs=(CostSpec(resource_id="charge", amount=1),)),
    )
    package = _package((first, second))
    observation = _observation((first, second), resources={1: {"charge": 1}})

    batch = RuleInterpreter().plan(
        package,
        observation,
        (
            _request(first, actor=1, targets=(), request_id="first"),
            _request(second, actor=1, targets=(), request_id="second"),
        ),
    )

    assert {item.reason for item in batch.dispositions} == {"insufficient_resource"}
    assert all(item.status == "REJECTED" for item in batch.dispositions)
    assert batch.cost_updates == ()
    assert batch.history_updates == ()


@pytest.mark.parametrize("charge_on_pass", [False, True])
def test_pass_cost_requires_explicit_policy(charge_on_pass: bool) -> None:
    skill = _skill(
        "optional-action",
        1,
        parameters=(ParameterSpec(name="mode", value_type="str"),),
        usage=UsagePolicy(
            costs=(CostSpec(resource_id="potion", amount=1),),
            cost_policy="ON_ATTEMPT",
            charge_on_pass=charge_on_pass,
        ),
    )
    package = _package((skill,))
    observation = _observation((skill,), resources={1: {"potion": 1}})
    request = SkillRequest(
        request_id="pass-action",
        ability_instance_id="ability-1-1",
        skill_id=skill.skill_id,
        action_code=skill.action_code,
        actor_seat=1,
        passed=True,
    )

    batch = RuleInterpreter().plan(package, observation, (request,))

    assert batch.dispositions[0].status == "PASSED"
    assert bool(batch.cost_updates) is charge_on_pass


def test_pass_skips_required_action_parameters_but_rejects_supplied_parameters() -> None:
    skill = _skill(
        "parameterized-action",
        1,
        parameters=(ParameterSpec(name="mode", value_type="str"),),
    )
    package = _package((skill,))
    observation = _observation((skill,))

    passed = SkillRequest(
        request_id="pass-without-mode",
        ability_instance_id="ability-1-1",
        skill_id=skill.skill_id,
        action_code=skill.action_code,
        actor_seat=1,
        passed=True,
    )
    malformed = passed.model_copy(
        update={"request_id": "pass-with-mode", "parameters": {"mode": "quiet"}}
    )

    batch = RuleInterpreter().plan(package, observation, (passed, malformed))
    dispositions = {item.request_id: item for item in batch.dispositions}
    assert dispositions["pass-without-mode"].status == "PASSED"
    assert dispositions["pass-with-mode"].status == "REJECTED"
    assert dispositions["pass-with-mode"].reason == "pass_not_allowed"


def test_shield_and_heal_block_tagged_attack_but_poison_and_cause_priority_apply() -> None:
    attack = _damage_skill("attack", 1, "wolf_attack")
    poison = _damage_skill("poison", 2, "poison")
    protection = _skill(
        "protection",
        3,
        min_targets=1,
        effects=(
            EffectSpec(
                effect_id="shield",
                effect_type="PROTECTION",
                target=_ref("target", "seat"),
                tags=("wolf_attack",),
            ),
        ),
    )
    heal = _skill(
        "healing",
        4,
        min_targets=1,
        effects=(
            EffectSpec(
                effect_id="antidote",
                effect_type="HEAL",
                target=_ref("target", "seat"),
                tags=("antidote",),
            ),
        ),
    )
    package = _package(
        (attack, poison, protection, heal),
        interactions=(
            InteractionRule(
                interaction_id="shield-wolf-attack",
                rule_type="BLOCK_DAMAGE",
                damage_tags=("wolf_attack",),
                counter_tags=("wolf_attack",),
            ),
            InteractionRule(
                interaction_id="heal-wolf-attack",
                rule_type="CANCEL_DAMAGE_HEAL",
                damage_tags=("wolf_attack",),
                counter_tags=("antidote",),
            ),
            InteractionRule(
                interaction_id="wolf-cause",
                rule_type="CONFIRM_DEATH",
                priority=10,
                damage_tags=("wolf_attack",),
                death_cause="wolf_attack",
            ),
            InteractionRule(
                interaction_id="poison-cause",
                rule_type="CONFIRM_DEATH",
                priority=20,
                damage_tags=("poison",),
                death_cause="poison",
            ),
        ),
    )
    observation = _observation((attack, poison, protection, heal))

    batch = RuleInterpreter().plan(
        package,
        observation,
        (
            _request(attack, actor=1, targets=(5,), request_id="attack"),
            _request(poison, actor=2, targets=(5,), request_id="poison"),
            _request(protection, actor=3, targets=(5,), request_id="shield"),
            _request(heal, actor=4, targets=(5,), request_id="heal"),
            _request(attack, actor=6, targets=(7,), request_id="attack-7"),
            _request(poison, actor=2, targets=(7,), request_id="poison-7"),
        ),
    )

    outcomes = {item.seat: item for item in batch.mortality}
    assert outcomes[5].deceased is True
    assert outcomes[5].death_cause == "poison"
    assert outcomes[7].deceased is True
    assert outcomes[7].death_cause == "poison"
    effects = {item.effect_id: item for item in batch.effects}
    assert (
        effects[
            next(item.effect_id for item in batch.intents if item.source_request_id == "attack")
        ].applied
        is False
    )
    assert (
        effects[
            next(item.effect_id for item in batch.intents if item.source_request_id == "poison")
        ].applied
        is True
    )
    assert (
        effects[
            next(item.effect_id for item in batch.intents if item.source_request_id == "shield")
        ].applied
        is True
    )
    assert (
        effects[
            next(item.effect_id for item in batch.intents if item.source_request_id == "heal")
        ].applied
        is True
    )


def test_equal_priority_conflicting_death_causes_are_rejected() -> None:
    first = _damage_skill("first-damage", 1, "cause_a")
    second = _damage_skill("second-damage", 2, "cause_b")
    package = _package(
        (first, second),
        interactions=(
            InteractionRule(
                interaction_id="confirm-a",
                rule_type="CONFIRM_DEATH",
                damage_tags=("cause_a",),
                death_cause="cause_a",
            ),
            InteractionRule(
                interaction_id="confirm-b",
                rule_type="CONFIRM_DEATH",
                damage_tags=("cause_b",),
                death_cause="cause_b",
            ),
        ),
    )

    with pytest.raises(ValueError, match="ambiguous death cause for seat 5"):
        RuleInterpreter().plan(
            package,
            _observation((first, second)),
            (
                _request(first, actor=1, targets=(5,), request_id="cause-a"),
                _request(second, actor=2, targets=(5,), request_id="cause-b"),
            ),
        )


def test_data_declared_exile_replacement_prevents_death_and_removes_vote_right() -> None:
    exile = _damage_skill("exile", 1, "exiled")
    replace_exile = InteractionRule(
        interaction_id="replace-exiled-idiot",
        rule_type="REPLACE_DEATH",
        damage_tags=("exiled",),
        when=BooleanExpr(
            op="and",
            values=(
                _compare("contains", _ref("item", "tags"), _literal("exiled")),
                _compare("eq", _ref("target", "role_id"), _literal("idiot")),
            ),
        ),
        effects=(
            EffectSpec(effect_id="save", effect_type="PREVENT_DEATH"),
            EffectSpec(
                effect_id="remove-vote",
                effect_type="SET_CAN_VOTE",
                value=_literal(False),
            ),
            EffectSpec(
                effect_id="reveal",
                effect_type="FACT",
                fact_type="idiot_revealed",
                value=_ref("target", "role_id"),
            ),
        ),
    )
    package = _package(
        (exile,),
        interactions=(
            replace_exile,
            InteractionRule(
                interaction_id="confirm-exile",
                rule_type="CONFIRM_DEATH",
                damage_tags=("exiled",),
                death_cause="exile",
            ),
        ),
    )
    observation = _observation(
        (exile,),
        players=(
            PlayerObservation(seat=1, role_id="voter", faction_id="GOOD"),
            PlayerObservation(seat=2, role_id="idiot", faction_id="GOOD"),
        ),
    )

    batch = RuleInterpreter().plan(
        package,
        observation,
        (_request(exile, actor=1, targets=(2,), request_id="exile"),),
    )

    assert batch.mortality[0].deceased is False
    assert any(effect.effect_type == "PREVENT_DEATH" and effect.applied for effect in batch.effects)
    vote_effects = [effect for effect in batch.effects if effect.effect_type == "SET_CAN_VOTE"]
    assert len(vote_effects) == 1
    assert vote_effects[0].target_seat == 2
    assert vote_effects[0].value is False


def test_same_group_facts_obey_conditions_and_explicit_pass_effects() -> None:
    # Load the game package first because GameManager imports this adapter.
    __import__("werewolf.game")
    from werewolf.rules.adapter import RuleExecutionAdapter

    normal_effect = EffectSpec(
        effect_id="normal-fact",
        effect_type="FACT",
        target=_ref("target", "seat"),
        condition=_compare("eq", _ref("target", "role_id"), _literal("eligible_target")),
        fact_type="normal_choice",
    )
    pass_effect = EffectSpec(
        effect_id="pass-fact",
        effect_type="FACT",
        condition=_compare("eq", _ref("actor", "role_id"), _literal("seer")),
        fact_type="explicit_pass",
    )
    skill = _skill(
        "conditional-fact",
        1,
        min_targets=1,
        max_targets=1,
        condition=_compare("eq", _ref("actor", "role_id"), _literal("seer")),
        effects=(normal_effect,),
        pass_effects=(pass_effect,),
    )
    downstream = _skill(
        "fact-dependent",
        2,
        condition=CompareExpr(
            op="gt",
            left=CountExpr(
                selector=SelectorExpr(
                    source="facts",
                    where=_compare(
                        "eq",
                        _ref("item", "fact_type"),
                        _literal("normal_choice"),
                    ),
                ),
            ),
            right=_literal(0),
        ),
    )
    package = _package((skill, downstream))
    observation = _observation(
        (skill, downstream),
        players=(
            PlayerObservation(seat=1, role_id="seer", alive=True),
            PlayerObservation(seat=2, role_id="villager", alive=True),
            PlayerObservation(seat=3, role_id="eligible_target", alive=True),
            PlayerObservation(seat=4, role_id="villager", alive=True),
        ),
    )
    requests = (
        _request(skill, actor=1, targets=(3,), request_id="valid-normal"),
        _request(skill, actor=2, targets=(3,), request_id="failed-skill-condition"),
        _request(skill, actor=1, targets=(4,), request_id="failed-effect-condition"),
        SkillRequest(
            request_id="pass-with-explicit-fact",
            ability_instance_id="ability-1-1",
            skill_id=skill.skill_id,
            action_code=skill.action_code,
            actor_seat=1,
            passed=True,
        ),
    )

    adapter = RuleExecutionAdapter(package)
    facts = adapter._request_facts(observation, requests)

    assert {(fact.source_request_id, fact.fact_type) for fact in facts} == {
        ("valid-normal", "normal_choice"),
        ("pass-with-explicit-fact", "explicit_pass"),
    }

    batch = RuleInterpreter().plan(package, observation, requests)
    dispositions = {item.request_id: item.status for item in batch.dispositions}
    assert dispositions == {
        "failed-effect-condition": "ACCEPTED",
        "failed-skill-condition": "REJECTED",
        "pass-with-explicit-fact": "PASSED",
        "valid-normal": "ACCEPTED",
    }
    assert {(fact.source_request_id, fact.fact_type) for fact in batch.facts} == {
        ("valid-normal", "normal_choice"),
        ("pass-with-explicit-fact", "explicit_pass"),
    }

    downstream_request = _request(
        downstream,
        actor=3,
        targets=(),
        request_id="read-group-facts",
    )
    accepted = RuleInterpreter().plan(
        package,
        observation.model_copy(update={"facts": facts}),
        (downstream_request,),
    )
    assert accepted.dispositions[0].status == "ACCEPTED"

    invalid_only_facts = adapter._request_facts(
        observation,
        (requests[1], requests[2], requests[3]),
    )
    rejected = RuleInterpreter().plan(
        package,
        observation.model_copy(update={"facts": invalid_only_facts}),
        (downstream_request.model_copy(update={"request_id": "no-authorized-source"}),),
    )
    assert rejected.dispositions[0].reason == "condition_not_met"


def test_self_and_team_disclosures_keep_identity_with_intended_recipients() -> None:
    inspect = _skill(
        "identity-check",
        1,
        min_targets=1,
        disclosures=(
            DisclosureSpec(
                disclosure_id="private-to-actor",
                audience="SELF",
                values={"role": _ref("target", "role_id")},
            ),
            DisclosureSpec(
                disclosure_id="private-to-team",
                audience="TEAM",
                values={"role": _ref("target", "role_id")},
            ),
        ),
    )
    package = _package((inspect,))
    observation = _observation(
        (inspect,),
        players=(
            PlayerObservation(seat=1, role_id="seer", chat_group_ids=("team-a",)),
            PlayerObservation(seat=2, role_id="wolf", chat_group_ids=("team-a",)),
            PlayerObservation(seat=3, role_id="guard", chat_group_ids=("team-b",)),
        ),
    )

    batch = RuleInterpreter().plan(
        package,
        observation,
        (_request(inspect, actor=1, targets=(2,), request_id="inspect"),),
    )

    by_id = {item.disclosure_id: item for item in batch.disclosures}
    assert by_id["private-to-actor"].recipients == (1,)
    assert by_id["private-to-team"].recipients == (1, 2)
    assert by_id["private-to-team"].fields == {"role": "wolf"}
    assert all(3 not in projection.recipients for projection in batch.disclosures)
