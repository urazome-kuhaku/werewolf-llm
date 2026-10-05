"""Behavioral coverage for the extensible B rule-kernel contract."""

from __future__ import annotations

from typing import cast

import pytest

from werewolf.game.actions import ActionDefinition, ActionRegistry
from werewolf.rules.compiler import ExecutionCompilerError, validate_execution_package
from werewolf.rules.interpreter import RuleInterpreter
from werewolf.rules.models import (
    AbilityGrant,
    AbilityInstance,
    ActionSpec,
    BooleanExpr,
    BoundaryPolicy,
    CompareExpr,
    CostSpec,
    DisclosureSpec,
    DomainFact,
    EffectSpec,
    ExecutionPackage,
    ExecutionWindow,
    InteractionRule,
    LiteralExpr,
    PlayerFieldValues,
    PlayerObservation,
    RefExpr,
    RelationDeclaration,
    RelationExistsExpr,
    RelationValue,
    ResourceDeclaration,
    RuleObservation,
    RuleStateValue,
    SelectorExpr,
    SkillRequest,
    SkillSpec,
    StateDeclaration,
    TargetPolicy,
    TriggerSpec,
    UsagePolicy,
)
from werewolf.rules.predicates import evaluate_expr, validate_package_expressions

BOARD_ID = "kernel-b-test"
BOARD_VERSION = "1.0.0"


def _literal(value: object, value_type: str | None = None) -> LiteralExpr:
    return LiteralExpr(op="literal", value=cast(object, value), value_type=value_type)  # type: ignore[arg-type]


def _ref(source: str, name: str) -> RefExpr:
    return RefExpr(op="ref", source=source, name=name)  # type: ignore[arg-type]


def _selector() -> object:
    from werewolf.rules.models import SelectorExpr

    return SelectorExpr(source="players", map=_ref("item", "seat"))


def _skill(
    skill_id: str,
    action_code: int,
    *,
    mode: str = "PLAYER",
    effects: tuple[EffectSpec, ...] = (),
    condition: object | None = None,
    trigger: TriggerSpec | None = None,
    window_ids: tuple[str, ...] = (),
    hook_ids: tuple[str, ...] = (),
    targets: tuple[int, int] = (0, 1),
) -> SkillSpec:
    from werewolf.rules.models import SelectorExpr

    return SkillSpec(
        skill_id=skill_id,
        action_code=action_code,
        mode=mode,  # type: ignore[arg-type]
        grants=(AbilityGrant(grant_id=f"{skill_id}-grant", actor_selector=_selector()),),
        timing=(),
        window_ids=window_ids,
        hook_ids=hook_ids,  # type: ignore[arg-type]
        trigger=trigger,
        condition=condition,  # type: ignore[arg-type]
        targets=TargetPolicy(
            min_targets=targets[0],
            max_targets=targets[1],
            selector=SelectorExpr(source="players", map=_ref("item", "seat")),
        ),
        usage=UsagePolicy(),
        effects=effects,
    )


def _package(
    skills: tuple[SkillSpec, ...],
    *,
    states: tuple[StateDeclaration, ...] = (),
    relations: tuple[str, ...] = (),
    resources: tuple[ResourceDeclaration, ...] = (),
    field_values: PlayerFieldValues | None = None,
    windows: tuple[ExecutionWindow, ...] = (),
    groups: dict[str, str] | None = None,
    boundary_policy: BoundaryPolicy | None = None,
) -> ExecutionPackage:
    return ExecutionPackage(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        actions=tuple(
            ActionSpec(action_code=skill.action_code, action_id=f"A{skill.action_code}")
            for skill in skills
        ),
        skills=skills,
        state_declarations=states,
        relation_declarations=tuple(RelationDeclaration(relation_type=item) for item in relations),
        resource_declarations=resources,
        player_field_values=field_values,
        window_metadata=windows,
        window_settlement_groups=groups or {},
        boundary_policy=boundary_policy,
    )


def _observation(
    skills: tuple[SkillSpec, ...],
    *,
    facts: tuple[DomainFact, ...] = (),
    relations: tuple[RelationValue, ...] = (),
    state_values: tuple[RuleStateValue, ...] = (),
    current_window_id: str | None = None,
    current_logical_window_id: str | None = None,
    current_hook_id: str | None = None,
    players: tuple[PlayerObservation, ...] | None = None,
) -> RuleObservation:
    players = players or tuple(PlayerObservation(seat=seat) for seat in range(1, 5))
    instances = tuple(
        AbilityInstance(
            ability_instance_id=f"inst:{skill.skill_id}:{seat}",
            skill_id=skill.skill_id,
            actor_seat=seat,
            grant_id=skill.grants[0].grant_id,
        )
        for skill in skills
        for seat in range(1, 5)
    )
    return RuleObservation(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        revision=10,
        round_number=3,
        players=players,
        facts=facts,
        relations=relations,
        state_values=state_values,
        ability_instances=instances,
        game_id="game-b",
        group_id="group-1",
        current_window_id=current_window_id,
        current_logical_window_id=current_logical_window_id,
        current_hook_id=current_hook_id,  # type: ignore[arg-type]
    )


def _request(
    skill: SkillSpec,
    *,
    actor: int = 1,
    targets: tuple[int, ...] = (),
    request_id: str = "request-1",
    origin: str = "PLAYER",
    fact_id: str | None = None,
    occurrence_id: str | None = None,
    window_id: str | None = None,
    logical_window_id: str | None = None,
    hook_id: str | None = None,
) -> SkillRequest:
    return SkillRequest(
        request_id=request_id,
        ability_instance_id=f"inst:{skill.skill_id}:{actor}",
        skill_id=skill.skill_id,
        action_code=skill.action_code,
        actor_seat=actor,
        targets=targets,
        origin=origin,  # type: ignore[arg-type]
        source_fact_id=fact_id,
        trigger_occurrence_id=occurrence_id,
        window_id=window_id,
        logical_window_id=logical_window_id,
        hook_id=hook_id,  # type: ignore[arg-type]
    )


def test_automatic_requests_require_a_confirmed_matching_source_fact() -> None:
    skill = _skill(
        "automatic-listener",
        1,
        mode="AUTOMATIC",
        trigger=TriggerSpec(fact_types=("DEATH_CONFIRMED",), mode="AUTOMATIC"),
        targets=(0, 0),
    )
    package = _package((skill,))
    fact = DomainFact(
        fact_id="death-1", fact_type="DEATH_CONFIRMED", target_seat=2, death_cause="wolf"
    )
    observation = _observation((skill,), facts=(fact,))
    interpreter = RuleInterpreter()

    fabricated = interpreter.plan(
        package,
        observation,
        (_request(skill, origin="AUTOMATIC", fact_id="made-up", occurrence_id="occ-1"),),
    )
    player_origin = interpreter.plan(
        package,
        observation,
        (_request(skill, origin="PLAYER", fact_id="death-1", occurrence_id="occ-2"),),
    )
    wrong_fact = interpreter.plan(
        package,
        _observation(
            (skill,),
            facts=(DomainFact(fact_id="f", fact_type="proposed_death"),),
        ),
        (_request(skill, origin="AUTOMATIC", fact_id="f", occurrence_id="occ-3"),),
    )

    assert fabricated.dispositions[0].reason == "trigger_occurrence_not_bound"
    assert player_origin.dispositions[0].reason == "automatic_origin_not_authorized"
    assert wrong_fact.dispositions[0].reason == "trigger_fact_not_authorized"


def test_player_choice_binds_occurrence_fact_window_and_relation_condition() -> None:
    relation_condition = RelationExistsExpr(
        relation_type="marked",
        source_seat=_ref("actor", "seat"),
        target_seat=_literal(2, "seat"),
    )
    skill = _skill(
        "choice-after-fact",
        1,
        trigger=TriggerSpec(
            fact_types=("DEATH_CONFIRMED",),
            mode="PLAYER_CHOICE",
            condition=BooleanExpr(
                op="and",
                values=(
                    relation_condition,
                    CompareExpr(
                        op="eq",
                        left=_ref("source_fact", "death_cause"),
                        right=_literal("wolf"),
                    ),
                ),
            ),
        ),
        window_ids=("after_death",),
        targets=(0, 0),
    )
    fact = DomainFact(fact_id="death-2", fact_type="DEATH_CONFIRMED", death_cause="wolf")
    relation = RelationValue(
        relation_id="r1",
        relation_type="marked",
        source_seat=1,
        target_seat=2,
        source_skill_id="mark",
        source_request_id="old",
        source_ability_instance_id="old-instance",
    )
    observation = _observation(
        (skill,),
        facts=(fact,),
        relations=(relation,),
        current_window_id="physical:after-death:3",
        current_logical_window_id="after_death",
    )
    package = _package((skill,), relations=("marked",))
    request = _request(
        skill,
        occurrence_id="occurrence-1",
        fact_id="death-2",
        window_id="physical:after-death:3",
        logical_window_id="after_death",
    )

    accepted = RuleInterpreter().plan(package, observation, (request,))
    unmatched_window = RuleInterpreter().plan(
        package,
        observation,
        (request.model_copy(update={"logical_window_id": "other"}),),
    )

    assert accepted.dispositions[0].status == "ACCEPTED"
    assert unmatched_window.dispositions[0].reason == "logical_window_mismatch"
    assert (
        evaluate_expr(
            relation_condition,
            {"observation": observation, "actor": observation.players[0]},
        )
        is True
    )


def test_trigger_relation_expression_requires_declared_relation_type() -> None:
    skill = _skill(
        "unknown-relation",
        1,
        trigger=TriggerSpec(
            fact_types=("event",),
            mode="PLAYER_CHOICE",
            condition=RelationExistsExpr(
                relation_type="undeclared", source_seat=_ref("actor", "seat")
            ),
        ),
    )

    with pytest.raises(ValueError, match="undeclared type"):
        validate_package_expressions(_package((skill,)))


def test_field_assignment_supports_nullable_identity_and_requires_authorization() -> None:
    effect = EffectSpec(
        effect_id="clear-victory-group",
        effect_type="PLAYER_FIELD_SET",
        player_field="victory_group_id",
        target=_literal(2, "seat"),
        value=_literal(None, "nullable_str"),
    )
    skill = _skill("clear-field", 1, effects=(effect,))
    package = _package(
        (skill,), field_values=PlayerFieldValues(victory_group_ids=("town", "wolves"))
    )
    observation = _observation((skill,))
    request = _request(skill, targets=())

    with pytest.raises(ValueError, match="outside its declared authorization"):
        RuleInterpreter().plan(package, observation, (request,))

    authorized = effect.model_copy(update={"authorized_targets": (_literal(2, "seat"),)})
    skill = skill.model_copy(update={"effects": (authorized,)})
    package = _package(
        (skill,), field_values=PlayerFieldValues(victory_group_ids=("town", "wolves"))
    )
    batch = RuleInterpreter().plan(package, observation, (_request(skill),))

    assert batch.player_updates[0].seat == 2
    assert batch.player_updates[0].player_field == "victory_group_id"
    assert batch.player_updates[0].value is None
    assert batch.effects[0].skill_id == skill.skill_id
    assert batch.effects[0].actor_seat == 1
    assert batch.effects[0].authorized_targets == (2,)


def test_typed_player_field_effect_rejects_ignored_composite_payload() -> None:
    skill = _skill(
        "composite-field-effect",
        1,
        effects=(
            EffectSpec(
                effect_id="set-role-and-fact",
                effect_type="PLAYER_FIELD_SET",
                player_field="role_id",
                value=_literal("villager"),
                fact_type="also_emit",
            ),
        ),
    )
    package = _package((skill,), field_values=PlayerFieldValues(role_ids=("villager",)))

    with pytest.raises(ValueError, match="cannot carry fact_type"):
        validate_package_expressions(package)


def test_identity_values_are_bound_to_frozen_vocabularies() -> None:
    skill = _skill(
        "invalid-role-change",
        1,
        effects=(
            EffectSpec(
                effect_id="change-role",
                effect_type="PLAYER_FIELD_SET",
                player_field="role_id",
                value=_literal("unbound_role"),
            ),
        ),
    )
    package = _package((skill,), field_values=PlayerFieldValues(role_ids=("villager",)))

    with pytest.raises(ValueError, match="frozen vocabulary"):
        RuleInterpreter().plan(package, _observation((skill,)), (_request(skill),))


def test_same_field_conflict_rejects_but_resource_deltas_commute_and_bound() -> None:
    first = _skill(
        "first-write",
        1,
        effects=(
            EffectSpec(
                effect_id="set-role-a",
                effect_type="PLAYER_FIELD_SET",
                player_field="role_id",
                value=_literal("alpha"),
            ),
        ),
    )
    second = _skill(
        "second-write",
        2,
        effects=(
            EffectSpec(
                effect_id="set-role-b",
                effect_type="PLAYER_FIELD_SET",
                player_field="role_id",
                value=_literal("beta"),
            ),
        ),
    )
    fields = PlayerFieldValues(role_ids=("alpha", "beta"))
    package = _package((first, second), field_values=fields)
    observation = _observation((first, second))
    with pytest.raises(ValueError, match="conflicting player field updates"):
        RuleInterpreter().plan(
            package,
            observation,
            (
                _request(first, targets=(2,), request_id="first"),
                _request(second, targets=(2,), request_id="second"),
            ),
        )

    def delta_skill(skill_id: str, action: int, delta: int) -> SkillSpec:
        return _skill(
            skill_id,
            action,
            effects=(
                EffectSpec(
                    effect_id=f"{skill_id}-delta",
                    effect_type="RESOURCE_DELTA",
                    resource_id="charge",
                    delta=_literal(delta),
                ),
            ),
        )

    positive = delta_skill("positive", 3, 4)
    negative = delta_skill("negative", 4, -1)
    resources = _package(
        (positive, negative), resources=(ResourceDeclaration(resource_id="charge", max_value=5),)
    )
    observed = _observation(
        (positive, negative),
        players=(PlayerObservation(seat=1, resources={"charge": 2}),),
    )
    batch = RuleInterpreter().plan(
        resources,
        observed,
        (
            _request(positive, request_id="up"),
            _request(negative, request_id="down"),
        ),
    )
    assert sum(item.delta for item in batch.resource_updates) == 3

    too_much = delta_skill("too-much", 5, 4)
    capped = _package(
        (too_much,), resources=(ResourceDeclaration(resource_id="charge", max_value=5),)
    )
    with pytest.raises(ValueError, match="resource deltas and costs exceed bounds"):
        RuleInterpreter().plan(
            capped,
            _observation(
                (too_much,),
                players=(PlayerObservation(seat=1, resources={"charge": 2}),),
            ),
            (_request(too_much),),
        )


def test_resource_costs_and_typed_deltas_are_checked_as_one_net_update() -> None:
    skill = _skill(
        "net-resource",
        1,
        effects=(
            EffectSpec(
                effect_id="gain-charge",
                effect_type="RESOURCE_DELTA",
                resource_id="charge",
                delta=_literal(4),
            ),
        ),
    ).model_copy(update={"usage": UsagePolicy(costs=(CostSpec(resource_id="charge", amount=1),))})
    package = _package(
        (skill,), resources=(ResourceDeclaration(resource_id="charge", max_value=5),)
    )
    observation = _observation(
        (skill,), players=(PlayerObservation(seat=1, resources={"charge": 2}),)
    )

    batch = RuleInterpreter().plan(package, observation, (_request(skill),))

    assert batch.resource_updates[0].delta == 4
    assert batch.cost_updates[0].amount == 1

    negative = skill.model_copy(
        update={
            "effects": (
                skill.effects[0].model_copy(
                    update={"effect_id": "lose-too-much", "delta": _literal(-2)}
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="resource deltas and costs exceed bounds"):
        RuleInterpreter().plan(
            _package(
                (negative,), resources=(ResourceDeclaration(resource_id="charge", max_value=5),)
            ),
            observation,
            (_request(negative),),
        )


def test_disclosure_context_matches_trigger_request_and_scoped_state_defaults() -> None:
    skill = _skill(
        "contextual-disclosure",
        1,
        trigger=TriggerSpec(fact_types=("DEATH_CONFIRMED",), mode="PLAYER_CHOICE"),
        window_ids=("night_actions",),
        hook_ids=("DAY_SPEECH_BEFORE",),
        targets=(0, 0),
    )
    skill = skill.model_copy(
        update={
            "disclosures": (
                DisclosureSpec(
                    disclosure_id="bound-context",
                    audience="SELF",
                    condition=BooleanExpr(
                        op="and",
                        values=(
                            CompareExpr(
                                op="eq",
                                left=_ref("request", "window_id"),
                                right=_literal("physical:night:3"),
                            ),
                            CompareExpr(
                                op="eq",
                                left=_ref("request", "logical_window_id"),
                                right=_literal("night_actions"),
                            ),
                            CompareExpr(
                                op="eq",
                                left=_ref("request", "hook_id"),
                                right=_literal("DAY_SPEECH_BEFORE"),
                            ),
                        ),
                    ),
                    values={
                        "death_cause": _ref("source_fact", "death_cause"),
                        "game_count": _ref("skill_state", "game_count"),
                        "seat_count": _ref("skill_state", "seat_count"),
                        "ability_count": _ref("skill_state", "ability_count"),
                    },
                ),
            )
        }
    )
    states = (
        StateDeclaration(
            skill_id=skill.skill_id,
            key="game_count",
            value_type="int",
            initial=2,
            scope="GAME",
        ),
        StateDeclaration(
            skill_id=skill.skill_id,
            key="seat_count",
            value_type="int",
            initial=3,
            scope="SEAT",
        ),
        StateDeclaration(
            skill_id=skill.skill_id,
            key="ability_count",
            value_type="int",
            initial=4,
            scope="ABILITY",
        ),
    )
    fact = DomainFact(
        fact_id="context-death",
        fact_type="DEATH_CONFIRMED",
        target_seat=2,
        death_cause="wolf",
    )
    observation = _observation(
        (skill,),
        facts=(fact,),
        current_window_id="physical:night:3",
        current_logical_window_id="night_actions",
        current_hook_id="DAY_SPEECH_BEFORE",
        state_values=(
            RuleStateValue(
                scope="GAME",
                skill_id=skill.skill_id,
                key="game_count",
                value=8,
            ),
            RuleStateValue(
                scope="ABILITY",
                skill_id=skill.skill_id,
                key="ability_count",
                ability_instance_id="inst:contextual-disclosure:1",
                value=9,
            ),
        ),
    )
    request = _request(
        skill,
        request_id="context-request",
        fact_id=fact.fact_id,
        occurrence_id="context-occurrence",
        window_id="physical:night:3",
        logical_window_id="night_actions",
        hook_id="DAY_SPEECH_BEFORE",
    )

    batch = RuleInterpreter().plan(_package((skill,), states=states), observation, (request,))

    assert batch.disclosures[0].fields == {
        "death_cause": "wolf",
        "game_count": 8,
        "seat_count": 3,
        "ability_count": 9,
    }


def test_relation_add_remove_use_stable_identity_and_discrete_expiry() -> None:
    add = _skill(
        "add-relation",
        1,
        effects=(
            EffectSpec(
                effect_id="add-mark",
                effect_type="RELATION_ADD",
                relation_type="marked",
                relation_source=_ref("actor", "seat"),
                relation_target=_ref("target", "seat"),
                relation_expiry_policy="ROUND_END",
            ),
        ),
    )
    package = _package((add,), relations=("marked",))
    observation = _observation((add,))
    added = RuleInterpreter().plan(package, observation, (_request(add, targets=(2,)),))
    relation_update = added.relation_updates[0]
    assert relation_update.operation == "ADD"
    assert relation_update.expiry_policy == "ROUND_END"
    assert relation_update.relation_id == added.effects[0].relation_id
    assert relation_update.source_ability_instance_id == "inst:add-relation:1"

    remove = _skill(
        "remove-relation",
        1,
        effects=(
            EffectSpec(
                effect_id="remove-mark",
                effect_type="RELATION_REMOVE",
                relation_type="marked",
                relation_source=_ref("actor", "seat"),
                relation_target=_ref("target", "seat"),
            ),
        ),
    )
    relation = RelationValue(
        relation_id=relation_update.relation_id,
        relation_type="marked",
        source_seat=1,
        target_seat=2,
        source_skill_id="add-relation",
        source_request_id="prior",
        source_ability_instance_id="old-instance",
        expires_at_round=3,
    )
    removed = RuleInterpreter().plan(
        _package((remove,), relations=("marked",)),
        _observation((remove,), relations=(relation,)),
        (_request(remove, targets=(2,)),),
    )
    assert removed.relation_updates[0].operation == "REMOVE"
    assert removed.relation_updates[0].expiry_policy is None


def test_multi_scope_state_reads_and_writes_expiry_policy() -> None:
    state = StateDeclaration(
        skill_id="state-skill",
        key="count",
        value_type="int",
        initial=0,
        scope="SEAT",
        expiry_policy="NEXT_NIGHT_START",
    )
    skill = _skill(
        "state-skill",
        1,
        condition=CompareExpr(op="eq", left=_ref("skill_state", "count"), right=_literal(3)),
        effects=(
            EffectSpec(
                effect_id="set-count",
                effect_type="STATE_SET",
                state_key="count",
                target=_literal(2, "seat"),
                value=_literal(4),
                authorized_targets=(_literal(2, "seat"),),
            ),
        ),
    )
    package = _package((skill,), states=(state,))
    observation = _observation(
        (skill,),
        state_values=(
            RuleStateValue(scope="SEAT", skill_id="state-skill", key="count", seat=1, value=3),
        ),
    )
    batch = RuleInterpreter().plan(package, observation, (_request(skill),))

    assert batch.dispositions[0].status == "ACCEPTED"
    assert batch.state_updates[0].scope == "SEAT"
    assert batch.state_updates[0].seat == 2
    assert batch.state_updates[0].expiry_policy == "NEXT_NIGHT_START"


def test_state_set_defaults_by_scope_and_preserves_authorization() -> None:
    skill = _skill(
        "state-scope",
        1,
        effects=(
            EffectSpec(
                effect_id="set-game",
                effect_type="STATE_SET",
                state_key="game_count",
                value=_literal(1),
            ),
            EffectSpec(
                effect_id="set-seat",
                effect_type="STATE_SET",
                state_key="seat_count",
                value=_literal(2),
            ),
            EffectSpec(
                effect_id="set-explicit-seat",
                effect_type="STATE_SET",
                state_key="explicit_seat_count",
                target=_literal(3, "seat"),
                value=_literal(3),
                authorized_targets=(_literal(3, "seat"),),
            ),
            EffectSpec(
                effect_id="set-ability",
                effect_type="STATE_SET",
                state_key="ability_count",
                value=_literal(4),
            ),
        ),
    )
    states = (
        StateDeclaration(
            skill_id=skill.skill_id,
            key="game_count",
            value_type="int",
            initial=0,
            scope="GAME",
        ),
        StateDeclaration(
            skill_id=skill.skill_id,
            key="seat_count",
            value_type="int",
            initial=0,
            scope="SEAT",
        ),
        StateDeclaration(
            skill_id=skill.skill_id,
            key="explicit_seat_count",
            value_type="int",
            initial=0,
            scope="SEAT",
        ),
        # A-era declarations omitted scope; the default remains source-instance scoped.
        StateDeclaration(
            skill_id=skill.skill_id,
            key="ability_count",
            value_type="int",
            initial=0,
        ),
    )
    package = _package((skill,), states=states)
    observation = _observation((skill,))
    batch = RuleInterpreter().plan(package, observation, (_request(skill, targets=(2,)),))

    assert batch.dispositions[0].status == "ACCEPTED"
    updates = {update.key: update for update in batch.state_updates}
    assert updates["game_count"].scope == "GAME"
    assert updates["game_count"].seat is None
    assert updates["game_count"].ability_instance_id is None
    assert updates["seat_count"].scope == "SEAT"
    assert updates["seat_count"].seat == 2
    assert updates["explicit_seat_count"].seat == 3
    assert updates["ability_count"].scope == "ABILITY"
    assert updates["ability_count"].seat is None
    assert updates["ability_count"].ability_instance_id == "inst:state-scope:1"

    intents = {intent.source_rule_id: intent for intent in batch.intents}
    assert intents["set-game"].target_seat is None
    assert intents["set-ability"].target_seat is None
    assert intents["set-explicit-seat"].authorized_targets == (2, 3)

    self_batch = RuleInterpreter().plan(package, observation, (_request(skill),))
    self_intent = next(
        intent for intent in self_batch.intents if intent.source_rule_id == "set-seat"
    )
    assert self_intent.target_seat == 1
    assert self_intent.authorized_targets == (1,)
    self_update = next(update for update in self_batch.state_updates if update.key == "seat_count")
    assert self_update.seat == 1

    unauthenticated = skill.model_copy(
        update={
            "effects": (
                next(
                    effect for effect in skill.effects if effect.effect_id == "set-explicit-seat"
                ).model_copy(update={"authorized_targets": ()}),
            )
        }
    )
    with pytest.raises(ValueError, match="outside its declared authorization"):
        RuleInterpreter().plan(
            _package((unauthenticated,), states=states),
            _observation((unauthenticated,)),
            (_request(unauthenticated),),
        )

    game_with_target = skill.model_copy(
        update={
            "effects": (
                next(
                    effect for effect in skill.effects if effect.effect_id == "set-game"
                ).model_copy(update={"target": _literal(2, "seat")}),
            )
        }
    )
    with pytest.raises(ValueError, match="GAME-scoped state cannot declare a target"):
        validate_package_expressions(
            _package(
                (game_with_target,),
                states=tuple(state for state in states if state.key == "game_count"),
            )
        )

    ability_with_cross_instance_target = skill.model_copy(
        update={
            "effects": (
                next(
                    effect for effect in skill.effects if effect.effect_id == "set-ability"
                ).model_copy(update={"target": _literal(2, "seat")}),
            )
        }
    )
    with pytest.raises(ValueError, match="ABILITY-scoped state is bound to the source instance"):
        validate_package_expressions(
            _package(
                (ability_with_cross_instance_target,),
                states=tuple(state for state in states if state.key == "ability_count"),
            )
        )


def test_direct_self_mutations_carry_authorization_for_authority_recheck() -> None:
    skill = _skill(
        "self-mutations",
        1,
        targets=(0, 0),
        effects=(
            EffectSpec(
                effect_id="set-role",
                effect_type="PLAYER_FIELD_SET",
                player_field="role_id",
                value=_literal("villager"),
            ),
            EffectSpec(
                effect_id="gain-charge",
                effect_type="RESOURCE_DELTA",
                resource_id="charge",
                delta=_literal(1),
            ),
            EffectSpec(
                effect_id="consume-self-ability",
                effect_type="CONSUME_ABILITY",
                target=_ref("actor", "seat"),
                value=_literal("spent_ability"),
            ),
        ),
    )
    package = _package(
        (skill,),
        resources=(ResourceDeclaration(resource_id="charge", max_value=5),),
        field_values=PlayerFieldValues(role_ids=("villager",)),
    )

    batch = RuleInterpreter().plan(
        package,
        _observation((skill,)),
        (_request(skill),),
    )

    self_intents = {intent.source_rule_id: intent for intent in batch.intents}
    assert set(self_intents) == {"set-role", "gain-charge", "consume-self-ability"}
    assert all(intent.target_seat == 1 for intent in self_intents.values())
    assert all(intent.authorized_targets == (1,) for intent in self_intents.values())


def test_interaction_state_effects_use_request_fact_and_scoped_state_context() -> None:
    skill = _skill(
        "interaction-state",
        1,
        effects=(
            EffectSpec(
                effect_id="hit",
                effect_type="DAMAGE",
                target=_ref("target", "seat"),
                value=_literal(1),
                tags=("wolf_attack",),
            ),
        ),
        trigger=TriggerSpec(fact_types=("DEATH_CONFIRMED",), mode="PLAYER_CHOICE"),
        window_ids=("night_actions",),
        hook_ids=("DAY_SPEECH_BEFORE",),
    )
    state_declarations = (
        StateDeclaration(
            skill_id=skill.skill_id,
            key="game_count",
            value_type="int",
            initial=0,
            scope="GAME",
        ),
        StateDeclaration(
            skill_id=skill.skill_id,
            key="seat_count",
            value_type="int",
            initial=0,
            scope="SEAT",
        ),
        StateDeclaration(
            skill_id=skill.skill_id,
            key="ability_count",
            value_type="int",
            initial=0,
        ),
    )
    source_fact = DomainFact(
        fact_id="interaction-source-fact",
        fact_type="DEATH_CONFIRMED",
        target_seat=4,
        death_cause="wolf",
    )
    observation = _observation(
        (skill,),
        facts=(source_fact,),
        state_values=(
            RuleStateValue(
                scope="GAME",
                skill_id=skill.skill_id,
                key="game_count",
                value=7,
            ),
            RuleStateValue(
                scope="SEAT",
                skill_id=skill.skill_id,
                key="seat_count",
                seat=1,
                value=5,
            ),
            RuleStateValue(
                scope="ABILITY",
                skill_id=skill.skill_id,
                key="ability_count",
                ability_instance_id="inst:interaction-state:1",
                value=6,
            ),
        ),
        current_window_id="physical:night:3",
        current_logical_window_id="night_actions",
        current_hook_id="DAY_SPEECH_BEFORE",
    )
    request = _request(
        skill,
        targets=(2,),
        request_id="interaction-state-request",
        fact_id=source_fact.fact_id,
        occurrence_id="interaction-state-occurrence",
        window_id="physical:night:3",
        logical_window_id="night_actions",
        hook_id="DAY_SPEECH_BEFORE",
    )
    replacement = InteractionRule(
        interaction_id="replace-with-state-update",
        rule_type="REPLACE_DEATH",
        when=BooleanExpr(
            op="and",
            values=(
                CompareExpr(
                    op="eq",
                    left=_ref("request", "window_id"),
                    right=_literal("physical:night:3"),
                ),
                CompareExpr(
                    op="eq",
                    left=_ref("request", "hook_id"),
                    right=_literal("DAY_SPEECH_BEFORE"),
                ),
                CompareExpr(
                    op="eq",
                    left=_ref("skill_state", "game_count"),
                    right=_literal(7),
                ),
                CompareExpr(
                    op="eq",
                    left=_ref("skill_state", "seat_count"),
                    right=_literal(5),
                ),
                CompareExpr(
                    op="eq",
                    left=_ref("skill_state", "ability_count"),
                    right=_literal(6),
                ),
                CompareExpr(
                    op="eq",
                    left=_ref("source_fact", "death_cause"),
                    right=_literal("wolf"),
                ),
            ),
        ),
        effects=(
            EffectSpec(
                effect_id="set-game-count",
                effect_type="STATE_SET",
                state_key="game_count",
                value=_literal(8),
            ),
            EffectSpec(
                effect_id="set-ability-count",
                effect_type="STATE_SET",
                state_key="ability_count",
                value=_literal(9),
            ),
        ),
    )
    package = _package(
        (skill,),
        states=state_declarations,
        windows=(ExecutionWindow(window_id="night_actions", order=1, phase="NIGHT_ACTION"),),
    ).model_copy(
        update={
            "interactions": (
                replacement,
                InteractionRule(interaction_id="confirm", rule_type="CONFIRM_DEATH"),
            )
        }
    )

    batch = RuleInterpreter().plan(package, observation, (request,))

    assert batch.dispositions[0].status == "ACCEPTED"
    updates = {update.key: update for update in batch.state_updates}
    assert updates["game_count"].scope == "GAME"
    assert updates["game_count"].value == 8
    assert updates["game_count"].ability_instance_id is None
    assert updates["ability_count"].scope == "ABILITY"
    assert updates["ability_count"].value == 9
    assert updates["ability_count"].ability_instance_id == "inst:interaction-state:1"


@pytest.mark.parametrize(
    "effect_type",
    (
        "PLAYER_FIELD_SET",
        "RESOURCE_DELTA",
        "RELATION_ADD",
        "RELATION_REMOVE",
        "ABILITY_GRANT",
        "ABILITY_REVOKE",
        "FLOW",
    ),
)
def test_interactions_reject_unimplemented_typed_b_effects(effect_type: str) -> None:
    skill = _skill("typed-interaction", 1)
    interaction = InteractionRule(
        interaction_id="typed-interaction",
        rule_type="REPLACE_DEATH",
        effects=(EffectSpec(effect_id="typed-effect", effect_type=effect_type),),  # type: ignore[arg-type]
    )
    package = _package((skill,)).model_copy(update={"interactions": (interaction,)})

    with pytest.raises(
        ValueError, match="typed B effects are supported only on skill declarations"
    ):
        validate_package_expressions(package)


def test_only_replacement_interactions_can_declare_generated_effects() -> None:
    skill = _skill("nonreplacement-effect", 1)
    interaction = InteractionRule(
        interaction_id="nonreplacement-effect",
        rule_type="CONFIRM_DEATH",
        effects=(EffectSpec(effect_id="ignored", effect_type="FACT", fact_type="ignored"),),
    )
    package = _package((skill,)).model_copy(update={"interactions": (interaction,)})

    with pytest.raises(
        ValueError, match="interaction effects are supported only for REPLACE_DEATH"
    ):
        validate_package_expressions(package)


def test_ability_grant_revoke_and_flow_conditions_are_typed_and_audited() -> None:
    recipient = _skill("recipient", 2, targets=(0, 0))
    grantor = _skill(
        "grantor",
        1,
        effects=(
            EffectSpec(
                effect_id="grant-recipient",
                effect_type="ABILITY_GRANT",
                target=_ref("target", "seat"),
                grant_skill_id="recipient",
                grant_id="recipient-grant",
            ),
        ),
    )
    package = _package((grantor, recipient))
    observation = _observation((grantor, recipient))
    observation = observation.model_copy(
        update={
            "ability_instances": tuple(
                instance
                for instance in observation.ability_instances
                if (instance.skill_id, instance.actor_seat) != ("recipient", 2)
            )
        }
    )
    granted = RuleInterpreter().plan(package, observation, (_request(grantor, targets=(2,)),))
    update = granted.ability_updates[0]
    assert update.operation == "GRANT"
    assert update.skill_id == "recipient"
    assert update.ability_instance_id == granted.effects[0].granted_ability_instance_id
    assert update.target_seat == 2

    excluded_selector = recipient.model_copy(
        update={
            "grants": (
                AbilityGrant(
                    grant_id="recipient-grant",
                    actor_selector=SelectorExpr(
                        source="players",
                        where=CompareExpr(
                            op="eq", left=_ref("item", "seat"), right=_literal(1, "seat")
                        ),
                    ),
                ),
            )
        }
    )
    dynamically_granted_observation = observation.model_copy(
        update={
            "ability_instances": (
                *observation.ability_instances,
                AbilityInstance(
                    ability_instance_id="inst:recipient:2",
                    skill_id="recipient",
                    actor_seat=2,
                    grant_id="recipient-grant",
                ),
            )
        }
    )
    dynamically_granted = RuleInterpreter().plan(
        _package((excluded_selector,)),
        dynamically_granted_observation,
        (
            _request(
                excluded_selector,
                actor=2,
                request_id="dynamic-use",
            ),
        ),
    )
    assert dynamically_granted.dispositions[0].status == "ACCEPTED"

    revoker = _skill(
        "revoker",
        3,
        effects=(
            EffectSpec(
                effect_id="revoke-recipient",
                effect_type="ABILITY_REVOKE",
                target=_ref("target", "seat"),
                grant_skill_id="recipient",
                grant_id="recipient-grant",
            ),
        ),
    )
    revoke_observation = _observation((revoker, recipient))
    revoked = RuleInterpreter().plan(
        _package((revoker, recipient)),
        revoke_observation,
        (_request(revoker, targets=(2,)),),
    )
    assert revoked.ability_updates[0].operation == "REVOKE"
    assert revoked.ability_updates[0].ability_instance_id is None
    assert any(
        instance.ability_instance_id == "inst:recipient:2"
        for instance in revoke_observation.ability_instances
    )

    flow_skill = _skill(
        "boundary-flow",
        3,
        trigger=TriggerSpec(fact_types=("death",), mode="PLAYER_CHOICE"),
        hook_ids=("DAY_SPEECH_BEFORE",),
        effects=(
            EffectSpec(
                effect_id="advance-if-wolf-death",
                effect_type="FLOW",
                flow_action="ADVANCE_TO_NIGHT",
                condition=CompareExpr(
                    op="contains",
                    left=_ref("source_fact", "tags"),
                    right=_literal("wolf"),
                ),
            ),
        ),
        targets=(0, 0),
    )
    flow_package = _package((flow_skill,))

    def flow_batch(tags: tuple[str, ...]):
        fact = DomainFact(fact_id="death", fact_type="death", tags=tags)
        obs = _observation(
            (flow_skill,),
            facts=(fact,),
            current_hook_id="DAY_SPEECH_BEFORE",
        )
        request = _request(
            flow_skill,
            fact_id="death",
            occurrence_id="occ-flow",
            hook_id="DAY_SPEECH_BEFORE",
        )
        return RuleInterpreter().plan(flow_package, obs, (request,))

    successful = flow_batch(("wolf",))
    other = flow_batch(("poison",))
    assert successful.flow_updates[0].action == "ADVANCE_TO_NIGHT"
    assert successful.flow_updates[0].hook_id == "DAY_SPEECH_BEFORE"
    assert other.flow_updates == ()


def test_conflicting_relations_abilities_and_multiple_flows_are_rejected() -> None:
    relation_skill = _skill(
        "relation-conflict",
        1,
        effects=(
            EffectSpec(
                effect_id="add-one",
                effect_type="RELATION_ADD",
                relation_type="pair",
                relation_source=_ref("actor", "seat"),
                relation_target=_ref("target", "seat"),
                relation_expiry_policy="NEVER",
            ),
            EffectSpec(
                effect_id="remove-one",
                effect_type="RELATION_REMOVE",
                relation_type="pair",
                relation_source=_ref("actor", "seat"),
                relation_target=_ref("target", "seat"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="conflicting relation"):
        RuleInterpreter().plan(
            _package((relation_skill,), relations=("pair",)),
            _observation((relation_skill,)),
            (_request(relation_skill, targets=(2,)),),
        )

    target = _skill("revoke-target", 2, targets=(0, 0))
    ability_skill = _skill(
        "ability-conflict",
        3,
        effects=(
            EffectSpec(
                effect_id="grant",
                effect_type="ABILITY_GRANT",
                target=_ref("target", "seat"),
                grant_skill_id="revoke-target",
                grant_id="revoke-target-grant",
            ),
            EffectSpec(
                effect_id="revoke",
                effect_type="ABILITY_REVOKE",
                target=_ref("target", "seat"),
                grant_skill_id="revoke-target",
                grant_id="revoke-target-grant",
            ),
        ),
    )
    with pytest.raises(ValueError, match="conflicting ability"):
        RuleInterpreter().plan(
            _package((target, ability_skill)),
            _observation((target, ability_skill)),
            (_request(ability_skill, targets=(2,)),),
        )

    flow = EffectSpec(
        effect_id="advance",
        effect_type="FLOW",
        flow_action="ADVANCE_TO_NIGHT",
        condition=_literal(True),
    )
    double_flow = _skill(
        "double-flow", 4, effects=(flow, flow.model_copy(update={"effect_id": "advance-two"}))
    )
    with pytest.raises(ValueError, match="multiple FLOW"):
        RuleInterpreter().plan(
            _package((double_flow,)), _observation((double_flow,)), (_request(double_flow),)
        )


def test_rule_model_round_trip_and_legacy_a_package_hash_are_stable() -> None:
    skill = _skill(
        "roundtrip",
        1,
        trigger=TriggerSpec(fact_types=("event",), mode="PLAYER_CHOICE"),
        window_ids=("w1",),
        hook_ids=("DAY_SPEECH_AFTER",),
    )
    policy = BoundaryPolicy(
        last_words_enabled=True,
        eligible_death_causes=("night_kill",),
        sheriff_enabled=True,
        badge_transfer_enabled=None,
        badge_transfer_on_death=None,
    )
    package = _package(
        (skill,),
        windows=(ExecutionWindow(window_id="w1", order=1, phase="NIGHT_ACTION"),),
        groups={"w1": "night-one"},
        boundary_policy=policy,
    )
    restored = ExecutionPackage.model_validate_json(package.model_dump_json())
    observed = _observation(
        (skill,), current_window_id="physical:w1:3", current_logical_window_id="w1"
    ).model_copy(
        update={
            "state_values": (
                RuleStateValue(
                    scope="GAME",
                    skill_id=skill.skill_id,
                    key="count",
                    value=3,
                    expires_at_round=4,
                    expires_at_hook="DAY_SPEECH_AFTER",
                ),
            ),
            "relations": (
                RelationValue(
                    relation_id="expires-next-night",
                    relation_type="marked",
                    source_seat=1,
                    target_seat=2,
                    source_skill_id=skill.skill_id,
                    source_request_id="request-expiry",
                    source_ability_instance_id="instance-expiry",
                    expires_at_round=4,
                    expires_at_hook="NIGHT_ACTION",
                ),
            ),
        }
    )
    assert restored == package
    assert RuleObservation.model_validate_json(observed.model_dump_json()) == observed
    assert package.package_id == restored.package_id

    legacy_skill = SkillSpec(
        skill_id="hash-legacy",
        action_code=1,
        grants=(AbilityGrant(grant_id="g", actor_selector=SelectorExpr(source="players")),),
        timing=(),
        targets=TargetPolicy(selector=SelectorExpr(source="players")),
        usage=UsagePolicy(),
    )
    legacy = ExecutionPackage(
        board_id="legacy",
        board_version="1.0.0",
        actions=(ActionSpec(action_code=1, action_id="A"),),
        skills=(legacy_skill,),
    )
    assert legacy.package_id == "adf9949ed513c71080847413a4f276559cc964adcb5ac05a5102d3cd8c238f55"


def test_frozen_window_context_rejects_unknown_duplicate_forward_and_tampered_metadata() -> None:
    skill = _skill("windowed", 1, window_ids=("w1",), targets=(0, 0)).model_copy(
        update={"timing": ("NIGHT_ACTION",)}
    )
    package = _package(
        (skill,),
        windows=(ExecutionWindow(window_id="w1", order=1, phase="NIGHT_ACTION"),),
        groups={"w1": "g1"},
        boundary_policy=BoundaryPolicy(last_words_enabled=False),
    ).model_copy(update={"actions": (ActionSpec(action_code=1, action_id="WINDOWED"),)})
    registry = ActionRegistry(
        actions=(
            ActionDefinition(
                action_code=1, action_name="WINDOWED", target_policy="none", target_count=0
            ),
        )
    )
    frozen = package.window_metadata
    policy = package.boundary_policy
    assert policy is not None
    validate_execution_package(
        package, registry, available_windows=frozen, expected_boundary_policy=policy
    )

    unknown = skill.model_copy(update={"window_ids": ("missing",)})
    with pytest.raises(ExecutionCompilerError, match="unknown board windows"):
        validate_execution_package(
            package.model_copy(update={"skills": (unknown,)}),
            registry,
            available_windows=frozen,
            expected_boundary_policy=policy,
        )
    changed_phase = (frozen[0].model_copy(update={"phase": "NIGHT_RESOLVE"}),)
    with pytest.raises(ExecutionCompilerError, match="metadata disagrees"):
        validate_execution_package(
            package, registry, available_windows=changed_phase, expected_boundary_policy=policy
        )
    duplicate = (frozen[0], frozen[0].model_copy(update={"order": 2}))
    with pytest.raises(ExecutionCompilerError, match="IDs must be unique"):
        validate_execution_package(
            package, registry, available_windows=duplicate, expected_boundary_policy=policy
        )
    cyclic = (
        ExecutionWindow(window_id="w1", order=1, phase="NIGHT_ACTION", depends_on=("w2",)),
        ExecutionWindow(window_id="w2", order=2, phase="NIGHT_RESOLVE", depends_on=("w1",)),
    )
    with pytest.raises(ExecutionCompilerError, match="cyclic or forward"):
        validate_execution_package(
            package, registry, available_windows=cyclic, expected_boundary_policy=policy
        )


def test_disclosure_hooks_are_limited_to_frozen_or_supported_boundaries() -> None:
    skill = _skill("bad-hook", 1).model_copy(
        update={
            "timing": ("NIGHT_ACTION",),
            "disclosures": (
                DisclosureSpec(
                    disclosure_id="unknown-hook",
                    audience="SELF",
                    hook="not_a_boundary",
                ),
            ),
        }
    )
    package = _package((skill,)).model_copy(
        update={"actions": (ActionSpec(action_code=1, action_id="BAD_HOOK"),)}
    )
    registry = ActionRegistry(
        actions=(
            ActionDefinition(
                action_code=1,
                action_name="BAD_HOOK",
                target_policy="other_alive",
                target_count=1,
            ),
        )
    )

    with pytest.raises(ExecutionCompilerError, match="unsupported hook"):
        validate_execution_package(
            package,
            registry,
            available_windows=(
                ExecutionWindow(window_id="chat", order=1, phase="NIGHT_TEAM_CHAT"),
                ExecutionWindow(
                    window_id="actions", order=2, phase="NIGHT_ACTION", depends_on=("chat",)
                ),
                ExecutionWindow(
                    window_id="close", order=3, phase="NIGHT_RESOLVE", depends_on=("actions",)
                ),
            ),
        )


@pytest.mark.parametrize("group_id", ("night one", "night\n1", "x" * 97))
def test_settlement_group_ids_are_bounded_and_have_no_whitespace_or_controls(
    group_id: str,
) -> None:
    skill = _skill("bounded-group", 1)

    with pytest.raises(ValueError, match="bounded printable identifiers"):
        _package((skill,), groups={"night_actions": group_id})


def test_only_nonclassic_window_topologies_require_derived_frozen_metadata() -> None:
    skill = _skill("base-window-plan", 1).model_copy(update={"timing": ("NIGHT_ACTION",)})
    package = _package((skill,)).model_copy(
        update={"actions": (ActionSpec(action_code=1, action_id="BASE_WINDOW_PLAN"),)}
    )
    registry = ActionRegistry(
        actions=(
            ActionDefinition(
                action_code=1,
                action_name="BASE_WINDOW_PLAN",
                target_policy="other_alive",
                target_count=1,
            ),
        )
    )
    basic_windows = (
        ExecutionWindow(window_id="chat", order=1, phase="NIGHT_TEAM_CHAT"),
        ExecutionWindow(window_id="actions", order=2, phase="NIGHT_ACTION", depends_on=("chat",)),
        ExecutionWindow(window_id="close", order=3, phase="NIGHT_RESOLVE", depends_on=("actions",)),
    )
    validate_execution_package(package, registry, available_windows=basic_windows)

    multi_action_windows = (
        ExecutionWindow(window_id="chat", order=1, phase="NIGHT_TEAM_CHAT"),
        ExecutionWindow(
            window_id="first_action", order=2, phase="NIGHT_ACTION", depends_on=("chat",)
        ),
        ExecutionWindow(
            window_id="second_action", order=3, phase="NIGHT_ACTION", depends_on=("first_action",)
        ),
        ExecutionWindow(
            window_id="close", order=4, phase="NIGHT_RESOLVE", depends_on=("second_action",)
        ),
    )
    with pytest.raises(ExecutionCompilerError, match="frozen execution window metadata"):
        validate_execution_package(package, registry, available_windows=multi_action_windows)

    policy = BoundaryPolicy(last_words_enabled=False)
    derived = package.model_copy(
        update={"window_metadata": multi_action_windows, "boundary_policy": policy}
    )
    validate_execution_package(
        derived,
        registry,
        available_windows=multi_action_windows,
        expected_boundary_policy=policy,
    )
    tampered = derived.model_copy(
        update={
            "window_metadata": (
                multi_action_windows[0],
                multi_action_windows[1],
                multi_action_windows[3],
                multi_action_windows[2],
            )
        }
    )
    with pytest.raises(ExecutionCompilerError, match="metadata disagrees"):
        validate_execution_package(
            tampered,
            registry,
            available_windows=multi_action_windows,
            expected_boundary_policy=policy,
        )


def test_player_field_vocabulary_is_rechecked_against_frozen_restore_context() -> None:
    skill = _skill(
        "field-vocabulary",
        1,
        effects=(
            EffectSpec(
                effect_id="set-role",
                effect_type="PLAYER_FIELD_SET",
                player_field="role_id",
                value=_literal("villager"),
            ),
        ),
    ).model_copy(update={"timing": ("NIGHT_ACTION",)})
    expected_values = PlayerFieldValues(role_ids=("villager",))
    package = _package((skill,), field_values=expected_values).model_copy(
        update={"actions": (ActionSpec(action_code=1, action_id="FIELD_VOCABULARY"),)}
    )
    registry = ActionRegistry(
        actions=(
            ActionDefinition(
                action_code=1,
                action_name="FIELD_VOCABULARY",
                target_policy="other_alive",
                target_count=1,
            ),
        )
    )
    basic_windows = (
        ExecutionWindow(window_id="chat", order=1, phase="NIGHT_TEAM_CHAT"),
        ExecutionWindow(window_id="actions", order=2, phase="NIGHT_ACTION", depends_on=("chat",)),
        ExecutionWindow(window_id="close", order=3, phase="NIGHT_RESOLVE", depends_on=("actions",)),
    )

    validate_execution_package(
        package,
        registry,
        available_windows=basic_windows,
        expected_player_field_values=expected_values,
    )
    tampered = package.model_copy(
        update={"player_field_values": PlayerFieldValues(role_ids=("wolf", "villager"))}
    )
    with pytest.raises(ExecutionCompilerError, match="player field values disagree"):
        validate_execution_package(
            tampered,
            registry,
            available_windows=basic_windows,
            expected_player_field_values=expected_values,
        )
