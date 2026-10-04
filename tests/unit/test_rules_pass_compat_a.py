"""Regression coverage for classic player-skill PASS declarations."""

from __future__ import annotations

import asyncio
from pathlib import Path

from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.rules.compat import CLASSIC_BOARD_ID, compile_legacy_execution
from werewolf.rules.interpreter import RuleInterpreter
from werewolf.rules.models import AbilityInstance, PlayerObservation, RuleObservation, SkillRequest

PROJECT_ROOT = Path(__file__).parents[2]


def _classic_execution():
    async def load_package():
        return await KnowledgePackageLoader(PROJECT_ROOT / "vault" / "published").load(
            f"{CLASSIC_BOARD_ID}@1.0.0"
        )

    return compile_legacy_execution(asyncio.run(load_package())).execution


def _observation(instances: tuple[AbilityInstance, ...]) -> RuleObservation:
    return RuleObservation(
        board_id=CLASSIC_BOARD_ID,
        board_version="1.0.0",
        revision=7,
        round_number=2,
        timing="NIGHT_ACTION",
        group_id="classic-pass-regression",
        players=(
            PlayerObservation(seat=1, role_id="wolf", faction_id="wolf"),
            PlayerObservation(
                seat=2,
                role_id="witch",
                faction_id="good",
                resources={"witch_heal": 1, "witch_poison": 1},
            ),
            PlayerObservation(seat=3, role_id="villager", faction_id="good"),
        ),
        ability_instances=instances,
    )


def test_classic_player_skills_allow_pass_but_host_exile_does_not() -> None:
    actions = {item.action_code: item for item in _classic_execution().actions}

    assert all(actions[code].allow_pass for code in (101, 102, 103, 104, 105))
    assert actions[203].allow_pass is False
    assert actions[299].allow_pass is True


def test_passed_wolf_attack_notifies_witch_without_a_target_or_damage() -> None:
    package = _classic_execution()
    instance = AbilityInstance(
        ability_instance_id="wolf-kill-instance",
        skill_id="wolf_kill",
        actor_seat=1,
        grant_id="wolf_team_shared",
    )
    request = SkillRequest(
        request_id="wolf-pass",
        ability_instance_id=instance.ability_instance_id,
        skill_id="wolf_kill",
        action_code=101,
        actor_seat=1,
        passed=True,
    )

    batch = RuleInterpreter().plan(package, _observation((instance,)), (request,))

    assert batch.dispositions[0].status == "PASSED"
    assert batch.intents == ()
    assert batch.effects == ()
    assert batch.cost_updates == ()
    assert len(batch.disclosures) == 1
    notice = batch.disclosures[0]
    assert notice.disclosure_id == "wolf_pass_notice_to_witch"
    assert notice.audience == "SEATS"
    assert notice.recipients == (2,)
    assert notice.fields == {}
    assert notice.hook == "NIGHT_ACTION"
    assert notice.event_type == "wolf_attack_none"


def test_witch_can_pass_both_potions_without_spending_either_resource() -> None:
    package = _classic_execution()
    instances = (
        AbilityInstance(
            ability_instance_id="witch-heal-instance",
            skill_id="witch_heal",
            actor_seat=2,
            grant_id="witch_heal",
        ),
        AbilityInstance(
            ability_instance_id="witch-poison-instance",
            skill_id="witch_poison",
            actor_seat=2,
            grant_id="witch_poison",
        ),
    )
    requests = (
        SkillRequest(
            request_id="witch-heal-pass",
            ability_instance_id="witch-heal-instance",
            skill_id="witch_heal",
            action_code=104,
            actor_seat=2,
            passed=True,
        ),
        SkillRequest(
            request_id="witch-poison-pass",
            ability_instance_id="witch-poison-instance",
            skill_id="witch_poison",
            action_code=103,
            actor_seat=2,
            passed=True,
        ),
    )

    batch = RuleInterpreter().plan(package, _observation(instances), requests)

    assert {item.status for item in batch.dispositions} == {"PASSED"}
    assert batch.intents == ()
    assert batch.effects == ()
    assert batch.cost_updates == ()
