"""Static skill dependency validation and deterministic topology ranks."""

from __future__ import annotations

import heapq
from collections.abc import Iterable

from werewolf.rules.models import SkillSpec


class SkillDependencyError(ValueError):
    """Raised when a skill dependency graph cannot be scheduled."""


def skill_dependency_ranks(skills: Iterable[SkillSpec]) -> dict[str, int]:
    """Return each skill's longest-path dependency rank.

    Skills at rank zero have no prerequisites. Every direct prerequisite has a
    lower rank than its consumer. The result and diagnostics are independent of
    the input iteration order. Unknown references, self references, duplicate
    skill IDs, duplicate prerequisite references, and cycles are rejected.
    """

    ordered_skills = tuple(sorted(skills, key=lambda skill: skill.skill_id))
    skill_ids = [skill.skill_id for skill in ordered_skills]
    if len(skill_ids) != len(set(skill_ids)):
        duplicates = sorted(
            skill_id for skill_id in set(skill_ids) if skill_ids.count(skill_id) > 1
        )
        raise SkillDependencyError(
            "skill IDs must be unique: " + ", ".join(repr(item) for item in duplicates)
        )

    known_ids = set(skill_ids)
    indegree: dict[str, int] = {}
    dependents: dict[str, list[str]] = {skill_id: [] for skill_id in skill_ids}
    for skill in ordered_skills:
        prerequisites = skill.after_skills
        if len(prerequisites) != len(set(prerequisites)):
            raise SkillDependencyError(
                f"skill {skill.skill_id!r} declares a duplicate prerequisite"
            )
        for prerequisite in prerequisites:
            if prerequisite == skill.skill_id:
                raise SkillDependencyError(f"skill {skill.skill_id!r} cannot depend on itself")
            if prerequisite not in known_ids:
                raise SkillDependencyError(
                    f"skill {skill.skill_id!r} depends on unknown skill {prerequisite!r}"
                )
            dependents[prerequisite].append(skill.skill_id)
        indegree[skill.skill_id] = len(prerequisites)

    for items in dependents.values():
        items.sort()

    ready = [skill_id for skill_id in skill_ids if indegree[skill_id] == 0]
    heapq.heapify(ready)
    ranks = {skill_id: 0 for skill_id in ready}
    scheduled: list[str] = []
    while ready:
        prerequisite = heapq.heappop(ready)
        scheduled.append(prerequisite)
        for dependent in dependents[prerequisite]:
            ranks[dependent] = max(ranks.get(dependent, 0), ranks[prerequisite] + 1)
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)

    if len(scheduled) != len(ordered_skills):
        unresolved = sorted(set(skill_ids) - set(scheduled))
        raise SkillDependencyError(
            "skill dependency graph contains a cycle; unresolved skills: "
            + ", ".join(repr(item) for item in unresolved)
        )

    return {skill_id: ranks[skill_id] for skill_id in skill_ids}


__all__ = ["SkillDependencyError", "skill_dependency_ranks"]
