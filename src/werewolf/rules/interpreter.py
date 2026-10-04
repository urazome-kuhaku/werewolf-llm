"""Pure planner for a frozen, data-driven execution package."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence

from werewolf.rules.effects import project_disclosures, resolve_effects
from werewolf.rules.models import (
    AbilityInstance,
    CostUpdate,
    EffectIntent,
    EffectSpec,
    ExecutionPackage,
    RequestDisposition,
    ResolutionBatch,
    RuleObservation,
    SkillRequest,
    SkillSpec,
    SkillUseRecord,
    UseUpdate,
)
from werewolf.rules.predicates import (
    evaluate_expr,
    evaluate_predicate,
    validate_package_expressions,
)
from werewolf.rules.selectors import select_seats

_MAX_REQUESTS = 64


def _stable_id(*parts: object) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise ValueError("request parameter is not JSON serializable")


def _value_matches(value: object, value_type: str) -> bool:
    if value_type == "json":
        return value is None or isinstance(value, (str, bool, int, float, list, tuple, dict))
    if value_type == "null":
        return value is None
    if value is None:
        return value_type in {"nullable_seat", "nullable_str"}
    if value_type == "bool":
        return isinstance(value, bool)
    if value_type in {"int", "seat", "nullable_seat"}:
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        return value_type != "seat" or 1 <= value <= 64
    if value_type in {"str", "nullable_str"}:
        return isinstance(value, str)
    if value_type == "seat_list":
        return isinstance(value, (list, tuple)) and all(
            isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= 64
            for item in value
        )
    if value_type == "str_list":
        return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)
    return False


def _same_json(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _request_context(
    observation: RuleObservation,
    request: SkillRequest,
    skill: SkillSpec,
) -> dict[str, object]:
    players = {player.seat: player for player in observation.players}
    state = {
        item.key: item.value
        for item in observation.skill_state
        if item.ability_instance_id == request.ability_instance_id
    }
    return {
        "observation": observation,
        "actor": players.get(request.actor_seat),
        "target": players.get(request.targets[0]) if len(request.targets) == 1 else None,
        "request": {
            "request_id": request.request_id,
            "action_code": request.action_code,
            "passed": request.passed,
            "actor_seat": request.actor_seat,
            "target_count": len(request.targets),
            "parameters": request.parameters,
        },
        "request_targets": tuple(
            players[seat] for seat in sorted(request.targets) if seat in players
        ),
        "skill_state": state,
        "skill": skill,
        "item": None,
    }


def _parameter_error(skill: SkillSpec, parameters: Mapping[str, object]) -> str | None:
    definitions = {item.name: item for item in skill.parameters}
    if set(parameters).difference(definitions):
        return "unknown_parameter"
    for name, definition in definitions.items():
        if name not in parameters:
            if definition.required:
                return "missing_parameter"
            continue
        value = parameters[name]
        if not _value_matches(value, definition.value_type):
            return "invalid_parameter_type"
        if definition.choices and not any(
            _same_json(value, choice) for choice in definition.choices
        ):
            return "invalid_parameter_value"
    return None


class RuleInterpreter:
    """Generate an immutable, deterministic resolution batch from one snapshot."""

    def plan(
        self,
        package: ExecutionPackage,
        observation: RuleObservation,
        requests: Sequence[SkillRequest],
    ) -> ResolutionBatch:
        if not isinstance(package, ExecutionPackage):
            raise TypeError("package must be an ExecutionPackage")
        if not isinstance(observation, RuleObservation):
            raise TypeError("observation must be a RuleObservation")
        validate_package_expressions(package)
        if (package.board_id, package.board_version) != (
            observation.board_id,
            observation.board_version,
        ):
            raise ValueError("execution package does not match the observed frozen board identity")
        if len(requests) > _MAX_REQUESTS:
            raise ValueError("resolution group exceeds the request budget")
        request_values = tuple(requests)
        if any(not isinstance(item, SkillRequest) for item in request_values):
            raise TypeError("requests must contain only SkillRequest records")
        request_ids = [item.request_id for item in request_values]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("request ids must be unique within a resolution group")
        player_seats = [item.seat for item in observation.players]
        if len(player_seats) != len(set(player_seats)):
            raise ValueError("observation player seats must be unique")
        instance_ids = [item.ability_instance_id for item in observation.ability_instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("observation ability instance ids must be unique")

        players = {player.seat: player for player in observation.players}
        skills = {skill.skill_id: skill for skill in package.skills}
        actions = {action.action_code: action for action in package.actions}
        instances = {item.ability_instance_id: item for item in observation.ability_instances}
        old_request_ids = {item.request_id for item in observation.ledger}
        dispositions: dict[str, RequestDisposition] = {}
        candidates: dict[str, tuple[SkillSpec, AbilityInstance, SkillRequest]] = {}
        reserved_uses: dict[str, int] = defaultdict(int)

        for request in sorted(request_values, key=lambda item: item.request_id):
            skill: SkillSpec | None = None
            instance = instances.get(request.ability_instance_id)
            if request.request_id in old_request_ids:
                dispositions[request.request_id] = RequestDisposition(
                    request_id=request.request_id,
                    ability_instance_id=request.ability_instance_id,
                    skill_id=request.skill_id,
                    status="REJECTED",
                    reason="duplicate_request",
                )
                continue
            if instance is None:
                dispositions[request.request_id] = self._rejected(
                    request, "unknown_ability_instance"
                )
                continue
            skill_id = request.skill_id or instance.skill_id
            skill = skills.get(skill_id)
            if skill is None:
                dispositions[request.request_id] = self._rejected(request, "unknown_skill")
                continue
            if request.skill_id is not None and request.skill_id != instance.skill_id:
                dispositions[request.request_id] = self._rejected(
                    request, "skill_instance_mismatch", skill_id
                )
                continue
            if instance.skill_id != skill.skill_id or instance.actor_seat != request.actor_seat:
                dispositions[request.request_id] = self._rejected(
                    request, "ability_instance_mismatch", skill_id
                )
                continue
            if not instance.enabled:
                dispositions[request.request_id] = self._rejected(
                    request, "ability_disabled", skill_id
                )
                continue
            if skill.mode == "AUTOMATIC":
                dispositions[request.request_id] = self._rejected(
                    request, "automatic_skill_not_requestable", skill_id
                )
                continue
            if request.origin != skill.mode:
                dispositions[request.request_id] = self._rejected(
                    request, "request_origin_not_authorized", skill_id
                )
                continue
            if request.origin == "HOST":
                if (
                    skill.mode != "HOST"
                    or skill.grants
                    or instance.grant_id != "$host"
                    or instance.ability_instance_id != f"host:{skill.skill_id}"
                ):
                    dispositions[request.request_id] = self._rejected(
                        request, "host_operation_not_authorized", skill_id
                    )
                    continue
            else:
                grants = {grant.grant_id: grant for grant in skill.grants}
                grant = grants.get(instance.grant_id)
                if not instance.enabled or grant is None:
                    dispositions[request.request_id] = self._rejected(
                        request, "ability_not_granted", skill_id
                    )
                    continue
                grant_context = {"observation": observation}
                if request.actor_seat not in select_seats(grant.actor_selector, grant_context):
                    dispositions[request.request_id] = self._rejected(
                        request, "actor_not_eligible", skill_id
                    )
                    continue
            action = actions.get(request.action_code)
            if action is None or request.action_code != skill.action_code:
                dispositions[request.request_id] = self._rejected(
                    request, "action_not_available", skill_id
                )
                continue
            if skill.timing and observation.timing not in skill.timing:
                dispositions[request.request_id] = self._rejected(request, "wrong_timing", skill_id)
                continue
            if request.actor_seat not in players:
                dispositions[request.request_id] = self._rejected(
                    request, "unknown_actor", skill_id
                )
                continue
            if request.passed and (not action.allow_pass or request.targets or request.parameters):
                dispositions[request.request_id] = self._rejected(
                    request, "pass_not_allowed", skill_id
                )
                continue
            parameter_error = (
                None if request.passed else _parameter_error(skill, request.parameters)
            )
            if parameter_error is not None:
                dispositions[request.request_id] = self._rejected(
                    request, parameter_error, skill_id
                )
                continue
            target_seats = tuple(request.targets)
            if len(target_seats) != len(set(target_seats)):
                dispositions[request.request_id] = self._rejected(
                    request, "duplicate_target", skill_id
                )
                continue
            if request.passed:
                target_seats = ()
            elif not skill.targets.min_targets <= len(target_seats) <= skill.targets.max_targets:
                dispositions[request.request_id] = self._rejected(
                    request, "target_count_invalid", skill_id
                )
                continue
            target_context = _request_context(observation, request, skill)
            try:
                eligible_targets = set(select_seats(skill.targets.selector, target_context))
            except ValueError as exc:
                raise ValueError(f"invalid target selector for skill {skill.skill_id}") from exc
            if any(seat not in players or seat not in eligible_targets for seat in target_seats):
                dispositions[request.request_id] = self._rejected(
                    request, "target_not_authorized", skill_id
                )
                continue
            if not skill.targets.allow_self and request.actor_seat in target_seats:
                dispositions[request.request_id] = self._rejected(
                    request, "self_target_not_allowed", skill_id
                )
                continue
            target_context["request_targets"] = tuple(
                players[seat] for seat in sorted(target_seats)
            )
            if not request.passed and not evaluate_predicate(skill.condition, target_context):
                dispositions[request.request_id] = self._rejected(
                    request, "condition_not_met", skill_id
                )
                continue
            prior_uses = [
                record
                for record in observation.ledger
                if record.ability_instance_id == instance.ability_instance_id
                and record.skill_id == skill.skill_id
                and (skill.usage.scope == "GAME" or record.round_number == observation.round_number)
                and (not record.passed or skill.usage.pass_updates_history)
            ]
            use_count = len(prior_uses) + reserved_uses[instance.ability_instance_id]
            if skill.usage.max_uses is not None and use_count >= skill.usage.max_uses:
                dispositions[request.request_id] = self._rejected(
                    request, "usage_limit_reached", skill_id
                )
                continue
            if not request.passed or skill.usage.pass_updates_history:
                reserved_uses[instance.ability_instance_id] += 1
            candidates[request.request_id] = (skill, instance, request)
            dispositions[request.request_id] = RequestDisposition(
                request_id=request.request_id,
                ability_instance_id=request.ability_instance_id,
                skill_id=skill.skill_id,
                status="PASSED" if request.passed else "ACCEPTED",
            )

        # Resource sufficiency is checked over the whole simultaneous group.
        # If a cost pool is oversubscribed, reject all contenders for that pool
        # rather than letting tuple or arrival order choose who pays.
        cost_requests: dict[tuple[int, str], list[str]] = defaultdict(list)
        for request_id, (skill, _, request) in candidates.items():
            if request.passed and not skill.usage.charge_on_pass:
                continue
            for cost in skill.usage.costs:
                cost_requests[(request.actor_seat, cost.resource_id)].append(request_id)
        rejected_for_cost: set[str] = set()
        for (seat, resource_id), request_group in cost_requests.items():
            available = players[seat].resources.get(resource_id, 0)
            requested = sum(
                cost.amount
                for request_id in request_group
                for cost in candidates[request_id][0].usage.costs
                if cost.resource_id == resource_id
            )
            if requested > available:
                rejected_for_cost.update(request_group)
        for request_id in sorted(rejected_for_cost):
            skill, _, request = candidates.pop(request_id)
            dispositions[request_id] = self._rejected(
                request, "insufficient_resource", skill.skill_id
            )

        accepted_requests = tuple(candidates[key][2] for key in sorted(candidates))
        intents: list[EffectIntent] = []
        for request_id in sorted(candidates):
            skill, instance, request = candidates[request_id]
            specs = skill.pass_effects if request.passed else skill.effects
            intents.extend(
                self._make_intents(package, observation, skill, instance, request, specs)
            )

        effect_resolution = resolve_effects(package, observation, intents, accepted_requests)
        applied_by_request: dict[str, bool] = {}
        for effect in effect_resolution.effects:
            if effect.applied:
                applied_by_request[effect.source_request_id] = True

        updates = list(effect_resolution.state_updates)
        state_signatures: dict[tuple[str, str], object] = {}
        for update in updates:
            key = (update.ability_instance_id, update.key)
            if key in state_signatures and not _same_json(state_signatures[key], update.value):
                raise ValueError(f"simultaneous conflicting state updates for {key[1]}")
            state_signatures[key] = update.value
        vote_values: dict[int, bool] = {}
        for effect in effect_resolution.effects:
            if effect.effect_type != "SET_CAN_VOTE" or effect.target_seat is None:
                continue
            if not isinstance(effect.value, bool):
                raise ValueError("SET_CAN_VOTE result must be boolean")
            prior = vote_values.get(effect.target_seat)
            if prior is not None and prior is not effect.value:
                raise ValueError(
                    "simultaneous conflicting voting eligibility updates for "
                    f"seat {effect.target_seat}"
                )
            vote_values[effect.target_seat] = effect.value

        history_updates: list[SkillUseRecord] = []
        use_updates: list[UseUpdate] = []
        cost_updates: list[CostUpdate] = []
        for request_id in sorted(candidates):
            skill, instance, request = candidates[request_id]
            # ACCEPTED means the configured action was legally executed. A
            # simultaneous counter may prevent its effect without undoing the
            # action or its ON_SUCCESS usage record.
            successful = True
            if not request.passed or skill.usage.pass_records or skill.usage.pass_updates_history:
                history_updates.append(
                    SkillUseRecord(
                        record_id=_stable_id("use", request.request_id),
                        request_id=request.request_id,
                        ability_instance_id=instance.ability_instance_id,
                        skill_id=skill.skill_id,
                        action_code=request.action_code,
                        actor_seat=request.actor_seat,
                        round_number=observation.round_number,
                        targets=tuple(sorted(request.targets)),
                        passed=request.passed,
                        successful=successful,
                        disposition="PASSED" if request.passed else "ACCEPTED",
                    )
                )
            should_update_use = not request.passed or skill.usage.pass_updates_history
            use_updates.append(
                UseUpdate(
                    ability_instance_id=instance.ability_instance_id,
                    skill_id=skill.skill_id,
                    source_request_id=request.request_id,
                    actor_seat=request.actor_seat,
                    action_code=request.action_code,
                    accepted=should_update_use,
                )
            )
            for cost in skill.usage.costs:
                if request.passed and not skill.usage.charge_on_pass:
                    continue
                charged = (
                    (skill.usage.cost_policy == "ON_ATTEMPT")
                    or (skill.usage.cost_policy == "ON_SUCCESS" and successful)
                    or (
                        skill.usage.cost_policy == "ON_EFFECT"
                        and applied_by_request.get(request.request_id, False)
                    )
                )
                if charged:
                    cost_updates.append(
                        CostUpdate(
                            resource_id=cost.resource_id,
                            actor_seat=request.actor_seat,
                            amount=cost.amount,
                            source_request_id=request.request_id,
                            ability_instance_id=instance.ability_instance_id,
                        )
                    )

        disclosures = project_disclosures(
            package,
            observation,
            accepted_requests,
            tuple(dispositions[key] for key in sorted(dispositions)),
            effect_resolution,
        )
        normalized_requests = [
            {
                **item.model_dump(mode="json"),
                "targets": sorted(item.targets),
            }
            for item in sorted(request_values, key=lambda item: item.request_id)
        ]
        batch_id = _stable_id(
            package.package_id, observation.revision, observation.group_id, normalized_requests
        )
        return ResolutionBatch(
            batch_id=batch_id,
            package_id=package.package_id,
            board_id=package.board_id,
            board_version=package.board_version,
            read_revision=observation.revision,
            round_number=observation.round_number,
            group_id=observation.group_id,
            dispositions=tuple(dispositions[key] for key in sorted(dispositions)),
            intents=effect_resolution.intents,
            effects=effect_resolution.effects,
            state_updates=tuple(
                sorted(
                    updates,
                    key=lambda item: (item.ability_instance_id, item.key, item.source_request_id),
                )
            ),
            use_updates=tuple(
                sorted(
                    use_updates, key=lambda item: (item.ability_instance_id, item.source_request_id)
                )
            ),
            history_updates=tuple(history_updates),
            cost_updates=tuple(
                sorted(
                    cost_updates,
                    key=lambda item: (item.actor_seat, item.resource_id, item.source_request_id),
                )
            ),
            mortality=effect_resolution.mortality,
            outcomes=effect_resolution.outcomes,
            facts=effect_resolution.facts,
            disclosures=disclosures,
        )

    @staticmethod
    def _rejected(
        request: SkillRequest,
        reason: str,
        skill_id: str | None = None,
    ) -> RequestDisposition:
        return RequestDisposition(
            request_id=request.request_id,
            ability_instance_id=request.ability_instance_id,
            skill_id=skill_id or request.skill_id,
            status="REJECTED",
            reason=reason,
        )

    @staticmethod
    def _make_intents(
        package: ExecutionPackage,
        observation: RuleObservation,
        skill: SkillSpec,
        instance: AbilityInstance,
        request: SkillRequest,
        specs: Sequence[EffectSpec],
    ) -> tuple[EffectIntent, ...]:
        players = {player.seat: player for player in observation.players}
        base_context = _request_context(observation, request, skill)
        intents: list[EffectIntent] = []
        for spec in specs:
            if spec.target is None:
                targets: tuple[int | None, ...] = (
                    tuple(sorted(request.targets)) if request.targets else (None,)
                )
            else:
                source_targets: tuple[int | None, ...] = (
                    tuple(sorted(request.targets)) if request.targets else (None,)
                )
                resolved_targets: set[int | None] = set()
                for source_target in source_targets:
                    target_context = dict(base_context)
                    target_context["target"] = (
                        players.get(source_target) if source_target is not None else None
                    )
                    value = evaluate_expr(spec.target, target_context)
                    if value is None:
                        resolved_targets.add(None)
                    elif isinstance(value, int) and not isinstance(value, bool):
                        resolved_targets.add(value)
                    elif isinstance(value, (tuple, list)) and all(
                        isinstance(item, int) and not isinstance(item, bool) for item in value
                    ):
                        resolved_targets.update(value)
                    else:
                        raise ValueError(f"effect {spec.effect_id} target is not a seat")
                targets = tuple(sorted(resolved_targets, key=lambda item: item or 0))
            for target in targets:
                context = dict(base_context)
                context["target"] = players.get(target) if target is not None else None
                if not evaluate_predicate(spec.condition, context):
                    continue
                authorized = set(request.targets)
                for authorized_expr in spec.authorized_targets:
                    resolved = evaluate_expr(authorized_expr, context)
                    values = resolved if isinstance(resolved, (tuple, list)) else (resolved,)
                    for seat in values:
                        if seat is None:
                            continue
                        if (
                            not isinstance(seat, int)
                            or isinstance(seat, bool)
                            or seat not in players
                        ):
                            raise ValueError(
                                f"effect {spec.effect_id} has an invalid authorized target"
                            )
                        authorized.add(seat)
                if target is not None and target not in authorized:
                    raise ValueError(
                        f"effect {spec.effect_id} writes outside its declared authorization"
                    )
                if target is not None and target not in players:
                    raise ValueError(
                        f"effect {spec.effect_id} target is absent from the observation"
                    )
                if (
                    spec.effect_type
                    in {
                        "DAMAGE",
                        "PROTECTION",
                        "HEAL",
                        "PREVENT_DEATH",
                        "SET_CAN_VOTE",
                        "CONSUME_ABILITY",
                    }
                    and target is None
                ):
                    raise ValueError(f"effect {spec.effect_id} requires a target")
                value_result = (
                    evaluate_expr(spec.value, context) if spec.value is not None else None
                )
                if spec.effect_type == "CONSUME_ABILITY" and (
                    not isinstance(value_result, str) or not value_result
                ):
                    raise ValueError(
                        f"effect {spec.effect_id} requires a non-empty string ability ID"
                    )
                intents.append(
                    EffectIntent(
                        effect_id=_stable_id(
                            package.package_id, request.request_id, spec.effect_id, target
                        ),
                        effect_type=spec.effect_type,
                        source_rule_id=spec.effect_id,
                        source_request_id=request.request_id,
                        ability_instance_id=instance.ability_instance_id,
                        skill_id=skill.skill_id,
                        actor_seat=request.actor_seat,
                        target_seat=target,
                        value=_json_value(value_result),  # type: ignore[arg-type]
                        tags=spec.tags,
                        state_key=spec.state_key,
                        fact_type=spec.fact_type,
                        authorized_targets=tuple(sorted(authorized)),
                    )
                )
        return tuple(
            sorted(
                intents,
                key=lambda item: (item.source_rule_id, item.target_seat or 0, item.effect_id),
            )
        )


def plan(
    package: ExecutionPackage,
    observation: RuleObservation,
    requests: Sequence[SkillRequest],
) -> ResolutionBatch:
    """Module-level convenience wrapper around :class:`RuleInterpreter`."""

    return RuleInterpreter().plan(package, observation, requests)


__all__ = ["RuleInterpreter", "plan"]
