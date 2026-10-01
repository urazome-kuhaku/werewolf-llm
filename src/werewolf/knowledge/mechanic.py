"""Strict, versioned models for public game mechanics.

Mechanics describe rules shared by several roles or by the board as a whole.
They deliberately contain typed execution fields so a published document does
not have to be interpreted from prose before it can be queried or compiled.
The model is generic: a mechanic is identified by data and does not encode a
particular voting, wolf-team, or other named rule.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from werewolf.domain.enums import Channel, GamePhase

from .interaction import Predicate, ResolutionStep, ScenarioExample, VisibilityRule
from .refs import VersionedRef

_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_PATH_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z", re.ASCII)
_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z", re.ASCII)
_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z",
    re.ASCII,
)


def _validate_logical_id(value: str) -> str:
    """Apply a full-match check because regex constraints use search semantics."""

    if _ID_PATTERN.fullmatch(value) is None:
        raise ValueError("must be a lowercase logical identifier")
    return value


def _validate_semantic_version(value: str) -> str:
    """Require a plain three-component semantic version."""

    if _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("must be a semantic version in X.Y.Z form")
    return value


LogicalId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$",
        strict=True,
    ),
    AfterValidator(_validate_logical_id),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(
        min_length=5,
        max_length=64,
        pattern=r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$",
        strict=True,
    ),
    AfterValidator(_validate_semantic_version),
]
BoundedText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2_000, strip_whitespace=True, strict=True),
]
ReviewerId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, strip_whitespace=True, strict=True),
]


class _StrictKnowledgeModel(BaseModel):
    """Shared immutable settings for published mechanic records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


def _check_unique(values: Sequence[object], field_name: str) -> None:
    """Reject repeated persisted identifiers while preserving list order."""

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


def _enum_value(enum_type: type[StrEnum], value: object) -> StrEnum:
    """Normalize an enum string without allowing unrelated coercion."""

    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TypeError(f"expected {enum_type.__name__} or its string value")
    return enum_type(value)


def _validate_step_order(steps: list[ResolutionStep], field_name: str) -> None:
    """Require an explicit contiguous persisted execution order."""

    _check_unique([step.step_id for step in steps], f"{field_name} step_id")
    orders = [step.order for step in steps]
    if orders != list(range(1, len(steps) + 1)):
        raise ValueError(f"{field_name} must use contiguous order values starting at 1")


class ParticipantEligibility(_StrictKnowledgeModel):
    """Machine-readable eligibility for one participant category."""

    participant_id: LogicalId = Field(
        validation_alias=AliasChoices("participant_id", "participant")
    )
    participant_kind: LogicalId = Field(validation_alias=AliasChoices("participant_kind", "kind"))
    required: bool = True
    conditions: list[Predicate] = Field(default_factory=list)

    @field_validator("conditions")
    @classmethod
    def validate_condition_subjects(cls, value: list[Predicate]) -> list[Predicate]:
        _check_unique(
            [(predicate.subject, predicate.field, predicate.operator.value) for predicate in value],
            "conditions",
        )
        return value


class MechanicInput(_StrictKnowledgeModel):
    """One typed value accepted by a mechanic request."""

    input_id: LogicalId = Field(validation_alias=AliasChoices("input_id", "field_id"))
    value_type: LogicalId
    description: BoundedText
    required: bool = True
    source: LogicalId | None = None
    allowed_values: list[JsonValue] | None = Field(default=None, min_length=1)


class MechanicOutput(_StrictKnowledgeModel):
    """One typed value emitted after mechanic resolution."""

    output_id: LogicalId = Field(validation_alias=AliasChoices("output_id", "output_code"))
    value_type: LogicalId
    description: BoundedText
    visibility: Channel

    @field_validator("visibility", mode="before")
    @classmethod
    def normalize_visibility(cls, value: object) -> Channel:
        return _enum_value(Channel, value)  # type: ignore[return-value]


class ExceptionBranch(_StrictKnowledgeModel):
    """A named exceptional path with typed conditions and consequences."""

    branch_id: LogicalId = Field(
        validation_alias=AliasChoices("branch_id", "exception_id", "exception_code")
    )
    conditions: list[Predicate] = Field(
        min_length=1,
        validation_alias=AliasChoices("conditions", "when"),
    )
    outcome_code: LogicalId = Field(validation_alias=AliasChoices("outcome_code", "outcome"))
    processing_order: list[ResolutionStep] = Field(
        default_factory=list,
        validation_alias=AliasChoices("processing_order", "ordering", "steps"),
    )
    output_ids: list[LogicalId] = Field(default_factory=list)
    visibility: Channel
    description: BoundedText | None = None

    @field_validator("visibility", mode="before")
    @classmethod
    def normalize_visibility(cls, value: object) -> Channel:
        return _enum_value(Channel, value)  # type: ignore[return-value]

    @field_validator("output_ids")
    @classmethod
    def validate_output_ids(cls, value: list[str]) -> list[str]:
        _check_unique(value, "output_ids")
        return value

    @field_validator("processing_order")
    @classmethod
    def validate_processing_order(cls, value: list[ResolutionStep]) -> list[ResolutionStep]:
        _validate_step_order(value, "exception processing_order")
        return value


class MechanicDefinition(_StrictKnowledgeModel):
    """Version-pinned, board-scoped public mechanic definition."""

    schema_version: Literal[1]
    kind: Literal["mechanic"]
    id: LogicalId
    version: SemanticVersion
    name: BoundedText
    aliases: list[BoundedText] = Field(default_factory=list)
    summary: BoundedText
    status: Literal["published"]
    reviewed_by: ReviewerId
    reviewed_at: date
    board_refs: list[VersionedRef] = Field(
        min_length=1,
        validation_alias=AliasChoices("board_refs", "applicable_boards"),
    )
    applicable_phases: list[GamePhase] = Field(
        min_length=1,
        validation_alias=AliasChoices("applicable_phases", "phases"),
    )
    participation: list[ParticipantEligibility] = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "participation",
            "eligibility",
            "participant_eligibility",
        ),
    )
    inputs: list[MechanicInput] = Field(default_factory=list)
    outputs: list[MechanicOutput] = Field(min_length=1)
    processing_order: list[ResolutionStep] = Field(
        min_length=1,
        validation_alias=AliasChoices("processing_order", "ordering", "resolution_order"),
    )
    exception_branches: list[ExceptionBranch] = Field(
        default_factory=list,
        validation_alias=AliasChoices("exception_branches", "exceptions"),
    )
    result_visibility: list[VisibilityRule] = Field(
        min_length=1,
        validation_alias=AliasChoices("result_visibility", "visibility"),
    )
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

    @field_validator("applicable_phases", mode="before")
    @classmethod
    def normalize_phases(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [_enum_value(GamePhase, phase) for phase in value]

    @field_validator("applicable_phases")
    @classmethod
    def validate_phases(cls, value: list[GamePhase]) -> list[GamePhase]:
        _check_unique(value, "applicable_phases")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        _check_unique(value, "aliases")
        return value

    @field_validator("participation")
    @classmethod
    def validate_participation(
        cls,
        value: list[ParticipantEligibility],
    ) -> list[ParticipantEligibility]:
        _check_unique(
            [participant.participant_id for participant in value],
            "participation participant_id",
        )
        return value

    @field_validator("inputs")
    @classmethod
    def validate_inputs(cls, value: list[MechanicInput]) -> list[MechanicInput]:
        _check_unique([item.input_id for item in value], "inputs input_id")
        return value

    @field_validator("outputs")
    @classmethod
    def validate_outputs(cls, value: list[MechanicOutput]) -> list[MechanicOutput]:
        _check_unique([item.output_id for item in value], "outputs output_id")
        return value

    @field_validator("processing_order")
    @classmethod
    def validate_processing_order(cls, value: list[ResolutionStep]) -> list[ResolutionStep]:
        _validate_step_order(value, "processing_order")
        return value

    @field_validator("exception_branches")
    @classmethod
    def validate_exception_branches(cls, value: list[ExceptionBranch]) -> list[ExceptionBranch]:
        _check_unique([branch.branch_id for branch in value], "exception_branches branch_id")
        return value

    @field_validator("result_visibility")
    @classmethod
    def validate_result_visibility(cls, value: list[VisibilityRule]) -> list[VisibilityRule]:
        _check_unique(
            [notification.notification_id for notification in value],
            "result_visibility notification_id",
        )
        return value

    @field_validator("examples")
    @classmethod
    def validate_examples(cls, value: list[ScenarioExample]) -> list[ScenarioExample]:
        _check_unique([example.example_id for example in value], "examples example_id")
        return value

    @field_validator("claim_refs", "source_refs")
    @classmethod
    def validate_references(cls, value: list[str]) -> list[str]:
        _check_unique(value, "knowledge references")
        return value

    @model_validator(mode="after")
    def validate_output_references(self) -> MechanicDefinition:
        """Ensure exception branches refer only to declared outputs."""

        output_ids = {output.output_id for output in self.outputs}
        for branch in self.exception_branches:
            unknown = set(branch.output_ids) - output_ids
            if unknown:
                raise ValueError("exception_branches output_ids must reference declared outputs")
        return self

    @property
    def id_version(self) -> VersionedRef:
        """Return this mechanic's immutable reference."""

        return VersionedRef(id=self.id, version=self.version)

    @property
    def eligibility(self) -> list[ParticipantEligibility]:
        """Compatibility spelling for the participation field."""

        return self.participation

    @property
    def ordering(self) -> list[ResolutionStep]:
        """Compatibility spelling for the processing order field."""

        return self.processing_order

    @property
    def visibility(self) -> list[VisibilityRule]:
        """Compatibility spelling for result visibility rules."""

        return self.result_visibility


__all__ = [
    "ExceptionBranch",
    "LogicalId",
    "MechanicDefinition",
    "MechanicInput",
    "MechanicOutput",
    "ParticipantEligibility",
    "SemanticVersion",
]
