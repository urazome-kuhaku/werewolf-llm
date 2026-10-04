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

from .events import DeliveryCursor, GameEvent

SCHEMA_VERSION: Literal[1] = 1
_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_RULESET_SNAPSHOT_ID_PATTERN = re.compile(r"ruleset-[0-9a-f]{64}", re.ASCII)

GameId = Annotated[str, Field(min_length=1, max_length=64, strict=True)]
LogicalId = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
SeatNo = Annotated[int, Field(ge=1, le=64, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]


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
    outcome_digest: Annotated[str, Field(min_length=64, max_length=64, strict=True)]

    @field_validator("request_ids", mode="before")
    @classmethod
    def accept_receipt_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


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
        state_keys = tuple((item.scope, item.scope_id, item.key) for item in self.rule_state)
        if len(set(state_keys)) != len(state_keys):
            raise ValueError("rule state scope/key pairs must be unique")
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
    "RuleUseRecord",
    "RuleFactRecord",
    "SerialTurnBinding",
    "RandomStateRef",
    "RulesetRef",
    "SeatNo",
    "utc_now",
]
