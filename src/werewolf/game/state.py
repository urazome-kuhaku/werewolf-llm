"""Strict, board-agnostic models for the authoritative game state.

This module models the durable shape of a game without deciding any role,
action, vote, or victory rule.  Those details belong to a published ruleset
and to later validators/reducers.  The deliberately generic extension
containers are private to the game process and are not player projections.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal, NoReturn

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.knowledge.role import (
    ActionCode,
    ResourceDefinition,
    TargetRule,
    TriggerRule,
    UsageLimit,
)

from .actions import ActionWindow
from .events import DeliveryCursor, GameEvent

SCHEMA_VERSION: Literal[1] = 1
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_RULESET_SNAPSHOT_ID_PATTERN = re.compile(r"ruleset-[0-9a-f]{64}", re.ASCII)

GameId = Annotated[str, Field(min_length=1, max_length=64, strict=True)]
LogicalId = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
SeatNo = Annotated[int, Field(ge=1, le=64, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
RuleHook = Literal["DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"]


def utc_now() -> datetime:
    """Return an aware UTC timestamp suitable for a state transition."""

    return datetime.now(UTC)


def _utc_datetime(value: object) -> datetime:
    """Validate and normalize an RFC 3339-like value to aware UTC.

    Pydantic's strict mode intentionally rejects implicit datetime coercion,
    but snapshots commonly arrive as JSON strings.  Explicitly parsing those
    strings here keeps the model strict while still accepting the documented
    wire representation.  Naive values are always rejected.
    """

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be a valid RFC 3339 datetime") from exc
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime or RFC 3339 string")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _validate_id(value: str, *, name: str) -> str:
    if _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must contain only lowercase ASCII letters, digits, '-' or '_'")
    return value


class _FrozenDict(dict[object, object]):
    """A JSON-compatible mapping that rejects in-place mutation.

    ``ConfigDict(frozen=True)`` protects model attributes, but Python's built-in
    dictionaries remain mutable when they are nested in a model.  Keeping a
    dict subclass preserves the normal JSON object representation while closing
    those mutation paths for the authoritative in-memory state.
    """

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("state mappings are immutable; create a new state instead")

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


def _deep_freeze(value: object) -> object:
    """Recursively freeze JSON containers without changing their wire shape."""

    if isinstance(value, dict):
        return _FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw_json(value: object) -> object:
    """Return ordinary JSON containers for the wire serializer.

    State extension fields are frozen in memory by converting arrays to tuples.
    That representation is useful for preventing accidental mutation, but it
    does not satisfy Pydantic's ``JsonValue`` serializer, whose array schema is
    list-shaped.  Thaw only at the JSON serialization boundary so Python mode
    dumps continue to expose the in-memory shape used by reducers.
    """

    if isinstance(value, dict):
        return {key: _deep_thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_thaw_json(item) for item in value]
    return value


class _StrictModel(BaseModel):
    """Common immutable Pydantic configuration for state records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    def model_post_init(self, __context: object) -> None:
        """Freeze nested JSON containers after Pydantic validation."""

        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            frozen = _deep_freeze(value)
            if frozen is not value:
                object.__setattr__(self, field_name, frozen)


class RulesetRef(_StrictModel):
    """Reference to the immutable published ruleset used by one game."""

    schema_version: Literal[1] = 1
    board_id: LogicalId
    version: Annotated[str, Field(min_length=5, max_length=32, strict=True)]
    snapshot_id: Annotated[str, Field(min_length=1, max_length=128, strict=True)]
    manifest_sha256: Annotated[str, Field(min_length=64, max_length=64, strict=True)]

    @field_validator("board_id")
    @classmethod
    def validate_board_id(cls, value: str) -> str:
        return _validate_id(value, name="board_id")

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if (
            re.fullmatch(
                r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                value,
            )
            is None
        ):
            raise ValueError("version must be a plain semantic version in X.Y.Z form")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def validate_snapshot_id(cls, value: str) -> str:
        if _ID_PATTERN.fullmatch(value) or _RULESET_SNAPSHOT_ID_PATTERN.fullmatch(value):
            return value
        raise ValueError(
            "snapshot_id must contain lowercase ASCII letters, digits, '-' or '_', "
            "or use the ruleset-<sha256> format"
        )

    @field_validator("manifest_sha256")
    @classmethod
    def validate_manifest_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("manifest_sha256 must be 64 lowercase hexadecimal characters")
        return value


class RandomStateRef(_StrictModel):
    """Deterministic random source metadata persisted with a game snapshot."""

    schema_version: Literal[1] = 1
    seed: int = Field(strict=True)
    draw_count: NonNegativeInt = 0


class RuleExecutionIdentity(_StrictModel):
    """Identity of the immutable executable package bound to a game."""

    schema_version: Literal[1] = 1
    package_id: LogicalId
    board_id: LogicalId
    board_version: Annotated[str, Field(min_length=1, max_length=64, strict=True)]
    execution_digest: Annotated[str, Field(min_length=1, max_length=128, strict=True)]
    action_registry_digest: Annotated[str | None, Field(strict=True)] = None

    @field_validator("action_registry_digest")
    @classmethod
    def validate_action_registry_digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("action_registry_digest must be 64 lowercase hexadecimal characters")
        return value


class AbilityInstanceState(_StrictModel):
    """One durable, seat-bound instance of an executable package skill."""

    schema_version: Literal[1] = 1
    ability_instance_id: LogicalId
    skill_id: LogicalId
    grant_id: LogicalId
    action_code: ActionCode
    actor_seat: SeatNo
    grant_kind: Literal["ACTIVE", "TRIGGER"]
    uses_consumed: NonNegativeInt = 0
    consumed: bool = False
    enabled: bool = True


class RuleStateValue(_StrictModel):
    """One typed, declared state value written by a rules execution batch."""

    schema_version: Literal[1] = 1
    scope: Literal["GAME", "SEAT", "ABILITY"]
    scope_id: LogicalId | None = None
    key: LogicalId
    value_type: LogicalId
    value: JsonValue
    source_batch_id: LogicalId
    skill_id: LogicalId | None = None
    source_request_id: LogicalId | None = None
    source_rule_id: LogicalId | None = None
    source_ability_instance_id: LogicalId | None = None
    expiry_policy: Literal["NEVER", "ROUND_END", "NEXT_NIGHT_START"] = "NEVER"
    expires_at_round: NonNegativeInt | None = None
    expires_at_hook: LogicalId | None = None

    @field_validator("value", mode="before")
    @classmethod
    def accept_frozen_json_value(cls, value: object) -> object:
        """Thaw snapshot arrays before strict JsonValue validation."""

        return _deep_thaw_json(value)

    @field_serializer("value")
    def serialize_rule_json_value(self, value: JsonValue) -> object:
        """Restore JSON list containers at either serialization boundary."""

        return _deep_thaw_json(value)

    @model_validator(mode="after")
    def validate_scope(self) -> RuleStateValue:
        if self.scope == "GAME" and self.scope_id is not None:
            raise ValueError("game-scoped rule state must not have scope_id")
        if self.scope != "GAME" and self.scope_id is None:
            raise ValueError("seat- and ability-scoped rule state require scope_id")
        return self


class RuleRelationValue(_StrictModel):
    """One persistent typed relation with the provenance that created it."""

    schema_version: Literal[1] = 1
    relation_id: LogicalId
    relation_type: LogicalId
    source_seat: SeatNo
    target_seat: SeatNo
    source_skill_id: LogicalId
    source_rule_id: LogicalId
    source_request_id: LogicalId
    created_round: NonNegativeInt
    source_ability_instance_id: LogicalId
    expiry_policy: Literal["NEVER", "ROUND_END", "NEXT_NIGHT_START"] = "NEVER"
    expires_at_round: NonNegativeInt | None = None
    expires_at_hook: LogicalId | None = None


class RuleDeferredDisclosure(_StrictModel):
    """A fixed, package-authorized projection waiting for its declared hook."""

    schema_version: Literal[1] = 1
    delivery_id: LogicalId
    package_id: LogicalId
    source_batch_id: LogicalId
    source_request_id: LogicalId
    source_revision: NonNegativeInt
    source_round_no: NonNegativeInt
    source_phase: GamePhase
    source_window_id: LogicalId | None = None
    source_logical_window_id: LogicalId | None = None
    actor_seat: SeatNo
    ability_instance_id: LogicalId
    action_code: ActionCode
    skill_id: LogicalId
    disclosure_id: LogicalId
    audience: Literal["SELF", "TEAM", "ALL", "SEATS"]
    recipients: tuple[SeatNo, ...]
    source_team_roster: tuple[SeatNo, ...] = ()
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    hook: LogicalId
    event_type: LogicalId | None = None

    @field_validator("recipients", "source_team_roster", mode="before")
    @classmethod
    def accept_disclosure_recipients(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("fields", mode="before")
    @classmethod
    def accept_disclosure_fields(cls, value: object) -> object:
        return _deep_thaw_json(value) if isinstance(value, dict) else value

    @field_serializer("fields")
    def serialize_disclosure_fields(self, value: dict[str, JsonValue]) -> object:
        return _deep_thaw_json(value)

    @model_validator(mode="after")
    def validate_deferred_disclosure(self) -> RuleDeferredDisclosure:
        if len(set(self.recipients)) != len(self.recipients):
            raise ValueError("deferred disclosure recipients must be unique")
        if self.audience == "TEAM" and self.source_team_roster != self.recipients:
            raise ValueError("TEAM deferred disclosure must preserve its source roster")
        if self.audience != "TEAM" and self.source_team_roster:
            raise ValueError("only TEAM disclosures may carry a source roster")
        return self


class RuleReturnPoint(_StrictModel):
    """A workflow resume reference that never duplicates speech queue state."""

    schema_version: Literal[1] = 1
    phase: GamePhase
    hook_id: RuleHook | None = None
    window_id: LogicalId | None = None
    logical_window_id: LogicalId | None = None
    speaker_seat: SeatNo | None = None
    serial_turn_id: LogicalId | None = None
    event_ids: tuple[NonNegativeInt, ...] = ()
    day_no: NonNegativeInt | None = None

    @field_validator("event_ids", mode="before")
    @classmethod
    def accept_return_event_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("event_ids")
    @classmethod
    def validate_return_event_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("return point event_ids must be sorted and unique")
        return value


class RuleBoundary(_StrictModel):
    """Confirmed-death boundary and its durable host-work completion proof."""

    schema_version: Literal[1] = 1
    boundary_id: LogicalId
    source_group_id: LogicalId
    source_batch_id: LogicalId
    death_fact_ids: tuple[LogicalId, ...]
    death_seats: tuple[SeatNo, ...]
    return_point: RuleReturnPoint
    last_words_required: bool = False
    last_words_seats: tuple[SeatNo, ...] = ()
    last_words_completed_seats: tuple[SeatNo, ...] = ()
    sheriff_badge_required: bool = False
    sheriff_badge_completed: bool = False
    created_at: datetime
    completed_at: datetime | None = None

    @field_validator(
        "death_fact_ids",
        "death_seats",
        "last_words_seats",
        "last_words_completed_seats",
        mode="before",
    )
    @classmethod
    def accept_boundary_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("created_at", "completed_at", mode="before")
    @classmethod
    def validate_boundary_timestamps(cls, value: object) -> datetime | None:
        return None if value is None else _utc_datetime(value)

    @model_validator(mode="after")
    def validate_boundary(self) -> RuleBoundary:
        if not self.death_fact_ids or not self.death_seats:
            raise ValueError("rule boundary requires confirmed death facts and seats")
        if len(set(self.death_fact_ids)) != len(self.death_fact_ids):
            raise ValueError("rule boundary death fact IDs must be unique")
        if len(set(self.death_seats)) != len(self.death_seats):
            raise ValueError("rule boundary death seats must be unique")
        if len(set(self.last_words_seats)) != len(self.last_words_seats):
            raise ValueError("rule boundary last-words seats must be unique")
        if len(set(self.last_words_completed_seats)) != len(self.last_words_completed_seats):
            raise ValueError("completed last-words seats must be unique")
        if not set(self.last_words_completed_seats).issubset(self.last_words_seats):
            raise ValueError("last-words completion must belong to a pending boundary seat")
        if not self.last_words_required and self.last_words_seats:
            raise ValueError("last-words seats require last_words_required")
        if self.sheriff_badge_completed and not self.sheriff_badge_required:
            raise ValueError("sheriff badge completion requires a badge boundary")
        if self.completed_at is not None and self.is_pending:
            raise ValueError("incomplete rule boundary cannot have completed_at")
        return self

    @property
    def is_pending(self) -> bool:
        return (
            self.last_words_required and self.last_words_completed_seats != self.last_words_seats
        ) or (self.sheriff_badge_required and not self.sheriff_badge_completed)


class RuleTriggerOccurrence(_StrictModel):
    """One deduplicated, resumable automatic or player-choice workflow item."""

    schema_version: Literal[1] = 1
    occurrence_id: LogicalId
    kind: Literal["TRIGGER", "HOOK"]
    source_fact_id: LogicalId
    source_batch_id: LogicalId
    ability_instance_id: LogicalId
    skill_id: LogicalId
    actor_seat: SeatNo
    mode: Literal["AUTOMATIC", "PLAYER_CHOICE"]
    order: NonNegativeInt
    status: Literal["QUEUED", "READY", "WAITING_CHOICE", "COMPLETED", "FAILED"] = "QUEUED"
    window_id: LogicalId | None = None
    request_id: LogicalId | None = None
    hook_id: RuleHook | None = None
    logical_window_id: LogicalId | None = None


class RuleWorkflowCursor(_StrictModel):
    """Durable progress for collection, trigger draining, and flow resumption."""

    schema_version: Literal[1] = 1
    cursor_id: LogicalId | None = None
    settlement_group_id: LogicalId | None = None
    active_window_ids: tuple[LogicalId, ...] = ()
    completed_collection_window_ids: tuple[LogicalId, ...] = ()
    active_occurrence_id: LogicalId | None = None
    return_point: RuleReturnPoint | None = None
    pending_flow_action: Literal["RESUME_HOOK", "ADVANCE_TO_NIGHT"] | None = None
    next_logical_window_id: LogicalId | None = None
    pending_boundary_id: LogicalId | None = None
    steps_used: NonNegativeInt = 0
    budget_limit: Annotated[int, Field(ge=1, le=100_000, strict=True)] = 512
    status: Literal[
        "IDLE",
        "COLLECTING",
        "DRAINING",
        "WAITING_CHOICE",
        "WAITING_BOUNDARY",
        "RETURN_READY",
        "ERROR",
    ] = "IDLE"
    error_code: LogicalId | None = None

    @field_validator("active_window_ids", "completed_collection_window_ids", mode="before")
    @classmethod
    def accept_workflow_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_cursor(self) -> RuleWorkflowCursor:
        if len(set(self.active_window_ids)) != len(self.active_window_ids):
            raise ValueError("active_window_ids must be unique")
        if len(set(self.completed_collection_window_ids)) != len(
            self.completed_collection_window_ids
        ):
            raise ValueError("completed collection window IDs must be unique")
        if self.status == "ERROR" and self.error_code is None:
            raise ValueError("an error workflow cursor requires an error_code")
        return self


class RuleWorkflowStep(_StrictModel):
    """Read-only manager response describing the next durable workflow step."""

    schema_version: Literal[1] = 1
    kind: Literal["IDLE", "RETURN", "AUTOMATIC", "PLAYER_CHOICE"]
    occurrence_id: LogicalId | None = None
    source_fact_id: LogicalId | None = None
    actor_seat: SeatNo | None = None
    ability_instance_id: LogicalId | None = None
    skill_id: LogicalId | None = None
    mode: Literal["AUTOMATIC", "PLAYER_CHOICE"] | None = None
    window_id: LogicalId | None = None
    hook_id: RuleHook | None = None
    action_window: ActionWindow | None = None
    return_point: RuleReturnPoint | None = None
    next_phase: GamePhase | None = None
    queue_pending: bool = False
    cursor_id: LogicalId | None = None
    boundary: RuleBoundary | None = None

    @model_validator(mode="after")
    def validate_step_shape(self) -> RuleWorkflowStep:
        if self.kind in {"AUTOMATIC", "PLAYER_CHOICE"} and any(
            value is None
            for value in (
                self.occurrence_id,
                self.source_fact_id,
                self.actor_seat,
                self.ability_instance_id,
                self.skill_id,
                self.mode,
            )
        ):
            raise ValueError("trigger workflow steps require a complete occurrence binding")
        if self.kind == "PLAYER_CHOICE" and (self.window_id is None or self.action_window is None):
            raise ValueError("player-choice workflow steps require an installed action window")
        return self


class RuleUseRecord(_StrictModel):
    """Typed committed use-history row retained for interpreter observations."""

    schema_version: Literal[1] = 1
    record_id: LogicalId
    request_id: LogicalId
    ability_instance_id: LogicalId
    skill_id: LogicalId
    action_code: ActionCode
    actor_seat: SeatNo
    round_number: NonNegativeInt
    targets: tuple[SeatNo, ...] = ()
    passed: bool = False
    successful: bool = True
    disposition: Literal["ACCEPTED", "PASSED", "REJECTED"] = "ACCEPTED"

    @field_validator("targets", mode="before")
    @classmethod
    def accept_use_targets(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class RuleFactRecord(_StrictModel):
    """Typed, private fact retained with the package execution provenance."""

    schema_version: Literal[1] = 1
    fact_id: LogicalId
    fact_type: LogicalId
    source_rule_id: LogicalId | None = None
    source_request_id: LogicalId | None = None
    actor_seat: SeatNo | None = None
    target_seat: SeatNo | None = None
    death_cause: LogicalId | None = None
    tags: tuple[LogicalId, ...] = ()
    data: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("tags", mode="before")
    @classmethod
    def accept_fact_tags(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("data", mode="before")
    @classmethod
    def accept_frozen_fact_json(cls, value: object) -> object:
        # State snapshots recursively freeze arrays as tuples. Thaw them at
        # this validation boundary so strict JsonValue accepts Python-mode
        # dumps during phase transitions and restore.
        return _deep_thaw_json(value) if isinstance(value, dict) else value

    @field_serializer("data")
    def serialize_fact_json(self, value: dict[str, JsonValue]) -> object:
        """Restore JSON list containers at either serialization boundary."""

        return _deep_thaw_json(value)


class RuleLedgerEntry(_StrictModel):
    """Auditable provenance for one committed pure-interpreter batch."""

    schema_version: Literal[1] = 1
    batch_id: LogicalId
    package_id: LogicalId
    group_id: LogicalId
    timing: LogicalId
    read_revision: NonNegativeInt
    committed_revision: NonNegativeInt
    round_no: NonNegativeInt
    request_ids: tuple[LogicalId, ...]
    actor_seats: tuple[SeatNo, ...]
    skill_ids: tuple[LogicalId, ...]
    action_codes: tuple[ActionCode, ...]
    history_updates: tuple[RuleUseRecord, ...] = ()
    facts: tuple[RuleFactRecord, ...] = ()
    outcome_digest: Annotated[str, Field(min_length=64, max_length=64, strict=True)]
    created_at: datetime

    @field_validator("request_ids", "actor_seats", "skill_ids", "action_codes", mode="before")
    @classmethod
    def accept_ledger_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("history_updates", "facts", mode="before")
    @classmethod
    def accept_nested_ledger_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("request_ids", "actor_seats", "skill_ids", "action_codes")
    @classmethod
    def validate_unique_ledger_values(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        if len(set(value)) != len(value):
            raise ValueError("rule ledger values must be unique")
        return value

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: object) -> datetime:
        return _utc_datetime(value)


class RuleCommitReceipt(_StrictModel):
    """Idempotency receipt for a committed interpreter batch."""

    schema_version: Literal[1] = 1
    batch_id: LogicalId
    package_id: LogicalId
    group_id: LogicalId
    timing: LogicalId
    read_revision: NonNegativeInt
    committed_revision: NonNegativeInt
    request_ids: tuple[LogicalId, ...]
    occurrence_ids: tuple[LogicalId, ...] = ()
    outcome_digest: Annotated[str, Field(min_length=64, max_length=64, strict=True)]

    @field_validator("request_ids", "occurrence_ids", mode="before")
    @classmethod
    def accept_receipt_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("request_ids", "occurrence_ids")
    @classmethod
    def validate_receipt_unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("rule receipt IDs must be unique")
        return value


class GrantedTriggerAbility(_StrictModel):
    """One trigger-capable ability granted to a seat at assignment time.

    This is a private, authoritative copy of the executable parts of a
    published role ability.  It deliberately contains no prose: the runtime
    uses the frozen trigger and target contracts to decide whether a window
    can open and the seat's controller only chooses within that contract.
    ``consumed`` tracks one-shot triggers without making the published role
    definition mutable.
    """

    schema_version: Literal[1] = 1
    ability_id: LogicalId
    action_code: ActionCode
    trigger: TriggerRule
    target_rule: TargetRule
    consumed: bool = False


class GrantedAbility(_StrictModel):
    """One active role ability granted to a seat at assignment time.

    This is the private, authoritative authorization record used by the game
    process when it builds an action window.  It is deliberately a compact
    copy of the executable contract from the frozen role definition: prose
    request/effect descriptions stay in the knowledge package, while timing,
    target, usage, and resource constraints remain available to authorization
    code after assignment.

    ``uses_consumed`` is state owned by the game and starts at zero.  Resource
    balances remain in ``PlayerState.skill_resources`` so resource deductions
    and ability use counters can be committed together by the reducer.
    """

    schema_version: Literal[1] = 1
    ability_id: LogicalId
    action_code: ActionCode
    timing: GamePhase
    allowed_phases: tuple[GamePhase, ...] = Field(min_length=1)
    target_rule: TargetRule
    usage_limit: UsageLimit | None = None
    resource: ResourceDefinition | None = None
    uses_consumed: NonNegativeInt = 0

    @field_validator("allowed_phases")
    @classmethod
    def validate_allowed_phases(
        cls,
        value: tuple[GamePhase, ...],
    ) -> tuple[GamePhase, ...]:
        if len(set(value)) != len(value):
            raise ValueError("granted ability allowed_phases must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> GrantedAbility:
        if self.action_code == 0:
            raise ValueError("granted active abilities require a positive action_code")
        if self.timing not in self.allowed_phases:
            raise ValueError("granted ability timing must be included in allowed_phases")
        if (
            self.usage_limit is not None
            and self.usage_limit.max_uses is not None
            and self.uses_consumed > self.usage_limit.max_uses
        ):
            raise ValueError("granted ability uses_consumed exceeds usage_limit.max_uses")
        return self


class PlayerState(_StrictModel):
    """Private authoritative state for one seat.

    ``role_id`` and ``faction_id`` are intentionally present only in this
    private model.  Public projections must be created by a separate mapper;
    this type never acts as a player-facing payload.
    """

    schema_version: Literal[1] = 1
    seat: SeatNo
    role_id: LogicalId
    faction_id: LogicalId
    victory_group_id: LogicalId | None = None
    chat_group_ids: tuple[LogicalId, ...] = ()
    alive: bool = True
    death_cause: LogicalId | None = None
    vote_weight: float = Field(default=1.0, ge=0.0, strict=True)
    can_vote: bool = True
    skill_resources: dict[LogicalId, NonNegativeInt] = Field(default_factory=dict)
    granted_abilities: tuple[GrantedAbility, ...] = ()
    granted_trigger_abilities: tuple[GrantedTriggerAbility, ...] = ()
    runtime_ref: Annotated[str, Field(min_length=1, max_length=128, strict=True)] | None = None
    session_epoch: NonNegativeInt = 0
    knowledge_receipt_ids: tuple[
        Annotated[str, Field(min_length=1, max_length=128, strict=True)], ...
    ] = ()
    current_request_id: Annotated[str, Field(min_length=1, max_length=128, strict=True)] | None = (
        None
    )
    confirmed_event_cursor: NonNegativeInt = 0

    @field_validator("role_id", "faction_id", "victory_group_id", "death_cause")
    @classmethod
    def validate_logical_ids(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _validate_id(value, name="logical ID")

    @field_validator("chat_group_ids", mode="before")
    @classmethod
    def accept_chat_groups(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("chat_group_ids")
    @classmethod
    def validate_chat_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for group_id in value:
            _validate_id(group_id, name="chat group ID")
        if len(set(value)) != len(value):
            raise ValueError("chat_group_ids must be unique")
        return value

    @field_validator("skill_resources")
    @classmethod
    def validate_skill_resource_keys(cls, value: dict[str, int]) -> dict[str, int]:
        for key in value:
            _validate_id(key, name="skill resource ID")
        return value

    @field_validator("granted_trigger_abilities")
    @classmethod
    def validate_granted_trigger_abilities(
        cls,
        value: tuple[GrantedTriggerAbility, ...],
    ) -> tuple[GrantedTriggerAbility, ...]:
        ability_ids = tuple(ability.ability_id for ability in value)
        if len(set(ability_ids)) != len(ability_ids):
            raise ValueError("granted trigger ability IDs must be unique")
        return value

    @field_validator("granted_abilities")
    @classmethod
    def validate_granted_abilities(
        cls,
        value: tuple[GrantedAbility, ...],
    ) -> tuple[GrantedAbility, ...]:
        ability_ids = tuple(ability.ability_id for ability in value)
        if len(set(ability_ids)) != len(ability_ids):
            raise ValueError("granted ability IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_granted_ability_ids(self) -> PlayerState:
        active_ids = tuple(ability.ability_id for ability in self.granted_abilities)
        trigger_ids = tuple(ability.ability_id for ability in self.granted_trigger_abilities)
        all_ids = (*active_ids, *trigger_ids)
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("granted ability IDs must be unique across all ability grants")
        return self


class SerialTurnBinding(_StrictModel):
    """The durable identity of the one physical request at queue head."""

    seat: SeatNo
    session_epoch: NonNegativeInt
    request_id: LogicalId
    logical_request_id: LogicalId
    attempt_no: Annotated[int, Field(ge=1, strict=True)]
    event_ids: tuple[NonNegativeInt, ...] = ()
    rule_boundary_id: LogicalId | None = None

    @field_validator("event_ids")
    @classmethod
    def validate_event_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("serial turn event_ids must be sorted and unique")
        return value


class GameState(_StrictModel):
    """The in-memory authoritative state for one game.

    Events and delivery cursors use the dedicated protocol models.  The other
    action, vote, receipt, audit, and snapshot records remain extension
    containers until their dedicated protocol models are added.  Keeping
    those records here preserves the state boundary without inventing rules or
    pretending that unreviewed board material is authoritative.
    """

    schema_version: Literal[1] = 1
    game_id: GameId
    state_revision: NonNegativeInt = 0
    created_at: datetime
    updated_at: datetime
    phase: GamePhase = GamePhase.CREATED
    run_status: RunStatus = RunStatus.READY
    round_no: NonNegativeInt = 0
    day_no: NonNegativeInt = 0
    ruleset: RulesetRef | None = None
    rng: RandomStateRef | None = None
    # The execution identity and interpreter-owned records are optional so
    # schema-1 snapshots created before the rules engine remain loadable.  New
    # games bind these records to the exact executable package at start.
    execution_identity: RuleExecutionIdentity | None = None
    ability_instances: tuple[AbilityInstanceState, ...] = ()
    rule_state: tuple[RuleStateValue, ...] = ()
    # B adds typed relations and one durable trigger/workflow queue.  These
    # empty defaults preserve schema-1 A snapshots and manual legacy paths.
    rule_relations: tuple[RuleRelationValue, ...] = ()
    rule_deferred_disclosures: tuple[RuleDeferredDisclosure, ...] = ()
    rule_trigger_queue: tuple[RuleTriggerOccurrence, ...] = ()
    rule_workflow_cursor: RuleWorkflowCursor | None = None
    rule_boundaries: tuple[RuleBoundary, ...] = ()
    rule_ledger: tuple[RuleLedgerEntry, ...] = ()
    rule_receipts: tuple[RuleCommitReceipt, ...] = ()
    players: dict[SeatNo, PlayerState] = Field(default_factory=dict)

    # Events and delivery cursors are protocol records rather than untyped
    # extension JSON.  The ``dict`` branch is retained for reading old
    # snapshots created before the event protocol was introduced; all new
    # event commits in GameManager require GameEvent instances and never write
    # this legacy branch.
    events: tuple[GameEvent | dict[str, JsonValue], ...] = ()
    delivery_cursors: dict[SeatNo, DeliveryCursor] = Field(default_factory=dict)
    current_queue: tuple[SeatNo, ...] | None = None
    # The current serialized runtime request, when a queue head is waiting on
    # a response.  This is deliberately JSON shaped so snapshots can be
    # resumed without importing the runtime protocol into the game state.
    # ``GameManager`` is the only writer and replaces it atomically with the
    # delivery cursor and the public speech event.
    serial_turn: SerialTurnBinding | None = None
    # The last ordinary committed speech turn remains as a durable source
    # reference for DAY_SPEECH_AFTER hooks. It is not an active turn and does
    # not alter the original serialized queue.
    last_serial_turn: SerialTurnBinding | None = None
    action_windows: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    action_requests: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    resolutions: tuple[dict[str, JsonValue], ...] = ()
    pending_resolution: dict[str, JsonValue] | None = None
    knowledge_receipts: tuple[dict[str, JsonValue], ...] = ()
    vote_state: dict[str, JsonValue] | None = None
    # The sheriff election contains private ballots and moderator-only
    # decisions.  It remains JSON shaped here so public projections cannot
    # accidentally expose the ballot collector.  GameManager is the only
    # supported writer; ``sheriff_seat`` is the small public outcome.
    sheriff_election: dict[str, JsonValue] | None = None
    sheriff_seat: SeatNo | None = None
    # Durable marker for a sheriff whose office became ineligible.  The
    # marker is intentionally JSON shaped so a process restart can resume the
    # exact transfer/tear request without changing the private election record.
    sheriff_badge: dict[str, JsonValue] | None = None
    moderator_audit: tuple[dict[str, JsonValue], ...] = ()
    winner: dict[str, JsonValue] | None = None
    last_snapshot: dict[str, JsonValue] | None = None

    @field_serializer(
        "action_windows",
        "action_requests",
        "resolutions",
        "pending_resolution",
        "knowledge_receipts",
        "vote_state",
        "sheriff_election",
        "sheriff_badge",
        "moderator_audit",
        "winner",
        "last_snapshot",
        when_used="json",
    )
    def serialize_json_extensions(self, value: object) -> object:
        """Restore JSON array/object containers for wire serialization."""

        return _deep_thaw_json(value)

    @field_serializer("events", when_used="json")
    def serialize_events(self, value: tuple[GameEvent | dict[str, JsonValue], ...]) -> list[object]:
        """Serialize typed events and legacy JSON events without warnings."""

        return [
            event.model_dump(mode="json")
            if isinstance(event, GameEvent)
            else _deep_thaw_json(event)
            for event in value
        ]

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def validate_timestamps(cls, value: object) -> datetime:
        return _utc_datetime(value)

    @field_validator(
        "ability_instances",
        "rule_state",
        "rule_relations",
        "rule_deferred_disclosures",
        "rule_trigger_queue",
        "rule_boundaries",
        "rule_ledger",
        "rule_receipts",
        mode="before",
    )
    @classmethod
    def accept_rule_record_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("game_id")
    @classmethod
    def validate_game_id(cls, value: str) -> str:
        return _validate_id(value, name="game_id")

    @field_validator("current_queue")
    @classmethod
    def validate_queue_seats(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("current_queue must not contain duplicate seats")
        return value

    @model_validator(mode="after")
    def validate_player_indexes(self) -> GameState:
        for seat, player in self.players.items():
            if seat != player.seat:
                raise ValueError("players mapping keys must match PlayerState.seat")
        for seat in self.delivery_cursors:
            if seat not in self.players:
                raise ValueError("delivery_cursors may only refer to known seats")
        typed_events = [event for event in self.events if isinstance(event, GameEvent)]
        event_ids = tuple(event.event_id for event in typed_events)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("events must not contain duplicate event IDs")
        if event_ids != tuple(sorted(event_ids)):
            raise ValueError("events must be ordered by ascending event ID")
        for event in typed_events:
            if event.game_id != self.game_id:
                raise ValueError("events may only refer to the current game")
        instance_ids = tuple(item.ability_instance_id for item in self.ability_instances)
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("ability instance IDs must be unique")
        if any(item.actor_seat not in self.players for item in self.ability_instances):
            raise ValueError("ability instances may only belong to assigned seats")
        state_keys = tuple(
            (item.scope, item.scope_id, item.skill_id, item.key) for item in self.rule_state
        )
        if len(set(state_keys)) != len(state_keys):
            raise ValueError("rule state scope/key pairs must be unique")
        relation_ids = tuple(item.relation_id for item in self.rule_relations)
        if len(set(relation_ids)) != len(relation_ids):
            raise ValueError("rule relation IDs must be unique")
        disclosure_ids = tuple(item.delivery_id for item in self.rule_deferred_disclosures)
        if len(set(disclosure_ids)) != len(disclosure_ids):
            raise ValueError("deferred rule disclosure IDs must be unique")
        if any(
            item.source_seat not in self.players or item.target_seat not in self.players
            for item in self.rule_relations
        ):
            raise ValueError("rule relation endpoints must be assigned seats")
        occurrence_ids = tuple(item.occurrence_id for item in self.rule_trigger_queue)
        if len(set(occurrence_ids)) != len(occurrence_ids):
            raise ValueError("rule trigger occurrence IDs must be unique")
        if any(
            item.actor_seat not in self.players or item.ability_instance_id not in set(instance_ids)
            for item in self.rule_trigger_queue
        ):
            raise ValueError("rule trigger occurrence must bind an assigned seat and instance")
        boundary_ids = tuple(item.boundary_id for item in self.rule_boundaries)
        if len(set(boundary_ids)) != len(boundary_ids):
            raise ValueError("rule boundary IDs must be unique")
        if any(
            seat not in self.players
            for boundary in self.rule_boundaries
            for seat in (*boundary.death_seats, *boundary.last_words_seats)
        ):
            raise ValueError("rule boundaries may only refer to assigned seats")
        if self.rule_workflow_cursor is not None:
            active_occurrence = self.rule_workflow_cursor.active_occurrence_id
            if active_occurrence is not None and active_occurrence not in set(occurrence_ids):
                raise ValueError("workflow cursor references an unknown trigger occurrence")
            if any(
                window_id not in self.action_windows
                for window_id in self.rule_workflow_cursor.active_window_ids
            ):
                raise ValueError("workflow cursor references an unknown action window")
        batch_ids = tuple(item.batch_id for item in self.rule_receipts)
        if len(set(batch_ids)) != len(batch_ids):
            raise ValueError("rule batch receipts must be unique")
        ledger_batches = tuple(item.batch_id for item in self.rule_ledger)
        if len(set(ledger_batches)) != len(ledger_batches):
            raise ValueError("rule ledger batch IDs must be unique")
        if self.execution_identity is not None and self.ruleset is not None:
            if (
                self.execution_identity.board_id != self.ruleset.board_id
                or self.execution_identity.board_version != self.ruleset.version
            ):
                raise ValueError("execution identity must match the frozen ruleset reference")
        return self


__all__ = [
    "SCHEMA_VERSION",
    "GameId",
    "GameState",
    "AbilityInstanceState",
    "GrantedAbility",
    "GrantedTriggerAbility",
    "LogicalId",
    "PlayerState",
    "RuleCommitReceipt",
    "RuleExecutionIdentity",
    "RuleLedgerEntry",
    "RuleStateValue",
    "RuleRelationValue",
    "RuleDeferredDisclosure",
    "RuleBoundary",
    "RuleReturnPoint",
    "RuleTriggerOccurrence",
    "RuleWorkflowCursor",
    "RuleWorkflowStep",
    "RuleHook",
    "RuleUseRecord",
    "RuleFactRecord",
    "SerialTurnBinding",
    "RandomStateRef",
    "RulesetRef",
    "SeatNo",
    "utc_now",
]
