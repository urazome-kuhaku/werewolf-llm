"""Versioned, structured knowledge for rules that resolve together.

An interaction is deliberately smaller than a game engine rule.  It records
the facts which make an interaction applicable, the ordered operations used to
resolve it, and the information that may be disclosed.  The model does not
encode any particular board or role, so the same schema can describe many
different rule sets.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from enum import StrEnum, unique
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from werewolf.domain.enums import Channel

from .refs import VersionedRef

_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z", re.ASCII)
_PATH_PATTERN = re.compile(
    r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z",
    re.ASCII,
)
_SITUATION_PATTERN = re.compile(
    r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z",
    re.ASCII,
)
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)

LogicalId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$",
        strict=True,
    ),
]
LogicalPath = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$",
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
ReviewerId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        strip_whitespace=True,
        strict=True,
    ),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=64,
        pattern=r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$",
        strict=True,
    ),
]


class _StrictKnowledgeModel(BaseModel):
    """Common immutable settings for all published interaction records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


def _enum_value(enum_type: type[StrEnum], value: object) -> StrEnum:
    """Validate an enum without allowing implicit coercion."""

    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TypeError(f"expected {enum_type.__name__} or its string value")
    return enum_type(value)


def _check_unique(values: Sequence[object], field_name: str) -> None:
    """Reject repeated machine identifiers while preserving input order."""

    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicate values")


def _normalize_reviewed_at(value: object) -> object:
    """Accept a YAML ISO date while rejecting timestamps and invalid dates."""

    if type(value) is date:
        return value
    if isinstance(value, str) and _DATE_PATTERN.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("reviewed_at must be a valid ISO calendar date") from exc
    raise TypeError("reviewed_at must be a date in YYYY-MM-DD form")


@unique
class PredicateOperator(StrEnum):
    """Operators supported by a precondition predicate."""

    EQ = "EQ"
    NE = "NE"
    IN = "IN"
    NOT_IN = "NOT_IN"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    EXISTS = "EXISTS"
    NOT_EXISTS = "NOT_EXISTS"
    IS_TRUE = "IS_TRUE"
    IS_FALSE = "IS_FALSE"


class Predicate(_StrictKnowledgeModel):
    """One typed fact check which gates an interaction."""

    subject: LogicalId
    field: LogicalPath
    operator: PredicateOperator
    value: JsonValue | None = None

    @field_validator("operator", mode="before")
    @classmethod
    def normalize_operator(cls, value: object) -> PredicateOperator:
        return _enum_value(PredicateOperator, value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_operator_value(self) -> Predicate:
        """Keep unary operators and their values unambiguous."""

        unary = {
            PredicateOperator.EXISTS,
            PredicateOperator.NOT_EXISTS,
            PredicateOperator.IS_TRUE,
            PredicateOperator.IS_FALSE,
        }
        if self.operator in unary and self.value is not None:
            raise ValueError(f"{self.operator.value} does not accept a value")
        if self.operator not in unary and self.value is None:
            raise ValueError(f"{self.operator.value} requires a value")
        if self.operator in {PredicateOperator.IN, PredicateOperator.NOT_IN}:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.operator.value} requires a non-empty list value")
        return self


class ResolutionStep(_StrictKnowledgeModel):
    """One ordered, machine-readable operation in an interaction."""

    step_id: LogicalId
    order: Annotated[int, Field(gt=0, strict=True)]
    action: LogicalId
    subject: LogicalId | None = None
    target: LogicalId | None = None
    parameters: dict[LogicalId, JsonValue] = Field(default_factory=dict)
    visibility: Channel | None = None

    @field_validator("parameters")
    @classmethod
    def validate_parameter_keys(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if _ID_PATTERN.fullmatch(key) is None:
                raise ValueError("parameters keys must be lowercase logical identifiers")
        return value

    @field_validator("visibility", mode="before")
    @classmethod
    def normalize_visibility(cls, value: object) -> Channel | None:
        if value is None:
            return None
        return _enum_value(Channel, value)  # type: ignore[return-value]


@unique
class StateOperation(StrEnum):
    """Typed state operation used by an outcome effect."""

    SET = "SET"
    ADD = "ADD"
    REMOVE = "REMOVE"
    APPEND = "APPEND"
    CLEAR = "CLEAR"


@unique
class OutcomeStatus(StrEnum):
    """Generic resolution status shared by interaction outcome examples."""

    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    NO_EFFECT = "NO_EFFECT"
    PARTIAL = "PARTIAL"


class OutcomeEffect(_StrictKnowledgeModel):
    """One structured state change produced by an interaction."""

    effect_id: LogicalId
    operation: StateOperation = Field(
        validation_alias=AliasChoices("operation", "action"),
    )
    subject: LogicalId
    field: LogicalPath
    value: JsonValue | None = None
    visibility: Channel | None = None

    @field_validator("operation", mode="before")
    @classmethod
    def normalize_operation(cls, value: object) -> StateOperation:
        return _enum_value(StateOperation, value)  # type: ignore[return-value]

    @field_validator("visibility", mode="before")
    @classmethod
    def normalize_visibility(cls, value: object) -> Channel | None:
        if value is None:
            return None
        return _enum_value(Channel, value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_operation_value(self) -> OutcomeEffect:
        """Keep operation payloads explicit and unambiguous."""

        has_value = "value" in self.model_fields_set
        if self.operation is StateOperation.CLEAR:
            if has_value:
                raise ValueError("CLEAR must not include a value")
        elif not has_value:
            raise ValueError(f"{self.operation.value} requires an explicit value")
        return self


class OutcomeDefinition(_StrictKnowledgeModel):
    """Structured result of resolving an interaction."""

    outcome_code: LogicalId = Field(
        validation_alias=AliasChoices("outcome_code", "result"),
    )
    status: OutcomeStatus
    effects: list[OutcomeEffect] = Field(
        validation_alias=AliasChoices("effects", "state_changes"),
    )
    summary: BoundedText | None = Field(
        default=None,
        validation_alias=AliasChoices("summary", "description"),
    )

    @field_validator("effects")
    @classmethod
    def validate_effect_ids(cls, value: list[OutcomeEffect]) -> list[OutcomeEffect]:
        _check_unique([effect.effect_id for effect in value], "effects effect_id")
        return value

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> OutcomeStatus:
        return _enum_value(OutcomeStatus, value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_effects_for_status(self) -> OutcomeDefinition:
        """Require a state change when the interaction reports success."""

        if self.status in {OutcomeStatus.APPLIED, OutcomeStatus.PARTIAL} and not self.effects:
            raise ValueError(f"{self.status.value} requires at least one effect")
        return self


class VisibilityRule(_StrictKnowledgeModel):
    """One typed notification rule and its allowed visibility channel."""

    notification_id: LogicalId
    visibility: Channel = Field(
        validation_alias=AliasChoices("visibility", "channel"),
    )
    message_code: LogicalId
    audience: LogicalId | None = None
    recipients: list[LogicalId] = Field(default_factory=list)

    @field_validator("visibility", mode="before")
    @classmethod
    def normalize_visibility(cls, value: object) -> Channel:
        return _enum_value(Channel, value)  # type: ignore[return-value]

    @field_validator("recipients")
    @classmethod
    def validate_recipients(cls, value: list[str]) -> list[str]:
        _check_unique(value, "recipients")
        return value


class ScenarioAction(_StrictKnowledgeModel):
    """A typed action used in a scenario example."""

    action_id: LogicalId
    action: LogicalId
    subject: LogicalId | None = None
    target: LogicalId | None = None
    parameters: dict[LogicalId, JsonValue] = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def validate_parameter_keys(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in value:
            if _ID_PATTERN.fullmatch(key) is None:
                raise ValueError("parameters keys must be lowercase logical identifiers")
        return value


class ScenarioExample(_StrictKnowledgeModel):
    """A reproducible given/when/then example for an interaction."""

    example_id: LogicalId = Field(
        validation_alias=AliasChoices("example_id", "scenario_id", "id"),
    )
    given: list[Predicate] = Field(
        min_length=1,
        validation_alias=AliasChoices("given", "preconditions"),
    )
    when: list[ScenarioAction] = Field(
        min_length=1,
        validation_alias=AliasChoices("when", "actions"),
    )
    then: OutcomeDefinition = Field(
        validation_alias=AliasChoices("then", "expected_outcome", "outcome"),
    )
    notes: BoundedText | None = None

    @field_validator("when")
    @classmethod
    def validate_action_ids(cls, value: list[ScenarioAction]) -> list[ScenarioAction]:
        _check_unique([action.action_id for action in value], "when action_id")
        return value


class InteractionDefinition(_StrictKnowledgeModel):
    """A version-pinned rule for resolving multiple subjects together."""

    schema_version: Literal[1]
    kind: Literal["interaction"]
    id: LogicalId
    version: SemanticVersion
    name: BoundedText
    status: Literal["published"]
    reviewed_by: ReviewerId
    reviewed_at: date
    board_refs: list[VersionedRef] = Field(min_length=1)
    subjects: list[LogicalId] = Field(min_length=2)
    situation_key: str
    preconditions: list[Predicate] = Field(min_length=1)
    ordering: list[ResolutionStep] = Field(min_length=1)
    outcome: OutcomeDefinition
    notifications: list[VisibilityRule] = Field(min_length=1)
    examples: list[ScenarioExample] = Field(min_length=1)
    claim_refs: list[LogicalId] = Field(min_length=1)
    source_refs: list[LogicalId] = Field(min_length=1)

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def normalize_review_date(cls, value: object) -> object:
        return _normalize_reviewed_at(value)

    @field_validator("board_refs", mode="before")
    @classmethod
    def parse_board_refs(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [VersionedRef.parse(item) if isinstance(item, str) else item for item in value]

    @field_validator("board_refs")
    @classmethod
    def validate_board_refs(cls, value: list[VersionedRef]) -> list[VersionedRef]:
        if any(reference.id == "latest" for reference in value):
            raise ValueError("board_refs must not use the unpinned 'latest' id")
        _check_unique([reference.format() for reference in value], "board_refs")
        return value

    @field_validator("subjects")
    @classmethod
    def validate_subjects(cls, value: list[str]) -> list[str]:
        _check_unique(value, "subjects")
        if len(value) < 2:
            raise ValueError("subjects must contain at least two distinct IDs")
        return value

    @field_validator("situation_key", mode="before")
    @classmethod
    def normalize_situation_key(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        normalized = re.sub(r"\s*\.\s*", ".", normalized)
        normalized = re.sub(r"[-\s]+", "_", normalized)
        normalized = re.sub(r"_+", "_", normalized)
        return normalized

    @field_validator("situation_key")
    @classmethod
    def validate_situation_key(cls, value: str) -> str:
        if _SITUATION_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "situation_key must be a normalized dotted lowercase logical key",
            )
        return value

    @field_validator("claim_refs", "source_refs")
    @classmethod
    def validate_references(cls, value: list[str]) -> list[str]:
        _check_unique(value, "knowledge references")
        return value

    @field_validator("notifications")
    @classmethod
    def validate_notifications(cls, value: list[VisibilityRule]) -> list[VisibilityRule]:
        _check_unique(
            [notification.notification_id for notification in value],
            "notifications notification_id",
        )
        return value

    @field_validator("examples")
    @classmethod
    def validate_examples(cls, value: list[ScenarioExample]) -> list[ScenarioExample]:
        _check_unique([example.example_id for example in value], "examples example_id")
        return value

    @field_validator("ordering")
    @classmethod
    def validate_step_ids(cls, value: list[ResolutionStep]) -> list[ResolutionStep]:
        _check_unique([step.step_id for step in value], "ordering step_id")
        return value

    @model_validator(mode="after")
    def validate_ordering(self) -> InteractionDefinition:
        """Require the persisted list and order numbers to agree exactly."""

        orders = [step.order for step in self.ordering]
        expected = list(range(1, len(self.ordering) + 1))
        if orders != expected:
            raise ValueError("ordering must use contiguous order values starting at 1")
        return self


# ``StateChange`` is the vocabulary used by a few callers for an outcome
# effect.  Keep it as an exact type alias so both names retain the same strict
# validation contract without introducing a second schema.
StateChange = OutcomeEffect


__all__ = [
    "InteractionDefinition",
    "LogicalId",
    "LogicalPath",
    "OutcomeDefinition",
    "OutcomeEffect",
    "OutcomeStatus",
    "Predicate",
    "PredicateOperator",
    "ResolutionStep",
    "ScenarioAction",
    "ScenarioExample",
    "StateChange",
    "StateOperation",
    "VisibilityRule",
]
