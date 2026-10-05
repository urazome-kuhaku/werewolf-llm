"""Hook timing needs an adapter-authored occurrence binding."""

from __future__ import annotations

import pytest

from werewolf.rules.interpreter import RuleInterpreter
from werewolf.rules.models import (
    AbilityGrant,
    AbilityInstance,
    ActionSpec,
    ExecutionPackage,
    PlayerObservation,
    RefExpr,
    RuleHook,
    RuleObservation,
    RuleWindowBinding,
    SelectorExpr,
    SkillRequest,
    SkillSpec,
    TargetPolicy,
    UsagePolicy,
)

_BOARD_ID = "hook-timing-test"
_BOARD_VERSION = "1.0.0"
_REQUEST_ID = "hook-request"
_INSTANCE_ID = "hook-instance"
_OCCURRENCE_ID = "manager-occurrence"
_SOURCE_FACT_ID = "manager-speech-source"


def _skill(hook_id: RuleHook) -> SkillSpec:
    return SkillSpec(
        skill_id="speaker-hook",
        action_code=701,
        grants=(
            AbilityGrant(
                grant_id="speaker-hook-grant",
                actor_selector=SelectorExpr(
                    source="players",
                    map=RefExpr(source="item", name="seat"),
                ),
            ),
        ),
        timing=("DAY_SPEECH",),
        hook_ids=(hook_id,),
        targets=TargetPolicy(
            min_targets=0,
            max_targets=0,
            selector=SelectorExpr(
                source="players",
                map=RefExpr(source="item", name="seat"),
            ),
        ),
        usage=UsagePolicy(),
    )


def _package(skill: SkillSpec) -> ExecutionPackage:
    return ExecutionPackage(
        board_id=_BOARD_ID,
        board_version=_BOARD_VERSION,
        actions=(ActionSpec(action_code=skill.action_code, action_id="SPEAKER_HOOK"),),
        skills=(skill,),
    )


def _observation(
    skill: SkillSpec,
    *,
    timing: str = "TRIGGER_ACTION",
    hook_id: RuleHook | None = None,
    trusted_binding: bool = True,
) -> RuleObservation:
    bindings = (
        (
            RuleWindowBinding(
                request_id=_REQUEST_ID,
                window_id="hook-window",
                hook_id=hook_id,
                trigger_occurrence_id=_OCCURRENCE_ID,
                source_fact_id=_SOURCE_FACT_ID,
            ),
        )
        if trusted_binding and hook_id is not None
        else ()
    )
    return RuleObservation(
        board_id=_BOARD_ID,
        board_version=_BOARD_VERSION,
        revision=1,
        round_number=1,
        players=(PlayerObservation(seat=1),),
        ability_instances=(
            AbilityInstance(
                ability_instance_id=_INSTANCE_ID,
                skill_id=skill.skill_id,
                actor_seat=1,
                grant_id=skill.grants[0].grant_id,
            ),
        ),
        timing=timing,
        current_hook_id=hook_id,
        current_window_bindings=bindings,
    )


def _request(
    skill: SkillSpec,
    hook_id: RuleHook,
    *,
    occurrence_id: str = _OCCURRENCE_ID,
    source_fact_id: str = _SOURCE_FACT_ID,
    request_hook_id: RuleHook | None = None,
) -> SkillRequest:
    return SkillRequest(
        request_id=_REQUEST_ID,
        ability_instance_id=_INSTANCE_ID,
        skill_id=skill.skill_id,
        action_code=skill.action_code,
        actor_seat=1,
        trigger_occurrence_id=occurrence_id,
        source_fact_id=source_fact_id,
        window_id="hook-window",
        hook_id=request_hook_id if request_hook_id is not None else hook_id,
    )


@pytest.mark.parametrize("hook_id", ["DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"])
def test_day_speech_hook_timing_uses_trusted_occurrence_binding(hook_id: RuleHook) -> None:
    skill = _skill(hook_id)
    observation = _observation(skill, hook_id=hook_id)

    batch = RuleInterpreter().plan(_package(skill), observation, (_request(skill, hook_id),))

    assert batch.dispositions[0].status == "ACCEPTED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("occurrence_id", "caller-invented-occurrence"),
        ("source_fact_id", "caller-invented-source"),
    ],
)
def test_hook_request_cannot_forge_occurrence_or_source(
    field: str,
    value: str,
) -> None:
    hook_id: RuleHook = "DAY_SPEECH_AFTER"
    skill = _skill(hook_id)
    observation = _observation(skill, hook_id=hook_id)
    request = _request(skill, hook_id)
    if field == "occurrence_id":
        request = request.model_copy(update={"trigger_occurrence_id": value})
    else:
        request = request.model_copy(update={"source_fact_id": value})

    batch = RuleInterpreter().plan(_package(skill), observation, (request,))

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "trigger_occurrence_not_bound"


def test_hook_timing_rejects_a_different_hook_than_the_skill_declares() -> None:
    skill = _skill("DAY_SPEECH_AFTER")
    observation = _observation(skill, hook_id="DAY_SPEECH_BEFORE")
    request = _request(skill, "DAY_SPEECH_BEFORE")

    batch = RuleInterpreter().plan(_package(skill), observation, (request,))

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "wrong_hook"


def test_hook_timing_does_not_authorize_another_observation_timing() -> None:
    hook_id: RuleHook = "DAY_SPEECH_AFTER"
    skill = _skill(hook_id)
    observation = _observation(skill, timing="NIGHT_ACTION", hook_id=hook_id)

    batch = RuleInterpreter().plan(_package(skill), observation, (_request(skill, hook_id),))

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "wrong_timing"


def test_hook_fields_without_an_active_occurrence_do_not_open_day_skill() -> None:
    hook_id: RuleHook = "DAY_SPEECH_AFTER"
    skill = _skill(hook_id)
    observation = _observation(skill, hook_id=hook_id, trusted_binding=False)
    request = _request(skill, hook_id).model_copy(
        update={
            "trigger_occurrence_id": None,
            "source_fact_id": None,
            "window_id": None,
        }
    )

    batch = RuleInterpreter().plan(_package(skill), observation, (request,))

    assert batch.dispositions[0].status == "REJECTED"
    assert batch.dispositions[0].reason == "wrong_timing"
