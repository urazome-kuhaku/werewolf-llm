"""Board-agnostic action registry and pure action request validation.

The ruleset snapshot owns which actions are available in a particular window.
This module only enforces the stable action protocol and constraints supplied
by an authoritative validation context. Validation never mutates game state,
consumes a resource, or resolves a target.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from werewolf.domain.enums import GamePhase

ActionCode = Annotated[int, Field(gt=0, strict=True)]
SeatNo = Annotated[int, Field(ge=1, le=64, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
NonEmptyId = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
Fingerprint = Annotated[str, Field(pattern=r"[0-9a-f]{64}", strict=True)]
TargetPolicy = Literal[
    "alive_non_authorized_wolf",
    "other_alive",
    "board_eligible",
    "current_kill_not_self",
    "self_and_other_alive",
    "candidate",
    "none",
]
RuleHook = Literal["DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"]


def _as_tuple(value: object) -> object:
    """Accept JSON/YAML arrays while storing immutable tuples."""

    if isinstance(value, (list, tuple)):
        return tuple(value)
    return value


class _ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ActionDefinition(_ActionModel):
    """One stable action type from the global action registry."""

    action_code: ActionCode
    action_name: Annotated[str, Field(min_length=1, max_length=64, strict=True)]
    target_policy: TargetPolicy
    target_count: Annotated[int, Field(ge=0, le=16, strict=True)]
    resource_id: NonEmptyId | None = None

    @field_validator("action_name")
    @classmethod
    def action_name_is_upper_snake_case(cls, value: str) -> str:
        if value != value.upper() or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for char in value
        ):
            raise ValueError("action_name must be uppercase snake case")
        return value

    @model_validator(mode="after")
    def target_policy_matches_count(self) -> ActionDefinition:
        if (self.target_policy == "none") != (self.target_count == 0):
            raise ValueError("target_policy=none requires target_count=0 and vice versa")
        if self.target_policy == "self_and_other_alive" and self.target_count != 2:
            raise ValueError("target_policy=self_and_other_alive requires target_count=2")
        return self


class ActionRegistry(_ActionModel):
    """Immutable registry loaded from ``config/actions.yaml``."""

    schema_version: Literal[1] = 1
    actions: tuple[ActionDefinition, ...] = Field(min_length=1)

    @field_validator("actions", mode="before")
    @classmethod
    def accept_yaml_array(cls, value: object) -> object:
        return _as_tuple(value)

    @model_validator(mode="after")
    def unique_codes_and_names(self) -> ActionRegistry:
        codes = [action.action_code for action in self.actions]
        names = [action.action_name for action in self.actions]
        if len(codes) != len(set(codes)):
            raise ValueError("action codes must be unique")
        if len(names) != len(set(names)):
            raise ValueError("action names must be unique")
        return self

    def get(self, action_code: int) -> ActionDefinition:
        for action in self.actions:
            if action.action_code == action_code:
                return action
        raise KeyError(action_code)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_ACTION_REGISTRY_PATH = _PROJECT_ROOT / "config" / "actions.yaml"


def load_action_registry(path: str | Path | None = None) -> ActionRegistry:
    """Load and strictly validate an action registry from YAML.

    The checked-in registry is resolved relative to this module, so callers do
    not need to run from the repository root. A supplied path remains explicit.
    """

    source = _DEFAULT_ACTION_REGISTRY_PATH if path is None else Path(path)
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("action registry must be a YAML object")
    return ActionRegistry.model_validate(raw)


class Action(_ActionModel):
    """One proposed action, wire-compatible with runtime ``Action``."""

    action_code: ActionCode
    targets: tuple[SeatNo, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    reason_public: Annotated[str, Field(max_length=2_000, strict=True)] | None = None

    @field_validator("targets", mode="before")
    @classmethod
    def accept_runtime_array(cls, value: object) -> object:
        return _as_tuple(value)

    @field_validator("targets")
    @classmethod
    def target_seats_are_unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(value) != len(set(value)):
            raise ValueError("action targets must not contain duplicates")
        return value


class ActionWindow(_ActionModel):
    """Authoritative, already constructed action window.

    The window is generic. Its allowed roles, codes, target sets, and
    dependencies are supplied by a frozen ruleset/coordinator.
    """

    schema_version: Literal[1] = 1
    window_id: NonEmptyId
    game_id: NonEmptyId
    session_epoch: NonNegativeInt
    phase: GamePhase
    allowed_seats: tuple[SeatNo, ...] = ()
    allowed_role_ids: tuple[NonEmptyId, ...] = ()
    allowed_action_codes: tuple[ActionCode, ...] = ()
    forbidden_action_combinations: tuple[tuple[ActionCode, ...], ...] = ()
    min_actions: Annotated[int, Field(ge=0, le=32, strict=True)] = 1
    max_actions: Annotated[int, Field(ge=0, le=32, strict=True)] = 1
    allow_duplicate_action_codes: bool = False
    allow_pass: bool = False
    opened_at: datetime | None = None
    # Collection is a separate boundary from final settlement.  A window
    # marked complete rejects further submissions, while remaining open
    # until every window in its settlement group is ready to commit.
    collection_complete_at: datetime | None = None
    closed_at: datetime | None = None
    # Windows that observe the same provisional action facts share one
    # atomic settlement group.  This is durable coordinator metadata, not a
    # player-facing context field.
    settlement_group_id: NonEmptyId | None = None
    # The package-level window identity remains stable across rounds even
    # when the physical ActionWindow ID is unique for each activation.
    logical_window_id: NonEmptyId | None = None
    # The frozen scheduler supplies the next logical night window.  The
    # manager persists it as a workflow return reference if triggers interrupt
    # between independently committed settlement groups.
    next_window_id: NonEmptyId | None = None
    hook_id: RuleHook | None = None
    dependency_receipt_ids: tuple[NonEmptyId, ...] = ()
    dependencies_satisfied: bool = True
    visible_context: dict[str, JsonValue] = Field(default_factory=dict)
    allow_concurrent: bool = False
    collection_only: bool = False
    max_submissions_per_seat: Annotated[int, Field(ge=1, le=64, strict=True)] = 1
    submitted_request_ids: tuple[NonEmptyId, ...] = ()
    submitted_request_fingerprints: dict[NonEmptyId, Fingerprint] = Field(default_factory=dict)

    @field_validator(
        "allowed_seats",
        "allowed_role_ids",
        "allowed_action_codes",
        "forbidden_action_combinations",
        "dependency_receipt_ids",
        "submitted_request_ids",
        mode="before",
    )
    @classmethod
    def accept_wire_arrays(cls, value: object) -> object:
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
            return tuple(tuple(item) for item in value)
        return _as_tuple(value)

    @field_validator("opened_at", "collection_complete_at", "closed_at", mode="before")
    @classmethod
    def timestamps_are_aware(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("window timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_window_shape(self) -> ActionWindow:
        if len(set(self.allowed_seats)) != len(self.allowed_seats):
            raise ValueError("allowed_seats must not contain duplicates")
        if len(set(self.allowed_action_codes)) != len(self.allowed_action_codes):
            raise ValueError("allowed_action_codes must not contain duplicates")
        for combination in self.forbidden_action_combinations:
            if len(combination) < 2:
                raise ValueError("forbidden action combinations require at least two codes")
            if len(set(combination)) != len(combination):
                raise ValueError("forbidden action combinations must not repeat codes")
        if self.min_actions > self.max_actions:
            raise ValueError("min_actions must not exceed max_actions")
        if self.collection_only:
            if (
                self.allowed_seats
                or self.allowed_action_codes
                or self.min_actions != 0
                or self.max_actions != 0
                or self.allow_pass
            ):
                raise ValueError(
                    "collection-only windows require empty seats/codes, zero actions, and no pass"
                )
        elif not self.allowed_seats or not self.allowed_action_codes:
            raise ValueError("interactive windows require allowed seats and action codes")
        if self.opened_at and self.closed_at and self.closed_at < self.opened_at:
            raise ValueError("closed_at must not precede opened_at")
        if (
            self.opened_at
            and self.collection_complete_at
            and self.collection_complete_at < self.opened_at
        ):
            raise ValueError("collection_complete_at must not precede opened_at")
        if (
            self.collection_complete_at
            and self.closed_at
            and self.closed_at < self.collection_complete_at
        ):
            raise ValueError("closed_at must not precede collection_complete_at")
        return self

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def accepts_submissions(self) -> bool:
        """Whether the collection side of this window still accepts input."""

        return self.closed_at is None and self.collection_complete_at is None


class ActionRequest(_ActionModel):
    """A player's atomic intent bundle, before authoritative validation."""

    schema_version: Literal[1] = 1
    request_id: NonEmptyId
    game_id: NonEmptyId
    window_id: NonEmptyId
    seat: SeatNo
    session_epoch: NonNegativeInt
    actions: tuple[Action, ...] = Field(min_length=1, max_length=32)
    phase: GamePhase | None = None
    idempotency_key: NonEmptyId | None = None

    @field_validator("actions", mode="before")
    @classmethod
    def accept_runtime_actions(cls, value: object) -> object:
        return _as_tuple(value)


class ActionValidationContext(_ActionModel):
    """Authoritative facts injected by the game coordinator.

    The context is not inferred from a player request. A coordinator supplies
    role authorization, live seats, target sets, current kill target,
    resources, and dependency state from its snapshot.
    """

    game_id: NonEmptyId
    session_epoch: NonNegativeInt
    active_request_id: NonEmptyId
    player_alive: bool = True
    player_qualified: bool = True
    role_id: NonEmptyId | None = None
    authorized_action_codes: tuple[ActionCode, ...] = ()
    skill_resources: dict[NonEmptyId, NonNegativeInt] = Field(default_factory=dict)
    alive_seats: tuple[SeatNo, ...] = ()
    eligible_targets_by_action: dict[ActionCode, tuple[SeatNo, ...]] = Field(default_factory=dict)
    current_kill_target_seat: SeatNo | None = None
    submitted_request_ids: tuple[NonEmptyId, ...] = ()
    submitted_request_fingerprints: dict[NonEmptyId, Fingerprint] = Field(default_factory=dict)
    submitted_counts_by_seat: dict[SeatNo, NonNegativeInt] = Field(default_factory=dict)
    dependency_receipt_ids: tuple[NonEmptyId, ...] = ()
    dependencies_satisfied: bool = True

    @field_validator(
        "authorized_action_codes",
        "alive_seats",
        "submitted_request_ids",
        "dependency_receipt_ids",
        mode="before",
    )
    @classmethod
    def accept_context_arrays(cls, value: object) -> object:
        return _as_tuple(value)

    @field_validator("eligible_targets_by_action", mode="before")
    @classmethod
    def accept_target_arrays(cls, value: object) -> object:
        if isinstance(value, dict):
            return {key: _as_tuple(item) for key, item in value.items()}
        return value

    @model_validator(mode="after")
    def unique_context_values(self) -> ActionValidationContext:
        if len(set(self.authorized_action_codes)) != len(self.authorized_action_codes):
            raise ValueError("authorized_action_codes must not contain duplicates")
        if len(set(self.submitted_request_ids)) != len(self.submitted_request_ids):
            raise ValueError("submitted_request_ids must not contain duplicates")
        return self


class ValidatedActionRequest(ActionRequest):
    """An accepted intent; no state mutation or resolution is implied."""

    validated_at: datetime
    request_fingerprint: Fingerprint
    idempotent_replay: bool = False

    @field_validator("validated_at", mode="before")
    @classmethod
    def validated_at_is_aware(cls, value: object) -> object:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("validated_at must include a timezone")
        return value.astimezone(UTC)


class ActionValidationError(ValueError):
    """Stable error carrying the first failed validation stage."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _fingerprint(request: ActionRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _first_target(action: Action, definition: ActionDefinition) -> int | None:
    if definition.target_count == 0:
        return None
    return action.targets[0] if action.targets else None


def validate_action_request(
    request: ActionRequest,
    window: ActionWindow,
    context: ActionValidationContext,
    *,
    registry: ActionRegistry,
    now: datetime | None = None,
) -> ValidatedActionRequest:
    """Validate one complete action bundle without mutating authoritative state.

    ``registry`` is deliberately required. The coordinator must inject the
    strictly loaded registry belonging to the running process; validation does
    not silently choose a second, potentially stale action definition set.
    """

    if request.game_id != context.game_id or request.game_id != window.game_id:
        raise ActionValidationError(
            "GAME_MISMATCH", "request, window, and context game_id must match"
        )
    if (
        request.session_epoch != context.session_epoch
        or request.session_epoch != window.session_epoch
    ):
        raise ActionValidationError(
            "SESSION_MISMATCH", "request, window, and context session_epoch must match"
        )
    if request.window_id != window.window_id:
        raise ActionValidationError(
            "WINDOW_MISMATCH", "request.window_id does not match the open window"
        )
    if request.phase is not None and request.phase != window.phase:
        raise ActionValidationError(
            "PHASE_MISMATCH", "request.phase does not match the action window"
        )
    if request.request_id != context.active_request_id:
        raise ActionValidationError(
            "REQUEST_MISMATCH", "request_id is not the active runtime request"
        )
    request_fingerprint = _fingerprint(request)
    if not window.is_open:
        raise ActionValidationError("WINDOW_CLOSED", "action window is closed")
    if not context.dependencies_satisfied or not window.dependencies_satisfied:
        raise ActionValidationError(
            "DEPENDENCY_UNSATISFIED", "window dependencies are not satisfied"
        )
    if not set(window.dependency_receipt_ids).issubset(context.dependency_receipt_ids):
        raise ActionValidationError(
            "DEPENDENCY_CONTEXT_MISSING", "required dependency receipts are not confirmed"
        )
    if request.seat not in window.allowed_seats:
        raise ActionValidationError("SEAT_NOT_ALLOWED", "seat is not allowed in this window")
    if not context.player_alive:
        raise ActionValidationError("PLAYER_DEAD", "dead players cannot submit actions")
    if not context.player_qualified:
        raise ActionValidationError(
            "PLAYER_NOT_QUALIFIED", "player is not qualified for this window"
        )
    if window.allowed_role_ids and context.role_id not in window.allowed_role_ids:
        raise ActionValidationError("ROLE_NOT_ALLOWED", "role is not allowed in this window")

    submitted_ids = set(window.submitted_request_ids) | set(context.submitted_request_ids)
    submitted_ids.update(window.submitted_request_fingerprints)
    submitted_ids.update(context.submitted_request_fingerprints)
    if request.request_id in submitted_ids:
        known_fingerprint = context.submitted_request_fingerprints.get(
            request.request_id,
            window.submitted_request_fingerprints.get(request.request_id),
        )
        if known_fingerprint is None:
            raise ActionValidationError(
                "DUPLICATE_REQUEST", "request_id has already been submitted"
            )
        if known_fingerprint != request_fingerprint:
            raise ActionValidationError(
                "IDEMPOTENCY_CONFLICT",
                "request_id was already submitted with a different request payload",
            )
        validation_time = datetime.now(UTC) if now is None else now
        if validation_time.tzinfo is None or validation_time.utcoffset() is None:
            raise ActionValidationError("TIMESTAMP", "validation time must include a timezone")
        return ValidatedActionRequest(
            **request.model_dump(mode="python"),
            validated_at=validation_time.astimezone(UTC),
            request_fingerprint=request_fingerprint,
            idempotent_replay=True,
        )

    if context.submitted_counts_by_seat.get(request.seat, 0) >= window.max_submissions_per_seat:
        raise ActionValidationError(
            "SUBMISSION_LIMIT", "seat has reached the maximum submissions for this window"
        )

    count = len(request.actions)
    if count < window.min_actions or count > window.max_actions:
        raise ActionValidationError("ACTION_COUNT", "bundle size is outside the window bounds")
    codes = [action.action_code for action in request.actions]
    if not window.allow_duplicate_action_codes and len(codes) != len(set(codes)):
        raise ActionValidationError(
            "DUPLICATE_ACTION", "action codes may not repeat in this window"
        )
    if any(code not in window.allowed_action_codes for code in codes):
        raise ActionValidationError(
            "ACTION_NOT_ALLOWED", "an action code is not allowed by this window"
        )
    # An empty authorization set is an explicit denial.  Treating it as
    # "no restriction" made a forged or stale context equivalent to a full
    # role grant, which is especially dangerous for per-seat night windows.
    if any(code not in context.authorized_action_codes for code in codes):
        raise ActionValidationError(
            "ACTION_UNAUTHORIZED", "role authorization does not permit an action"
        )
    pass_codes = {
        definition.action_code
        for definition in registry.actions
        if definition.action_name == "PASS"
    }
    if pass_codes.intersection(codes) and (len(codes) != 1 or not window.allow_pass):
        raise ActionValidationError(
            "PASS_MIXED", "PASS must be the only action and must be explicitly allowed"
        )
    for combination in window.forbidden_action_combinations:
        if set(combination).issubset(codes):
            formatted = ", ".join(str(code) for code in combination)
            raise ActionValidationError(
                "ACTION_COMBINATION_FORBIDDEN",
                f"action combination [{formatted}] is forbidden by the frozen window policy",
            )

    requested_resources: dict[str, int] = {}
    for action in request.actions:
        try:
            definition = registry.get(action.action_code)
        except KeyError as exc:
            raise ActionValidationError(
                "UNKNOWN_ACTION", f"unknown action code {action.action_code}"
            ) from exc
        if len(action.targets) != definition.target_count:
            raise ActionValidationError(
                "TARGET_COUNT",
                f"{definition.action_name} requires {definition.target_count} target(s)",
            )
        if (
            definition.resource_id is not None
            and context.skill_resources.get(definition.resource_id, 0) < 1
        ):
            raise ActionValidationError(
                "RESOURCE_UNAVAILABLE", f"resource {definition.resource_id} is unavailable"
            )
        if definition.resource_id is not None:
            requested_resources[definition.resource_id] = (
                requested_resources.get(definition.resource_id, 0) + 1
            )
        allowed_targets = context.eligible_targets_by_action.get(action.action_code)
        if definition.target_count and allowed_targets is None:
            raise ActionValidationError(
                "TARGET_CONTEXT_MISSING", "authoritative target set is required"
            )
        if allowed_targets is not None and any(
            target not in allowed_targets for target in action.targets
        ):
            raise ActionValidationError(
                "TARGET_NOT_ALLOWED", f"target is not eligible for {definition.action_name}"
            )
        if context.alive_seats and any(
            target not in context.alive_seats for target in action.targets
        ):
            raise ActionValidationError(
                "TARGET_NOT_ALIVE", f"target is not alive for {definition.action_name}"
            )
        if definition.target_policy == "other_alive" and any(
            target == request.seat for target in action.targets
        ):
            raise ActionValidationError(
                "SELF_TARGET", f"{definition.action_name} cannot target the actor"
            )
        if definition.target_policy == "self_and_other_alive":
            if action.targets[0] != request.seat:
                raise ActionValidationError(
                    "ACTOR_TARGET_REQUIRED",
                    f"{definition.action_name} must place the acting seat first",
                )
            if action.targets[1] == request.seat:
                raise ActionValidationError(
                    "OTHER_TARGET_REQUIRED",
                    f"{definition.action_name} requires a different second target",
                )
        if definition.target_policy == "current_kill_not_self":
            target = _first_target(action, definition)
            if target != context.current_kill_target_seat or target == request.seat:
                raise ActionValidationError(
                    "HEAL_TARGET", "WITCH_HEAL must target the current kill and cannot target self"
                )
        if (
            definition.target_policy == "alive_non_authorized_wolf"
            and request.seat in action.targets
        ):
            raise ActionValidationError(
                "WOLF_SELF_TARGET", "WOLF_KILL cannot target the acting seat"
            )

    for resource_id, requested in requested_resources.items():
        if requested > context.skill_resources.get(resource_id, 0):
            raise ActionValidationError(
                "RESOURCE_BUNDLE_EXCEEDED", f"bundle exceeds resource {resource_id}"
            )

    validation_time = datetime.now(UTC) if now is None else now
    if validation_time.tzinfo is None or validation_time.utcoffset() is None:
        raise ActionValidationError("TIMESTAMP", "validation time must include a timezone")
    return ValidatedActionRequest(
        **request.model_dump(mode="python"),
        validated_at=validation_time.astimezone(UTC),
        request_fingerprint=_fingerprint(request),
    )


__all__ = [
    "Action",
    "ActionCode",
    "ActionDefinition",
    "ActionRegistry",
    "ActionRequest",
    "ActionValidationContext",
    "ActionValidationError",
    "ActionWindow",
    "ValidatedActionRequest",
    "load_action_registry",
    "validate_action_request",
]
