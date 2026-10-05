"""Bridge immutable game state to the pure, frozen rules interpreter.

This adapter does not commit state and does not resolve anything by role or
action name. The package and its typed request/output models are supplied at
construction; GameManager remains responsible for session/window binding and
for the serialized atomic state replacement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
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
    RelationValue,
    ResolutionBatch,
    RuleObservation,
    RuleWindowBinding,
    SkillRequest,
    SkillSpec,
    SkillStateValue,
    SkillUseRecord,
)
from .models import (
    RuleStateValue as GenericRuleStateValue,
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


def _frozen_int_tuple(value: object) -> tuple[int, ...] | None:
    """Narrow recursively frozen JSON arrays of exact integer IDs."""

    if not isinstance(value, (list, tuple)) or any(type(item) is not int for item in value):
        return None
    return tuple(value)


def _stable_manager_identifier(*components: object) -> str:
    """Reproduce the manager's bounded ID for independently checked sources."""

    payload = json.dumps(components, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _rule_value_expired(
    state: GameState,
    *,
    expiry_policy: str,
    expires_at_round: int | None,
    expires_at_hook: str | None,
) -> bool:
    """Evaluate discrete rule expiry at its declared game boundary.

    ``NEXT_NIGHT_START`` crosses a round boundary at victory checking, but its
    value remains available through that check and expires only when the next
    night team-chat phase is actually entered. ``ROUND_END`` expires as soon
    as the round counter advances.
    """

    if expiry_policy == "ROUND_END":
        return expires_at_round is not None and state.round_no >= expires_at_round
    if expiry_policy == "NEXT_NIGHT_START":
        if expires_at_round is None or state.round_no < expires_at_round:
            return False
        if state.round_no > expires_at_round:
            return True
        # The victory check that first advances to expires_at_round is still
        # before the next night. Once that hook has been crossed, keep the
        # value expired for every later phase even if an older snapshot still
        # contains the row.
        return state.phase.value != "VICTORY_CHECK"
    return expires_at_hook is not None and state.phase.value == expires_at_hook


def _has_trusted_speech_event(
    state: GameState,
    *,
    event_ids: tuple[int, ...],
    actor_seat: int,
) -> bool:
    """Validate the persisted speech source without changing snapshot shape."""

    from werewolf.game.events import GameEvent

    for raw_event in state.events:
        if isinstance(raw_event, GameEvent):
            event = raw_event
        elif isinstance(raw_event, Mapping):
            try:
                event = GameEvent.model_validate_json(json.dumps(raw_event, allow_nan=False))
            except (TypeError, ValueError):
                continue
        else:
            continue
        if (
            event.game_id == state.game_id
            and event.event_id in event_ids
            and event.event_type.value == "speech"
            and event.phase.value == "DAY_SPEECH"
            and event.actor_seat == actor_seat
        ):
            return True
    return False


class RuleExecutionAdapter:
    """Pure package adapter used by GameManager while holding its commit lock."""

    def __init__(
        self,
        package: ExecutionPackage,
        *,
        action_registry_digest: str | None = None,
        legacy_compatibility: bool = False,
    ) -> None:
        if not isinstance(package, ExecutionPackage):
            raise TypeError("package must be an ExecutionPackage")
        if type(legacy_compatibility) is not bool:
            raise TypeError("legacy_compatibility must be a bool")
        self.package = package
        self.action_registry_digest = action_registry_digest
        self.legacy_compatibility = legacy_compatibility
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

    def _window_bindings(
        self,
        state: GameState,
        requests: tuple[SkillRequest, ...],
        *,
        group_id: str,
        timing: str,
    ) -> tuple[RuleWindowBinding, ...]:
        """Derive request contexts from stored windows or the active occurrence.

        Legacy unbound requests retain the single-context compatibility path.
        Once any request carries a B window, hook, or occurrence identity, the
        observation gets one binding for every request; callers cannot omit a
        request or choose a physical/logical ID that is absent from the game.
        """

        bound_batch = any(
            item.window_id is not None
            or item.logical_window_id is not None
            or item.hook_id is not None
            or item.trigger_occurrence_id is not None
            or item.source_fact_id is not None
            or item.origin == "AUTOMATIC"
            for item in requests
        )
        if not bound_batch:
            return ()

        skills = {item.skill_id: item for item in self.package.skills}
        cursor = state.rule_workflow_cursor
        bindings: list[RuleWindowBinding] = []
        for request in requests:
            has_occurrence_identity = (
                request.trigger_occurrence_id is not None or request.source_fact_id is not None
            )
            occurrence = next(
                (
                    item
                    for item in state.rule_trigger_queue
                    if item.occurrence_id == request.trigger_occurrence_id
                    and item.source_fact_id == request.source_fact_id
                ),
                None,
            )
            if has_occurrence_identity and occurrence is None:
                raise RuleAdapterError(
                    "request occurrence/source differs from its durable workflow occurrence"
                )
            raw_window = (
                state.action_windows.get(request.window_id)
                if request.window_id is not None
                else None
            )
            if isinstance(raw_window, Mapping):
                raw_phase = raw_window.get("phase")
                phase_value = getattr(raw_phase, "value", raw_phase)
                physical_id = raw_window.get("window_id")
                logical_id = raw_window.get("logical_window_id")
                hook_id = raw_window.get("hook_id")
                settlement_group_id = raw_window.get("settlement_group_id") or physical_id
                if (
                    physical_id != request.window_id
                    or phase_value != timing
                    or logical_id != request.logical_window_id
                    or hook_id != request.hook_id
                    or not isinstance(physical_id, str)
                    or (logical_id is not None and not isinstance(logical_id, str))
                    or (hook_id is not None and not isinstance(hook_id, str))
                ):
                    raise RuleAdapterError(
                        "request window binding differs from its durable action window"
                    )
                if settlement_group_id != group_id:
                    raise RuleAdapterError(
                        "request settlement group differs from its durable action window"
                    )
                if occurrence is not None:
                    if (
                        cursor is None
                        or cursor.status not in {"WAITING_CHOICE", "COLLECTING"}
                        or cursor.active_occurrence_id != occurrence.occurrence_id
                        or cursor.settlement_group_id != settlement_group_id
                        or physical_id not in cursor.active_window_ids
                        or occurrence.status != "WAITING_CHOICE"
                        or occurrence.mode != "PLAYER_CHOICE"
                        or occurrence.kind not in {"HOOK", "TRIGGER"}
                        or occurrence.actor_seat != request.actor_seat
                        or occurrence.ability_instance_id != request.ability_instance_id
                        or occurrence.skill_id != request.skill_id
                        or occurrence.source_fact_id != request.source_fact_id
                        or occurrence.hook_id != hook_id
                    ):
                        raise RuleAdapterError(
                            "request is not bound to the active installed occurrence window"
                        )
                    skill = skills.get(occurrence.skill_id)
                    visible = raw_window.get("visible_context")
                    if skill is None or not isinstance(visible, Mapping):
                        raise RuleAdapterError(
                            "active occurrence window has no frozen skill or context"
                        )
                    allowed_action_codes = _frozen_int_tuple(raw_window.get("allowed_action_codes"))
                    allowed_seats = _frozen_int_tuple(raw_window.get("allowed_seats"))
                    if (
                        request.origin != "PLAYER"
                        or request.action_code not in {skill.action_code, 299}
                        or visible.get("rule_occurrence_id") != occurrence.occurrence_id
                        or visible.get("rule_source_fact_id") != occurrence.source_fact_id
                        or visible.get("rule_actor_seat") != occurrence.actor_seat
                        or visible.get("rule_ability_instance_id") != occurrence.ability_instance_id
                        or visible.get("rule_skill_id") != occurrence.skill_id
                        or visible.get("rule_action_code") != skill.action_code
                        or allowed_action_codes
                        != (
                            (skill.action_code, 299)
                            if raw_window.get("allow_pass") is True
                            else (skill.action_code,)
                        )
                        or allowed_seats != (occurrence.actor_seat,)
                        or raw_window.get("phase") != "TRIGGER_ACTION"
                        or raw_window.get("game_id") != state.game_id
                        or raw_window.get("logical_window_id")
                        != (
                            occurrence.logical_window_id
                            or (skill.window_ids[0] if skill.window_ids else None)
                        )
                    ):
                        raise RuleAdapterError(
                            "installed occurrence window context differs from its source occurrence"
                        )
                    instance = next(
                        (
                            item
                            for item in state.ability_instances
                            if item.ability_instance_id == occurrence.ability_instance_id
                            and item.actor_seat == occurrence.actor_seat
                            and item.skill_id == occurrence.skill_id
                            and item.enabled
                            and not item.consumed
                            and item.grant_id in {grant.grant_id for grant in skill.grants}
                        ),
                        None,
                    )
                    if instance is None:
                        raise RuleAdapterError(
                            "active occurrence ability instance is no longer frozen and enabled"
                        )
                    if occurrence.kind == "HOOK":
                        if (
                            skill.trigger is not None
                            or occurrence.hook_id not in skill.hook_ids
                            or not self._trusted_speech_hook_source(state, occurrence)
                        ):
                            raise RuleAdapterError(
                                "hook occurrence lacks an authentic ordinary speech source"
                            )
                    elif not self._trusted_trigger_fact(state, occurrence, skill):
                        raise RuleAdapterError("trigger occurrence lacks its persisted source fact")
                    bindings.append(
                        RuleWindowBinding(
                            request_id=request.request_id,
                            window_id=physical_id,
                            logical_window_id=logical_id,
                            hook_id=hook_id,
                            trigger_occurrence_id=occurrence.occurrence_id,
                            source_fact_id=occurrence.source_fact_id,
                            legacy_trigger=(
                                occurrence.kind == "TRIGGER"
                                and skill.trigger is None
                                and self.legacy_compatibility
                            ),
                        )
                    )
                    continue
                if has_occurrence_identity or request.origin == "AUTOMATIC":
                    raise RuleAdapterError(
                        "occurrence-bound request has no durable active occurrence window"
                    )
                if (
                    cursor is None
                    or cursor.status != "COLLECTING"
                    or cursor.settlement_group_id != settlement_group_id
                    or physical_id not in cursor.active_window_ids
                ):
                    raise RuleAdapterError(
                        "action window is not active in its durable collection cursor"
                    )
                bindings.append(
                    RuleWindowBinding(
                        request_id=request.request_id,
                        window_id=physical_id,
                        logical_window_id=logical_id,
                        hook_id=hook_id,
                    )
                )
                continue
            if (
                occurrence is None
                or cursor is None
                or cursor.active_occurrence_id != occurrence.occurrence_id
                or occurrence.status not in {"READY", "WAITING_CHOICE"}
                or occurrence.actor_seat != request.actor_seat
                or occurrence.ability_instance_id != request.ability_instance_id
                or occurrence.skill_id != request.skill_id
                or occurrence.mode
                != ("AUTOMATIC" if request.origin == "AUTOMATIC" else "PLAYER_CHOICE")
                or timing != "TRIGGER_ACTION"
            ):
                raise RuleAdapterError(
                    "window-bound request has no durable action window or active occurrence"
                )
            skill = skills.get(occurrence.skill_id)
            if skill is None:
                raise RuleAdapterError("active occurrence skill is not in the frozen package")
            if occurrence.mode == "AUTOMATIC":
                if (
                    cursor.status != "DRAINING"
                    or occurrence.status != "READY"
                    or cursor.settlement_group_id != group_id
                    or group_id != f"automatic-{occurrence.occurrence_id}"
                ):
                    raise RuleAdapterError("automatic request is not the active ready occurrence")
                expected_window_id = occurrence.window_id or f"auto-{occurrence.occurrence_id}"
                expected_logical_id = occurrence.logical_window_id or (
                    skill.window_ids[0] if skill.window_ids else None
                )
                expected_hook_id = occurrence.hook_id
            else:
                # Player choices are installed as actual ActionWindows before
                # the interpreter is called. A missing window must not be
                # papered over with occurrence metadata.
                raise RuleAdapterError("player-choice request has no durable action window")
            if (
                request.window_id != expected_window_id
                or request.logical_window_id != expected_logical_id
                or request.hook_id != expected_hook_id
                or request.request_id != f"automatic-{occurrence.occurrence_id}"
                or not self._trusted_trigger_fact(state, occurrence, skill)
            ):
                raise RuleAdapterError(
                    "automatic request context differs from its active occurrence"
                )
            bindings.append(
                RuleWindowBinding(
                    request_id=request.request_id,
                    window_id=expected_window_id,
                    logical_window_id=expected_logical_id,
                    hook_id=expected_hook_id,
                    trigger_occurrence_id=occurrence.occurrence_id,
                    source_fact_id=occurrence.source_fact_id,
                    legacy_trigger=(
                        occurrence.kind == "TRIGGER"
                        and skill.trigger is None
                        and self.legacy_compatibility
                    ),
                )
            )
        return tuple(bindings)

    def _trusted_trigger_fact(
        self,
        state: GameState,
        occurrence: object,
        skill: SkillSpec,
    ) -> bool:
        trigger = skill.trigger
        if getattr(occurrence, "kind", None) != "TRIGGER":
            return False
        source_fact_id = getattr(occurrence, "source_fact_id", None)
        source_batch_id = getattr(occurrence, "source_batch_id", None)
        mode = getattr(occurrence, "mode", None)
        fact = next(
            (
                item
                for entry in state.rule_ledger
                if entry.batch_id == source_batch_id
                for item in entry.facts
                if item.fact_id == source_fact_id
            ),
            None,
        )
        if fact is None:
            return False
        if trigger is not None:
            return mode == trigger.mode and fact.fact_type in trigger.fact_types
        if not self.legacy_compatibility:
            return False
        actor_seat = getattr(occurrence, "actor_seat", None)
        instance_id = getattr(occurrence, "ability_instance_id", None)
        instance = next(
            (
                item
                for item in state.ability_instances
                if item.ability_instance_id == instance_id
                and item.actor_seat == actor_seat
                and item.skill_id == skill.skill_id
                and item.grant_kind == "TRIGGER"
                and item.enabled
                and not item.consumed
                and item.grant_id in {grant.grant_id for grant in skill.grants}
            ),
            None,
        )
        player = state.players.get(actor_seat) if type(actor_seat) is int else None
        if instance is None or player is None or player.alive:
            return False
        legacy = next(
            (
                item
                for item in player.granted_trigger_abilities
                if item.action_code == skill.action_code and not item.consumed
            ),
            None,
        )
        if (
            sum(
                item.action_code == skill.action_code and not item.consumed
                for item in player.granted_trigger_abilities
            )
            != 1
        ):
            return False
        return bool(
            legacy is not None
            and legacy.trigger.event.value == "DEATH_CONFIRMED"
            and legacy.trigger.mode.value == mode
            and fact.fact_type.lower() == "death_confirmed"
            and fact.target_seat == actor_seat
            and fact.death_cause in legacy.trigger.allowed_death_causes
            and player.death_cause == fact.death_cause
        )

    @staticmethod
    def _trusted_speech_hook_source(state: GameState, occurrence: object) -> bool:
        hook_id = getattr(occurrence, "hook_id", None)
        source_id = getattr(occurrence, "source_fact_id", None)
        cursor = state.rule_workflow_cursor
        return_point = cursor.return_point if cursor is not None else None
        pending = state.pending_resolution
        if (
            hook_id not in {"DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"}
            or not isinstance(source_id, str)
            or state.phase.value != "TRIGGER_ACTION"
            or cursor is None
            or cursor.status not in {"WAITING_CHOICE", "COLLECTING"}
            or return_point is None
            or return_point.phase.value != "DAY_SPEECH"
            or return_point.hook_id != hook_id
            or return_point.day_no != state.day_no
            or not isinstance(pending, Mapping)
            or pending.get("operation") != "RULE_TRIGGER"
            or pending.get("status") != "RULE_TRIGGER_ACTION_REQUIRED"
            or pending.get("occurrence_id") != getattr(occurrence, "occurrence_id", None)
            or pending.get("source_fact_id") != source_id
            or pending.get("window_id") != getattr(occurrence, "window_id", None)
            or pending.get("actor_seat") != getattr(occurrence, "actor_seat", None)
            or any(item.is_pending for item in state.rule_boundaries)
            or (
                isinstance(state.sheriff_badge, Mapping)
                and state.sheriff_badge.get("status") == "OPEN"
            )
        ):
            return False
        election_status = (
            state.sheriff_election.get("status")
            if isinstance(state.sheriff_election, Mapping)
            else None
        )
        if election_status in {"SPEECH", "VOTING", "WAITING_GM"}:
            return False
        if hook_id == "DAY_SPEECH_BEFORE":
            if (
                state.serial_turn is not None
                or not state.current_queue
                or state.current_queue[0] != return_point.speaker_seat
                or return_point.serial_turn_id != f"before-{source_id}"
                or return_point.event_ids
            ):
                return False
            expected_source = _stable_manager_identifier(
                "speech-before",
                state.game_id,
                state.day_no,
                return_point.speaker_seat,
                ",".join(str(item) for item in state.current_queue),
            )
            return source_id == expected_source

        last_turn = state.last_serial_turn
        if (
            state.serial_turn is not None
            or last_turn is None
            or last_turn.request_id != return_point.serial_turn_id
            or last_turn.seat != return_point.speaker_seat
            or last_turn.event_ids != return_point.event_ids
            or not _has_trusted_speech_event(
                state,
                event_ids=last_turn.event_ids,
                actor_seat=last_turn.seat,
            )
        ):
            return False
        expected_source = _stable_manager_identifier(
            "speech-after",
            state.game_id,
            state.day_no,
            last_turn.request_id,
            ",".join(str(item) for item in last_turn.event_ids),
        )
        return source_id == expected_source

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

        request_values = tuple(requests)
        request_contexts = {
            (item.window_id, item.logical_window_id, item.hook_id) for item in request_values
        }
        request_context = (
            request_values[0] if request_values and len(request_contexts) == 1 else None
        )
        window_bindings = self._window_bindings(
            state, request_values, group_id=group_id, timing=timing
        )

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
        generic_state_items: list[GenericRuleStateValue] = []
        for value in state.rule_state:
            if _rule_value_expired(
                state,
                expiry_policy=value.expiry_policy,
                expires_at_round=value.expires_at_round,
                expires_at_hook=value.expires_at_hook,
            ):
                continue
            skill_id = value.skill_id
            seat: int | None = None
            ability_instance_id: str | None = None
            if value.scope == "SEAT":
                if value.scope_id is None or not value.scope_id.startswith("seat-"):
                    raise RuleAdapterError("seat-scoped rule state has an invalid owner")
                try:
                    seat = int(value.scope_id[5:])
                except ValueError as exc:
                    raise RuleAdapterError("seat-scoped rule state has an invalid owner") from exc
                if seat not in state.players:
                    raise RuleAdapterError("seat-scoped rule state references an unknown seat")
            elif value.scope == "ABILITY":
                if value.scope_id is None:
                    raise RuleAdapterError("ability-scoped rule state has no instance id")
                ability_instance_id = value.scope_id
                instance = instances_by_id.get(value.scope_id)
                if instance is None:
                    raise RuleAdapterError(
                        "ability-scoped rule state references an unknown ability instance"
                    )
                # A snapshots predate the optional skill_id field on state
                # records.  An ability-scoped cell has one unambiguous skill
                # owner in the frozen instance table, so restore that binding
                # from the authority rather than rejecting a valid old save.
                if skill_id is None:
                    skill_id = instance.skill_id
                if instance.skill_id != skill_id:
                    raise RuleAdapterError(
                        "ability-scoped rule state skill does not match instance"
                    )
            if skill_id is None:
                raise RuleAdapterError(
                    "typed game/seat rule state is missing its declared skill id"
                )
            generic_state_items.append(
                GenericRuleStateValue(
                    scope=value.scope,
                    skill_id=skill_id,
                    key=value.key,
                    value=_thaw_json(value.value),
                    seat=seat,
                    ability_instance_id=ability_instance_id,
                    expires_at_round=value.expires_at_round,
                    expires_at_hook=value.expires_at_hook,
                )
            )
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
                    skill_id=skill_id,
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
        relations = tuple(
            RelationValue(
                relation_id=item.relation_id,
                relation_type=item.relation_type,
                source_seat=item.source_seat,
                target_seat=item.target_seat,
                source_skill_id=item.source_skill_id,
                source_request_id=item.source_request_id,
                source_ability_instance_id=item.source_ability_instance_id,
                created_round=item.created_round,
                expires_at_round=item.expires_at_round,
                expires_at_hook=item.expires_at_hook,
            )
            for item in state.rule_relations
            if not _rule_value_expired(
                state,
                expiry_policy=item.expiry_policy,
                expires_at_round=item.expires_at_round,
                expires_at_hook=item.expires_at_hook,
            )
        )
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
            state_values=tuple(generic_state_items),
            ledger=history,
            facts=tuple(facts),
            relations=relations,
            ability_instances=ability_instances,
            game_id=state.game_id,
            group_id=group_id,
            timing=timing,
            current_window_id=request_context.window_id if request_context else None,
            current_logical_window_id=(
                request_context.logical_window_id if request_context else None
            ),
            current_hook_id=request_context.hook_id if request_context else None,
            current_window_bindings=window_bindings,
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
            requests,
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
            death_cause=record.death_cause,
            tags=record.tags,
            data=_thaw_json_object(record.data),
        )


def initial_rule_state(
    package: ExecutionPackage,
    ability_instances: Sequence[object],
    *,
    seats: Sequence[int] = (),
) -> tuple[RuleStateValue, ...]:
    """Build initial declared state at its frozen GAME, SEAT, or ABILITY scope."""

    from werewolf.game.state import RuleStateValue

    rows: list[RuleStateValue] = []
    for instance in ability_instances:
        for declaration in package.state_declarations:
            if declaration.scope != "ABILITY" or declaration.skill_id != getattr(
                instance, "skill_id", None
            ):
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
                    skill_id=declaration.skill_id,
                    expiry_policy=declaration.expiry_policy,
                )
            )
    for declaration in package.state_declarations:
        if declaration.scope == "GAME":
            rows.append(
                RuleStateValue(
                    scope="GAME",
                    scope_id=None,
                    key=declaration.key,
                    value_type=declaration.value_type,
                    value=declaration.initial,
                    source_batch_id="setup",
                    skill_id=declaration.skill_id,
                    expiry_policy=declaration.expiry_policy,
                )
            )
        elif declaration.scope == "SEAT":
            for seat in sorted(set(seats)):
                rows.append(
                    RuleStateValue(
                        scope="SEAT",
                        scope_id=f"seat-{seat}",
                        key=declaration.key,
                        value_type=declaration.value_type,
                        value=declaration.initial,
                        source_batch_id="setup",
                        skill_id=declaration.skill_id,
                        expiry_policy=declaration.expiry_policy,
                    )
                )
    return tuple(rows)


__all__ = ["RuleAdapterError", "RuleExecutionAdapter", "initial_rule_state"]
