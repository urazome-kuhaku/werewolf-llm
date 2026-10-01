"""Board agnostic moderator resolution records.

An :class:`~werewolf.game.actions.ActionRequest` is only a player's intent.
This module contains the typed, immutable record a moderator submits after
reviewing that intent.  It deliberately describes effects without deciding
what a particular board means by them; the manager validates the references
and applies them atomically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum, unique
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from .actions import Action

SeatNo = Annotated[int, Field(ge=1, le=64, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
NonEmptyId = Annotated[str, Field(min_length=1, max_length=128, strict=True)]


class _ResolutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, strict=True)


@unique
class ResolutionStatus(StrEnum):
    """The moderator's decision for one complete action bundle."""

    CONFIRMED = "CONFIRMED"
    OVERRIDDEN = "OVERRIDDEN"
    CANCELLED = "CANCELLED"


@unique
class ActionDisposition(StrEnum):
    """The outcome of one requested action inside a bundle."""

    CONFIRMED = "CONFIRMED"
    OVERRIDDEN = "OVERRIDDEN"
    CANCELLED = "CANCELLED"


EffectType = Literal[
    "SET_ALIVE",
    "SET_DEATH_CAUSE",
    "SET_CAN_VOTE",
    "SET_VOTE_WEIGHT",
    "ADJUST_RESOURCE",
]


class ResolutionEffect(_ResolutionModel):
    """One explicit, board supplied player state change.

    ``value`` is intentionally JSON shaped.  The manager checks its concrete
    type against ``effect_type`` and checks that ``target_seat`` is a target
    of the resolved action before changing state.
    """

    effect_id: NonEmptyId
    action_index: Annotated[int, Field(ge=0, le=31, strict=True)]
    effect_type: EffectType
    target_seat: SeatNo
    value: JsonValue | None = None
    resource_id: NonEmptyId | None = None
    reason: Annotated[str, Field(max_length=2_000, strict=True)] | None = None


class ActionResolutionEntry(_ResolutionModel):
    """The moderator's ruling for one action in the original request."""

    action_index: Annotated[int, Field(ge=0, le=31, strict=True)]
    requested_action: Action
    disposition: ActionDisposition = ActionDisposition.CONFIRMED
    resolved_action: Action | None = None
    resource_cost: NonNegativeInt = 0
    effects: tuple[ResolutionEffect, ...] = ()
    reason: Annotated[str, Field(max_length=2_000, strict=True)] | None = None

    @field_validator("effects", mode="before")
    @classmethod
    def accept_effect_array(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("disposition", mode="before")
    @classmethod
    def accept_disposition_value(cls, value: object) -> object:
        if isinstance(value, str):
            return ActionDisposition(value)
        return value


class ActionResolution(_ResolutionModel):
    """A one-shot, auditable decision for a validated action request.

    ``base_revision`` is the observation revision the moderator reviewed.
    The game manager requires it to still be current while holding its commit
    lock, so a stale moderator decision cannot consume a resource or apply a
    player update.
    """

    schema_version: Literal[1] = 1
    resolution_id: NonEmptyId
    bundle_id: NonEmptyId
    game_id: NonEmptyId
    window_id: NonEmptyId
    request_id: NonEmptyId
    session_epoch: NonNegativeInt
    base_revision: NonNegativeInt
    status: ResolutionStatus
    actions: tuple[ActionResolutionEntry, ...] = Field(min_length=1, max_length=32)
    moderator_id: NonEmptyId
    reason: Annotated[str, Field(max_length=4_000, strict=True)] | None = None
    created_at: datetime

    @field_validator("actions", mode="before")
    @classmethod
    def accept_action_array(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("status", mode="before")
    @classmethod
    def accept_status_value(cls, value: object) -> object:
        if isinstance(value, str):
            return ResolutionStatus(value)
        return value

    @field_validator("created_at", mode="before")
    @classmethod
    def created_at_is_aware(cls, value: object) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("created_at must be a valid RFC 3339 datetime") from exc
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def unique_action_indexes(self) -> ActionResolution:
        indexes = tuple(entry.action_index for entry in self.actions)
        if len(indexes) != len(set(indexes)):
            raise ValueError("resolution action indexes must be unique")
        return self

    @property
    def entries(self) -> tuple[ActionResolutionEntry, ...]:
        """Descriptive alias used by moderator adapters."""

        return self.actions


# Short aliases keep integrations readable without creating a second wire
# schema.  They are also useful for callers that call a ruling an outcome.
ResolutionEntry = ActionResolutionEntry
ResolvedAction = ActionResolutionEntry


__all__ = [
    "ActionDisposition",
    "ActionResolution",
    "ActionResolutionEntry",
    "ResolutionEffect",
    "ResolutionEntry",
    "ResolutionStatus",
    "ResolvedAction",
]
