from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    GameManager,
    GameState,
    PlayerState,
    PublicVoteResultPayload,
    RulesetRef,
    VoteError,
    VoteRequest,
    VoteStatus,
    VoteWindow,
    load_action_registry,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _manager(*, vote_weights: dict[int, float] | None = None) -> GameManager:
    weights = vote_weights or {1: 1.0, 2: 1.0, 3: 1.0}
    state = GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.VOTE,
        ruleset=RulesetRef(
            board_id="classic-12",
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
                vote_weight=weights[seat],
                current_request_id=f"req-{seat}",
            )
            for seat in (1, 2, 3)
        },
    )
    return GameManager(state, registry=load_action_registry())


def _window(*, vote_weights: dict[int, float] | None = None) -> VoteWindow:
    weights = vote_weights or {1: 1.0, 2: 1.0, 3: 1.0}
    return VoteWindow(
        window_id="vote-r1",
        game_id="game-1",
        session_epoch=4,
        observation_revision=0,
        eligible_voters=(1, 2, 3),
        candidate_seats=(1, 2),
        vote_weights=weights,
        expected_request_ids={1: "req-1", 2: "req-2", 3: "req-3"},
    )


def _request(seat: int, target: int) -> VoteRequest:
    return VoteRequest(
        request_id=f"req-{seat}",
        game_id="game-1",
        window_id="vote-r1",
        seat=seat,
        session_epoch=4,
        observation_revision=0,
        target_seat=target,
    )


@pytest.mark.asyncio
async def test_manager_serializes_parallel_private_ballots() -> None:
    manager = _manager()
    opened = await manager.open_vote_window(_window())

    await asyncio.gather(
        manager.submit_vote(_request(1, 2)),
        manager.submit_vote(_request(2, 2)),
        manager.submit_vote(_request(3, 1)),
    )

    state = await manager.snapshot()
    assert state.state_revision == opened.state_revision + 3
    assert state.events == ()
    assert state.vote_state is not None
    assert set(state.vote_state["ballots"]) == {"1", "2", "3"}
    assert (await manager.vote_observation(1)).own_target_seat == 2
    assert "ballots" not in (await manager.vote_observation(1)).model_dump()


@pytest.mark.asyncio
async def test_manager_vote_idempotency_and_conflict_are_authoritative() -> None:
    manager = _manager()
    await manager.open_vote_window(_window())
    first = await manager.submit_vote(_request(1, 2))
    replay = await manager.submit_vote(_request(1, 2))
    assert replay is first
    with pytest.raises(VoteError, match="IDEMPOTENCY_CONFLICT"):
        await manager.submit_vote(_request(1, 1))


@pytest.mark.asyncio
async def test_vote_result_is_one_safe_public_event_after_gm_confirmation() -> None:
    manager = _manager()
    await manager.open_vote_window(_window())
    await asyncio.gather(
        manager.submit_vote(_request(1, 2)),
        manager.submit_vote(_request(2, 2)),
        manager.submit_vote(_request(3, 1)),
    )
    pending = await manager.finalize_vote()
    assert pending.vote_state is not None
    assert pending.vote_state["status"] == VoteStatus.WAITING_GM
    assert pending.events == ()

    confirmed = await manager.confirm_vote_tally()
    assert len(confirmed.events) == 1
    event = confirmed.events[0]
    assert isinstance(event.payload, PublicVoteResultPayload)
    assert "ballots" not in event.payload.model_dump()
    assert event.payload.eliminated_seat == 2
    assert event.payload.tally == {1: 1.0, 2: 2.0}


@pytest.mark.asyncio
async def test_vote_result_preserves_fractional_weighted_tally() -> None:
    weights = {1: 1.5, 2: 1.0, 3: 1.0}
    manager = _manager(vote_weights=weights)
    await manager.open_vote_window(_window(vote_weights=weights))
    await asyncio.gather(
        manager.submit_vote(_request(1, 1)),
        manager.submit_vote(_request(2, 2)),
        manager.submit_vote(_request(3, 2)),
    )
    await manager.finalize_vote()

    confirmed = await manager.confirm_vote_tally()
    payload = confirmed.events[0].payload
    assert isinstance(payload, PublicVoteResultPayload)
    assert payload.tally == {1: 1.5, 2: 2.0}


@pytest.mark.asyncio
async def test_stale_vote_revision_is_rejected_without_public_leak() -> None:
    manager = _manager()
    await manager.open_vote_window(_window())
    await asyncio.gather(
        manager.submit_vote(_request(1, 1)),
        manager.submit_vote(_request(2, 2)),
        manager.submit_vote(_request(3, 1)),
    )
    # The stale revision assertion covers the opening barrier.
    with pytest.raises(Exception, match="state revision mismatch"):
        await manager.submit_vote(_request(1, 2), expected_revision=0)
    assert (await manager.snapshot()).events == ()
