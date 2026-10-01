"""Versioned, executable role knowledge models.

Role documents describe a stable role contract.  Board-specific values belong
to a board binding and are intentionally not encoded in these models.  The
models below keep the fields consumed by the game engine typed and bounded so
that prose cannot silently become executable configuration.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from enum import StrEnum, unique
from typing import Annotated, Literal, TypeVar

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from werewolf.domain.enums import Channel, GamePhase

from .refs import VersionedRef

_LOGICAL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", re.ASCII)
_SEMANTIC_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$",
    re.ASCII,
)

LogicalId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=_LOGICAL_ID_PATTERN.pattern,
        strict=True,
    ),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=64,
        pattern=_SEMANTIC_VERSION_PATTERN.pattern,
        strict=True,
    ),
]
ReviewerId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        strip_whitespace=True,
        strict=True,
    ),
]
BoundedText = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=2_000,
        strip_whitespace=True,
        strict=True,
    ),
]
NonNegativeCount = Annotated[int, Field(ge=0, strict=True)]
ActionCode = Annotated[int, Field(ge=0, le=2_147_483_647, strict=True)]
_EnumT = TypeVar("_EnumT", bound=StrEnum)


@unique
class Faction(StrEnum):
    """Broad side classification used by a role document."""

    GOOD = "GOOD"
    WEREWOLF = "WEREWOLF"
    NEUTRAL = "NEUTRAL"


@unique
class TriggerType(StrEnum):
    """How an ability becomes eligible for resolution."""

    ACTIVE = "ACTIVE"
    PASSIVE = "PASSIVE"
    DEATH_TRIGGER = "DEATH_TRIGGER"


@unique
class TriggerEvent(StrEnum):
    """Authoritative game event that makes a triggered ability eligible."""

    EXILE_SELECTED = "EXILE_SELECTED"
    DEATH_CONFIRMED = "DEATH_CONFIRMED"


@unique
class TriggerMode(StrEnum):
    """Whether a trigger resolves immediately or asks its role controller."""

    AUTOMATIC = "AUTOMATIC"
    PLAYER_CHOICE = "PLAYER_CHOICE"


@unique
class TriggerEffect(StrEnum):
    """Closed set of state effects available at a trigger boundary."""

    REVEAL_ROLE = "REVEAL_ROLE"
    SURVIVE_TRIGGER = "SURVIVE_TRIGGER"
    REMOVE_VOTE_RIGHT = "REMOVE_VOTE_RIGHT"
    RETAIN_SPEECH = "RETAIN_SPEECH"
    OPEN_PLAYER_ACTION = "OPEN_PLAYER_ACTION"


@unique
class TargetKind(StrEnum):
    """The shape of an ability's target selection."""

    NONE = "NONE"
    SELF = "SELF"
    PLAYER = "PLAYER"
    PLAYERS = "PLAYERS"
    TEAM = "TEAM"


class _StrictKnowledgeModel(BaseModel):
    """Common immutable configuration for published knowledge records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


def _enum_value(enum_type: type[_EnumT], value: object) -> _EnumT:
    """Normalize YAML strings while keeping non-string enum input strict."""

    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TypeError(f"expected {enum_type.__name__} or its string value")
    return enum_type(value)


def _check_unique(values: Sequence[object], field_name: str) -> Sequence[object]:
    """Reject repeated persisted identifiers or enum values."""

    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicate values")
    return values


class TargetRule(_StrictKnowledgeModel):
    """Typed target constraints for one ability invocation."""

    kind: TargetKind
    min_targets: NonNegativeCount = 0
    max_targets: NonNegativeCount | None = None
    allow_self: bool = False
    allow_dead: bool = False

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_kind(cls, value: object) -> TargetKind:
        return _enum_value(TargetKind, value)

    @model_validator(mode="before")
    @classmethod
    def normalize_none_target_count(cls, value: object) -> object:
        """Materialize the zero-target default before model construction.

        Returning a replacement model from an ``after`` validator is not
        supported by Pydantic's direct ``__init__`` path.  A ``before``
        validator lets both ``TargetRule(...)`` and ``model_validate(...)``
        receive the same persisted value while leaving non-NONE rules'
        optional upper bound untouched.
        """

        if not isinstance(value, dict):
            return value
        raw_kind = value.get("kind")
        try:
            kind = _enum_value(TargetKind, raw_kind)
        except (TypeError, ValueError):
            return value
        if kind is not TargetKind.NONE or value.get("max_targets") is not None:
            return value
        normalized = dict(value)
        normalized["max_targets"] = 0
        return normalized

    @model_validator(mode="after")
    def validate_target_bounds(self) -> TargetRule:
        """Keep the target range internally consistent."""

        if self.max_targets is not None and self.max_targets < self.min_targets:
            raise ValueError("max_targets cannot be less than min_targets")
        if self.kind is TargetKind.NONE:
            if self.min_targets != 0 or self.max_targets not in (None, 0):
                raise ValueError("a NONE target rule must accept zero targets")
        if self.kind is TargetKind.SELF:
            if not self.allow_self:
                raise ValueError("a SELF target rule must allow self")
            if self.min_targets != 1 or self.max_targets != 1:
                raise ValueError("a SELF target rule must require exactly one target")
        return self


class UsageLimit(_StrictKnowledgeModel):
    """Optional non-negative usage counters for an ability."""

    max_uses: NonNegativeCount | None = None
    uses_per_round: NonNegativeCount | None = None


class ResourceDefinition(_StrictKnowledgeModel):
    """A typed consumable resource referenced by an ability."""

    resource_id: LogicalId
    initial_amount: NonNegativeCount
    cost_per_use: NonNegativeCount = 1


class InputInformation(_StrictKnowledgeModel):
    """One named value the engine may request from the role controller."""

    field_id: LogicalId
    description: BoundedText
    value_type: LogicalId
    required: bool = True


class EffectDefinition(_StrictKnowledgeModel):
    """A typed description of a request or a resolved game effect."""

    effect_code: LogicalId
    description: BoundedText
    visibility: Channel

    @field_validator("visibility", mode="before")
    @classmethod
    def normalize_visibility(cls, value: object) -> Channel:
        return _enum_value(Channel, value)


class FailureRule(_StrictKnowledgeModel):
    """A named, visible outcome when an ability request is rejected."""

    failure_code: LogicalId
    condition: BoundedText
    outcome: BoundedText
    visibility: Channel

    @field_validator("visibility", mode="before")
    @classmethod
    def normalize_visibility(cls, value: object) -> Channel:
        return _enum_value(Channel, value)


class KnowledgeItem(_StrictKnowledgeModel):
    """One fact granted to the role at game start."""

    knowledge_id: LogicalId
    description: BoundedText
    visibility: Channel

    @field_validator("visibility", mode="before")
    @classmethod
    def normalize_visibility(cls, value: object) -> Channel:
        return _enum_value(Channel, value)


class TeamVisibility(_StrictKnowledgeModel):
    """How the role's team-facing identity information is exposed."""

    channel: Channel
    share_identity: bool
    shared_knowledge: list[KnowledgeItem] = Field(default_factory=list)

    @field_validator("channel", mode="before")
    @classmethod
    def normalize_channel(cls, value: object) -> Channel:
        return _enum_value(Channel, value)

    @field_validator("shared_knowledge")
    @classmethod
    def validate_shared_knowledge(cls, value: list[KnowledgeItem]) -> list[KnowledgeItem]:
        _check_unique([item.knowledge_id for item in value], "shared_knowledge knowledge_id")
        return value


class DeathBehavior(_StrictKnowledgeModel):
    """Machine-readable behavior of a role after its death."""

    active_abilities_allowed: bool
    passive_abilities_continue: bool
    death_trigger_fires: bool
    description: BoundedText


class TriggerRule(_StrictKnowledgeModel):
    """Typed eligibility and effects for an ability-trigger event.

    Trigger rules are intentionally a closed contract.  A role document may
    select a logical death-cause code, but it cannot turn prose into an
    executable event or effect.  The game layer remains responsible for
    matching the event and applying these primitives.
    """

    event: TriggerEvent
    allowed_death_causes: list[LogicalId] = Field(default_factory=list)
    mode: TriggerMode
    effects: list[TriggerEffect] = Field(min_length=1)
    allow_pass: bool = False
    once: bool = True

    @field_validator("event", mode="before")
    @classmethod
    def normalize_event(cls, value: object) -> TriggerEvent:
        return _enum_value(TriggerEvent, value)

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: object) -> TriggerMode:
        return _enum_value(TriggerMode, value)

    @field_validator("effects", mode="before")
    @classmethod
    def normalize_effects(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [_enum_value(TriggerEffect, effect) for effect in value]

    @field_validator("allowed_death_causes")
    @classmethod
    def validate_death_causes(cls, value: list[str]) -> list[str]:
        _check_unique(value, "allowed_death_causes")
        return value

    @field_validator("effects")
    @classmethod
    def validate_effects(cls, value: list[TriggerEffect]) -> list[TriggerEffect]:
        _check_unique(value, "effects")
        return value

    @model_validator(mode="after")
    def validate_trigger_contract(self) -> TriggerRule:
        """Keep event, choice mode, and effect primitives coherent."""

        if not self.once:
            raise ValueError(
                "trigger rules with once=false are unsupported until "
                "trigger usage counters are persisted"
            )
        if self.event is TriggerEvent.DEATH_CONFIRMED and not self.allowed_death_causes:
            raise ValueError("DEATH_CONFIRMED requires allowed_death_causes")
        if self.event is TriggerEvent.EXILE_SELECTED and self.allowed_death_causes:
            raise ValueError("EXILE_SELECTED must not declare death causes")
        has_open_action = TriggerEffect.OPEN_PLAYER_ACTION in self.effects
        if self.mode is TriggerMode.AUTOMATIC:
            if self.allow_pass:
                raise ValueError("AUTOMATIC triggers cannot allow PASS")
            if has_open_action:
                raise ValueError("AUTOMATIC triggers cannot open a player action")
        elif not has_open_action:
            raise ValueError("PLAYER_CHOICE triggers must open a player action")
        return self


class AbilityDefinition(_StrictKnowledgeModel):
    """Complete request and resolution contract for one role ability."""

    ability_id: LogicalId
    name: BoundedText
    action_code: ActionCode
    timing: GamePhase
    allowed_phases: list[GamePhase]
    trigger_type: TriggerType
    target_rule: TargetRule
    usage_limit: UsageLimit | None = None
    resource: ResourceDefinition | None = None
    input_information: list[InputInformation] = Field(default_factory=list)
    request_effect: EffectDefinition
    resolution_effect: EffectDefinition
    result_visibility: list[Channel]
    failure_rules: list[FailureRule] = Field(default_factory=list)
    trigger: TriggerRule | None = None

    @field_validator("timing", mode="before")
    @classmethod
    def normalize_timing(cls, value: object) -> GamePhase:
        return _enum_value(GamePhase, value)

    @field_validator("allowed_phases", mode="before")
    @classmethod
    def normalize_allowed_phases(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [_enum_value(GamePhase, phase) for phase in value]

    @field_validator("trigger_type", mode="before")
    @classmethod
    def normalize_trigger_type(cls, value: object) -> TriggerType:
        return _enum_value(TriggerType, value)

    @field_validator("result_visibility", mode="before")
    @classmethod
    def normalize_result_visibility(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [_enum_value(Channel, channel) for channel in value]

    @field_validator("allowed_phases")
    @classmethod
    def validate_allowed_phases(cls, value: list[GamePhase]) -> list[GamePhase]:
        _check_unique(value, "allowed_phases")
        if not value:
            raise ValueError("allowed_phases must contain at least one phase")
        return value

    @field_validator("result_visibility")
    @classmethod
    def validate_result_visibility(cls, value: list[Channel]) -> list[Channel]:
        _check_unique(value, "result_visibility")
        if not value:
            raise ValueError("result_visibility must contain at least one channel")
        return value

    @field_validator("input_information")
    @classmethod
    def validate_input_information(
        cls,
        value: list[InputInformation],
    ) -> list[InputInformation]:
        _check_unique([item.field_id for item in value], "input_information field_id")
        return value

    @field_validator("failure_rules")
    @classmethod
    def validate_failure_rules(cls, value: list[FailureRule]) -> list[FailureRule]:
        _check_unique([rule.failure_code for rule in value], "failure_rules failure_code")
        return value

    @model_validator(mode="after")
    def validate_timing_phase(self) -> AbilityDefinition:
        """Validate the relationship between invocation and trigger semantics.

        ``action_code == 0`` is reserved for a rule-driven passive effect that
        is applied by the game reducer.  It must never be a player-submitted
        action (including ``PASS``).  Conversely, every player-choice ability
        needs a real action code so that the opened window can authorize and
        validate the request.  Keeping these constraints in the knowledge
        model prevents a role document from smuggling an arbitrary automatic
        effect or an unaddressable player action into a published package.
        """

        if self.timing not in self.allowed_phases:
            raise ValueError("timing must be included in allowed_phases")

        trigger = self.trigger
        if self.trigger_type is TriggerType.ACTIVE:
            if trigger is not None:
                raise ValueError("ACTIVE abilities must not declare a trigger rule")
            if self.action_code == 0:
                raise ValueError("ACTIVE abilities require a positive action_code")
            return self

        if trigger is None:
            raise ValueError(f"{self.trigger_type} abilities require a trigger rule")

        if self.usage_limit is not None and (
            (self.usage_limit.max_uses is not None and self.usage_limit.max_uses != 1)
            or (
                self.usage_limit.uses_per_round is not None and self.usage_limit.uses_per_round != 1
            )
        ):
            raise ValueError("trigger abilities only support one total use and one use per round")

        if trigger.mode is TriggerMode.AUTOMATIC:
            if self.action_code != 0:
                raise ValueError("AUTOMATIC triggers must use action_code 0")
            if trigger.allow_pass:
                raise ValueError("AUTOMATIC triggers cannot allow PASS")
            if self.target_rule.kind is not TargetKind.NONE:
                raise ValueError("AUTOMATIC triggers must not declare a target rule")
        else:
            if self.action_code == 0:
                raise ValueError("PLAYER_CHOICE triggers require a positive action_code")

        if self.trigger_type is TriggerType.PASSIVE:
            if trigger.mode is not TriggerMode.AUTOMATIC:
                raise ValueError("PASSIVE abilities require an AUTOMATIC trigger")
        elif self.trigger_type is TriggerType.DEATH_TRIGGER:
            if trigger.event is not TriggerEvent.DEATH_CONFIRMED:
                raise ValueError("DEATH_TRIGGER abilities only match DEATH_CONFIRMED")
            if trigger.mode is not TriggerMode.PLAYER_CHOICE:
                raise ValueError("DEATH_TRIGGER abilities require a PLAYER_CHOICE trigger")

        return self


class RoleDefinition(_StrictKnowledgeModel):
    """Version-pinned, board-independent role definition."""

    schema_version: Literal[1]
    kind: Literal["role"]
    role_id: LogicalId = Field(validation_alias=AliasChoices("role_id", "id"))
    name: BoundedText
    aliases: list[BoundedText] = Field(default_factory=list)
    version: SemanticVersion
    status: Literal["published"]
    reviewed_by: ReviewerId
    reviewed_at: date
    faction: Faction
    team: LogicalId
    victory_goal: LogicalId
    public_summary: BoundedText
    private_identity_card: BoundedText
    abilities: list[AbilityDefinition] = Field(default_factory=list)
    knowledge_at_start: list[KnowledgeItem] = Field(default_factory=list)
    team_visibility: TeamVisibility
    death_behavior: DeathBehavior
    board_compatibility: list[VersionedRef] = Field(
        default_factory=list,
        validation_alias=AliasChoices("board_compatibility", "applicable_boards"),
    )
    common_mistakes: list[BoundedText] = Field(default_factory=list)
    claim_refs: list[LogicalId] = Field(default_factory=list)
    source_refs: list[LogicalId] = Field(default_factory=list)

    @field_validator("faction", mode="before")
    @classmethod
    def normalize_faction(cls, value: object) -> Faction:
        if isinstance(value, str):
            try:
                return _enum_value(Faction, value)
            except ValueError:
                # Role frontmatter documents use the lower-case spelling from
                # the knowledge design, while Python callers use Faction enum
                # values.  Keep both representations explicit and typed.
                return _enum_value(Faction, value.upper())
        return _enum_value(Faction, value)

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def normalize_reviewed_at(cls, value: object) -> object:
        """Accept the ISO date emitted by YAML while rejecting timestamps."""

        if type(value) is date:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("reviewed_at must be a valid ISO calendar date") from exc
        raise TypeError("reviewed_at must be a date in YYYY-MM-DD form")

    @field_validator("aliases", "common_mistakes")
    @classmethod
    def validate_text_lists(cls, value: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "text list")
        _check_unique(value, field_name)
        return value

    @field_validator("knowledge_at_start")
    @classmethod
    def validate_starting_knowledge(cls, value: list[KnowledgeItem]) -> list[KnowledgeItem]:
        _check_unique([item.knowledge_id for item in value], "knowledge_at_start knowledge_id")
        return value

    @field_validator("claim_refs", "source_refs")
    @classmethod
    def validate_references(cls, value: list[str]) -> list[str]:
        _check_unique(value, "knowledge references")
        return value

    @field_validator("board_compatibility", mode="before")
    @classmethod
    def parse_board_compatibility(cls, value: object) -> object:
        """Parse frontmatter refs without treating them as filesystem paths."""

        if not isinstance(value, list):
            return value
        return [VersionedRef.parse(item) if isinstance(item, str) else item for item in value]

    @field_validator("board_compatibility")
    @classmethod
    def validate_board_compatibility(cls, value: list[VersionedRef]) -> list[VersionedRef]:
        if any(reference.id == "latest" for reference in value):
            raise ValueError("board_compatibility must not use the unpinned 'latest' id")
        keys = [reference.format() for reference in value]
        _check_unique(keys, "board_compatibility")
        return value

    @model_validator(mode="after")
    def validate_ability_ids(self) -> RoleDefinition:
        _check_unique([ability.ability_id for ability in self.abilities], "ability_id")
        return self

    @property
    def applicable_boards(self) -> list[VersionedRef]:
        """Compatibility spelling used by role document frontmatter."""

        return self.board_compatibility


__all__ = [
    "AbilityDefinition",
    "ActionCode",
    "BoundedText",
    "DeathBehavior",
    "EffectDefinition",
    "Faction",
    "FailureRule",
    "InputInformation",
    "KnowledgeItem",
    "LogicalId",
    "ResourceDefinition",
    "RoleDefinition",
    "SemanticVersion",
    "TargetKind",
    "TargetRule",
    "TriggerEffect",
    "TriggerEvent",
    "TriggerMode",
    "TriggerRule",
    "TeamVisibility",
    "TriggerType",
    "UsageLimit",
]
