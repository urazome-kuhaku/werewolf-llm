"""Static skill ordering and metadata compatibility checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest

from werewolf.game.actions import ActionDefinition, ActionRegistry
from werewolf.knowledge.package_loader import KnowledgePackage, KnowledgePackageLoader
from werewolf.rules.compat import compile_legacy_execution
from werewolf.rules.compiler import ExecutionCompilerError, validate_execution_package
from werewolf.rules.models import (
    AbilityGrant,
    ActionSpec,
    ExecutionPackage,
    SelectorExpr,
    SkillSpec,
    TargetPolicy,
    UsagePolicy,
)
from werewolf.rules.scheduler import SkillDependencyError, skill_dependency_ranks

PROJECT_ROOT = Path(__file__).parents[2]


def _skill(
    skill_id: str,
    action_code: int = 1,
    *,
    after_skills: tuple[str, ...] = (),
    coordination_scope: Literal["INDIVIDUAL", "CHAT_GROUP"] = "INDIVIDUAL",
) -> SkillSpec:
    return SkillSpec(
        skill_id=skill_id,
        action_code=action_code,
        mode="HOST",
        grants=(),
        timing=("NIGHT_ACTION",),
        after_skills=after_skills,
        coordination_scope=coordination_scope,
        targets=TargetPolicy(selector=SelectorExpr(source="players")),
        usage=UsagePolicy(),
    )


def _legacy_default_package() -> ExecutionPackage:
    skill = SkillSpec(
        skill_id="hash-legacy",
        action_code=1,
        grants=(
            AbilityGrant(
                grant_id="g",
                actor_selector=SelectorExpr(source="players"),
            ),
        ),
        timing=(),
        targets=TargetPolicy(selector=SelectorExpr(source="players")),
        usage=UsagePolicy(),
    )
    return ExecutionPackage(
        board_id="legacy",
        board_version="1.0.0",
        actions=(ActionSpec(action_code=1, action_id="A"),),
        skills=(skill,),
    )


def _registry_and_package(
    skills: tuple[SkillSpec, ...],
) -> tuple[ActionRegistry, ExecutionPackage]:
    actions = tuple(
        ActionDefinition(
            action_code=skill.action_code,
            action_name=f"SKILL_{skill.action_code}",
            target_policy="other_alive",
            target_count=1,
        )
        for skill in skills
    )
    registry = ActionRegistry(actions=actions)
    package = ExecutionPackage(
        board_id="scheduler-test",
        board_version="1.0.0",
        actions=tuple(
            ActionSpec(action_code=action.action_code, action_id=action.action_name)
            for action in actions
        ),
        skills=skills,
    )
    return registry, package


def test_single_skill_has_rank_zero() -> None:
    assert skill_dependency_ranks((_skill("solo"),)) == {"solo": 0}


def test_multiple_dependencies_get_stable_longest_path_ranks() -> None:
    skills = (
        _skill("result", 4, after_skills=("left", "middle")),
        _skill("middle", 3, after_skills=("root",)),
        _skill("left", 2, after_skills=("root",)),
        _skill("root", 1),
    )

    expected = {"left": 1, "middle": 1, "result": 2, "root": 0}
    assert skill_dependency_ranks(skills) == expected
    assert skill_dependency_ranks(tuple(reversed(skills))) == expected


@pytest.mark.parametrize(
    ("skills", "message"),
    [
        ((_skill("self", after_skills=("self",)),), "depend on itself"),
        ((_skill("unknown", after_skills=("missing",)),), "unknown skill"),
        (
            (
                _skill("first", 1, after_skills=("second",)),
                _skill("second", 2, after_skills=("first",)),
            ),
            "contains a cycle",
        ),
    ],
)
def test_invalid_skill_dependency_graphs_are_rejected(
    skills: tuple[SkillSpec, ...], message: str
) -> None:
    with pytest.raises(SkillDependencyError, match=message):
        skill_dependency_ranks(skills)


def test_compiler_applies_generic_dependency_validation() -> None:
    registry, package = _registry_and_package(
        (
            _skill("first", 1, after_skills=("missing",)),
            _skill("second", 2),
        )
    )

    with pytest.raises(ExecutionCompilerError, match="skill dependencies are invalid"):
        validate_execution_package(package, registry)


def test_skill_schedule_fields_round_trip_as_json() -> None:
    skill = _skill("after-wolf", after_skills=("wolf-kill",), coordination_scope="CHAT_GROUP")

    restored = SkillSpec.model_validate_json(skill.model_dump_json())

    assert restored == skill
    assert restored.after_skills == ("wolf-kill",)
    assert restored.coordination_scope == "CHAT_GROUP"
    assert skill.model_dump(mode="json")["after_skills"] == ["wolf-kill"]


def test_classic_compatibility_declares_chat_group_and_witch_predecessors() -> None:
    async def load_classic() -> KnowledgePackage:
        return await KnowledgePackageLoader(PROJECT_ROOT / "vault" / "published").load(
            "classic_12_seer_witch_hunter_idiot@1.0.0"
        )

    package = asyncio.run(load_classic())
    execution = compile_legacy_execution(package).execution
    skills = {skill.skill_id: skill for skill in execution.skills}

    assert skills["wolf_kill"].coordination_scope == "CHAT_GROUP"
    assert skills["witch_heal"].after_skills == ("wolf_kill",)
    assert skills["witch_poison"].after_skills == ("wolf_kill",)


def test_new_default_schedule_fields_preserve_the_old_package_identity() -> None:
    package = _legacy_default_package()
    skill_dump = package.model_dump(mode="json")["skills"][0]
    semantic_dump = package.model_dump(mode="json", exclude_defaults=True)

    assert package.package_id == "adf9949ed513c71080847413a4f276559cc964adcb5ac05a5102d3cd8c238f55"
    assert skill_dump["after_skills"] == []
    assert skill_dump["coordination_scope"] == "INDIVIDUAL"
    assert "after_skills" not in semantic_dump["skills"][0]
    assert "coordination_scope" not in semantic_dump["skills"][0]
    expected_identity = hashlib.sha256(
        json.dumps(semantic_dump, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    assert package.package_id == expected_identity
