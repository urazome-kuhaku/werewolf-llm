from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import Channel, GamePhase
from werewolf.game import (
    DeliveryCursor,
    DeliveryCursorError,
    EventType,
    GameEvent,
    GmAuditPayload,
    MessageRouter,
    PrivateRolePayload,
    PublicAnnouncementPayload,
    TeamSpeechPayload,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _public(event_id: int, *, audience: tuple[int, ...] = (1, 2)) -> GameEvent:
    return GameEvent.public(
        event_id=event_id,
        game_id="game-1",
        state_revision=event_id,
        round_no=1,
        phase=GamePhase.DAY_SPEECH,
        created_at=NOW,
        event_type=EventType.ANNOUNCEMENT,
        eligible_seats=audience,
        payload=PublicAnnouncementPayload(content=f"公告 {event_id}"),
    )


def test_channel_factories_freeze_authorized_audience() -> None:
    team = GameEvent.team(
        event_id=1,
        game_id="game-1",
        state_revision=1,
        round_no=1,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        created_at=NOW,
        event_type=EventType.TEAM_SPEECH,
        authorized_seats=(1, 3),
        actor_seat=1,
        payload=TeamSpeechPayload(speaker_seat=1, content="夜间讨论"),
    )
    gm = GameEvent.gm_only(
        event_id=2,
        game_id="game-1",
        state_revision=2,
        round_no=1,
        phase=GamePhase.NIGHT_RESOLVE,
        created_at=NOW,
        event_type=EventType.GM_AUDIT,
        payload=GmAuditPayload(code="hidden", details={"target": 4}),
    )
    assert team.audience == (1, 3)
    assert gm.audience == ()
    assert gm.channel is Channel.GM_ONLY


def test_nested_payload_mapping_is_immutable() -> None:
    gm = GameEvent.gm_only(
        event_id=1,
        game_id="game-1",
        state_revision=1,
        round_no=1,
        phase=GamePhase.NIGHT_RESOLVE,
        created_at=NOW,
        event_type=EventType.GM_AUDIT,
        payload=GmAuditPayload(code="hidden", details={"target": 4}),
    )
    with pytest.raises(TypeError):
        gm.payload.details["target"] = 5  # type: ignore[index]


def test_private_or_role_payload_cannot_be_public() -> None:
    with pytest.raises(ValidationError, match="public-safe"):
        GameEvent(
            event_id=1,
            game_id="game-1",
            state_revision=1,
            round_no=1,
            phase=GamePhase.DAY_SPEECH,
            created_at=NOW,
            event_type=EventType.ROLE_ASSIGNMENT,
            channel=Channel.PUBLIC,
            audience=(1, 2),
            payload=PrivateRolePayload(role_id="seer", faction_id="town"),
        )


def test_router_filters_by_seat_without_rendering_away_secrets() -> None:
    public = _public(1)
    private = GameEvent.private(
        event_id=2,
        game_id="game-1",
        state_revision=2,
        round_no=1,
        phase=GamePhase.DAY_SPEECH,
        created_at=NOW,
        event_type=EventType.ROLE_ASSIGNMENT,
        seat=1,
        payload=PrivateRolePayload(role_id="seer", faction_id="town"),
    )
    router = MessageRouter((public, private))
    assert tuple(event.event_id for event in router.peek_delivery(1, 0)) == (1, 2)
    assert tuple(event.event_id for event in router.peek_delivery(2, 0)) == (1,)
    assert router.peek_delivery(2, 0)[0].payload == public.payload


def test_peek_does_not_advance_and_retry_keeps_stable_ids() -> None:
    router = MessageRouter((_public(1), _public(2)))
    first = router.peek_delivery(1, 0)
    assert tuple(event.event_id for event in first) == (1, 2)
    assert router.peek_delivery(1, 0) == first
    cursor = router.prepare_ack(1, 0, request_id="request-1")
    retry_router = MessageRouter(router.events, {1: cursor})
    assert tuple(event.event_id for event in retry_router.peek_delivery(1, 0)) == (1, 2)
    acknowledged = cursor.acknowledge(request_id="request-1", session_epoch=0)
    assert acknowledged.committed_event_id == 2
    assert acknowledged.in_flight_event_ids == ()


def test_ack_candidate_can_ack_only_authorized_subset() -> None:
    router = MessageRouter((_public(1), _public(2)))
    candidate = router.prepare_ack(1, 0, request_id="request-1", event_ids=(1,))
    assert candidate.in_flight_event_ids == (1,)
    with pytest.raises(DeliveryCursorError):
        router.prepare_ack(1, 0, request_id="request-2", event_ids=(2, 1))
    with pytest.raises(DeliveryCursorError, match="prefix"):
        router.prepare_ack(1, 0, request_id="request-3", event_ids=(2,))


def test_stale_session_is_rejected() -> None:
    router = MessageRouter((_public(1),), {1: DeliveryCursor(session_epoch=3)})
    with pytest.raises(ValueError, match="session_epoch mismatch"):
        router.peek_delivery(1, 2)
