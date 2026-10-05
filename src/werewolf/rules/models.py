"""Typed, immutable records for data-driven rule execution.

The rules package is deliberately a small data language.  These models do not
contain board, role, or action-name behavior; the interpreter only dispatches
the fixed operation vocabulary declared below.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable
from typing import Annotated, Literal, Never, Self, SupportsIndex, TypeAlias, get_origin

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

SeatNo = Annotated[int, Field(ge=1, le=64, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
ValueType = Literal[
    "null",
    "bool",
    "int",
    "str",
    "seat",
    "nullable_seat",
    "nullable_str",
    "seat_list",
    "str_list",
    "json",
]
ExpiryPolicy = Literal["NEVER", "ROUND_END", "NEXT_NIGHT_START"]
PlayerField = Literal["role_id", "faction_id", "victory_group_id", "chat_group_ids"]
RuleHook = Literal["DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"]
FlowAction = Literal["RESUME_HOOK", "ADVANCE_TO_NIGHT"]


class _FrozenDict(dict[object, object]):
    """A JSON-shaped mapping that rejects in-place mutation."""

    @staticmethod
    def _immutable() -> Never:
        raise TypeError("rule observation mappings are immutable")

    def __setitem__(self, key: object, value: object) -> None:
        del key, value
        self._immutable()

    def __delitem__(self, key: object) -> None:
        del key
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, key: object, default: object = None) -> object:
        del key, default
        self._immutable()

    def popitem(self) -> tuple[object, object]:
        self._immutable()

    def setdefault(self, key: object, default: object = None) -> object:
        del key, default
        self._immutable()

    def update(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self._immutable()

    def __ior__(self, value: object) -> _FrozenDict:  # type: ignore[misc]
        del value
        self._immutable()


class _FrozenList(list[object]):
    """A JSON array that remains serializer-compatible while immutable."""

    @staticmethod
    def _immutable() -> Never:
        raise TypeError("rule observation arrays are immutable")

    def __setitem__(self, key: object, value: object) -> None:
        del key, value
        self._immutable()

    def __delitem__(self, key: object) -> None:
        del key
        self._immutable()

    def append(self, value: object) -> None:
        del value
        self._immutable()

    def extend(self, values: object) -> None:
        del values
        self._immutable()

    def insert(self, index: SupportsIndex, value: object) -> None:
        del index, value
        self._immutable()

    def remove(self, value: object) -> None:
        del value
        self._immutable()

    def pop(self, index: SupportsIndex = -1) -> object:
        del index
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def reverse(self) -> None:
        self._immutable()

    def sort(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self._immutable()

    def __iadd__(self, values: Iterable[object]) -> Self:  # type: ignore[misc]  # list stubs suppress this operator mismatch too
        del values
        self._immutable()

    def __imul__(self, value: SupportsIndex) -> Self:
        del value
        self._immutable()


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


class RuleModel(BaseModel):
    """Common strict and frozen configuration for the rule language."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def accept_array_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for name, field in cls.model_fields.items():
            if get_origin(field.annotation) is tuple and isinstance(normalized.get(name), list):
                normalized[name] = tuple(normalized[name])
        return normalized

    def model_post_init(self, __context: object) -> None:
        for name in type(self).model_fields:
            value = getattr(self, name)
            frozen = _freeze(value)
            if frozen is not value:
                object.__setattr__(self, name, frozen)


class LiteralExpr(RuleModel):
    op: Literal["literal"] = "literal"
    value: JsonValue
    value_type: ValueType | None = None


class RefExpr(RuleModel):
    """A reference into a finite, named execution context.

    ``name`` is a field name, never an arbitrary attribute path.  State names
    are resolved only when declared by the package.
    """

    op: Literal["ref"] = "ref"
    source: Literal[
        "actor", "target", "request", "observation", "skill_state", "source_fact", "item"
    ]
    name: Identifier


class CompareExpr(RuleModel):
    op: Literal["eq", "ne", "lt", "le", "gt", "ge", "in", "contains"]
    left: Expr
    right: Expr


class BooleanExpr(RuleModel):
    op: Literal["and", "or", "not"]
    values: tuple[Expr, ...] = ()


class SelectorExpr(RuleModel):
    op: Literal["select"] = "select"
    source: Literal["players", "request_targets", "ledger", "facts"]
    where: Expr | None = None
    map: Expr | None = None
    distinct: bool = True


class CountExpr(RuleModel):
    op: Literal["count"] = "count"
    selector: SelectorExpr


class MapExpr(RuleModel):
    op: Literal["map"] = "map"
    selector: SelectorExpr
    value: Expr


class RelationExistsExpr(RuleModel):
    """Test one declared directed relation in the bounded observation set."""

    op: Literal["relation_exists"] = "relation_exists"
    relation_type: Identifier
    source_seat: Expr | None = None
    target_seat: Expr | None = None


Expr: TypeAlias = (
    LiteralExpr | RefExpr | CompareExpr | BooleanExpr | CountExpr | MapExpr | RelationExistsExpr
)
for _expr_model in (CompareExpr, BooleanExpr, MapExpr, RelationExistsExpr):
    _expr_model.model_rebuild(_types_namespace={"Expr": Expr})


class ActionSpec(RuleModel):
    action_code: Annotated[int, Field(gt=0, strict=True)]
    action_id: Identifier
    allow_pass: bool = False


class StateDeclaration(RuleModel):
    skill_id: Identifier
    key: Identifier
    value_type: ValueType
    initial: JsonValue | None = None
    scope: Literal["GAME", "SEAT", "ABILITY"] = "ABILITY"
    expiry_policy: ExpiryPolicy = "NEVER"


class RelationDeclaration(RuleModel):
    relation_type: Identifier


class ExecutionWindow(RuleModel):
    """Compiler-bound copy of one frozen board window used for scheduling."""

    window_id: Identifier
    order: Annotated[int, Field(gt=0, le=64, strict=True)]
    phase: Literal["NIGHT_TEAM_CHAT", "NIGHT_ACTION", "NIGHT_RESOLVE"]
    depends_on: tuple[Identifier, ...] = ()


class BoundaryPolicy(RuleModel):
    """Compiler-derived death and badge boundary rules from the frozen board."""

    last_words_enabled: bool
    eligible_death_causes: tuple[Identifier, ...] = ()
    before_reveal: bool = True
    night_death_policy: Literal["none", "first_night_only", "every_night"] = "every_night"
    day_death_policy: Literal["none", "every_day"] = "every_day"
    sheriff_enabled: bool = False
    badge_transfer_enabled: bool | None = None
    badge_transfer_on_death: bool | None = None


class ResourceDeclaration(RuleModel):
    resource_id: Identifier
    min_value: Annotated[int, Field(ge=0, le=1_000_000, strict=True)] = 0
    max_value: Annotated[int, Field(ge=0, le=1_000_000, strict=True)] = 1_000_000

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.min_value > self.max_value:
            raise ValueError("resource min_value cannot exceed max_value")
        return self


class PlayerFieldValues(RuleModel):
    """Closed identifiers a compiled execution may assign to player fields."""

    role_ids: tuple[Identifier, ...] = ()
    faction_ids: tuple[Identifier, ...] = ()
    victory_group_ids: tuple[Identifier, ...] = ()
    chat_group_ids: tuple[Identifier, ...] = ()


class TriggerSpec(RuleModel):
    fact_types: tuple[Identifier, ...]
    mode: Literal["AUTOMATIC", "PLAYER_CHOICE"]
    condition: Expr | None = None


class ParameterSpec(RuleModel):
    name: Identifier
    value_type: ValueType
    required: bool = True
    choices: tuple[JsonValue, ...] = ()


class AbilityGrant(RuleModel):
    grant_id: Identifier
    actor_selector: SelectorExpr


class TargetPolicy(RuleModel):
    min_targets: Annotated[int, Field(ge=0, le=16, strict=True)] = 0
    max_targets: Annotated[int, Field(ge=0, le=16, strict=True)] = 1
    selector: SelectorExpr
    allow_self: bool = True


class CostSpec(RuleModel):
    resource_id: Identifier
    amount: Annotated[int, Field(gt=0, le=1_000_000, strict=True)]


class UsagePolicy(RuleModel):
    max_uses: Annotated[int, Field(gt=0, le=1_000_000, strict=True)] | None = None
    scope: Literal["GAME", "ROUND"] = "GAME"
    pass_records: bool = False
    pass_updates_history: bool = False
    costs: tuple[CostSpec, ...] = ()
    cost_policy: Literal["ON_SUCCESS", "ON_ATTEMPT", "ON_EFFECT"] = "ON_SUCCESS"
    charge_on_pass: bool = False

    def model_post_init(self, __context: object) -> None:
        super().model_post_init(__context)
        object.__setattr__(
            self, "costs", tuple(sorted(self.costs, key=lambda item: item.resource_id))
        )


class EffectSpec(RuleModel):
    effect_id: Identifier
    effect_type: Literal[
        "DAMAGE",
        "PROTECTION",
        "HEAL",
        "PREVENT_DEATH",
        "STATE_SET",
        "SET_CAN_VOTE",
        "FACT",
        "CONSUME_ABILITY",
        "PLAYER_FIELD_SET",
        "RESOURCE_DELTA",
        "RELATION_ADD",
        "RELATION_REMOVE",
        "ABILITY_GRANT",
        "ABILITY_REVOKE",
        "FLOW",
    ]
    target: Expr | None = None
    condition: Expr | None = None
    value: Expr | None = None
    tags: tuple[Identifier, ...] = ()
    state_key: Identifier | None = None
    fact_type: Identifier | None = None
    authorized_targets: tuple[Expr, ...] = ()
    player_field: PlayerField | None = None
    resource_id: Identifier | None = None
    delta: Expr | None = None
    relation_type: Identifier | None = None
    relation_source: Expr | None = None
    relation_target: Expr | None = None
    relation_expiry_policy: ExpiryPolicy | None = None
    grant_skill_id: Identifier | None = None
    grant_id: Identifier | None = None
    flow_action: FlowAction | None = None


class DisclosureSpec(RuleModel):
    disclosure_id: Identifier
    audience: Literal["SELF", "TEAM", "ALL", "SEATS"]
    fields: tuple[Identifier, ...] = ()
    values: dict[str, Expr] = Field(default_factory=dict)
    condition: Expr | None = None
    recipients: SelectorExpr | None = None
    hook: Identifier | None = None
    event_type: Identifier | None = None


class SkillSpec(RuleModel):
    skill_id: Identifier
    action_code: Annotated[int, Field(gt=0, strict=True)]
    mode: Literal["PLAYER", "HOST", "AUTOMATIC"] = "PLAYER"
    grants: tuple[AbilityGrant, ...]
    timing: tuple[Identifier, ...]
    after_skills: tuple[Identifier, ...] = ()
    window_ids: tuple[Identifier, ...] = ()
    hook_ids: tuple[RuleHook, ...] = ()
    trigger: TriggerSpec | None = None
    coordination_scope: Literal["INDIVIDUAL", "CHAT_GROUP"] = "INDIVIDUAL"
    condition: Expr | None = None
    targets: TargetPolicy
    usage: UsagePolicy
    effects: tuple[EffectSpec, ...] = ()
    pass_effects: tuple[EffectSpec, ...] = ()
    disclosures: tuple[DisclosureSpec, ...] = ()
    parameters: tuple[ParameterSpec, ...] = ()

    def model_post_init(self, __context: object) -> None:
        super().model_post_init(__context)
        object.__setattr__(
            self, "grants", tuple(sorted(self.grants, key=lambda item: item.grant_id))
        )
        object.__setattr__(self, "timing", tuple(sorted(self.timing)))
        object.__setattr__(self, "after_skills", tuple(sorted(self.after_skills)))
        object.__setattr__(self, "window_ids", tuple(sorted(self.window_ids)))
        object.__setattr__(self, "hook_ids", tuple(sorted(self.hook_ids)))
        object.__setattr__(
            self, "effects", tuple(sorted(self.effects, key=lambda item: item.effect_id))
        )
        object.__setattr__(
            self,
            "pass_effects",
            tuple(sorted(self.pass_effects, key=lambda item: item.effect_id)),
        )
        object.__setattr__(
            self,
            "disclosures",
            tuple(sorted(self.disclosures, key=lambda item: item.disclosure_id)),
        )
        object.__setattr__(
            self, "parameters", tuple(sorted(self.parameters, key=lambda item: item.name))
        )


class InteractionRule(RuleModel):
    interaction_id: Identifier
    rule_type: Literal[
        "BLOCK_DAMAGE",
        "CANCEL_DAMAGE_HEAL",
        "CONFIRM_DEATH",
        "REPLACE_DEATH",
        "EMIT_POST_DEATH_FACT",
    ]
    priority: Annotated[int, Field(ge=-1_000_000, le=1_000_000, strict=True)] = 0
    when: Expr | None = None
    damage_tags: tuple[Identifier, ...] = ()
    counter_tags: tuple[Identifier, ...] = ()
    fact_type: Identifier | None = None
    death_cause: Identifier | None = None
    effects: tuple[EffectSpec, ...] = ()
    disclosures: tuple[DisclosureSpec, ...] = ()

    def model_post_init(self, __context: object) -> None:
        super().model_post_init(__context)
        object.__setattr__(self, "damage_tags", tuple(sorted(self.damage_tags)))
        object.__setattr__(self, "counter_tags", tuple(sorted(self.counter_tags)))
        object.__setattr__(
            self, "effects", tuple(sorted(self.effects, key=lambda item: item.effect_id))
        )
        object.__setattr__(
            self,
            "disclosures",
            tuple(sorted(self.disclosures, key=lambda item: item.disclosure_id)),
        )


class ExecutionPackage(RuleModel):
    schema_version: Literal[1] = 1
    language_version: Literal[1] = 1
    board_id: Identifier
    board_version: Identifier
    actions: tuple[ActionSpec, ...]
    skills: tuple[SkillSpec, ...]
    state_declarations: tuple[StateDeclaration, ...] = ()
    relation_declarations: tuple[RelationDeclaration, ...] = ()
    resource_declarations: tuple[ResourceDeclaration, ...] = ()
    player_field_values: PlayerFieldValues | None = None
    window_metadata: tuple[ExecutionWindow, ...] = ()
    window_settlement_groups: dict[str, str] = Field(default_factory=dict)
    boundary_policy: BoundaryPolicy | None = None
    interactions: tuple[InteractionRule, ...] = ()

    def model_post_init(self, __context: object) -> None:
        super().model_post_init(__context)
        object.__setattr__(
            self, "actions", tuple(sorted(self.actions, key=lambda item: item.action_code))
        )
        object.__setattr__(
            self, "skills", tuple(sorted(self.skills, key=lambda item: item.skill_id))
        )
        object.__setattr__(
            self,
            "state_declarations",
            tuple(sorted(self.state_declarations, key=lambda item: (item.skill_id, item.key))),
        )
        object.__setattr__(
            self,
            "relation_declarations",
            tuple(sorted(self.relation_declarations, key=lambda item: item.relation_type)),
        )
        object.__setattr__(
            self,
            "resource_declarations",
            tuple(sorted(self.resource_declarations, key=lambda item: item.resource_id)),
        )
        object.__setattr__(
            self,
            "window_metadata",
            tuple(sorted(self.window_metadata, key=lambda item: item.order)),
        )
        object.__setattr__(
            self,
            "interactions",
            tuple(sorted(self.interactions, key=lambda item: (item.priority, item.interaction_id))),
        )

    @field_validator(
        "actions",
        "skills",
        "state_declarations",
        "relation_declarations",
        "resource_declarations",
        "window_metadata",
        "interactions",
        mode="before",
    )
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_window_settlement_identifiers(self) -> Self:
        for window_id, group_id in self.window_settlement_groups.items():
            for label, identifier in (("window", window_id), ("group", group_id)):
                if (
                    not identifier
                    or len(identifier) > 96
                    or any(
                        character.isspace() or unicodedata.category(character).startswith("C")
                        for character in identifier
                    )
                ):
                    raise ValueError(
                        f"window settlement {label} IDs must be bounded printable identifiers"
                    )
        return self

    @property
    def package_id(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", exclude_defaults=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class PlayerObservation(RuleModel):
    seat: SeatNo
    alive: bool = True
    role_id: Identifier | None = None
    faction_id: Identifier | None = None
    victory_group_id: Identifier | None = None
    chat_group_ids: tuple[Identifier, ...] = ()
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    resources: dict[str, NonNegativeInt] = Field(default_factory=dict)
    abilities: tuple[Identifier, ...] = ()


class AbilityInstance(RuleModel):
    ability_instance_id: Identifier
    skill_id: Identifier
    actor_seat: SeatNo
    grant_id: Identifier
    enabled: bool = True


class SkillStateValue(RuleModel):
    ability_instance_id: Identifier
    skill_id: Identifier
    key: Identifier
    value: JsonValue | None


class RuleStateValue(RuleModel):
    """A bounded state cell read from the authoritative observation."""

    scope: Literal["GAME", "SEAT", "ABILITY"]
    skill_id: Identifier
    key: Identifier
    value: JsonValue | None
    seat: SeatNo | None = None
    ability_instance_id: Identifier | None = None
    expires_at_round: NonNegativeInt | None = None
    expires_at_hook: Identifier | None = None

    @model_validator(mode="after")
    def validate_scope_owner(self) -> Self:
        if self.scope == "GAME" and (self.seat is not None or self.ability_instance_id is not None):
            raise ValueError("GAME state value cannot have an owner")
        if self.scope == "SEAT" and (self.seat is None or self.ability_instance_id is not None):
            raise ValueError("SEAT state value requires a seat owner")
        if self.scope == "ABILITY" and (self.ability_instance_id is None or self.seat is not None):
            raise ValueError("ABILITY state value requires an ability instance owner")
        return self


class SkillUseRecord(RuleModel):
    record_id: Identifier
    request_id: Identifier
    ability_instance_id: Identifier
    skill_id: Identifier
    action_code: Annotated[int, Field(gt=0, strict=True)]
    actor_seat: SeatNo
    round_number: NonNegativeInt
    targets: tuple[SeatNo, ...] = ()
    passed: bool = False
    successful: bool = True
    disposition: Literal["ACCEPTED", "PASSED", "REJECTED"] = "ACCEPTED"


class RelationValue(RuleModel):
    relation_id: Identifier
    relation_type: Identifier
    source_seat: SeatNo
    target_seat: SeatNo
    source_skill_id: Identifier
    source_request_id: Identifier
    source_ability_instance_id: Identifier
    created_round: NonNegativeInt = 0
    expires_at_round: NonNegativeInt | None = None
    expires_at_hook: Identifier | None = None


class DomainFact(RuleModel):
    fact_id: Identifier
    fact_type: Identifier
    source_rule_id: Identifier | None = None
    source_request_id: Identifier | None = None
    actor_seat: SeatNo | None = None
    target_seat: SeatNo | None = None
    death_cause: Identifier | None = None
    tags: tuple[Identifier, ...] = ()
    data: dict[str, JsonValue] = Field(default_factory=dict)


class RuleWindowBinding(RuleModel):
    """Trusted window context captured for one request in an observation."""

    request_id: Identifier
    window_id: Identifier
    logical_window_id: Identifier | None = None
    hook_id: RuleHook | None = None
    trigger_occurrence_id: Identifier | None = None
    source_fact_id: Identifier | None = None
    # Adapter-authored compatibility authorization for frozen legacy grants.
    # This is observation state, not part of a package or its stable identity.
    legacy_trigger: bool = False


class RuleObservation(RuleModel):
    board_id: Identifier
    board_version: Identifier
    revision: NonNegativeInt
    round_number: NonNegativeInt
    players: tuple[PlayerObservation, ...]
    skill_state: tuple[SkillStateValue, ...] = ()
    state_values: tuple[RuleStateValue, ...] = ()
    ledger: tuple[SkillUseRecord, ...] = ()
    facts: tuple[DomainFact, ...] = ()
    relations: tuple[RelationValue, ...] = ()
    ability_instances: tuple[AbilityInstance, ...] = ()
    game_id: Identifier = "game"
    group_id: Identifier = "group"
    timing: str = ""
    current_window_id: Identifier | None = None
    current_logical_window_id: Identifier | None = None
    current_hook_id: RuleHook | None = None
    current_window_bindings: tuple[RuleWindowBinding, ...] = ()

    @field_validator(
        "players",
        "skill_state",
        "state_values",
        "ledger",
        "facts",
        "relations",
        "ability_instances",
        "current_window_bindings",
        mode="before",
    )
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_current_window_binding_request_ids(self) -> Self:
        request_ids = tuple(binding.request_id for binding in self.current_window_bindings)
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("current window bindings must have unique request ids")
        return self


class SkillRequest(RuleModel):
    request_id: Identifier
    ability_instance_id: Identifier
    skill_id: Identifier | None = None
    action_code: Annotated[int, Field(gt=0, strict=True)]
    actor_seat: SeatNo
    targets: tuple[SeatNo, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    passed: bool = False
    origin: Literal["PLAYER", "HOST", "AUTOMATIC"] = "PLAYER"
    trigger_occurrence_id: Identifier | None = None
    source_fact_id: Identifier | None = None
    window_id: Identifier | None = None
    logical_window_id: Identifier | None = None
    hook_id: RuleHook | None = None


class RequestDisposition(RuleModel):
    request_id: Identifier
    ability_instance_id: Identifier
    skill_id: Identifier | None = None
    status: Literal["ACCEPTED", "PASSED", "REJECTED"]
    reason: str | None = None


class EffectIntent(RuleModel):
    effect_id: Identifier
    effect_type: Literal[
        "DAMAGE",
        "PROTECTION",
        "HEAL",
        "PREVENT_DEATH",
        "STATE_SET",
        "SET_CAN_VOTE",
        "FACT",
        "CONSUME_ABILITY",
        "PLAYER_FIELD_SET",
        "RESOURCE_DELTA",
        "RELATION_ADD",
        "RELATION_REMOVE",
        "ABILITY_GRANT",
        "ABILITY_REVOKE",
        "FLOW",
    ]
    source_rule_id: Identifier
    source_request_id: Identifier
    ability_instance_id: Identifier
    skill_id: Identifier
    actor_seat: SeatNo
    target_seat: SeatNo | None = None
    value: JsonValue | None = None
    tags: tuple[Identifier, ...] = ()
    state_key: Identifier | None = None
    fact_type: Identifier | None = None
    authorized_targets: tuple[SeatNo, ...] = ()
    player_field: PlayerField | None = None
    resource_id: Identifier | None = None
    delta: Annotated[int, Field(ge=-1_000_000, le=1_000_000, strict=True)] | None = None
    relation_type: Identifier | None = None
    relation_id: Identifier | None = None
    relation_source_seat: SeatNo | None = None
    relation_target_seat: SeatNo | None = None
    relation_expiry_policy: ExpiryPolicy | None = None
    grant_skill_id: Identifier | None = None
    grant_id: Identifier | None = None
    granted_ability_instance_id: Identifier | None = None
    state_scope: Literal["GAME", "SEAT", "ABILITY"] | None = None
    state_ability_instance_id: Identifier | None = None
    state_expiry_policy: ExpiryPolicy | None = None
    flow_action: FlowAction | None = None
    trigger_occurrence_id: Identifier | None = None
    source_fact_id: Identifier | None = None
    window_id: Identifier | None = None
    logical_window_id: Identifier | None = None
    hook_id: RuleHook | None = None

    @model_validator(mode="after")
    def validate_ability_consumption(self) -> Self:
        if self.effect_type == "CONSUME_ABILITY" and (
            self.target_seat is None or not isinstance(self.value, str) or not self.value
        ):
            raise ValueError("CONSUME_ABILITY requires a target seat and non-empty ability ID")
        return self


class ResolvedEffect(RuleModel):
    effect_id: Identifier
    effect_type: Literal[
        "DAMAGE",
        "PROTECTION",
        "HEAL",
        "PREVENT_DEATH",
        "STATE_SET",
        "SET_CAN_VOTE",
        "FACT",
        "CONSUME_ABILITY",
        "PLAYER_FIELD_SET",
        "RESOURCE_DELTA",
        "RELATION_ADD",
        "RELATION_REMOVE",
        "ABILITY_GRANT",
        "ABILITY_REVOKE",
        "FLOW",
    ]
    target_seat: SeatNo | None = None
    applied: bool
    reason: str | None = None
    source_request_id: Identifier
    source_rule_id: Identifier
    skill_id: Identifier | None = None
    ability_instance_id: Identifier | None = None
    actor_seat: SeatNo | None = None
    value: JsonValue | None = None
    tags: tuple[Identifier, ...] = ()
    authorized_targets: tuple[SeatNo, ...] = ()
    player_field: PlayerField | None = None
    resource_id: Identifier | None = None
    delta: Annotated[int, Field(ge=-1_000_000, le=1_000_000, strict=True)] | None = None
    relation_type: Identifier | None = None
    relation_id: Identifier | None = None
    relation_source_seat: SeatNo | None = None
    relation_target_seat: SeatNo | None = None
    relation_expiry_policy: ExpiryPolicy | None = None
    grant_skill_id: Identifier | None = None
    grant_id: Identifier | None = None
    granted_ability_instance_id: Identifier | None = None
    state_scope: Literal["GAME", "SEAT", "ABILITY"] | None = None
    state_ability_instance_id: Identifier | None = None
    state_expiry_policy: ExpiryPolicy | None = None
    flow_action: FlowAction | None = None
    trigger_occurrence_id: Identifier | None = None
    source_fact_id: Identifier | None = None
    window_id: Identifier | None = None
    logical_window_id: Identifier | None = None
    hook_id: RuleHook | None = None

    @model_validator(mode="after")
    def validate_ability_consumption(self) -> Self:
        if self.effect_type == "CONSUME_ABILITY" and (
            self.target_seat is None or not isinstance(self.value, str) or not self.value
        ):
            raise ValueError("CONSUME_ABILITY requires a target seat and non-empty ability ID")
        if self.effect_type in {
            "PLAYER_FIELD_SET",
            "RESOURCE_DELTA",
            "RELATION_ADD",
            "RELATION_REMOVE",
            "ABILITY_GRANT",
            "ABILITY_REVOKE",
            "FLOW",
        } and (
            self.skill_id is None or self.ability_instance_id is None or self.actor_seat is None
        ):
            raise ValueError("typed effects require complete source provenance")
        return self


class StateUpdate(RuleModel):
    ability_instance_id: Identifier | None = None
    seat: SeatNo | None = None
    scope: Literal["GAME", "SEAT", "ABILITY"] = "ABILITY"
    skill_id: Identifier
    key: Identifier
    value: JsonValue | None
    source_request_id: Identifier
    source_rule_id: Identifier = "state_set"
    source_ability_instance_id: Identifier | None = None
    expiry_policy: ExpiryPolicy = "NEVER"
    authorized_targets: tuple[SeatNo, ...] = ()

    @model_validator(mode="after")
    def validate_scope_target(self) -> Self:
        if self.scope == "ABILITY" and (self.ability_instance_id is None or self.seat is not None):
            raise ValueError("ABILITY state update requires an ability instance id only")
        if self.scope == "SEAT" and (self.seat is None or self.ability_instance_id is not None):
            raise ValueError("SEAT state update requires a seat only")
        if self.scope == "GAME" and (self.seat is not None or self.ability_instance_id is not None):
            raise ValueError("GAME state update cannot have a seat or ability instance id")
        return self


class PlayerFieldUpdate(RuleModel):
    seat: SeatNo
    player_field: PlayerField
    value: JsonValue
    source_request_id: Identifier
    source_rule_id: Identifier
    source_skill_id: Identifier
    source_ability_instance_id: Identifier
    authorized_targets: tuple[SeatNo, ...] = ()


class ResourceUpdate(RuleModel):
    seat: SeatNo
    resource_id: Identifier
    delta: Annotated[int, Field(ge=-1_000_000, le=1_000_000, strict=True)]
    source_request_id: Identifier
    source_rule_id: Identifier
    source_skill_id: Identifier
    source_ability_instance_id: Identifier
    authorized_targets: tuple[SeatNo, ...] = ()


class RelationUpdate(RuleModel):
    operation: Literal["ADD", "REMOVE"]
    relation_id: Identifier
    relation_type: Identifier
    source_seat: SeatNo
    target_seat: SeatNo
    expiry_policy: ExpiryPolicy | None = None
    source_request_id: Identifier
    source_rule_id: Identifier
    source_skill_id: Identifier
    source_ability_instance_id: Identifier
    authorized_targets: tuple[SeatNo, ...] = ()


class AbilityUpdate(RuleModel):
    operation: Literal["GRANT", "REVOKE"]
    target_seat: SeatNo
    skill_id: Identifier
    grant_id: Identifier
    ability_instance_id: Identifier | None = None
    source_request_id: Identifier
    source_rule_id: Identifier
    source_skill_id: Identifier
    source_ability_instance_id: Identifier
    authorized_targets: tuple[SeatNo, ...] = ()


class FlowUpdate(RuleModel):
    action: FlowAction
    window_id: Identifier | None = None
    logical_window_id: Identifier | None = None
    hook_id: RuleHook | None = None
    source_request_id: Identifier
    source_rule_id: Identifier
    source_skill_id: Identifier
    source_ability_instance_id: Identifier


class UseUpdate(RuleModel):
    ability_instance_id: Identifier
    skill_id: Identifier
    source_request_id: Identifier
    actor_seat: SeatNo
    action_code: Annotated[int, Field(gt=0, strict=True)]
    accepted: bool


class CostUpdate(RuleModel):
    resource_id: Identifier
    actor_seat: SeatNo
    amount: NonNegativeInt
    source_request_id: Identifier
    ability_instance_id: Identifier


class MortalityOutcome(RuleModel):
    seat: SeatNo
    deceased: bool
    death_cause: Identifier | None = None
    cause_effect_ids: tuple[Identifier, ...] = ()
    source_request_ids: tuple[Identifier, ...] = ()
    prevented_effect_ids: tuple[Identifier, ...] = ()


class DisclosureProjection(RuleModel):
    disclosure_id: Identifier
    source_request_id: Identifier
    skill_id: Identifier
    audience: Literal["SELF", "TEAM", "ALL", "SEATS"]
    recipients: tuple[SeatNo, ...]
    fields: dict[str, JsonValue]
    hook: Identifier = "immediate"
    event_type: Identifier | None = None


class ResolutionBatch(RuleModel):
    schema_version: Literal[1] = 1
    batch_id: Identifier
    package_id: Identifier
    board_id: Identifier
    board_version: Identifier
    read_revision: NonNegativeInt
    round_number: NonNegativeInt
    group_id: Identifier
    dispositions: tuple[RequestDisposition, ...]
    intents: tuple[EffectIntent, ...] = ()
    effects: tuple[ResolvedEffect, ...] = ()
    state_updates: tuple[StateUpdate, ...] = ()
    player_updates: tuple[PlayerFieldUpdate, ...] = ()
    resource_updates: tuple[ResourceUpdate, ...] = ()
    relation_updates: tuple[RelationUpdate, ...] = ()
    ability_updates: tuple[AbilityUpdate, ...] = ()
    flow_updates: tuple[FlowUpdate, ...] = ()
    use_updates: tuple[UseUpdate, ...] = ()
    history_updates: tuple[SkillUseRecord, ...] = ()
    cost_updates: tuple[CostUpdate, ...] = ()
    mortality: tuple[MortalityOutcome, ...] = ()
    outcomes: tuple[DomainFact, ...] = ()
    facts: tuple[DomainFact, ...] = ()
    disclosures: tuple[DisclosureProjection, ...] = ()


__all__ = [
    "AbilityUpdate",
    "AbilityGrant",
    "AbilityInstance",
    "ActionSpec",
    "BoundaryPolicy",
    "BooleanExpr",
    "CompareExpr",
    "CostSpec",
    "CostUpdate",
    "CountExpr",
    "DisclosureProjection",
    "DisclosureSpec",
    "DomainFact",
    "EffectIntent",
    "EffectSpec",
    "ExpiryPolicy",
    "ExecutionWindow",
    "ExecutionPackage",
    "Expr",
    "InteractionRule",
    "LiteralExpr",
    "MapExpr",
    "MortalityOutcome",
    "PlayerFieldUpdate",
    "PlayerFieldValues",
    "PlayerField",
    "ParameterSpec",
    "PlayerObservation",
    "RefExpr",
    "RequestDisposition",
    "RelationDeclaration",
    "RelationExistsExpr",
    "RelationUpdate",
    "RelationValue",
    "ResourceDeclaration",
    "ResourceUpdate",
    "ResolutionBatch",
    "ResolvedEffect",
    "RuleObservation",
    "RuleWindowBinding",
    "SelectorExpr",
    "SkillRequest",
    "SkillSpec",
    "SkillStateValue",
    "SkillUseRecord",
    "RuleStateValue",
    "StateDeclaration",
    "StateUpdate",
    "RuleHook",
    "FlowAction",
    "FlowUpdate",
    "TriggerSpec",
    "TargetPolicy",
    "UsagePolicy",
    "UseUpdate",
    "ValueType",
]
