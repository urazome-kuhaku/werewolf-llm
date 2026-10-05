"""Legacy trigger abilities need a trusted per-request occurrence binding."""

from __future__ import annotations

import pytest

from werewolf.rules.interpreter import RuleInterpreter
from werewolf.rules.models import (
    AbilityGrant,
    AbilityInstance,
    ActionSpec,
    DomainFact,
    EffectSpec,
    ExecutionPackage,
    PlayerObservation,
    RefExpr,
    RuleObservation,
    RuleWindowBinding,
    SelectorExpr,
    SkillRequest,
    SkillSpec,
    TargetPolicy,
    TriggerSpec,
    UsagePolicy,
)

_BOARD_ID = "legacy-binding-test"
_BOARD_VERSION = "1.0.0"
_REQUEST_ID = "legacy-hunter-request"
_INSTANCE_ID = "legacy-hunter-instance"
_OCCURRENCE_ID = "death-occurrence-8"
_SOURCE_FACT_ID = "death-confirmed-8"
_WINDOW_ID = "physical:death-trigger:8"
_LOGICAL_WINDOW_ID = "death_trigger"


def _skill(*, trigger: TriggerSpec | None = None) -> SkillSpec:
    return SkillSpec(
        skill_id="classic-hunter-exile",
        action_code=708,
        grants=(
            AbilityGrant(
                grant_id="classic-hunter-grant",
                actor_selector=SelectorExpr(
                    source="players",
                    map=RefExpr(source="item", name="seat"),
                ),
            ),
        ),
        timing=("NIGHT_ACTION",),
        window_ids=(_LOGICAL_WINDOW_ID,),
        trigger=trigger,
        targets=TargetPolicy(
            min_targets=0,
            max_targets=0,
            selector=SelectorExpr(
                source="players",
                map=RefExpr(source="item", name="seat"),
            ),
        ),
        usage=UsagePolicy(),
        effects=(
            EffectSpec(effect_id="hunter-exiled", effect_type="FACT", fact_type="hunter_exiled"),
        ),
    )


def _package(skill: SkillSpec) -> ExecutionPackage:
    return ExecutionPackage(
        board_id=_BOARD_ID,
        board_version=_BOARD_VERSION,
        actions=(ActionSpec(action_code=skill.action_code, action_id="CLASSIC_HUNTER_EXILE"),),
        skills=(skill,),
    )


def _fact() -> DomainFact:
    return DomainFact(
        fact_id=_SOURCE_FACT_ID,
        fact_type="DEATH_CONFIRMED",
        target_seat=2,
        death_cause="hunter_exile",
        tags=("exiled",),
    )


def _observation(
    skill: SkillSpec,
    *,
    legacy_trigger: bool = True,
    include_source_fact: bool = True,
    timing: str = "TRIGGER_ACTION",
) -> RuleObservation:
    return RuleObservation(
        board_id=_BOARD_ID,
        board_version=_BOARD_VERSION,
        revision=18,
        round_number=3,
        players=(PlayerObservation(seat=1), PlayerObservation(seat=2)),
        facts=(_fact(),) if include_source_fact else (),
        ability_instances=(
            AbilityInstance(
                ability_instance_id=_INSTANCE_ID,
                skill_id=skill.skill_id,
                actor_seat=1,
                grant_id=skill.grants[0].grant_id,
            ),
        ),
        timing=timing,
        current_window_id=_WINDOW_ID,
        current_logical_window_id=_LOGICAL_WINDOW_ID,
        current_window_bindings=(
            RuleWindowBinding(
                request_id=_REQUEST_ID,
                window_id=_WINDOW_ID,
                logical_window_id=_LOGICAL_WINDOW_ID,
                trigger_occurrence_id=_OCCURRENCE_ID,
                source_fact_id=_SOURCE_FACT_ID,
                legacy_trigger=legacy_trigger,
            ),
        ),
    )


def _request(skill: SkillSpec) -> SkillRequest:
    return SkillRequest(
        request_id=_REQUEST_ID,
        ability_instance_id=_INSTANCE_ID,
        skill_id=skill.skill_id,
        action_code=skill.action_code,
        actor_seat=1,
        trigger_occurrence_id=_OCCURRENCE_ID,
        source_fact_id=_SOURCE_FACT_ID,
        window_id=_WINDOW_ID,
        logical_window_id=_LOGICAL_WINDOW_ID,
    )


def test_adapter_bound_legacy_trigger_executes_and_keeps_its_provenance() -> None:
    skill = _skill()
    request = _request(skill)

    batch = RuleInterpreter().plan(
        _package(skill),
        _observation(skill),
        (request,),
    )

    assert batch.dispositions[0].status == "ACCEPTED"
    assert len(batch.intents) == 1
    assert batch.intents[0].fact_type == "hunter_exiled"
    assert batch.intents[0].source_request_id == request.request_id
    assert batch.intents[0].trigger_occurrence_id == _OCCURRENCE_ID
    assert batch.intents[0].source_fact_id == _SOURCE_FACT_ID


def test_triggerless_request_without_legacy_binding_stays_rejected() -> None:
    skill = _skill()
    batch = RuleInterpreter().plan(
        _package(skill),
        _observation(skill, legacy_trigger=False),
        (_request(skill),),
    )

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "trigger_occurrence_not_bound"


def test_legacy_binding_requires_the_source_fact_to_be_present() -> None:
    skill = _skill()
    batch = RuleInterpreter().plan(
        _package(skill),
        _observation(skill, include_source_fact=False),
        (_request(skill),),
    )

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "trigger_occurrence_not_bound"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("trigger_occurrence_id", "caller-made-occurrence"),
        ("source_fact_id", "caller-made-fact"),
    ),
)
def test_legacy_binding_rejects_forged_occurrence_or_source_claims(
    field: str,
    value: str,
) -> None:
    skill = _skill()
    request = _request(skill).model_copy(update={field: value})
    batch = RuleInterpreter().plan(_package(skill), _observation(skill), (request,))

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "trigger_occurrence_not_bound"


def test_legacy_binding_is_confined_to_trigger_action_timing() -> None:
    skill = _skill()
    batch = RuleInterpreter().plan(
        _package(skill),
        _observation(skill, timing="NIGHT_ACTION"),
        (_request(skill),),
    )

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "wrong_timing"


def test_declared_trigger_cannot_use_legacy_timing_authorization() -> None:
    skill = _skill(trigger=TriggerSpec(fact_types=("DEATH_CONFIRMED",), mode="PLAYER_CHOICE"))
    batch = RuleInterpreter().plan(
        _package(skill),
        _observation(skill),
        (_request(skill),),
    )

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "wrong_timing"
