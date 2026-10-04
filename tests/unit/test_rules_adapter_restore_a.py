"""Adapter projections from frozen, typed rule state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from werewolf.game.state import (
    AbilityInstanceState,
    GameState,
    PlayerState,
    RuleFactRecord,
    RuleLedgerEntry,
    RuleStateValue,
)
from werewolf.rules.adapter import RuleExecutionAdapter
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
    SelectorExpr,
    SkillRequest,
    SkillSpec,
    StateDeclaration,
    TargetPolicy,
    UsagePolicy,
)

NOW = datetime(2026, 10, 4, tzinfo=UTC)
_SEAT_SELECTOR = SelectorExpr(
    source="players",
    map=RefExpr(source="item", name="seat"),
)


def _package(*, effect_id: str = "record-request") -> ExecutionPackage:
    skill = SkillSpec(
        skill_id="record-choice",
        action_code=1,
        grants=(AbilityGrant(grant_id="record-choice-grant", actor_selector=_SEAT_SELECTOR),),
        timing=("night",),
        targets=TargetPolicy(
            min_targets=0,
            max_targets=2,
            selector=_SEAT_SELECTOR,
        ),
        usage=UsagePolicy(),
        effects=(
            EffectSpec(
                effect_id=effect_id,
                effect_type="FACT",
                target=RefExpr(source="target", name="seat"),
                fact_type="request_recorded",
            ),
        ),
    )
    return ExecutionPackage(
        board_id="adapter-restore-test",
        board_version="1.0.0",
        actions=(ActionSpec(action_code=1, action_id="RECORD", allow_pass=True),),
        skills=(skill,),
        state_declarations=(
            # The adapter projects this declared nested value into the observation.
            StateDeclaration(
                skill_id="record-choice",
                key="nested_state",
                value_type="json",
                initial=None,
            ),
        ),
    )


def _state(package: ExecutionPackage) -> GameState:
    nested_fact_data = {
        "history": [
            {"request": "first", "tags": ["dawn", {"source": "ledger"}]},
            [1, {"enabled": True}],
        ],
        "metadata": {"origin": "typed-ledger"},
    }
    nested_skill_state = {
        "selections": [[2, 3], {"flags": [True, {"rank": 4}]}],
        "metadata": {"source": "rule-state"},
    }
    ability = AbilityInstanceState(
        ability_instance_id="record-choice-ability",
        skill_id="record-choice",
        grant_id="record-choice-grant",
        action_code=1,
        actor_seat=1,
        grant_kind="ACTIVE",
    )
    fact = RuleFactRecord(
        fact_id="prior-nested-fact",
        fact_type="prior_choice",
        source_rule_id="record-choice",
        source_request_id="prior-request",
        actor_seat=1,
        data=nested_fact_data,
    )
    ledger = RuleLedgerEntry(
        batch_id="prior-batch",
        package_id=package.package_id,
        group_id="prior-group",
        timing="night",
        read_revision=0,
        committed_revision=1,
        round_no=1,
        request_ids=("prior-request",),
        actor_seats=(1,),
        skill_ids=("record-choice",),
        action_codes=(1,),
        facts=(fact,),
        outcome_digest="a" * 64,
        created_at=NOW,
    )
    state_value = RuleStateValue(
        scope="ABILITY",
        scope_id=ability.ability_instance_id,
        key="nested_state",
        value_type="json",
        value=nested_skill_state,
        source_batch_id="prior-batch",
    )
    return GameState(
        game_id="adapter-test-game",
        created_at=NOW,
        updated_at=NOW,
        round_no=1,
        players={1: PlayerState(seat=1, role_id="seer", faction_id="good")},
        ability_instances=(ability,),
        rule_state=(state_value,),
        rule_ledger=(ledger,),
    )


def test_observation_and_plan_project_nested_json_from_typed_game_ledger() -> None:
    package = _package()
    state = _state(package)
    adapter = RuleExecutionAdapter(package)

    frozen_state_value = state.rule_state[0].value
    assert isinstance(frozen_state_value, dict)
    assert isinstance(frozen_state_value["selections"], tuple)
    frozen_fact_data = state.rule_ledger[0].facts[0].data
    assert isinstance(frozen_fact_data["history"], tuple)

    observation = adapter.observation(state, group_id="night-group", timing="night")

    nested_fact = observation.facts[0]
    assert isinstance(nested_fact, DomainFact)
    assert nested_fact.data == {
        "history": [
            {"request": "first", "tags": ["dawn", {"source": "ledger"}]},
            [1, {"enabled": True}],
        ],
        "metadata": {"origin": "typed-ledger"},
    }
    assert observation.skill_state[0].value == {
        "selections": [[2, 3], {"flags": [True, {"rank": 4}]}],
        "metadata": {"source": "rule-state"},
    }
    with pytest.raises(TypeError, match="immutable"):
        nested_fact.data["metadata"]["origin"] = "changed"  # type: ignore[index]

    batch = adapter.plan(
        state,
        (
            SkillRequest(
                request_id="current-choice",
                ability_instance_id="record-choice-ability",
                skill_id="record-choice",
                action_code=1,
                actor_seat=1,
            ),
        ),
        group_id="night-group",
        timing="night",
    )
    assert batch.dispositions[0].status == "ACCEPTED"
    assert any(fact.fact_type == "request_recorded" for fact in batch.facts)


def test_temporary_fact_ids_are_bounded_and_bind_long_raw_components() -> None:
    request_id = "r" * 128
    effect_id = "e" * 128
    package = _package(effect_id=effect_id)
    observation = RuleObservation(
        board_id=package.board_id,
        board_version=package.board_version,
        revision=0,
        round_number=1,
        players=tuple(PlayerObservation(seat=seat) for seat in (1, 2, 3)),
        ability_instances=(
            AbilityInstance(
                ability_instance_id="record-choice-ability",
                skill_id="record-choice",
                actor_seat=1,
                grant_id="record-choice-grant",
            ),
        ),
        group_id="night-group",
        timing="night",
    )
    request = SkillRequest(
        request_id=request_id,
        ability_instance_id="record-choice-ability",
        skill_id="record-choice",
        action_code=1,
        actor_seat=1,
        targets=(2, 3),
    )
    adapter = RuleExecutionAdapter(package)

    facts = adapter._request_facts(observation, (request,))
    repeated_facts = adapter._request_facts(observation, (request,))

    assert len(facts) == 2
    assert [fact.fact_id for fact in facts] == [fact.fact_id for fact in repeated_facts]
    assert len({fact.fact_id for fact in facts}) == 2
    assert all(len(fact.fact_id) <= 128 for fact in facts)
    assert {fact.target_seat for fact in facts} == {2, 3}
