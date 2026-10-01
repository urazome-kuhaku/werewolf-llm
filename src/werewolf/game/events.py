"""Immutable events and delivery cursors for the game message boundary.

The game process creates events after it has applied the relevant rule.  A
runtime can only read events that are already addressed to its seat.  The
payload types in this module are intentionally split by visibility channel so
that a public event cannot accidentally carry a role, an unreleased ballot,
or another private record and rely on a renderer to remove it later.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, NoReturn, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from werewolf.domain.enums import Channel, GamePhase


class EventType(StrEnum):
    """Stable event kinds used by the initial game protocol.

    The protocol can add a new kind without changing the delivery contract.
    Unknown values are accepted when they are valid lower-case identifiers;
    visibility is still guarded by the typed payload below.
    """

    ANNOUNCEMENT = "announcement"
    SPEECH = "speech"
    VOTE_RESULT = "vote_result"
    TEAM_SPEECH = "team_speech"
    TEAM_NOTICE = "team_notice"
    PRIVATE_NOTICE = "private_notice"
    ROLE_ASSIGNMENT = "role_assignment"
    ACTION_RECEIPT = "action_receipt"
    SEER_RESULT = "seer_result"
    WITCH_TARGET = "witch_target"
    GM_AUDIT = "gm_audit"

    @classmethod
    def _missing_(cls, value: object) -> EventType | None:
        """Represent future event kinds without weakening identifier checks."""

        if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
            return None
        member = str.__new__(cls, value)
        member._name_ = value.upper().replace("-", "_")
        member._value_ = value
        cls._value2member_map_[value] = member
        return member


_StrictPayload = ConfigDict(
    extra="forbid",
    frozen=True,
    hide_input_in_errors=True,
    strict=True,
)


class _FrozenDict(dict[object, object]):
    """JSON-shaped mapping that cannot be changed after event creation."""

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("event payload mappings are immutable")

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


def _freeze_payload(value: object) -> object:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_payload(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_payload(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_payload(item) for item in value)
    return value


class _Payload(BaseModel):
    model_config = _StrictPayload

    def model_post_init(self, __context: object) -> None:
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            frozen = _freeze_payload(value)
            if frozen is not value:
                object.__setattr__(self, field_name, frozen)


_Content = Annotated[str, Field(min_length=1, max_length=8_000, strict=True)]
_Seat = Annotated[int, Field(ge=1, le=64, strict=True)]
_Id = Annotated[str, Field(min_length=1, max_length=128, strict=True)]


class PublicAnnouncementPayload(_Payload):
    """A public announcement that contains no player-private facts."""

    kind: Literal["announcement"] = "announcement"
    content: _Content = Field(validation_alias=AliasChoices("content", "text"))


class PublicSpeechPayload(_Payload):
    """A confirmed public speech record."""

    kind: Literal["speech"] = "speech"
    speaker_seat: _Seat = Field(validation_alias=AliasChoices("speaker_seat", "speaker"))
    content: _Content = Field(validation_alias=AliasChoices("content", "text"))


class PublicVoteResultPayload(_Payload):
    """A completed, authorized vote result.

    The individual ballots are deliberately not represented here.  A future
    board can introduce another typed public result once its publication rule
    has been reviewed.
    """

    kind: Literal["vote_result"] = "vote_result"
    eliminated_seat: _Seat | None = None
    tally: dict[_Seat, float] = Field(default_factory=dict)

    @field_validator("tally")
    @classmethod
    def validate_tally(cls, value: dict[int, float]) -> dict[int, float]:
        if any(v < 0 or not math.isfinite(v) for v in value.values()):
            raise ValueError("vote tally counts must be finite and non-negative")
        return value


class TeamSpeechPayload(_Payload):
    """A wolf/team message addressed only to explicitly authorized seats."""

    kind: Literal["team_speech"] = "team_speech"
    speaker_seat: _Seat
    content: _Content = Field(validation_alias=AliasChoices("content", "text"))


class TeamNoticePayload(_Payload):
    """A typed team notice with no public projection."""

    kind: Literal["team_notice"] = "team_notice"
    content: _Content = Field(validation_alias=AliasChoices("content", "text"))


class PrivateNoticePayload(_Payload):
    """A notice visible only to the addressed seat."""

    kind: Literal["private_notice"] = "private_notice"
    content: _Content = Field(validation_alias=AliasChoices("content", "text"))


class PrivateRolePayload(_Payload):
    """The minimum role briefing sent to the role owner."""

    kind: Literal["role_assignment"] = "role_assignment"
    role_id: _Id
    faction_id: _Id


class PrivateActionReceiptPayload(_Payload):
    """A private acknowledgement of one submitted action."""

    kind: Literal["action_receipt"] = "action_receipt"
    action_code: int = Field(ge=1, strict=True)
    accepted: bool
    message: Annotated[str, Field(default="", max_length=2_000, strict=True)]


class PrivateSeerResultPayload(_Payload):
    """A seer result addressed to the seer only."""

    kind: Literal["seer_result"] = "seer_result"
    target_seat: _Seat
    faction_id: _Id


class PrivateWitchTargetPayload(_Payload):
    """The night attack target shown to the witch."""

    kind: Literal["witch_target"] = "witch_target"
    target_seat: _Seat


class GmAuditPayload(_Payload):
    """A host-only audit record; it is never routable to a player seat."""

    kind: Literal["gm_audit"] = "gm_audit"
    code: _Id
    details: dict[str, JsonValue] = Field(default_factory=dict)


PublicPayload: TypeAlias = Annotated[
    PublicAnnouncementPayload | PublicSpeechPayload | PublicVoteResultPayload,
    Field(discriminator="kind"),
]
TeamPayload: TypeAlias = Annotated[
    TeamSpeechPayload | TeamNoticePayload,
    Field(discriminator="kind"),
]
PrivatePayload: TypeAlias = Annotated[
    PrivateNoticePayload
    | PrivateRolePayload
    | PrivateActionReceiptPayload
    | PrivateSeerResultPayload
    | PrivateWitchTargetPayload,
    Field(discriminator="kind"),
]
GMOnlyPayload: TypeAlias = Annotated[GmAuditPayload, Field(discriminator="kind")]
EventPayload: TypeAlias = Annotated[
    PublicAnnouncementPayload
    | PublicSpeechPayload
    | PublicVoteResultPayload
    | TeamSpeechPayload
    | TeamNoticePayload
    | PrivateNoticePayload
    | PrivateRolePayload
    | PrivateActionReceiptPayload
    | PrivateSeerResultPayload
    | PrivateWitchTargetPayload
    | GmAuditPayload,
    Field(discriminator="kind"),
]

_PUBLIC_PAYLOAD_TYPES = (
    PublicAnnouncementPayload,
    PublicSpeechPayload,
    PublicVoteResultPayload,
)
_TEAM_PAYLOAD_TYPES = (TeamSpeechPayload, TeamNoticePayload)
_PRIVATE_PAYLOAD_TYPES = (
    PrivateNoticePayload,
    PrivateRolePayload,
    PrivateActionReceiptPayload,
    PrivateSeerResultPayload,
    PrivateWitchTargetPayload,
)


def _utc_datetime(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at must be a valid RFC 3339 datetime") from exc
    if not isinstance(value, datetime):
        raise TypeError("created_at must be a datetime or RFC 3339 string")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    return value.astimezone(UTC)


def _seats(value: tuple[int, ...]) -> tuple[int, ...]:
    if len(set(value)) != len(value):
        raise ValueError("audience must not contain duplicate seats")
    if tuple(sorted(value)) != value:
        raise ValueError("audience must be in ascending seat order")
    return value


class GameEvent(BaseModel):
    """An immutable, already-authorized game event."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    event_id: int = Field(ge=0, strict=True)
    game_id: Annotated[str, Field(min_length=1, max_length=64, strict=True)]
    state_revision: int = Field(ge=0, strict=True)
    round_no: int = Field(ge=0, strict=True)
    phase: GamePhase
    created_at: datetime
    event_type: EventType
    channel: Channel
    actor_seat: _Seat | None = None
    audience: tuple[_Seat, ...]
    payload: EventPayload
    public_projection: PublicPayload | None = None
    correlation_id: Annotated[str, Field(min_length=1, max_length=128, strict=True)] | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: object) -> datetime:
        return _utc_datetime(value)

    @field_validator("game_id")
    @classmethod
    def validate_game_id(cls, value: str) -> str:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value, re.ASCII) is None:
            raise ValueError(
                "game_id must contain only lowercase ASCII letters, digits, '-' or '_'"
            )
        return value

    @field_validator("audience")
    @classmethod
    def validate_audience(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _seats(value)

    @model_validator(mode="after")
    def validate_visibility_payload(self) -> GameEvent:
        if self.channel is Channel.GM_ONLY:
            if self.audience:
                raise ValueError("GM_ONLY events must have an empty audience")
            if not isinstance(self.payload, GmAuditPayload):
                raise ValueError("GM_ONLY events require a gm_audit payload")
            if self.public_projection is not None:
                raise ValueError("GM_ONLY events cannot contain a public projection")
        elif self.channel is Channel.PUBLIC:
            if not self.audience:
                raise ValueError("PUBLIC events require at least one audience seat")
            if not isinstance(self.payload, _PUBLIC_PAYLOAD_TYPES):
                raise ValueError("PUBLIC events require a public-safe payload")
            if self.public_projection is not None and not isinstance(
                self.public_projection, _PUBLIC_PAYLOAD_TYPES
            ):
                raise ValueError("public_projection must be public-safe")
        elif self.channel is Channel.TEAM:
            if not self.audience:
                raise ValueError("TEAM events require explicitly authorized seats")
            if not isinstance(self.payload, _TEAM_PAYLOAD_TYPES):
                raise ValueError("TEAM events require a team payload")
            if self.public_projection is not None:
                raise ValueError("TEAM events cannot contain a public projection")
        elif self.channel is Channel.PRIVATE:
            if len(self.audience) != 1:
                raise ValueError("PRIVATE events require exactly one authorized seat")
            if not isinstance(self.payload, _PRIVATE_PAYLOAD_TYPES):
                raise ValueError("PRIVATE events require a private payload")
            if self.public_projection is not None:
                raise ValueError("PRIVATE events cannot contain a public projection")
        return self

    @classmethod
    def public(
        cls,
        *,
        event_id: int,
        game_id: str,
        state_revision: int,
        round_no: int,
        phase: GamePhase,
        created_at: datetime,
        event_type: EventType,
        eligible_seats: tuple[int, ...],
        payload: PublicPayload,
        actor_seat: int | None = None,
        public_projection: PublicPayload | None = None,
        correlation_id: str | None = None,
    ) -> GameEvent:
        return cls(
            event_id=event_id,
            game_id=game_id,
            state_revision=state_revision,
            round_no=round_no,
            phase=phase,
            created_at=created_at,
            event_type=event_type,
            channel=Channel.PUBLIC,
            actor_seat=actor_seat,
            audience=eligible_seats,
            payload=payload,
            public_projection=public_projection,
            correlation_id=correlation_id,
        )

    @classmethod
    def team(
        cls,
        *,
        event_id: int,
        game_id: str,
        state_revision: int,
        round_no: int,
        phase: GamePhase,
        created_at: datetime,
        event_type: EventType,
        authorized_seats: tuple[int, ...],
        payload: TeamPayload,
        actor_seat: int | None = None,
        correlation_id: str | None = None,
    ) -> GameEvent:
        return cls(
            event_id=event_id,
            game_id=game_id,
            state_revision=state_revision,
            round_no=round_no,
            phase=phase,
            created_at=created_at,
            event_type=event_type,
            channel=Channel.TEAM,
            actor_seat=actor_seat,
            audience=authorized_seats,
            payload=payload,
            correlation_id=correlation_id,
        )

    @classmethod
    def private(
        cls,
        *,
        event_id: int,
        game_id: str,
        state_revision: int,
        round_no: int,
        phase: GamePhase,
        created_at: datetime,
        event_type: EventType,
        seat: int,
        payload: PrivatePayload,
        actor_seat: int | None = None,
        correlation_id: str | None = None,
    ) -> GameEvent:
        return cls(
            event_id=event_id,
            game_id=game_id,
            state_revision=state_revision,
            round_no=round_no,
            phase=phase,
            created_at=created_at,
            event_type=event_type,
            channel=Channel.PRIVATE,
            actor_seat=actor_seat,
            audience=(seat,),
            payload=payload,
            correlation_id=correlation_id,
        )

    @classmethod
    def gm_only(
        cls,
        *,
        event_id: int,
        game_id: str,
        state_revision: int,
        round_no: int,
        phase: GamePhase,
        created_at: datetime,
        event_type: EventType,
        payload: GMOnlyPayload,
        actor_seat: int | None = None,
        correlation_id: str | None = None,
    ) -> GameEvent:
        return cls(
            event_id=event_id,
            game_id=game_id,
            state_revision=state_revision,
            round_no=round_no,
            phase=phase,
            created_at=created_at,
            event_type=event_type,
            channel=Channel.GM_ONLY,
            actor_seat=actor_seat,
            audience=(),
            payload=payload,
            correlation_id=correlation_id,
        )


class DeliveryCursor(BaseModel):
    """Persistable delivery state for one seat and one runtime session."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    committed_event_id: int = Field(default=0, ge=0, strict=True)
    in_flight_request_id: (
        Annotated[str, Field(min_length=1, max_length=128, strict=True)] | None
    ) = None
    in_flight_event_ids: tuple[int, ...] = ()
    session_epoch: int = Field(default=0, ge=0, strict=True)

    @field_validator("in_flight_event_ids")
    @classmethod
    def validate_in_flight_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(event_id < 0 for event_id in value):
            raise ValueError("in_flight_event_ids must be non-negative")
        if len(set(value)) != len(value):
            raise ValueError("in_flight_event_ids must not contain duplicates")
        if tuple(sorted(value)) != value:
            raise ValueError("in_flight_event_ids must be in ascending order")
        return value

    @model_validator(mode="after")
    def validate_in_flight_request(self) -> DeliveryCursor:
        if self.in_flight_event_ids and self.in_flight_request_id is None:
            raise ValueError("in_flight_request_id is required for in-flight events")
        if any(event_id <= self.committed_event_id for event_id in self.in_flight_event_ids):
            raise ValueError("in-flight events must be newer than committed_event_id")
        return self

    def with_in_flight(self, request_id: str, event_ids: tuple[int, ...]) -> DeliveryCursor:
        """Return a candidate cursor for the same commit as a runtime request."""

        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "in_flight_request_id": request_id,
                "in_flight_event_ids": event_ids,
            }
        )

    def acknowledge(
        self,
        *,
        request_id: str,
        event_ids: tuple[int, ...] | None = None,
        session_epoch: int | None = None,
    ) -> DeliveryCursor:
        """Return the post-ack candidate; this method never mutates state."""

        if session_epoch is not None and session_epoch != self.session_epoch:
            raise ValueError("session_epoch does not match delivery cursor")
        if self.in_flight_request_id != request_id:
            raise ValueError("request_id does not match in-flight delivery")
        acknowledged = self.in_flight_event_ids if event_ids is None else event_ids
        if not set(acknowledged).issubset(self.in_flight_event_ids):
            raise ValueError("acknowledged event IDs must be in-flight")
        if tuple(sorted(set(acknowledged))) != tuple(acknowledged):
            raise ValueError("acknowledged event IDs must be sorted and unique")
        if acknowledged != self.in_flight_event_ids[: len(acknowledged)]:
            raise ValueError("acknowledged event IDs must form a prefix of in-flight events")
        remaining = tuple(
            event_id for event_id in self.in_flight_event_ids if event_id not in acknowledged
        )
        committed = max((self.committed_event_id, *acknowledged))
        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "committed_event_id": committed,
                "in_flight_request_id": self.in_flight_request_id if remaining else None,
                "in_flight_event_ids": remaining,
            }
        )


__all__ = [
    "DeliveryCursor",
    "EventPayload",
    "EventType",
    "GameEvent",
    "GMOnlyPayload",
    "GmAuditPayload",
    "PrivateActionReceiptPayload",
    "PrivateNoticePayload",
    "PrivatePayload",
    "PrivateRolePayload",
    "PrivateSeerResultPayload",
    "PrivateWitchTargetPayload",
    "PublicAnnouncementPayload",
    "PublicPayload",
    "PublicSpeechPayload",
    "PublicVoteResultPayload",
    "TeamNoticePayload",
    "TeamPayload",
    "TeamSpeechPayload",
]
