"""Contracts shared by game scheduling and player runtimes.

The game process owns these models.  A runtime may propose a response, but it
does not get to mutate game state or choose the seat to which a response is
attached.  The discriminated response union is intentionally small so the Pi
adapter and the deterministic test runtime use exactly the same boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypeAlias, cast, runtime_checkable

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from werewolf.domain.enums import GamePhase


class ResponseKind(StrEnum):
    """The four terminal response kinds understood by the game process."""

    SPEECH = "speech"
    ACTION = "action"
    READY = "ready"
    ERROR = "error"


class _StrictRuntimeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


NonEmptyId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, strict=True),
]


class ObservationEvent(_StrictRuntimeModel):
    """One already-authorized observation supplied to a player."""

    event_id: int = Field(ge=0)
    event_type: NonEmptyId
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class Observation(_StrictRuntimeModel):
    """The player-visible increment for one turn request.

    ``payload`` is structured data produced by Python.  Player text is kept
    in event payloads and is never concatenated into a system instruction.
    """

    summary: str = Field(default="", max_length=8_000)
    events: list[ObservationEvent] = Field(default_factory=list)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class ActionWindowView(_StrictRuntimeModel):
    """The subset of an action window that is safe for one player to see."""

    window_id: NonEmptyId
    allowed_action_codes: list[int] = Field(min_length=1)
    min_actions: int = Field(default=1, ge=1)
    max_actions: int = Field(default=1, ge=1)
    allow_pass: bool = False
    allow_duplicate_action_codes: bool = False
    candidate_seats: list[int] = Field(default_factory=list)
    # Context is projected by the coordinator from the installed window. It
    # contains board-authored facts for this seat (for example a selector or
    # parameter choice set), never the coordinator's live mutable state.
    visible_context: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("allowed_action_codes")
    @classmethod
    def validate_action_codes(cls, value: list[int]) -> list[int]:
        if any(code < 1 for code in value):
            raise ValueError("allowed_action_codes must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("allowed_action_codes must not contain duplicates")
        return value

    @field_validator("candidate_seats")
    @classmethod
    def validate_candidate_seats(cls, value: list[int]) -> list[int]:
        if any(seat < 1 for seat in value):
            raise ValueError("candidate_seats must contain positive seat numbers")
        if len(value) != len(set(value)):
            raise ValueError("candidate_seats must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_action_count(self) -> ActionWindowView:
        if self.min_actions > self.max_actions:
            raise ValueError("min_actions must not exceed max_actions")
        if not self.allow_pass and 299 in self.allowed_action_codes:
            raise ValueError("PASS action code requires allow_pass=true")
        return self


class Deadline(_StrictRuntimeModel):
    """Soft and hard deadlines for one request.

    ``soft_at`` and ``hard_at`` are accepted as input aliases because those
    names are convenient at the subprocess boundary.  The canonical Python
    names remain ``soft_deadline`` and ``hard_deadline``.
    """

    soft_deadline: datetime = Field(
        validation_alias=AliasChoices("soft_deadline", "soft_at"),
    )
    hard_deadline: datetime = Field(
        validation_alias=AliasChoices("hard_deadline", "hard_at"),
    )

    @model_validator(mode="after")
    def validate_order(self) -> Deadline:
        if self.soft_deadline > self.hard_deadline:
            raise ValueError("soft_deadline must not be later than hard_deadline")
        return self

    @property
    def soft_at(self) -> datetime:
        return self.soft_deadline

    @property
    def hard_at(self) -> datetime:
        return self.hard_deadline


class TurnRequest(_StrictRuntimeModel):
    """A single physically addressable request sent to a runtime."""

    schema_version: Literal[1] = 1
    request_id: NonEmptyId
    logical_request_id: NonEmptyId
    attempt_no: int = Field(ge=1)
    game_id: NonEmptyId
    session_epoch: int = Field(ge=0)
    phase: GamePhase
    expected_kind: ResponseKind
    action_window: ActionWindowView | None = None
    observation: Observation
    output_schema: dict[str, JsonValue]
    deadline: Deadline


class Action(_StrictRuntimeModel):
    """One proposed action inside an atomic action bundle."""

    action_code: int = Field(gt=0)
    targets: list[int] = Field(default_factory=list)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    reason_public: str | None = Field(default=None, max_length=2_000)

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, value: list[int]) -> list[int]:
        if any(target < 1 for target in value):
            raise ValueError("targets must contain positive seat numbers")
        return value


class Speech(_StrictRuntimeModel):
    """A proposed public or team speech."""

    text: str = Field(min_length=1, max_length=20_000)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("speech text must not be blank")
        return value


class Ready(_StrictRuntimeModel):
    """Model-side readiness assertion with receipt identifiers.

    The identifiers are only a claim.  The server must independently validate
    its current ``get_board`` and ``get_role`` receipts before advancing the
    game phase.
    """

    knowledge_receipts: list[NonEmptyId] = Field(min_length=1)

    @field_validator("knowledge_receipts")
    @classmethod
    def validate_distinct_receipts(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("knowledge_receipts must not contain duplicate IDs")
        return value


class ResponseError(_StrictRuntimeModel):
    """A bounded runtime error that must not contain hidden game state."""

    code: NonEmptyId
    message: str = Field(min_length=1, max_length=2_000)
    retryable: bool = False


class _ResponseBase(_StrictRuntimeModel):
    schema_version: Literal[1] = 1
    request_id: NonEmptyId


class SpeechResponse(_ResponseBase):
    kind: Literal["speech"] = "speech"
    speech: Speech
    actions: None = None
    ready: None = None
    error: None = None


class ActionResponse(_ResponseBase):
    kind: Literal["action"] = "action"
    speech: None = None
    actions: list[Action] = Field(min_length=1)
    ready: None = None
    error: None = None


class ReadyResponse(_ResponseBase):
    kind: Literal["ready"] = "ready"
    speech: None = None
    actions: None = None
    ready: Ready
    error: None = None


class ErrorResponse(_ResponseBase):
    kind: Literal["error"] = "error"
    speech: None = None
    actions: None = None
    ready: None = None
    error: ResponseError


_RESPONSE_MODELS: dict[ResponseKind, type[_ResponseBase]] = {
    ResponseKind.SPEECH: SpeechResponse,
    ResponseKind.ACTION: ActionResponse,
    ResponseKind.READY: ReadyResponse,
    ResponseKind.ERROR: ErrorResponse,
}


def build_turn_response_schema(
    kind: ResponseKind,
    request_id: str,
    *,
    action_window: ActionWindowView | None = None,
    action_code_descriptions: Mapping[int, str] | None = None,
) -> dict[str, JsonValue]:
    """Build the complete JSON schema for one physically bound turn.

    The response models remain the source of truth for the shape accepted by
    :func:`validate_turn_response`.  This helper only specializes that model
    schema to the active request and its private action window; it never
    changes parsing or fills missing response metadata.
    """

    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
        raise ValueError("request_id must be a non-empty string of at most 256 characters")
    if action_window is not None and kind is not ResponseKind.ACTION:
        raise ValueError("action_window is only valid for action response schemas")

    model = _RESPONSE_MODELS[kind]
    schema = cast(dict[str, JsonValue], deepcopy(model.model_json_schema()))
    properties = cast(dict[str, JsonValue], schema["properties"])
    cast(dict[str, JsonValue], properties["schema_version"])["const"] = 1
    cast(dict[str, JsonValue], properties["kind"])["const"] = kind.value
    cast(dict[str, JsonValue], properties["request_id"])["const"] = request_id

    payload_field = {
        ResponseKind.SPEECH: "speech",
        ResponseKind.ACTION: "actions",
        ResponseKind.READY: "ready",
        ResponseKind.ERROR: "error",
    }[kind]
    required = cast(list[str], schema.get("required", []))
    for field_name in ("schema_version", "request_id", "kind", payload_field):
        if field_name not in required:
            required.append(field_name)
    schema["required"] = cast(JsonValue, required)

    if action_window is not None:
        actions_schema = cast(dict[str, JsonValue], properties["actions"])
        actions_schema["minItems"] = action_window.min_actions
        actions_schema["maxItems"] = action_window.max_actions
        action_definition = cast(
            dict[str, JsonValue], cast(dict[str, JsonValue], schema["$defs"])["Action"]
        )
        action_properties = cast(dict[str, JsonValue], action_definition["properties"])
        action_code_schema = cast(dict[str, JsonValue], action_properties["action_code"])
        action_code_schema["enum"] = list(action_window.allowed_action_codes)
        if action_window.candidate_seats:
            targets_schema = cast(dict[str, JsonValue], action_properties["targets"])
            targets_items = cast(dict[str, JsonValue], targets_schema["items"])
            targets_items["enum"] = list(action_window.candidate_seats)
        if action_code_descriptions:
            descriptions = "; ".join(
                f"{code}: {action_code_descriptions[code]}"
                for code in action_window.allowed_action_codes
                if code in action_code_descriptions
            )
            if descriptions:
                action_code_schema["description"] = descriptions

    return schema


TurnResponse: TypeAlias = Annotated[
    SpeechResponse | ActionResponse | ReadyResponse | ErrorResponse,
    Field(discriminator="kind"),
]
"""Discriminated union for the only responses accepted by the game process."""

TURN_RESPONSE_ADAPTER: TypeAdapter[TurnResponse] = TypeAdapter(TurnResponse)


def validate_turn_response(value: object) -> TurnResponse:
    """Parse an untrusted runtime value using the strict response contract."""

    return TURN_RESPONSE_ADAPTER.validate_python(value)


class RuntimeConfig(_StrictRuntimeModel):
    """Stable per-seat runtime configuration.

    The Pi adapter can extend its private process configuration later; these
    fields are the values needed by every runtime implementation.
    """

    session_id: NonEmptyId
    session_dir: Path | None = None
    provider: NonEmptyId | None = None
    model: NonEmptyId | None = None
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = "medium"
    executable: str | None = None
    compatible_version: str | None = None
    auto_compaction: bool = True
    auto_retry: bool = False


class InitialContext(_StrictRuntimeModel):
    """Trusted context used to bind a newly started player session."""

    game_id: NonEmptyId
    seat: int = Field(ge=1)
    session_epoch: int = Field(ge=0)
    role_id: NonEmptyId | None = None
    # This is copied from the validated assignment plan.  It is used only to
    # select the faction strategy layer; prompt composition never infers a
    # faction from role_id (special wolf-side roles are allowed).
    faction_id: NonEmptyId | None = None
    system_prompt: str | None = Field(default=None, max_length=100_000)


class RuntimeRef(_StrictRuntimeModel):
    """Identity returned by ``PlayerRuntime.start``."""

    session_id: NonEmptyId
    game_id: NonEmptyId
    seat: int = Field(ge=1)
    session_epoch: int = Field(ge=0)


class RuntimeTurnResult(_StrictRuntimeModel):
    """A validated candidate result and request metrics from a runtime."""

    request_id: NonEmptyId
    logical_request_id: NonEmptyId
    attempt_no: int = Field(ge=1)
    response: TurnResponse
    elapsed_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def response_must_match_request(self) -> RuntimeTurnResult:
        if self.response.request_id != self.request_id:
            raise ValueError("response.request_id must match result.request_id")
        return self


@runtime_checkable
class PlayerRuntime(Protocol):
    """Async boundary implemented by PiRuntime and ScriptedRuntime."""

    async def start(self, config: RuntimeConfig, context: InitialContext) -> RuntimeRef: ...

    async def run_turn(self, request: TurnRequest) -> RuntimeTurnResult: ...

    async def steer(self, request_id: str, message: str) -> None: ...

    async def abort(self, request_id: str) -> None: ...

    async def close(self, reason: str) -> None: ...

    def get_session_ref(self) -> RuntimeRef: ...


class RuntimeLifecycleError(RuntimeError):
    """The runtime is not in a state that can accept the requested operation."""


class RuntimeProtocolError(RuntimeError):
    """An untrusted runtime response violated the shared protocol."""


class RuntimeResponseValidationError(RuntimeProtocolError):
    """The response could not be parsed as a strict ``TurnResponse``."""


class RuntimeRequestMismatchError(RuntimeProtocolError):
    """A response was associated with a request other than the active one."""


__all__ = [
    "Action",
    "ActionResponse",
    "ActionWindowView",
    "build_turn_response_schema",
    "Deadline",
    "ErrorResponse",
    "InitialContext",
    "Observation",
    "ObservationEvent",
    "PlayerRuntime",
    "Ready",
    "ReadyResponse",
    "ResponseError",
    "ResponseKind",
    "RuntimeConfig",
    "RuntimeLifecycleError",
    "RuntimeProtocolError",
    "RuntimeRef",
    "RuntimeRequestMismatchError",
    "RuntimeResponseValidationError",
    "RuntimeTurnResult",
    "Speech",
    "SpeechResponse",
    "TurnRequest",
    "TurnResponse",
    "TURN_RESPONSE_ADAPTER",
    "validate_turn_response",
]
