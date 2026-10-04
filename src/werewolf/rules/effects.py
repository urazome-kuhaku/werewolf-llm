"""Generic effect intent collection and deterministic mortality resolution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from werewolf.rules.models import (
    DisclosureProjection,
    DomainFact,
    EffectIntent,
    EffectSpec,
    ExecutionPackage,
    InteractionRule,
    MortalityOutcome,
    PlayerObservation,
    RequestDisposition,
    ResolvedEffect,
    RuleObservation,
    SkillRequest,
    SkillSpec,
    StateUpdate,
)
from werewolf.rules.predicates import evaluate_expr, evaluate_predicate
from werewolf.rules.selectors import select_seats


@dataclass(frozen=True)
class EffectResolution:
    intents: tuple[EffectIntent, ...]
    effects: tuple[ResolvedEffect, ...]
    state_updates: tuple[StateUpdate, ...]
    mortality: tuple[MortalityOutcome, ...]
    outcomes: tuple[DomainFact, ...]
    facts: tuple[DomainFact, ...]
    activations: tuple[InteractionActivation, ...] = ()


@dataclass(frozen=True)
class InteractionActivation:
    interaction_id: str
    source_intent: EffectIntent
    target_seat: int
    death_cause: str | None = None


def _stable_id(*parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise ValueError("rule value is not JSON serializable")


def _player(observation: RuleObservation, seat: int | None) -> PlayerObservation | None:
    if seat is None:
        return None
    return next((item for item in observation.players if item.seat == seat), None)


def _tags_match(tags: tuple[str, ...], accepted: tuple[str, ...]) -> bool:
    return not accepted or bool(set(tags).intersection(accepted))


def _context(
    *,
    observation: RuleObservation,
    intent: EffectIntent,
    request: SkillRequest | None,
    skill: SkillSpec | None,
    item: object | None = None,
) -> dict[str, object]:
    request_mapping: dict[str, object] = {}
    if request is not None:
        request_mapping = {
            "request_id": request.request_id,
            "action_code": request.action_code,
            "passed": request.passed,
            "actor_seat": request.actor_seat,
            "target_count": len(request.targets),
            "parameters": request.parameters,
        }
    state = (
        {
            item.key: item.value
            for item in observation.skill_state
            if request is not None and item.ability_instance_id == request.ability_instance_id
        }
        if request is not None
        else {}
    )
    return {
        "observation": observation,
        "actor": _player(observation, intent.actor_seat),
        "target": _player(observation, intent.target_seat),
        "request": request_mapping,
        "request_targets": tuple(
            player
            for seat in (sorted(request.targets) if request is not None else ())
            if (player := _player(observation, seat)) is not None
        ),
        "skill_state": state,
        "skill": skill,
        "item": intent if item is None else item,
    }


def _matches_rule(
    rule: InteractionRule,
    *,
    damage: EffectIntent,
    observation: RuleObservation,
    request_by_id: Mapping[str, SkillRequest],
    skill_by_id: Mapping[str, SkillSpec],
    item: object | None = None,
) -> bool:
    if not _tags_match(damage.tags, rule.damage_tags):
        return False
    request = request_by_id.get(damage.source_request_id)
    skill = skill_by_id.get(damage.skill_id)
    context = _context(
        observation=observation,
        intent=damage,
        request=request,
        skill=skill,
        item=damage if item is None else item,
    )
    return evaluate_predicate(rule.when, context)


def _target_values(
    spec: EffectSpec,
    *,
    observation: RuleObservation,
    source: EffectIntent,
    request: SkillRequest | None,
    skill: SkillSpec | None,
) -> tuple[int | None, ...]:
    if spec.target is None:
        return (source.target_seat,)
    context = _context(observation=observation, intent=source, request=request, skill=skill)
    value = evaluate_expr(spec.target, context)
    if value is None:
        return (None,)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 64:
        raise ValueError(f"effect {spec.effect_id} target did not resolve to a seat")
    return (value,)


def _value(
    spec: EffectSpec,
    *,
    observation: RuleObservation,
    source: EffectIntent,
    request: SkillRequest | None,
    skill: SkillSpec | None,
) -> object:
    if spec.value is None:
        return None
    context = _context(observation=observation, intent=source, request=request, skill=skill)
    return _json_value(evaluate_expr(spec.value, context))


def _interaction_effects(
    rule: InteractionRule,
    candidate: EffectIntent,
    *,
    observation: RuleObservation,
    request_by_id: Mapping[str, SkillRequest],
    skill_by_id: Mapping[str, SkillSpec],
) -> tuple[EffectIntent, ...]:
    request = request_by_id.get(candidate.source_request_id)
    skill = skill_by_id.get(candidate.skill_id)
    generated: list[EffectIntent] = []
    for spec in rule.effects:
        context = _context(observation=observation, intent=candidate, request=request, skill=skill)
        if not evaluate_predicate(spec.condition, context):
            continue
        target_values = _target_values(
            spec,
            observation=observation,
            source=candidate,
            request=request,
            skill=skill,
        )
        value = _value(
            spec, observation=observation, source=candidate, request=request, skill=skill
        )
        authorized = set(candidate.authorized_targets)
        for authorized_expr in spec.authorized_targets:
            resolved = evaluate_expr(authorized_expr, context)
            if isinstance(resolved, int) and not isinstance(resolved, bool) and 1 <= resolved <= 64:
                authorized.add(resolved)
            elif resolved is not None:
                raise ValueError(f"interaction {rule.interaction_id} has invalid authorized target")
        for target in target_values:
            if target is not None and target not in authorized:
                raise ValueError(
                    f"interaction {rule.interaction_id} effect target is not authorized "
                    "by its source"
                )
            generated.append(
                EffectIntent(
                    effect_id=_stable_id(
                        rule.interaction_id, candidate.effect_id, spec.effect_id, target
                    ),
                    effect_type=spec.effect_type,
                    source_rule_id=rule.interaction_id,
                    source_request_id=candidate.source_request_id,
                    ability_instance_id=candidate.ability_instance_id,
                    skill_id=candidate.skill_id,
                    actor_seat=candidate.actor_seat,
                    target_seat=target,
                    value=value,  # type: ignore[arg-type]
                    tags=spec.tags,
                    state_key=spec.state_key,
                    fact_type=spec.fact_type,
                    authorized_targets=tuple(sorted(authorized)),
                )
            )
    return tuple(generated)


def _fact_from_intent(intent: EffectIntent, *, observation: RuleObservation) -> DomainFact:
    data: dict[str, object] = {}
    if intent.value is not None:
        data["value"] = intent.value
    data["round_number"] = observation.round_number
    return DomainFact(
        fact_id=_stable_id("fact", intent.effect_id, intent.fact_type),
        fact_type=intent.fact_type or "fact",
        source_rule_id=intent.source_rule_id,
        source_request_id=intent.source_request_id,
        actor_seat=intent.actor_seat,
        target_seat=intent.target_seat,
        tags=intent.tags,
        data=data,  # type: ignore[arg-type]
    )


def resolve_effects(
    package: ExecutionPackage,
    observation: RuleObservation,
    intents: Sequence[EffectIntent],
    requests: Sequence[SkillRequest],
) -> EffectResolution:
    """Apply explicit interactions to one simultaneous set of planned intents."""

    request_by_id = {request.request_id: request for request in requests}
    skill_by_id = {skill.skill_id: skill for skill in package.skills}
    all_intents = list(intents)
    damage = [item for item in all_intents if item.effect_type == "DAMAGE"]
    rules = tuple(
        sorted(package.interactions, key=lambda item: (item.priority, item.interaction_id))
    )

    replaced: set[str] = set()
    generated_by_id: dict[str, EffectIntent] = {}
    activation_by_key: dict[tuple[str, str], InteractionActivation] = {}
    for candidate in damage:
        for rule in rules:
            if rule.rule_type != "REPLACE_DEATH":
                continue
            if _matches_rule(
                rule,
                damage=candidate,
                observation=observation,
                request_by_id=request_by_id,
                skill_by_id=skill_by_id,
            ):
                replaced.add(candidate.effect_id)
                activation_by_key[(rule.interaction_id, candidate.effect_id)] = (
                    InteractionActivation(
                        interaction_id=rule.interaction_id,
                        source_intent=candidate,
                        target_seat=candidate.target_seat or 0,
                    )
                )
                for effect in _interaction_effects(
                    rule,
                    candidate,
                    observation=observation,
                    request_by_id=request_by_id,
                    skill_by_id=skill_by_id,
                ):
                    generated_by_id[effect.effect_id] = effect

    generated = tuple(generated_by_id[key] for key in sorted(generated_by_id))
    all_intents.extend(generated)
    by_target: dict[int, list[EffectIntent]] = {}
    for item in all_intents:
        if item.target_seat is not None:
            by_target.setdefault(item.target_seat, []).append(item)

    blocked_by: dict[str, set[str]] = {}
    used_counter_effects: set[str] = set()
    for candidate in damage:
        if any(
            item.effect_type == "PREVENT_DEATH" and item.target_seat == candidate.target_seat
            for item in all_intents
        ):
            blocked_by.setdefault(candidate.effect_id, set()).add("prevent_death")
        if candidate.effect_id in replaced:
            blocked_by.setdefault(candidate.effect_id, set()).add("replacement")
            continue
        for rule in rules:
            if rule.rule_type not in {"BLOCK_DAMAGE", "CANCEL_DAMAGE_HEAL"}:
                continue
            if not _matches_rule(
                rule,
                damage=candidate,
                observation=observation,
                request_by_id=request_by_id,
                skill_by_id=skill_by_id,
            ):
                continue
            for counter in by_target.get(candidate.target_seat or 0, ()):
                if rule.rule_type == "BLOCK_DAMAGE" and counter.effect_type == "PROTECTION":
                    if _tags_match(counter.tags, rule.counter_tags):
                        blocked_by.setdefault(candidate.effect_id, set()).add(counter.effect_id)
                        used_counter_effects.add(counter.effect_id)
                        activation_by_key[(rule.interaction_id, candidate.effect_id)] = (
                            InteractionActivation(
                                interaction_id=rule.interaction_id,
                                source_intent=candidate,
                                target_seat=candidate.target_seat or 0,
                            )
                        )
                if rule.rule_type == "CANCEL_DAMAGE_HEAL" and counter.effect_type == "HEAL":
                    if _tags_match(counter.tags, rule.counter_tags):
                        blocked_by.setdefault(candidate.effect_id, set()).add(counter.effect_id)
                        used_counter_effects.add(counter.effect_id)
                        activation_by_key[(rule.interaction_id, candidate.effect_id)] = (
                            InteractionActivation(
                                interaction_id=rule.interaction_id,
                                source_intent=candidate,
                                target_seat=candidate.target_seat or 0,
                            )
                        )

    confirmed: dict[int, list[tuple[EffectIntent, int, str]]] = {}
    prevented: dict[int, set[str]] = {}
    resolved_damage: set[str] = set()
    for candidate in damage:
        target = candidate.target_seat
        if target is None:
            raise ValueError("damage intent requires a target")
        if candidate.effect_id in blocked_by:
            prevented.setdefault(target, set()).add(candidate.effect_id)
            continue
        matching_rules = [
            rule
            for rule in rules
            if rule.rule_type == "CONFIRM_DEATH"
            and _matches_rule(
                rule,
                damage=candidate,
                observation=observation,
                request_by_id=request_by_id,
                skill_by_id=skill_by_id,
            )
        ]
        if not matching_rules:
            # A package must make damage confirmation explicit. This protects
            # against silently treating a new damage vocabulary as a kill.
            raise ValueError(f"unconfirmed damage intent: {candidate.effect_id}")
        for rule in matching_rules:
            cause = rule.death_cause or (candidate.tags[0] if candidate.tags else "damage")
            confirmed.setdefault(target, []).append((candidate, rule.priority, cause))
            resolved_damage.add(candidate.effect_id)
            activation_by_key[(rule.interaction_id, candidate.effect_id)] = InteractionActivation(
                interaction_id=rule.interaction_id,
                source_intent=candidate,
                target_seat=target,
                death_cause=cause,
            )

    mortality: list[MortalityOutcome] = []
    outcomes: list[DomainFact] = []
    for target in sorted(set(confirmed).union(prevented)):
        candidates = confirmed.get(target, [])
        if candidates:
            highest = max(priority for _, priority, _ in candidates)
            causes = sorted({cause for _, priority, cause in candidates if priority == highest})
            if len(causes) != 1:
                raise ValueError(
                    f"ambiguous death cause for seat {target}; declare interaction priority"
                )
            death_cause = causes[0]
            source_ids = tuple(sorted({item.source_request_id for item, _, _ in candidates}))
            effect_ids = tuple(sorted({item.effect_id for item, _, _ in candidates}))
            mortality.append(
                MortalityOutcome(
                    seat=target,
                    deceased=True,
                    death_cause=death_cause,
                    cause_effect_ids=effect_ids,
                    source_request_ids=source_ids,
                    prevented_effect_ids=tuple(sorted(prevented.get(target, set()))),
                )
            )
            outcomes.append(
                DomainFact(
                    fact_id=_stable_id(
                        "death-confirmed", observation.revision, target, death_cause, effect_ids
                    ),
                    fact_type="DEATH_CONFIRMED",
                    target_seat=target,
                    tags=(death_cause,),
                    data={"death_cause": death_cause, "source_request_ids": list(source_ids)},
                )
            )
        else:
            mortality.append(
                MortalityOutcome(
                    seat=target,
                    deceased=False,
                    prevented_effect_ids=tuple(sorted(prevented.get(target, set()))),
                )
            )

    consumed_abilities: set[tuple[int, str]] = set()
    facts: list[DomainFact] = []
    resolved: list[ResolvedEffect] = []
    state_updates: list[StateUpdate] = []
    for intent in sorted(
        all_intents, key=lambda item: (item.source_rule_id, item.source_request_id, item.effect_id)
    ):
        if intent.effect_type == "CONSUME_ABILITY":
            if intent.target_seat is None or not isinstance(intent.value, str) or not intent.value:
                raise ValueError("CONSUME_ABILITY requires a target seat and non-empty ability ID")
            observed_seats = {player.seat for player in observation.players}
            if intent.target_seat not in observed_seats:
                raise ValueError("CONSUME_ABILITY target is absent from the observation")
            key = (intent.target_seat, intent.value)
            if key in consumed_abilities:
                raise ValueError("duplicate CONSUME_ABILITY target and ability ID")
            consumed_abilities.add(key)
        applied = True
        reason: str | None = None
        if intent.effect_type == "DAMAGE":
            applied = intent.effect_id in resolved_damage
            if not applied:
                reason = "prevented_or_not_confirmed"
        elif intent.effect_type == "PROTECTION":
            applied = intent.effect_id in used_counter_effects
            if not applied:
                reason = "no_matching_damage_interaction"
        elif intent.effect_type == "HEAL":
            applied = intent.effect_id in used_counter_effects
            if not applied:
                reason = "no_matching_damage_interaction"
        elif intent.effect_type == "PREVENT_DEATH":
            applied = intent.target_seat is not None and any(
                "prevent_death" in reasons
                for effect_id, reasons in blocked_by.items()
                if any(
                    item.effect_id == effect_id and item.target_seat == intent.target_seat
                    for item in damage
                )
            )
            if not applied:
                reason = "no_matching_damage"
        elif intent.effect_type == "STATE_SET":
            if intent.state_key is None:
                raise ValueError("STATE_SET intent requires a state key")
            state_updates.append(
                StateUpdate(
                    ability_instance_id=intent.ability_instance_id,
                    skill_id=intent.skill_id,
                    key=intent.state_key,
                    value=intent.value,
                    source_request_id=intent.source_request_id,
                )
            )
        elif intent.effect_type == "SET_CAN_VOTE":
            if intent.target_seat is None or not isinstance(intent.value, bool):
                raise ValueError("SET_CAN_VOTE requires a target seat and bool value")
        elif intent.effect_type == "CONSUME_ABILITY":
            if intent.target_seat is None or not isinstance(intent.value, str) or not intent.value:
                raise ValueError("CONSUME_ABILITY requires a target seat and non-empty ability ID")
        elif intent.effect_type == "FACT":
            facts.append(_fact_from_intent(intent, observation=observation))
        resolved.append(
            ResolvedEffect(
                effect_id=intent.effect_id,
                effect_type=intent.effect_type,
                target_seat=intent.target_seat,
                applied=applied,
                reason=reason,
                source_request_id=intent.source_request_id,
                source_rule_id=intent.source_rule_id,
                value=intent.value,
                tags=intent.tags,
            )
        )

    # Facts from post-death interaction rules are emitted only from final,
    # confirmed mortality. Death causes and target identity are data values.
    post_death_activations: list[InteractionActivation] = []
    for outcome in mortality:
        if not outcome.deceased:
            continue
        target_player = _player(observation, outcome.seat)
        source_intent = next(
            (item for item in damage if item.effect_id in outcome.cause_effect_ids),
            None,
        )
        if source_intent is None:
            continue
        for rule in rules:
            if rule.rule_type != "EMIT_POST_DEATH_FACT":
                continue
            causal_tags = {
                tag
                for item in damage
                if item.effect_id in outcome.cause_effect_ids
                for tag in item.tags
            }
            if rule.damage_tags and not set(rule.damage_tags).intersection(causal_tags):
                continue
            context = _context(
                observation=observation,
                intent=source_intent,
                request=request_by_id.get(source_intent.source_request_id),
                skill=skill_by_id.get(source_intent.skill_id),
                item={
                    "seat": outcome.seat,
                    "alive": False,
                    "death_cause": outcome.death_cause,
                    "tags": tuple(sorted(causal_tags)),
                    "deceased": True,
                },
            )
            context["target"] = target_player
            if not evaluate_predicate(rule.when, context):
                continue
            post_death_activations.append(
                InteractionActivation(
                    interaction_id=rule.interaction_id,
                    source_intent=source_intent,
                    target_seat=outcome.seat,
                    death_cause=outcome.death_cause,
                )
            )
            facts.append(
                DomainFact(
                    fact_id=_stable_id(
                        "post-death", observation.revision, rule.interaction_id, outcome.seat
                    ),
                    fact_type=rule.fact_type or "POST_DEATH",
                    source_rule_id=rule.interaction_id,
                    source_request_id=source_intent.source_request_id,
                    actor_seat=source_intent.actor_seat,
                    target_seat=outcome.seat,
                    tags=(outcome.death_cause,) if outcome.death_cause else (),
                    data={"death_cause": outcome.death_cause},
                )
            )

    return EffectResolution(
        intents=tuple(
            sorted(
                all_intents,
                key=lambda item: (item.source_rule_id, item.source_request_id, item.effect_id),
            )
        ),
        effects=tuple(resolved),
        state_updates=tuple(
            sorted(
                state_updates,
                key=lambda item: (item.ability_instance_id, item.key, item.source_request_id),
            )
        ),
        mortality=tuple(mortality),
        outcomes=tuple(outcomes),
        facts=tuple(sorted(facts, key=lambda item: (item.fact_type, item.fact_id))),
        activations=tuple(
            sorted(
                (*activation_by_key.values(), *post_death_activations),
                key=lambda item: (
                    item.interaction_id,
                    item.source_intent.effect_id,
                    item.target_seat,
                ),
            )
        ),
    )


def _projection_context(
    *,
    observation: RuleObservation,
    request: SkillRequest,
    skill: SkillSpec,
    target_seat: int | None,
    item: object | None = None,
) -> dict[str, object]:
    return {
        "observation": observation,
        "actor": _player(observation, request.actor_seat),
        "target": _player(observation, target_seat),
        "request": {
            "request_id": request.request_id,
            "action_code": request.action_code,
            "passed": request.passed,
            "actor_seat": request.actor_seat,
            "target_count": len(request.targets),
            "parameters": request.parameters,
        },
        "request_targets": tuple(
            player for seat in request.targets if (player := _player(observation, seat)) is not None
        ),
        "skill_state": {
            state.key: state.value
            for state in observation.skill_state
            if state.ability_instance_id == request.ability_instance_id
        },
        "skill": skill,
        "item": item,
    }


def _legacy_disclosure_value(
    name: str,
    *,
    request: SkillRequest,
    skill: SkillSpec,
    observation: RuleObservation,
    target_seat: int | None,
    death_cause: str | None,
) -> object:
    fixed: dict[str, object] = {
        "request_id": request.request_id,
        "skill_id": skill.skill_id,
        "action_code": request.action_code,
        "actor_seat": request.actor_seat,
        "target_seat": target_seat,
        "target_seats": list(request.targets),
        "round_number": observation.round_number,
        "passed": request.passed,
        "death_cause": death_cause,
    }
    if name not in fixed:
        raise ValueError(f"unsupported disclosure field: {name}")
    return fixed[name]


def _recipients(
    disclosure: object,
    *,
    observation: RuleObservation,
    request: SkillRequest,
    context: Mapping[str, object],
) -> tuple[int, ...]:
    audience = getattr(disclosure, "audience")
    if audience == "SELF":
        return (request.actor_seat,)
    if audience == "ALL":
        return tuple(sorted(player.seat for player in observation.players))
    if audience == "TEAM":
        actor = _player(observation, request.actor_seat)
        if actor is None or not actor.chat_group_ids:
            return (request.actor_seat,)
        groups = set(actor.chat_group_ids)
        return tuple(
            sorted(
                player.seat
                for player in observation.players
                if groups.intersection(player.chat_group_ids)
            )
        )
    selector = getattr(disclosure, "recipients")
    if selector is None:
        raise ValueError("SEATS disclosure requires a recipient selector")
    existing = {player.seat for player in observation.players}
    seats = select_seats(selector, context)
    return tuple(seat for seat in seats if seat in existing)


def _build_projection(
    disclosure: object,
    *,
    request: SkillRequest,
    skill: SkillSpec,
    observation: RuleObservation,
    target_seat: int | None,
    death_cause: str | None = None,
    item: object | None = None,
) -> DisclosureProjection | None:
    context = _projection_context(
        observation=observation,
        request=request,
        skill=skill,
        target_seat=target_seat,
        item=item,
    )
    if not evaluate_predicate(getattr(disclosure, "condition"), context):
        return None
    projected: dict[str, object] = {
        name: _legacy_disclosure_value(
            name,
            request=request,
            skill=skill,
            observation=observation,
            target_seat=target_seat,
            death_cause=death_cause,
        )
        for name in getattr(disclosure, "fields")
    }
    for name, expression in getattr(disclosure, "values").items():
        projected[name] = _json_value(evaluate_expr(expression, context))
    recipients = _recipients(
        disclosure,
        observation=observation,
        request=request,
        context=context,
    )
    if not recipients:
        return None
    return DisclosureProjection(
        disclosure_id=getattr(disclosure, "disclosure_id"),
        source_request_id=request.request_id,
        skill_id=skill.skill_id,
        audience=getattr(disclosure, "audience"),
        recipients=recipients,
        fields=projected,  # type: ignore[arg-type]
        hook=getattr(disclosure, "hook") or "immediate",
        event_type=getattr(disclosure, "event_type"),
    )


def project_disclosures(
    package: ExecutionPackage,
    observation: RuleObservation,
    requests: Sequence[SkillRequest],
    dispositions: Sequence[RequestDisposition],
    resolution: EffectResolution,
) -> tuple[DisclosureProjection, ...]:
    """Build explicit per-recipient projections for configured disclosures."""

    request_by_id = {request.request_id: request for request in requests}
    disposition_by_id = {item.request_id: item for item in dispositions}
    skill_by_id = {skill.skill_id: skill for skill in package.skills}
    projections: list[DisclosureProjection] = []
    for request in sorted(requests, key=lambda item: item.request_id):
        disposition = disposition_by_id.get(request.request_id)
        skill = skill_by_id.get(disposition.skill_id or "") if disposition else None
        if skill is None or disposition is None or disposition.status == "REJECTED":
            continue
        target_seat = min(request.targets) if request.targets else None
        for disclosure in skill.disclosures:
            projection = _build_projection(
                disclosure,
                request=request,
                skill=skill,
                observation=observation,
                target_seat=target_seat,
            )
            if projection is not None:
                projections.append(projection)

    interaction_by_id = {item.interaction_id: item for item in package.interactions}
    for activation in resolution.activations:
        rule = interaction_by_id.get(activation.interaction_id)
        if rule is None:
            continue
        activation_request = request_by_id.get(activation.source_intent.source_request_id)
        skill = skill_by_id.get(activation.source_intent.skill_id)
        if activation_request is None or skill is None:
            continue
        item = {
            "effect_type": activation.source_intent.effect_type,
            "target_seat": activation.target_seat,
            "tags": activation.source_intent.tags,
            "death_cause": activation.death_cause,
            "deceased": activation.death_cause is not None,
        }
        for disclosure in rule.disclosures:
            projection = _build_projection(
                disclosure,
                request=activation_request,
                skill=skill,
                observation=observation,
                target_seat=activation.target_seat,
                death_cause=activation.death_cause,
                item=item,
            )
            if projection is not None:
                projections.append(projection)
    unique: dict[tuple[str, str, tuple[int, ...], str], DisclosureProjection] = {}
    for projection in projections:
        encoded = json.dumps(
            projection.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        key = (
            projection.disclosure_id,
            projection.source_request_id,
            projection.recipients,
            encoded,
        )
        unique[key] = projection
    return tuple(
        unique[key] for key in sorted(unique, key=lambda item: (item[1], item[0], item[2], item[3]))
    )


__all__ = ["EffectResolution", "InteractionActivation", "project_disclosures", "resolve_effects"]
