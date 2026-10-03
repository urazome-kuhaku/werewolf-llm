"""Board-bound public vote projections and their privacy boundary."""

import json
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    EventCommitError,
    EventType,
    GameEvent,
    GameManager,
    GameState,
    PlayerState,
    PrivateNoticePayload,
    PublicVoteResultPayload,
    RulesetRef,
    VoteError,
    VoteRequest,
    VoteWindow,
    load_action_registry,
)
from werewolf.knowledge.board import BoardDefinition

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _manager() -> GameManager:
    state = GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.VOTE,
        ruleset=RulesetRef(
            board_id="vote-public-board",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="a" * 64,
        ),
        players={
            seat: PlayerState(
                seat=seat,
                role_id="villager",
                faction_id="village",
                session_epoch=4,
                current_request_id=f"req-{seat}",
            )
            for seat in (1, 2, 3)
        },
    )
    return GameManager(state, registry=load_action_registry())


def _window() -> VoteWindow:
    return VoteWindow(
        window_id="vote-r1",
        game_id="game-1",
        session_epoch=4,
        observation_revision=0,
        eligible_voters=(1, 2, 3),
        candidate_seats=(1, 2),
        vote_weights={1: 1.0, 2: 1.0, 3: 1.0},
        expected_request_ids={1: "req-1", 2: "req-2", 3: "req-3"},
        allow_abstain=True,
    )


def _board(*, reveal: str) -> BoardDefinition:
    return BoardDefinition.model_validate(
        {
            "schema_version": 1,
            "kind": "board",
            "id": "vote-public-board",
            "version": "1.0.0",
            "name": "公开投票测试板",
            "aliases": [],
            "locale": "zh-CN",
            "status": "published",
            "reviewed_by": "GM",
            "reviewed_at": "2026-09-27",
            "summary": "验证投票公开边界。",
            "seat_count": 4,
            "factions": {"town": 2, "wolf": 2},
            "roles": [
                {
                    "role_ref": {"id": "wolf", "version": "1.0.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
                {
                    "role_ref": {"id": "villager", "version": "1.0.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
            ],
            "victory": {
                "mode": "eliminate_side",
                "winning_sides": ["town", "wolf"],
                "check_phases": ["VICTORY_CHECK"],
                "draw_policy": "no_winner",
            },
            "wolf_team_visibility": {
                "members_know_each_other": True,
                "discussion_enabled": True,
                "identity_visibility": "members",
            },
            "knife_rule": {"selection_mode": "consensus", "target_visibility": "wolf_team"},
            "night_windows": [{"window_id": "wolf_team_chat", "phase": "NIGHT_TEAM_CHAT"}],
            "day_flow": {
                "vote": {
                    "visibility_during_collection": "secret",
                    "reveal_after_close": reveal,
                    "tie_policy": "pk_then_no_exile_on_retie",
                    "allow_abstain": True,
                },
                "pk": {"enabled": True, "candidate_count": 2},
            },
            "mechanics": [],
            "interactions": [],
            "reading_plan": {
                "board_ref": {"id": "vote-public-board", "version": "1.0.0"},
                "bootstrap_topics": ["board:overview"],
                "role_required_topics": {},
                "phase_topics": {},
                "high_risk_topics": ["mechanic:voting"],
                "suggested_queries": [],
            },
            "claim_refs": ["claim-board"],
            "source_refs": ["source-board"],
        }
    )


def _request(seat: int, target: int | None) -> VoteRequest:
    return VoteRequest(
        request_id=f"req-{seat}",
        game_id="game-1",
        window_id="vote-r1",
        seat=seat,
        session_epoch=4,
        observation_revision=0,
        target_seat=target,
    )


async def _collect(manager: GameManager) -> None:
    await manager.open_vote_window(_window())
    for seat, target in ((1, 2), (2, 2), (3, None)):
        current = await manager.snapshot()
        await manager.submit_vote(_request(seat, target), expected_revision=current.state_revision)
    current = await manager.snapshot()
    await manager.finalize_vote(expected_revision=current.state_revision)


@pytest.mark.asyncio
async def test_public_ballots_are_absent_until_confirmation_and_strip_private_metadata() -> None:
    manager = _manager()
    board = _board(reveal="ballots_and_totals")
    await _collect(manager)
    pending = await manager.snapshot()
    assert pending.events == ()

    confirmed = await manager.confirm_vote_tally(board, expected_revision=pending.state_revision)
    payload = confirmed.events[0].payload
    assert isinstance(payload, PublicVoteResultPayload)
    assert len(payload.ballots) == 3
    assert [(item.voter_seat, item.target_seat, item.weight) for item in payload.ballots] == [
        (1, 2, 1.0),
        (2, 2, 1.0),
        (3, None, 1.0),
    ]
    encoded = payload.model_dump()
    assert "request_id" not in encoded
    assert "session_epoch" not in encoded
    assert "observation_revision" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reveal", "expected_tally"),
    [("totals_only", {1: 0.0, 2: 2.0}), ("none", {})],
)
async def test_board_reveal_policy_controls_confirmed_projection(
    reveal: str, expected_tally: dict[int, float]
) -> None:
    manager = _manager()
    await _collect(manager)
    current = await manager.snapshot()
    confirmed = await manager.confirm_vote_tally(
        _board(reveal=reveal), expected_revision=current.state_revision
    )
    payload = confirmed.events[0].payload
    assert isinstance(payload, PublicVoteResultPayload)
    assert payload.ballots == ()
    assert payload.tally == expected_tally


@pytest.mark.asyncio
async def test_public_result_confirmation_is_idempotent_and_board_bound() -> None:
    manager = _manager()
    await _collect(manager)
    current = await manager.snapshot()
    board = _board(reveal="ballots_and_totals")
    first = await manager.confirm_vote_tally(board, expected_revision=current.state_revision)
    replay = await manager.confirm_vote_tally(board, expected_revision=first.state_revision)
    assert replay is first
    assert len(replay.events) == 1
    restored = GameManager(
        GameState.model_validate_json(first.model_dump_json()),
        registry=load_action_registry(),
    )
    restored_replay = await restored.confirm_vote_tally(
        board, expected_revision=first.state_revision
    )
    assert len(restored_replay.events) == 1

    current = await restored.snapshot()
    private_event = GameEvent.private(
        event_id=2,
        game_id=current.game_id,
        state_revision=current.state_revision + 1,
        round_no=current.round_no,
        phase=current.phase,
        created_at=NOW,
        event_type=EventType.PRIVATE_NOTICE,
        seat=1,
        payload=PrivateNoticePayload(content="private confirmation receipt"),
    )
    committed = await restored.commit_events(
        (private_event,), expected_revision=current.state_revision
    )
    restored_with_private = GameManager(
        GameState.model_validate_json(committed.model_dump_json()),
        registry=load_action_registry(),
    )
    assert [event.event_id for event in await restored_with_private.peek_delivery(1)] == [1, 2]
    assert [event.event_id for event in await restored_with_private.peek_delivery(2)] == [1]

    other = _board(reveal="totals_only").model_copy(update={"board_id": "other-board"})
    with pytest.raises(VoteError, match="RULESET_MISMATCH"):
        await manager.confirm_vote_tally(other)


@pytest.mark.asyncio
async def test_malformed_or_mixed_legacy_event_entries_remain_rejected() -> None:
    manager = _manager()
    await _collect(manager)
    current = await manager.snapshot()
    confirmed = await manager.confirm_vote_tally(
        _board(reveal="totals_only"), expected_revision=current.state_revision
    )
    malformed_data = json.loads(confirmed.model_dump_json())
    malformed_data["events"] = [{"legacy": True}]
    malformed = GameState.model_validate_json(json.dumps(malformed_data))
    with pytest.raises(EventCommitError, match="typed event log"):
        await GameManager(malformed, registry=load_action_registry()).peek_delivery(1)

    mixed_data = json.loads(confirmed.model_dump_json())
    mixed_data["events"] = [mixed_data["events"][0], {"legacy": True}]
    mixed = GameState.model_validate_json(json.dumps(mixed_data))
    with pytest.raises(EventCommitError, match="typed event log"):
        await GameManager(mixed, registry=load_action_registry()).peek_delivery(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("duplicate", "foreign_game"))
async def test_restored_event_log_preserves_aggregate_invariants(corruption: str) -> None:
    manager = _manager()
    await _collect(manager)
    current = await manager.snapshot()
    confirmed = await manager.confirm_vote_tally(
        _board(reveal="totals_only"), expected_revision=current.state_revision
    )
    data = json.loads(confirmed.model_dump_json())
    event = dict(data["events"][0])
    if corruption == "duplicate":
        data["events"] = [event, dict(event)]
    else:
        event["game_id"] = "another-game"
        data["events"] = [event]
    restored = GameManager(
        GameState.model_validate_json(json.dumps(data)),
        registry=load_action_registry(),
    )
    with pytest.raises(EventCommitError, match="event delivery"):
        await restored.peek_delivery(1)
