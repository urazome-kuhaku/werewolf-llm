from __future__ import annotations

from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    EventCommitError,
    EventType,
    GameManager,
    GameState,
    PlayerState,
    PublicVoteResultPayload,
    RulesetRef,
    SheriffCampaignSpeechRequest,
    SheriffElectionError,
    SheriffElectionStatus,
    VoteRequest,
    build_sheriff_vote_window,
    load_action_registry,
)
from werewolf.knowledge.board import BoardDefinition

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _board(*, tie_policy: str | None = None, pk_enabled: bool | None = None) -> BoardDefinition:
    return BoardDefinition.model_validate(
        {
            "schema_version": 1,
            "kind": "board",
            "id": "sheriff-manager-board",
            "version": "1.0.0",
            "name": "警长经理集成测试板",
            "aliases": [],
            "locale": "zh-CN",
            "status": "published",
            "reviewed_by": "GM",
            "reviewed_at": "2026-09-28",
            "summary": "测试警长权威提交边界。",
            "seat_count": 3,
            "factions": {"town": 2, "wolf": 1},
            "roles": [
                {
                    "role_ref": {"id": "villager", "version": "1.0.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
                {
                    "role_ref": {"id": "wolf", "version": "1.0.0"},
                    "count": 1,
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
                "discussion_enabled": False,
                "identity_visibility": "members",
            },
            "knife_rule": {
                "selection_mode": "consensus",
                "target_visibility": "wolf_team",
                "available_after_window": "night_resolve",
            },
            "night_windows": ["night_resolve"],
            "day_flow": {
                "announce_deaths": True,
                "vote": {
                    "visibility_during_collection": "secret",
                    "reveal_after_close": "ballots_and_totals",
                    "tie_policy": "no_exile_on_tie",
                },
                "sheriff": {
                    "enabled": True,
                    "first_day_election": True,
                    "vote_weight": 1.5,
                    "final_speech": True,
                    "tie_policy": tie_policy,
                    "pk_enabled": pk_enabled,
                },
            },
            "mechanics": ["voting@1.0.0"],
            "interactions": [],
            "reading_plan": {
                "board_ref": {"id": "sheriff-manager-board", "version": "1.0.0"},
                "bootstrap_topics": ["board:overview"],
                "role_required_topics": {},
                "phase_topics": {},
                "high_risk_topics": ["mechanic:sheriff"],
                "suggested_queries": [],
            },
            "claim_refs": ["claim-sheriff-manager"],
            "source_refs": ["source-sheriff-manager"],
        }
    )


def _manager() -> GameManager:
    state = GameState(
        game_id="sheriff-manager-game",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.DAY_ANNOUNCE,
        day_no=1,
        ruleset=RulesetRef(
            board_id="sheriff-manager-board",
            version="1.0.0",
            snapshot_id="sheriff-manager-snapshot",
            manifest_sha256="a" * 64,
        ),
        players={
            seat: PlayerState(
                seat=seat,
                role_id="villager" if seat != 3 else "wolf",
                faction_id="town" if seat != 3 else "wolf",
            )
            for seat in (1, 2, 3)
        },
    )
    return GameManager(state, registry=load_action_registry())


async def _open_vote(manager: GameManager) -> object:
    for seat in (1, 2):
        current = await manager.snapshot()
        await manager.submit_sheriff_speech(
            SheriffCampaignSpeechRequest(
                request_id=f"speech-{seat}",
                game_id=current.game_id,
                day_no=1,
                seat=seat,
                session_epoch=0,
                observation_revision=current.state_revision,
                text=f"{seat}号上警。",
            ),
            expected_revision=current.state_revision,
        )
    current = await manager.snapshot()
    window = build_sheriff_vote_window(
        game_id=current.game_id,
        day_no=1,
        observation_revision=current.state_revision,
        session_epoch=0,
        eligible_voters=(1, 2, 3),
        candidates=(1, 2),
        vote_weights={1: 1.0, 2: 1.0, 3: 1.0},
        allow_abstain=True,
    )
    return await manager.open_sheriff_vote_window(
        window,
        expected_revision=current.state_revision,
    )


def _ballot(
    seat: int,
    target: int | None,
    *,
    window_id: str = "sheriff-vote-d1",
    request_prefix: str = "ballot",
    observation_revision: int = 3,
) -> VoteRequest:
    return VoteRequest(
        request_id=f"{request_prefix}-{seat}",
        game_id="sheriff-manager-game",
        window_id=window_id,
        seat=seat,
        session_epoch=0,
        observation_revision=observation_revision,
        target_seat=target,
    )


@pytest.mark.asyncio
async def test_manager_commits_unique_election_and_installs_weight() -> None:
    manager = _manager()
    board = _board()
    started = await manager.start_sheriff_election(board, candidates=(1, 2))
    assert started.phase is GamePhase.SHERIFF_ELECTION_SPEECH
    await _open_vote(manager)
    for seat, target in ((1, 2), (2, 2), (3, 1)):
        current = await manager.snapshot()
        await manager.submit_sheriff_vote(
            _ballot(seat, target), expected_revision=current.state_revision
        )

    current = await manager.snapshot()
    pending = await manager.finalize_sheriff_election(
        board, expected_revision=current.state_revision
    )
    assert pending.sheriff_election is not None
    assert pending.sheriff_election["status"] == SheriffElectionStatus.WAITING_GM
    confirmed = await manager.confirm_sheriff_election(
        board,
        expected_revision=pending.state_revision,
    )
    assert confirmed.phase is GamePhase.SHERIFF_TRANSFER
    assert confirmed.sheriff_seat == 2
    assert confirmed.players[2].vote_weight == 1.5
    result_events = [
        event for event in confirmed.events if event.event_type is EventType.VOTE_RESULT
    ]
    assert len(result_events) == 1
    payload = result_events[0].payload
    assert isinstance(payload, PublicVoteResultPayload)
    assert payload.vote_kind == "sheriff"
    assert payload.elected_seat == 2
    assert payload.eliminated_seat is None
    assert [(ballot.voter_seat, ballot.target_seat) for ballot in payload.ballots] == [
        (1, 2),
        (2, 2),
        (3, 1),
    ]
    replay = await manager.confirm_sheriff_election(
        board, expected_revision=confirmed.state_revision
    )
    assert replay is confirmed
    assert (
        await manager.complete_sheriff_transfer(expected_revision=confirmed.state_revision)
    ).phase is (GamePhase.DAY_SPEECH)


@pytest.mark.asyncio
async def test_unresolved_tie_stays_private_and_blocks_generic_phase_advance() -> None:
    manager = _manager()
    board = _board()
    await manager.start_sheriff_election(board, candidates=(1, 2))
    await _open_vote(manager)
    for seat, target in ((1, 2), (2, 1), (3, None)):
        current = await manager.snapshot()
        await manager.submit_sheriff_vote(
            _ballot(seat, target), expected_revision=current.state_revision
        )

    before = await manager.snapshot()
    with pytest.raises(SheriffElectionError, match="TIE_POLICY_REQUIRED"):
        await manager.finalize_sheriff_election(board, expected_revision=before.state_revision)
    after = await manager.snapshot()
    assert after.state_revision == before.state_revision
    assert after.phase is GamePhase.SHERIFF_ELECTION
    assert after.sheriff_seat is None
    assert after.sheriff_election is not None
    assert after.sheriff_election["status"] == SheriffElectionStatus.VOTING
    with pytest.raises(EventCommitError, match="sheriff election requires"):
        await manager.commit_moderator_operation(
            operation="NEXT",
            command="next",
            expected_revision=after.state_revision,
            target_phase=GamePhase.SHERIFF_TRANSFER,
        )


@pytest.mark.asyncio
async def test_manager_pk_revote_elects_winner_and_retains_first_ballot() -> None:
    manager = _manager()
    board = _board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True)
    await manager.start_sheriff_election(board, candidates=(1, 2))
    await _open_vote(manager)
    for seat, target in ((1, 2), (2, 1), (3, None)):
        current = await manager.snapshot()
        await manager.submit_sheriff_vote(
            _ballot(seat, target), expected_revision=current.state_revision
        )

    current = await manager.snapshot()
    first_pending = await manager.finalize_sheriff_election(
        board, expected_revision=current.state_revision
    )
    assert first_pending.phase is GamePhase.SHERIFF_ELECTION
    assert first_pending.sheriff_election is not None
    assert first_pending.sheriff_election["decision"]["action"] == "PK"

    pk_speech = await manager.confirm_sheriff_election(
        board, expected_revision=first_pending.state_revision
    )
    assert pk_speech.phase is GamePhase.SHERIFF_ELECTION_PK_SPEECH
    assert pk_speech.sheriff_election is not None
    assert pk_speech.sheriff_election["tie_round"] == 1
    assert len(pk_speech.sheriff_election["vote_history"]) == 1
    first_event = [
        event for event in pk_speech.events if event.event_type is EventType.VOTE_RESULT
    ][0]
    assert isinstance(first_event.payload, PublicVoteResultPayload)
    assert first_event.payload.vote_kind == "sheriff"
    assert first_event.payload.elected_seat is None
    assert first_event.payload.eliminated_seat is None
    assert first_event.payload.ballots[-1].target_seat is None
    assert (
        await manager.confirm_sheriff_election(board, expected_revision=pk_speech.state_revision)
    ) is pk_speech

    for seat in (1, 2):
        current = await manager.snapshot()
        await manager.submit_sheriff_speech(
            SheriffCampaignSpeechRequest(
                request_id=f"pk-speech-{seat}",
                game_id=current.game_id,
                day_no=1,
                seat=seat,
                session_epoch=0,
                observation_revision=current.state_revision,
                text=f"PK {seat}号发言。",
            ),
            expected_revision=current.state_revision,
        )

    current = await manager.snapshot()
    pk_window = build_sheriff_vote_window(
        game_id=current.game_id,
        day_no=1,
        observation_revision=current.state_revision,
        session_epoch=0,
        eligible_voters=(1, 2, 3),
        candidates=(1, 2),
        vote_weights={1: 1.0, 2: 1.0, 3: 1.0},
        allow_abstain=True,
        window_id="sheriff-vote-d1-pk",
    )
    pk_voting = await manager.open_sheriff_vote_window(
        pk_window, expected_revision=current.state_revision
    )
    for seat, target in ((1, 2), (2, 2), (3, None)):
        current = await manager.snapshot()
        await manager.submit_sheriff_vote(
            _ballot(
                seat,
                target,
                window_id="sheriff-vote-d1-pk",
                request_prefix="pk-ballot",
                observation_revision=pk_voting.sheriff_election["vote"]["window"][
                    "observation_revision"
                ],
            ),
            expected_revision=current.state_revision,
        )

    current = await manager.snapshot()
    second_pending = await manager.finalize_sheriff_election(
        board, expected_revision=current.state_revision
    )
    assert second_pending.sheriff_election is not None
    assert second_pending.sheriff_election["status"] == SheriffElectionStatus.WAITING_GM
    assert second_pending.sheriff_election["decision"]["action"] == "ELECT"
    confirmed = await manager.confirm_sheriff_election(
        board, expected_revision=second_pending.state_revision
    )
    assert confirmed.phase is GamePhase.SHERIFF_TRANSFER
    assert confirmed.sheriff_seat == 2
    assert confirmed.sheriff_election is not None
    assert len(confirmed.sheriff_election["vote_history"]) == 1
    result_events = [
        event for event in confirmed.events if event.event_type is EventType.VOTE_RESULT
    ]
    assert len(result_events) == 2
    assert result_events[0].correlation_id != result_events[1].correlation_id
    assert isinstance(result_events[1].payload, PublicVoteResultPayload)
    assert result_events[1].payload.vote_kind == "sheriff_pk"
    assert result_events[1].payload.elected_seat == 2
    assert result_events[1].payload.eliminated_seat is None


@pytest.mark.asyncio
async def test_manager_pk_retie_confirms_no_sheriff_and_can_recover_snapshot() -> None:
    manager = _manager()
    board = _board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True)
    await manager.start_sheriff_election(board, candidates=(1, 2))
    await _open_vote(manager)
    for seat, target in ((1, 2), (2, 1), (3, None)):
        current = await manager.snapshot()
        await manager.submit_sheriff_vote(
            _ballot(seat, target), expected_revision=current.state_revision
        )
    current = await manager.snapshot()
    first_pending = await manager.finalize_sheriff_election(
        board, expected_revision=current.state_revision
    )
    await manager.confirm_sheriff_election(board, expected_revision=first_pending.state_revision)
    current = await manager.snapshot()
    pk_window = build_sheriff_vote_window(
        game_id=current.game_id,
        day_no=1,
        observation_revision=current.state_revision,
        session_epoch=0,
        eligible_voters=(1, 2, 3),
        candidates=(1, 2),
        vote_weights={1: 1.0, 2: 1.0, 3: 1.0},
        allow_abstain=True,
        window_id="sheriff-vote-d1-pk",
    )
    pk_voting = await manager.open_sheriff_vote_window(
        pk_window, expected_revision=current.state_revision
    )
    observation_revision = pk_voting.sheriff_election["vote"]["window"]["observation_revision"]
    for seat, target in ((1, 2), (2, 1), (3, None)):
        current = await manager.snapshot()
        await manager.submit_sheriff_vote(
            _ballot(
                seat,
                target,
                window_id="sheriff-vote-d1-pk",
                request_prefix="pk-ballot",
                observation_revision=observation_revision,
            ),
            expected_revision=current.state_revision,
        )
    current = await manager.snapshot()
    second_pending = await manager.finalize_sheriff_election(
        board, expected_revision=current.state_revision
    )
    assert second_pending.sheriff_election is not None
    assert second_pending.sheriff_election["decision"]["action"] == "NO_SHERIFF"

    restored = GameManager(
        GameState.model_validate_json(second_pending.model_dump_json()),
        registry=load_action_registry(),
    )
    confirmed = await restored.confirm_sheriff_election(
        board, expected_revision=second_pending.state_revision
    )
    assert confirmed.phase is GamePhase.SHERIFF_TRANSFER
    assert confirmed.sheriff_seat is None
    assert confirmed.sheriff_election is not None
    assert confirmed.sheriff_election["status"] == SheriffElectionStatus.NO_SHERIFF
    transferred = await restored.complete_sheriff_transfer(
        expected_revision=confirmed.state_revision
    )
    assert transferred.phase is GamePhase.DAY_SPEECH
