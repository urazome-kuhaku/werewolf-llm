"""Bridge immutable game state to the pure, frozen rules interpreter.

This adapter does not commit state and does not resolve anything by role or
action name. The package and its typed request/output models are supplied at
construction; GameManager remains responsible for session/window binding and
for the serialized atomic state replacement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue

if TYPE_CHECKING:
    from werewolf.game.state import (
        GameState,
        RuleExecutionIdentity,
        RuleFactRecord,
        RuleStateValue,
        RuleUseRecord,
    )

from .models import (
    AbilityInstance,
    DomainFact,
    ExecutionPackage,
    PlayerObservation,
    ResolutionBatch,
    RuleObservation,
    SkillRequest,
    SkillSpec,
    SkillStateValue,
    SkillUseRecord,
)
from .predicates import evaluate_expr, evaluate_predicate


class RuleAdapterError(ValueError):
    """A frozen package cannot be projected or planned safely."""


def _thaw_json(value: JsonValue | None) -> JsonValue | None:
    """Copy a frozen JSON value back to ordinary JSON containers.

    Game state recursively freezes arrays as tuples.  An explicit JSON
    roundtrip restores the list/object shape expected by the rules models and
    ensures the projected value cannot alias the authoritative state.
    """

    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    return cast(JsonValue | None, json.loads(encoded))


def _thaw_json_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    thawed = _thaw_json(value)
    if not isinstance(thawed, dict):
        raise TypeError("JSON object projection did not produce an object")
    return thawed


def _pending_fact_id(request_id: str, effect_id: str, target_seat: int | None) -> str:
    """Build a bounded, stable identity from every temporary-fact component."""

    components = json.dumps(
        [request_id, effect_id, target_seat],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(components.encode("utf-8")).hexdigest()
    return f"pending-{digest}"


class RuleExecutionAdapter:
    """Pure package adapter used by GameManager while holding its commit lock."""

    def __init__(
        self,
        package: ExecutionPackage,
        *,
        action_registry_digest: str | None = None,
    ) -> None:
        if not isinstance(package, ExecutionPackage):
            raise TypeError("package must be an ExecutionPackage")
        self.package = package
        self.action_registry_digest = action_registry_digest
        self._skills_by_action: dict[int, tuple[SkillSpec, ...]] = {}
        for action_code in {skill.action_code for skill in package.skills}:
            self._skills_by_action[action_code] = tuple(
                skill for skill in package.skills if skill.action_code == action_code
            )

    @property
    def identity(self) -> RuleExecutionIdentity:
        """Return the typed durable identity for this exact execution package."""

        from werewolf.game.state import RuleExecutionIdentity

        package_id = self.package.package_id
        return RuleExecutionIdentity(
            package_id=package_id,
            board_id=self.package.board_id,
            board_version=self.package.board_version,
            execution_digest=package_id,
            action_registry_digest=self.action_registry_digest,
        )

    def skill_for(self, action_code: int, grant_id: str) -> object:
        """Resolve the unique skill bound to a frozen action/grant pair."""

        matches = [
            skill
            for skill in self._skills_by_action.get(action_code, ())
            if any(grant.grant_id == grant_id for grant in skill.grants)
        ]
        if len(matches) != 1:
            raise RuleAdapterError(
                "action and ability grant do not identify exactly one frozen skill"
            )
        return matches[0]

    def observation(
        self,
        state: GameState,
        requests: Sequence[SkillRequest] = (),
        *,
        group_id: str,
        timing: str,
        extra_ability_instances: Sequence[AbilityInstance] = (),
    ) -> RuleObservation:
        """Build one read-only rules observation from a single GameState."""

        from werewolf.game.state import GameState

        if not isinstance(state, GameState):
            raise TypeError("state must be a GameState")
        if state.execution_identity is not None and state.execution_identity != self.identity:
            raise RuleAdapterError("execution package does not match the game's pinned identity")

        players = tuple(
            PlayerObservation(
                seat=seat,
                alive=player.alive,
                role_id=player.role_id,
                faction_id=player.faction_id,
                victory_group_id=player.victory_group_id,
                chat_group_ids=player.chat_group_ids,
                attributes={
                    f"trigger_{trigger.ability_id}_consumed": trigger.consumed
                    for trigger in player.granted_trigger_abilities
                },
                resources=dict(player.skill_resources),
                abilities=tuple(
                    item.ability_instance_id
                    for item in state.ability_instances
                    if item.actor_seat == seat and item.enabled and not item.consumed
                ),
            )
            for seat, player in sorted(state.players.items())
        )
        instances_by_id = {
            instance.ability_instance_id: instance for instance in state.ability_instances
        }
        skill_state_items: list[SkillStateValue] = []
        for value in state.rule_state:
            if value.scope != "ABILITY" or value.scope_id is None:
                continue
            instance = instances_by_id.get(value.scope_id)
            if instance is None:
                raise RuleAdapterError(
                    "ability-scoped rule state references an unknown ability instance"
                )
            if value.value_type not in {
                "json",
                "null",
                "bool",
                "int",
                "seat",
                "str",
                "nullable_seat",
                "nullable_str",
                "seat_list",
                "str_list",
            }:
                raise RuleAdapterError("ability-scoped rule state has an unknown declared type")
            skill_state_items.append(
                SkillStateValue(
                    ability_instance_id=value.scope_id,
                    skill_id=instance.skill_id,
                    key=value.key,
                    value=_thaw_json(value.value),
                )
            )
        skill_state = tuple(skill_state_items)
        history = tuple(
            self._to_use_record(record)
            for entry in state.rule_ledger
            for record in entry.history_updates
        )
        facts = [self._to_domain_fact(fact) for entry in state.rule_ledger for fact in entry.facts]
        ability_instances = tuple(
            AbilityInstance(
                ability_instance_id=item.ability_instance_id,
                skill_id=item.skill_id,
                actor_seat=item.actor_seat,
                grant_id=item.grant_id,
                enabled=item.enabled and not item.consumed,
            )
            for item in state.ability_instances
        )
        ability_instances = (*ability_instances, *tuple(extra_ability_instances))
        if len({item.ability_instance_id for item in ability_instances}) != len(ability_instances):
            raise RuleAdapterError("observation contains duplicate ability instances")
        observation = RuleObservation(
            board_id=self.package.board_id,
            board_version=self.package.board_version,
            revision=state.state_revision,
            round_number=state.round_no,
            players=players,
            skill_state=skill_state,
            ledger=history,
            facts=tuple(facts),
            ability_instances=ability_instances,
            game_id=state.game_id,
            group_id=group_id,
            timing=timing,
        )
        return observation

    def plan(
        self,
        state: GameState,
        requests: Sequence[SkillRequest],
        *,
        group_id: str,
        timing: str,
        extra_ability_instances: Sequence[AbilityInstance] = (),
    ) -> ResolutionBatch:
        """Calculate one deterministic batch without mutating game state."""

        from .interpreter import RuleInterpreter

        observation = self.observation(
            state,
            group_id=group_id,
            timing=timing,
            extra_ability_instances=extra_ability_instances,
        )
        request_values = tuple(requests)
        interpreter = RuleInterpreter()
        preview = interpreter.plan(self.package, observation, request_values)
        accepted_ids = {
            item.request_id
            for item in preview.dispositions
            if item.status in {"ACCEPTED", "PASSED"}
        }
        accepted_requests = tuple(
            request for request in request_values if request.request_id in accepted_ids
        )
        temporary_facts = self._request_facts(observation, accepted_requests)
        if not temporary_facts:
            return preview
        enriched = observation.model_copy(update={"facts": (*observation.facts, *temporary_facts)})
        return interpreter.plan(self.package, enriched, request_values)

    def _request_facts(
        self,
        observation: RuleObservation,
        requests: Sequence[SkillRequest],
    ) -> tuple[DomainFact, ...]:
        """Project data-declared same-group effects as temporary input facts."""

        facts: list[DomainFact] = []
        players = {player.seat: player for player in observation.players}
        for request in requests:
            skill = next(
                (
                    candidate
                    for candidate in self.package.skills
                    if candidate.skill_id == request.skill_id
                    and candidate.action_code == request.action_code
                ),
                None,
            )
            if skill is None:
                raise RuleAdapterError("request does not resolve to a skill in the frozen package")
            actor = players.get(request.actor_seat)
            if actor is None:
                raise RuleAdapterError("request actor is absent from the observation")
            effects = skill.pass_effects if request.passed else skill.effects
            source_targets: tuple[int | None, ...] = (
                tuple(request.targets) if request.targets else (None,)
            )
            for effect in effects:
                if effect.fact_type is None:
                    continue
                for target_seat in source_targets:
                    target = players.get(target_seat) if target_seat is not None else None
                    if target_seat is not None and target is None:
                        raise RuleAdapterError("request target is absent from the observation")
                    context: dict[str, object] = {
                        "actor": actor,
                        "target": target,
                        "request": {
                            "request_id": request.request_id,
                            "action_code": request.action_code,
                            "passed": request.passed,
                            "actor_seat": request.actor_seat,
                            "target_count": len(request.targets),
                            "parameters": request.parameters,
                        },
                        "observation": observation,
                        "skill_state": {
                            value.key: value.value
                            for value in observation.skill_state
                            if value.ability_instance_id == request.ability_instance_id
                        },
                    }
                    if not evaluate_predicate(skill.condition, context):
                        continue
                    if not evaluate_predicate(effect.condition, context):
                        continue
                    resolved_target = (
                        evaluate_expr(effect.target, context) if effect.target is not None else None
                    )
                    if resolved_target is not None and type(resolved_target) is not int:
                        raise RuleAdapterError("effect fact target must resolve to a seat")
                    facts.append(
                        DomainFact(
                            fact_id=_pending_fact_id(
                                request.request_id,
                                effect.effect_id,
                                resolved_target,
                            ),
                            fact_type=effect.fact_type,
                            source_rule_id=effect.effect_id,
                            source_request_id=request.request_id,
                            actor_seat=request.actor_seat,
                            target_seat=resolved_target,
                            tags=effect.tags,
                        )
                    )
        return tuple(facts)

    @staticmethod
    def _to_use_record(record: RuleUseRecord) -> SkillUseRecord:
        return SkillUseRecord(
            record_id=record.record_id,
            request_id=record.request_id,
            ability_instance_id=record.ability_instance_id,
            skill_id=record.skill_id,
            action_code=record.action_code,
            actor_seat=record.actor_seat,
            round_number=record.round_number,
            targets=record.targets,
            passed=record.passed,
            successful=record.successful,
            disposition=record.disposition,
        )

    @staticmethod
    def _to_domain_fact(record: RuleFactRecord) -> DomainFact:
        return DomainFact(
            fact_id=record.fact_id,
            fact_type=record.fact_type,
            source_rule_id=record.source_rule_id,
            source_request_id=record.source_request_id,
            actor_seat=record.actor_seat,
            target_seat=record.target_seat,
            tags=record.tags,
            data=_thaw_json_object(record.data),
        )


def initial_rule_state(
    package: ExecutionPackage,
    ability_instances: Sequence[object],
) -> tuple[RuleStateValue, ...]:
    """Build initial declared skill state for each frozen ability instance."""

    from werewolf.game.state import RuleStateValue

    rows: list[RuleStateValue] = []
    for instance in ability_instances:
        for declaration in package.state_declarations:
            if declaration.skill_id != getattr(instance, "skill_id", None):
                continue
            instance_id = getattr(instance, "ability_instance_id", None)
            if not isinstance(instance_id, str):
                raise RuleAdapterError("ability instance has no durable identifier")
            rows.append(
                RuleStateValue(
                    scope="ABILITY",
                    scope_id=instance_id,
                    key=declaration.key,
                    value_type=declaration.value_type,
                    value=declaration.initial,
                    source_batch_id="setup",
                )
            )
    return tuple(rows)


__all__ = ["RuleAdapterError", "RuleExecutionAdapter", "initial_rule_state"]
