"""Per-request trusted window context for simultaneous rule plans."""

from __future__ import annotations

from typing import Literal

import pytest

from werewolf.rules import (
    AbilityGrant,
    AbilityInstance,
    ActionSpec,
    DomainFact,
    EffectSpec,
    ExecutionPackage,
    ExecutionWindow,
    InteractionRule,
    LiteralExpr,
    PlayerObservation,
    RefExpr,
    RuleHook,
    RuleInterpreter,
    RuleObservation,
    RuleWindowBinding,
    SelectorExpr,
    SkillRequest,
    SkillSpec,
    TargetPolicy,
    TriggerSpec,
    UsagePolicy,
)

BOARD_ID = "window-binding-test"
BOARD_VERSION = "1.0.0"


def _ref(source: str, name: str) -> RefExpr:
    return RefExpr(op="ref", source=source, name=name)  # type: ignore[arg-type]


def _all_players() -> SelectorExpr:
    return SelectorExpr(source="players", map=_ref("item", "seat"))


def _skill(
    skill_id: str,
    action_code: int,
    *,
    window_ids: tuple[str, ...] = (),
    hook_ids: tuple[RuleHook, ...] = (),
    trigger: TriggerSpec | None = None,
    mode: Literal["PLAYER", "AUTOMATIC"] = "PLAYER",
    targets: tuple[int, int] = (0, 1),
    effects: tuple[EffectSpec, ...] = (),
) -> SkillSpec:
    return SkillSpec(
        skill_id=skill_id,
        action_code=action_code,
        mode=mode,
        grants=(AbilityGrant(grant_id=f"{skill_id}-grant", actor_selector=_all_players()),),
        timing=(),
        window_ids=window_ids,
        hook_ids=hook_ids,
        trigger=trigger,
        targets=TargetPolicy(
            min_targets=targets[0],
            max_targets=targets[1],
            selector=_all_players(),
        ),
        usage=UsagePolicy(),
        effects=effects,
    )


def _package(
    skills: tuple[SkillSpec, ...],
    *,
    windows: tuple[ExecutionWindow, ...] = (),
    settlement_groups: dict[str, str] | None = None,
    interactions: tuple[InteractionRule, ...] = (),
) -> ExecutionPackage:
    action_codes = sorted({skill.action_code for skill in skills})
    return ExecutionPackage(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        actions=tuple(
            ActionSpec(action_code=code, action_id=f"action-{code}") for code in action_codes
        ),
        skills=skills,
        window_metadata=windows,
        window_settlement_groups=settlement_groups or {},
        interactions=interactions,
    )


def _observation(
    skills: tuple[SkillSpec, ...],
    *,
    bindings: tuple[RuleWindowBinding, ...] = (),
    facts: tuple[DomainFact, ...] = (),
    current_window_id: str | None = None,
    current_logical_window_id: str | None = None,
    current_hook_id: RuleHook | None = None,
) -> RuleObservation:
    return RuleObservation(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        revision=12,
        round_number=3,
        players=tuple(PlayerObservation(seat=seat) for seat in range(1, 5)),
        facts=facts,
        ability_instances=tuple(
            AbilityInstance(
                ability_instance_id=f"inst:{skill.skill_id}:{seat}",
                skill_id=skill.skill_id,
                actor_seat=seat,
                grant_id=skill.grants[0].grant_id,
            )
            for skill in skills
            for seat in range(1, 5)
        ),
        current_window_id=current_window_id,
        current_logical_window_id=current_logical_window_id,
        current_hook_id=current_hook_id,
        current_window_bindings=bindings,
    )


def _request(
    skill: SkillSpec,
    *,
    request_id: str,
    actor: int = 1,
    targets: tuple[int, ...] = (),
    origin: Literal["PLAYER", "AUTOMATIC"] = "PLAYER",
    fact_id: str | None = None,
    occurrence_id: str | None = None,
    window_id: str | None = None,
    logical_window_id: str | None = None,
    hook_id: RuleHook | None = None,
) -> SkillRequest:
    return SkillRequest(
        request_id=request_id,
        ability_instance_id=f"inst:{skill.skill_id}:{actor}",
        skill_id=skill.skill_id,
        action_code=skill.action_code,
        actor_seat=actor,
        targets=targets,
        origin=origin,
        source_fact_id=fact_id,
        trigger_occurrence_id=occurrence_id,
        window_id=window_id,
        logical_window_id=logical_window_id,
        hook_id=hook_id,
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("window_id", "other-physical-window", "window_mismatch"),
        ("logical_window_id", "other-logical-window", "logical_window_mismatch"),
        ("hook_id", "DAY_SPEECH_AFTER", "hook_mismatch"),
    ),
)
def test_bound_request_must_match_its_physical_logical_and_hook_context(
    field: str, value: str, reason: str
) -> None:
    skill = _skill(
        "bound-action",
        1,
        window_ids=("night_actions",),
        hook_ids=("DAY_SPEECH_BEFORE",),
        targets=(0, 0),
    )
    request = _request(
        skill,
        request_id="bound-request",
        window_id="physical:night-actions:3",
        logical_window_id="night_actions",
        hook_id="DAY_SPEECH_BEFORE",
    )
    observation = _observation(
        (skill,),
        bindings=(
            RuleWindowBinding(
                request_id=request.request_id,
                window_id="physical:night-actions:3",
                logical_window_id="night_actions",
                hook_id="DAY_SPEECH_BEFORE",
            ),
        ),
    )
    package = _package((skill,))

    result = RuleInterpreter().plan(
        package,
        observation,
        (request.model_copy(update={field: value}),),
    )

    assert result.dispositions[0].status == "REJECTED"
    assert result.dispositions[0].reason == reason


def test_nonempty_binding_table_rejects_requests_without_a_binding() -> None:
    skill = _skill("bound-action", 1, window_ids=("night_actions",), targets=(0, 0))
    request = _request(
        skill,
        request_id="unbound-request",
        window_id="physical:night-actions:3",
        logical_window_id="night_actions",
    )
    observation = _observation(
        (skill,),
        bindings=(
            RuleWindowBinding(
                request_id="some-other-request",
                window_id="physical:night-actions:3",
                logical_window_id="night_actions",
            ),
        ),
        # A legacy single-window value must not act as fallback once the table is present.
        current_window_id="physical:night-actions:3",
        current_logical_window_id="night_actions",
    )

    result = RuleInterpreter().plan(_package((skill,)), observation, (request,))

    assert result.dispositions[0].status == "REJECTED"
    assert result.dispositions[0].reason == "window_binding_missing"


def test_observation_rejects_duplicate_window_binding_request_ids() -> None:
    skill = _skill("bound-action", 1, targets=(0, 0))
    binding = RuleWindowBinding(
        request_id="duplicate-request",
        window_id="physical:night-actions:3",
        logical_window_id="night_actions",
    )

    with pytest.raises(ValueError, match="current window bindings must have unique request ids"):
        _observation((skill,), bindings=(binding, binding))

    invalid_observation = _observation((skill,), bindings=(binding,)).model_copy(
        update={"current_window_bindings": (binding, binding)}
    )
    with pytest.raises(ValueError, match="current window bindings must have unique request ids"):
        RuleInterpreter().plan(_package((skill,)), invalid_observation, ())


def test_shared_action_code_does_not_transfer_window_authority_between_skills() -> None:
    first = _skill("first-window-skill", 7, window_ids=("first_window",), targets=(0, 0))
    second = _skill("second-window-skill", 7, window_ids=("second_window",), targets=(0, 0))
    first_request = _request(
        first,
        request_id="first-request",
        window_id="physical:second:3",
        logical_window_id="second_window",
    )
    second_request = _request(
        second,
        request_id="second-request",
        actor=2,
        window_id="physical:second:3",
        logical_window_id="second_window",
    )
    observation = _observation(
        (first, second),
        bindings=(
            RuleWindowBinding(
                request_id=first_request.request_id,
                window_id="physical:second:3",
                logical_window_id="second_window",
            ),
            RuleWindowBinding(
                request_id=second_request.request_id,
                window_id="physical:second:3",
                logical_window_id="second_window",
            ),
        ),
    )

    result = RuleInterpreter().plan(
        _package((first, second)), observation, (second_request, first_request)
    )
    dispositions = {item.request_id: item for item in result.dispositions}

    assert dispositions["first-request"].reason == "wrong_window"
    assert dispositions["second-request"].status == "ACCEPTED"


@pytest.mark.parametrize(
    ("trigger_mode", "skill_mode", "origin"),
    (("PLAYER_CHOICE", "PLAYER", "PLAYER"), ("AUTOMATIC", "AUTOMATIC", "AUTOMATIC")),
)
def test_trigger_requests_use_their_bound_window_context(
    trigger_mode: Literal["PLAYER_CHOICE", "AUTOMATIC"],
    skill_mode: Literal["PLAYER", "AUTOMATIC"],
    origin: Literal["PLAYER", "AUTOMATIC"],
) -> None:
    skill = _skill(
        "triggered-action",
        1,
        mode=skill_mode,
        window_ids=("death_trigger",) if trigger_mode == "PLAYER_CHOICE" else (),
        trigger=TriggerSpec(fact_types=("DEATH_CONFIRMED",), mode=trigger_mode),
        targets=(0, 0),
    )
    logical_window_id = "death_trigger" if trigger_mode == "PLAYER_CHOICE" else None
    request = _request(
        skill,
        request_id="trigger-request",
        origin=origin,
        fact_id="death-fact",
        occurrence_id="occurrence-3",
        window_id="virtual:occurrence-3",
        logical_window_id=logical_window_id,
    )
    observation = _observation(
        (skill,),
        facts=(DomainFact(fact_id="death-fact", fact_type="DEATH_CONFIRMED"),),
        bindings=(
            RuleWindowBinding(
                request_id=request.request_id,
                window_id="virtual:occurrence-3",
                logical_window_id=logical_window_id,
            ),
        ),
    )

    result = RuleInterpreter().plan(_package((skill,)), observation, (request,))

    assert result.dispositions[0].status == "ACCEPTED"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("logical_window_id", "invented-logical-window", "logical_window_mismatch"),
        ("hook_id", "DAY_SPEECH_BEFORE", "hook_mismatch"),
    ),
)
def test_virtual_automatic_occurrence_rejects_unbound_logical_or_hook_claims(
    field: str, value: str, reason: str
) -> None:
    skill = _skill(
        "automatic-action",
        1,
        mode="AUTOMATIC",
        trigger=TriggerSpec(fact_types=("DEATH_CONFIRMED",), mode="AUTOMATIC"),
        targets=(0, 0),
    )
    request = _request(
        skill,
        request_id="automatic-request",
        origin="AUTOMATIC",
        fact_id="death-fact",
        occurrence_id="occurrence-4",
        window_id="auto-occurrence-4",
    )
    observation = _observation(
        (skill,),
        facts=(DomainFact(fact_id="death-fact", fact_type="DEATH_CONFIRMED"),),
        bindings=(
            RuleWindowBinding(
                request_id=request.request_id,
                window_id="auto-occurrence-4",
                logical_window_id=None,
                hook_id=None,
            ),
        ),
    )

    result = RuleInterpreter().plan(
        _package((skill,)),
        observation,
        (request.model_copy(update={field: value}),),
    )

    assert result.dispositions[0].status == "REJECTED"
    assert result.dispositions[0].reason == reason


def test_empty_binding_table_preserves_single_window_compatibility() -> None:
    skill = _skill("legacy-window-skill", 1, window_ids=("night_actions",), targets=(0, 0))
    request = _request(
        skill,
        request_id="legacy-request",
        window_id="physical:night-actions:3",
        logical_window_id="night_actions",
    )
    observation = _observation(
        (skill,),
        current_window_id="physical:night-actions:3",
        current_logical_window_id="night_actions",
    )

    result = RuleInterpreter().plan(_package((skill,)), observation, (request,))

    assert observation.current_window_bindings == ()
    assert result.dispositions[0].status == "ACCEPTED"


def test_same_group_damage_and_guard_bind_independently_and_reduce_simultaneously() -> None:
    wolf = _skill(
        "wolf-kill",
        9,
        window_ids=("wolf_kill",),
        targets=(1, 1),
        effects=(
            EffectSpec(
                effect_id="wolf-damage",
                effect_type="DAMAGE",
                target=_ref("target", "seat"),
                value=LiteralExpr(value=1),
                tags=("wolf_attack",),
            ),
        ),
    )
    guard = _skill(
        "guard",
        9,
        window_ids=("guard",),
        targets=(1, 1),
        effects=(
            EffectSpec(
                effect_id="guard-protection",
                effect_type="PROTECTION",
                target=_ref("target", "seat"),
                tags=("guard",),
            ),
        ),
    )
    wolf_request = _request(
        wolf,
        request_id="wolf-request",
        actor=1,
        targets=(3,),
        window_id="physical:wolf-kill:3",
        logical_window_id="wolf_kill",
    )
    guard_request = _request(
        guard,
        request_id="guard-request",
        actor=2,
        targets=(3,),
        window_id="physical:guard:3",
        logical_window_id="guard",
    )
    bindings = (
        RuleWindowBinding(
            request_id=wolf_request.request_id,
            window_id="physical:wolf-kill:3",
            logical_window_id="wolf_kill",
        ),
        RuleWindowBinding(
            request_id=guard_request.request_id,
            window_id="physical:guard:3",
            logical_window_id="guard",
        ),
    )
    observation = _observation((wolf, guard), bindings=bindings)
    package = _package(
        (wolf, guard),
        windows=(
            ExecutionWindow(window_id="wolf_kill", order=1, phase="NIGHT_ACTION"),
            ExecutionWindow(window_id="guard", order=2, phase="NIGHT_ACTION"),
        ),
        settlement_groups={"wolf_kill": "night_resolution", "guard": "night_resolution"},
        interactions=(
            InteractionRule(
                interaction_id="guard-blocks-wolf-kill",
                rule_type="BLOCK_DAMAGE",
                damage_tags=("wolf_attack",),
                counter_tags=("guard",),
            ),
            InteractionRule(interaction_id="confirm-death", rule_type="CONFIRM_DEATH"),
        ),
    )

    forward = RuleInterpreter().plan(package, observation, (wolf_request, guard_request))
    reverse = RuleInterpreter().plan(package, observation, (guard_request, wolf_request))

    assert forward == reverse
    assert {item.status for item in forward.dispositions} == {"ACCEPTED"}
    assert len(forward.mortality) == 1
    assert forward.mortality[0].seat == 3
    assert forward.mortality[0].deceased is False
    intent_by_request = {item.source_request_id: item for item in forward.intents}
    assert intent_by_request["wolf-request"].window_id == "physical:wolf-kill:3"
    assert intent_by_request["wolf-request"].logical_window_id == "wolf_kill"
    assert intent_by_request["wolf-request"].source_rule_id == "wolf-damage"
    assert intent_by_request["wolf-request"].skill_id == wolf.skill_id
    assert intent_by_request["wolf-request"].ability_instance_id == wolf_request.ability_instance_id
    assert intent_by_request["guard-request"].window_id == "physical:guard:3"
    assert intent_by_request["guard-request"].logical_window_id == "guard"
    assert intent_by_request["guard-request"].source_rule_id == "guard-protection"
    assert intent_by_request["guard-request"].skill_id == guard.skill_id
    assert (
        intent_by_request["guard-request"].ability_instance_id == guard_request.ability_instance_id
    )
