"""Cross-model checks for the candidate role/action execution contract.

Role Markdown is the source of role-specific authorization while
``config/actions.yaml`` owns the board-independent wire action definitions.
These checks keep the two models aligned before a candidate can be reviewed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from werewolf.game.actions import load_action_registry
from werewolf.knowledge.frontmatter import parse_markdown
from werewolf.knowledge.role import RoleDefinition

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ROLE_ROOT = REPOSITORY_ROOT / "vault" / "_workbench" / "official_12_20260928" / "draft" / "roles"


def _load_role(role_id: str) -> RoleDefinition:
    path = ROLE_ROOT / role_id / "1.0.0" / "role.md"
    parsed = parse_markdown(path.read_bytes())
    return RoleDefinition.model_validate(parsed.frontmatter)


@pytest.mark.parametrize(
    ("role_id", "action_code", "action_name"),
    (
        ("wolf", 101, "WOLF_KILL"),
        ("seer", 102, "SEER_INSPECT"),
        ("witch", 103, "WITCH_POISON"),
        ("witch", 104, "WITCH_HEAL"),
    ),
)
def test_candidate_role_abilities_match_action_registry(
    role_id: str,
    action_code: int,
    action_name: str,
) -> None:
    """Every executable candidate ability must resolve in the global registry."""

    role = _load_role(role_id)
    ability = next(
        (item for item in role.abilities if item.action_code == action_code),
        None,
    )
    assert ability is not None, f"{role_id} must declare action {action_code}"

    action = load_action_registry().get(action_code)
    assert action.action_name == action_name
    assert ability.target_rule.min_targets == action.target_count
    assert ability.target_rule.max_targets == action.target_count
    if ability.resource is None:
        assert action.resource_id is None
    else:
        assert ability.resource.resource_id == action.resource_id


def test_candidate_role_action_codes_are_unique_and_registered() -> None:
    registry = load_action_registry()
    abilities = [
        ability
        for role_id in ("wolf", "seer", "witch")
        for ability in _load_role(role_id).abilities
        if ability.trigger_type.value == "ACTIVE"
    ]

    action_codes = [ability.action_code for ability in abilities]
    assert len(action_codes) == len(set(action_codes))
    assert all(code > 0 and registry.get(code).action_name for code in action_codes)


def test_wolf_kill_ability_keeps_team_discussion_separate_from_final_submission() -> None:
    wolf = _load_role("wolf")
    ability = next(item for item in wolf.abilities if item.action_code == 101)

    assert [item.field_id for item in ability.input_information] == ["team_consensus_receipt"]
    assert "最终刀口提交授权" in ability.request_effect.description
    assert "只能在团队频道参与讨论" in ability.request_effect.description
    assert "final_submitter_unauthorized" in {rule.failure_code for rule in ability.failure_rules}


def test_witch_resources_use_registry_ids() -> None:
    witch = _load_role("witch")
    resources = {ability.ability_id: ability.resource for ability in witch.abilities}

    assert resources["heal"] is not None
    assert resources["heal"].resource_id == "witch_heal"
    assert resources["poison"] is not None
    assert resources["poison"].resource_id == "witch_poison"
