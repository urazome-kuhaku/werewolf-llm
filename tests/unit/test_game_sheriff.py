from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from werewolf.game.sheriff import (
    SheriffCampaignSpeechRequest,
    SheriffElectionAction,
    SheriffElectionError,
    SheriffElectionState,
    SheriffElectionStatus,
    build_sheriff_vote_window,
)
from werewolf.game.voting import VoteRequest, VoteStatus
from werewolf.knowledge.board import BoardDefinition


def _board(*, tie_policy: str | None, pk_enabled: bool | None) -> BoardDefinition:
    return BoardDefinition.model_validate(
        {
            "schema_version": 1,
            "kind": "board",
            "id": "sheriff-test-board",
            "version": "1.0.0",
            "name": "警长测试板",
            "aliases": [],
            "locale": "zh-CN",
            "status": "published",
            "reviewed_by": "GM",
            "reviewed_at": date(2026, 9, 28),
            "summary": "用于警长状态机单元测试。",
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
                "board_ref": {"id": "sheriff-test-board", "version": "1.0.0"},
                "bootstrap_topics": ["board:overview"],
                "role_required_topics": {},
                "phase_topics": {},
                "high_risk_topics": ["mechanic:sheriff"],
                "suggested_queries": [],
            },
            "claim_refs": ["claim-sheriff-test"],
            "source_refs": ["source-sheriff-test"],
        }
    )


def _election(*, expected_speech_request_ids: dict[int, str] | None = None) -> SheriffElectionState:
    return SheriffElectionState.start(
        game_id="game-1",
        day_no=1,
        candidates=(1, 2),
        eligible_voters=(1, 2, 3),
        speech_order=(2, 1),
        expected_speech_request_ids=expected_speech_request_ids,
    )


def _speech(
    seat: int,
    *,
    request_id: str,
    session_epoch: int = 4,
    observation_revision: int = 19,
    text: str = "我上警。",
) -> SheriffCampaignSpeechRequest:
    return SheriffCampaignSpeechRequest(
        request_id=request_id,
        game_id="game-1",
        day_no=1,
        seat=seat,
        session_epoch=session_epoch,
        observation_revision=observation_revision,
        text=text,
    )


def _vote_window(
    *,
    expected_request_ids: dict[int, str] | None = None,
    allow_abstain: bool = False,
):
    return build_sheriff_vote_window(
        game_id="game-1",
        day_no=1,
        observation_revision=19,
        session_epoch=4,
        eligible_voters=(1, 2, 3),
        candidates=(1, 2),
        vote_weights={1: 1.0, 2: 1.0, 3: 1.0},
        expected_request_ids=expected_request_ids,
        allow_abstain=allow_abstain,
    )


def _vote(seat: int, target: int | None, *, request_id: str | None = None) -> VoteRequest:
    return VoteRequest(
        request_id=request_id or f"vote-{seat}",
        game_id="game-1",
        window_id="sheriff-vote-d1",
        seat=seat,
        session_epoch=4,
        observation_revision=19,
        target_seat=target,
    )


def _tie_state() -> SheriffElectionState:
    state = _election().open_vote(_vote_window(allow_abstain=True))
    for seat, target in ((1, 2), (2, 1), (3, None)):
        state = state.submit_vote(_vote(seat, target))
    return state


def test_speech_requires_candidate_and_active_capability_and_is_idempotent() -> None:
    state = _election(expected_speech_request_ids={1: "speech-1", 2: "speech-2"})
    with pytest.raises(SheriffElectionError, match="SEAT_NOT_AUTHORIZED"):
        state.submit_speech(_speech(3, request_id="speech-3"))
    with pytest.raises(SheriffElectionError, match="REQUEST_MISMATCH"):
        state.submit_speech(_speech(1, request_id="stale"))

    accepted = state.submit_speech(_speech(1, request_id="speech-1"))
    assert accepted.submit_speech(_speech(1, request_id="speech-1")) == accepted
    with pytest.raises(SheriffElectionError, match="IDEMPOTENCY_CONFLICT"):
        accepted.submit_speech(_speech(1, request_id="speech-1", text="改稿"))
    duplicate_capability_state = _election()
    duplicate_capability_state = duplicate_capability_state.submit_speech(
        _speech(1, request_id="speech-1")
    )
    with pytest.raises(SheriffElectionError, match="IDEMPOTENCY_CONFLICT"):
        duplicate_capability_state.submit_speech(_speech(2, request_id="speech-1"))


def test_vote_uses_vote_state_for_one_ballot_and_authorization() -> None:
    state = _election().open_vote(_vote_window(expected_request_ids={1: "active-1"}))
    with pytest.raises(SheriffElectionError, match="REQUEST_MISMATCH"):
        state.submit_vote(_vote(1, 2))
    accepted = state.submit_vote(_vote(1, 2, request_id="active-1"))
    replay = accepted.submit_vote(_vote(1, 2, request_id="active-1"))
    assert replay == accepted
    with pytest.raises(SheriffElectionError, match="IDEMPOTENCY_CONFLICT"):
        accepted.submit_vote(_vote(1, 1, request_id="active-1"))


def test_unique_tally_remains_pending_until_confirmation() -> None:
    state = _election().open_vote(_vote_window())
    state = state.submit_vote(_vote(1, 2))
    state = state.submit_vote(_vote(2, 1))
    state = state.submit_vote(_vote(3, 2))
    pending = state.finalize(board=_board(tie_policy=None, pk_enabled=None))

    assert pending.status is SheriffElectionStatus.WAITING_GM
    assert pending.decision is not None
    assert pending.decision.action is SheriffElectionAction.ELECT
    assert pending.vote is not None and pending.vote.status is VoteStatus.WAITING_GM
    resolved = pending.confirm()
    assert resolved.status is SheriffElectionStatus.RESOLVED
    assert resolved.sheriff_seat == 2
    assert resolved.confirm() == resolved


def test_tie_without_published_policy_is_rejected_and_state_stays_open() -> None:
    state = _tie_state()
    with pytest.raises(SheriffElectionError, match="TIE_POLICY_REQUIRED"):
        state.finalize(board=_board(tie_policy=None, pk_enabled=None))
    assert state.status is SheriffElectionStatus.VOTING
    assert state.vote is not None and state.vote.status is VoteStatus.OPEN


def test_pk_tie_is_explicitly_pending_for_moderator() -> None:
    pending = _tie_state().finalize(
        board=_board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True),
        tie_round=0,
    )
    assert pending.status is SheriffElectionStatus.WAITING_GM
    assert pending.decision is not None
    assert pending.decision.action is SheriffElectionAction.PK
    with pytest.raises(SheriffElectionError, match="ELECTION_ACTION_PENDING"):
        pending.confirm()


def test_retie_policy_requires_confirmation_before_no_sheriff_terminal_state() -> None:
    first_tie = _tie_state().finalize(
        board=_board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True),
    )
    state = first_tie.begin_pk()
    pk_window = _vote_window(allow_abstain=True).model_copy(
        update={"window_id": "sheriff-vote-d1-pk"}
    )
    state = state.open_vote(pk_window)
    for seat, target in ((1, 2), (2, 1), (3, None)):
        state = state.submit_vote(
            _vote(
                seat,
                target,
                request_id=f"pk-vote-{seat}",
            ).model_copy(update={"window_id": "sheriff-vote-d1-pk"})
        )
    pending = state.finalize(
        board=_board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True),
    )
    assert pending.status is SheriffElectionStatus.WAITING_GM
    assert pending.decision is not None
    assert pending.decision.action is SheriffElectionAction.NO_SHERIFF
    assert pending.vote is not None and pending.vote.status is VoteStatus.WAITING_GM
    resolved = pending.confirm()
    assert resolved.status is SheriffElectionStatus.NO_SHERIFF
    assert resolved.decision is not None
    assert resolved.decision.action is SheriffElectionAction.NO_SHERIFF
    assert resolved.vote is not None and resolved.vote.status is VoteStatus.RESOLVED
    assert resolved.vote.public_result is not None
    assert resolved.vote.public_result.eliminated_seat is None
    assert resolved.confirm() == resolved


def test_pk_round_preserves_first_ballot_and_can_elect_a_winner() -> None:
    first_pending = _tie_state().finalize(
        board=_board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True),
    )
    pk_speech = first_pending.begin_pk()
    assert pk_speech.tie_round == 1
    assert len(pk_speech.vote_history) == 1
    assert pk_speech.vote_history[0].status is VoteStatus.WAITING_GM

    pk_window = _vote_window(allow_abstain=True)
    pk_window = pk_window.model_copy(update={"window_id": "sheriff-vote-d1-pk"})
    pk_voting = pk_speech.open_vote(pk_window)
    for seat, target in ((1, 2), (2, 2), (3, None)):
        pk_voting = pk_voting.submit_vote(
            _vote(seat, target, request_id=f"pk-vote-{seat}").model_copy(
                update={"window_id": "sheriff-vote-d1-pk"}
            )
        )

    pending = pk_voting.finalize(
        board=_board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True),
    )
    assert pending.decision is not None
    assert pending.decision.action is SheriffElectionAction.ELECT
    resolved = pending.confirm()
    assert resolved.status is SheriffElectionStatus.RESOLVED
    assert resolved.sheriff_seat == 2
    assert len(resolved.vote_history) == 1


def test_sheriff_no_tie_policy_maps_shared_no_exile_to_no_sheriff() -> None:
    pending = _tie_state().finalize(
        board=_board(tie_policy="no_sheriff_on_tie", pk_enabled=False),
    )
    assert pending.decision is not None
    assert pending.decision.action is SheriffElectionAction.NO_SHERIFF
    assert pending.decision.candidates == (1, 2)


def test_sheriff_finalize_rejects_external_tie_round_override() -> None:
    state = _tie_state()
    with pytest.raises(SheriffElectionError, match="TIE_ROUND_MISMATCH"):
        state.finalize(
            board=_board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True),
            tie_round=1,
        )


def test_rejects_duplicate_speech_capabilities_at_model_boundary() -> None:
    with pytest.raises(ValidationError):
        SheriffElectionState.start(
            game_id="game-1",
            day_no=1,
            candidates=(1, 2),
            eligible_voters=(1, 2, 3),
            expected_speech_request_ids={1: "same", 2: "same"},
        )
