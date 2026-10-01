from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from werewolf.game.voting import (
    TieAction,
    TieDecision,
    VoteCollector,
    VoteError,
    VoteRequest,
    VoteState,
    VoteStatus,
    VoteWindow,
    resolve_board_tie,
)


def _window(*, allow_abstain: bool = False, equal_weights: bool = False) -> VoteWindow:
    return VoteWindow(
        window_id="vote-r1",
        game_id="game-1",
        session_epoch=4,
        observation_revision=19,
        eligible_voters=(1, 2, 3),
        candidate_seats=(1, 2),
        vote_weights={1: 1.0, 2: 1.0, 3: 1.0 if equal_weights else 1.5},
        allow_abstain=allow_abstain,
    )


def _request(
    seat: int,
    target: int | None,
    *,
    request_id: str | None = None,
    revision: int = 19,
) -> VoteRequest:
    return VoteRequest(
        request_id=request_id or f"req-{seat}",
        game_id="game-1",
        window_id="vote-r1",
        seat=seat,
        session_epoch=4,
        observation_revision=revision,
        target_seat=target,
    )


def test_votes_share_revision_and_remain_private_until_gm_confirmation() -> None:
    state = VoteState.open(_window())
    state = state.submit(_request(1, 2)).state
    state = state.submit(_request(2, 1)).state

    observation = state.player_observation(1)
    assert observation.has_submitted is True
    assert observation.own_target_seat == 2
    assert state.public_projection() is None
    assert "ballots" not in observation.model_dump()
    assert state.player_observation(2).own_target_seat == 1
    assert state.ballots[1].target_seat == 2  # GM-side state only


def test_wrong_window_session_or_revision_is_rejected() -> None:
    state = VoteState.open(_window())
    with pytest.raises(VoteError, match="GAME_MISMATCH"):
        state.submit(_request(1, 2)).state.submit(
            _request(2, 1).model_copy(update={"game_id": "other-game"})
        )
    with pytest.raises(VoteError, match="SESSION_MISMATCH"):
        state.submit(_request(1, 2).model_copy(update={"session_epoch": 3}))
    with pytest.raises(VoteError, match="REVISION_MISMATCH"):
        state.submit(_request(1, 2, revision=20))
    with pytest.raises(VoteError, match="WINDOW_MISMATCH"):
        state.submit(_request(1, 2).model_copy(update={"window_id": "vote-other"}))


def test_active_request_id_is_part_of_the_authorization_boundary() -> None:
    window = _window().model_copy(update={"expected_request_ids": {1: "active-1"}})
    # model_copy is intentionally not used for production state mutation; it
    # is sufficient here to construct the test fixture with the same shape.
    window = VoteWindow.model_validate(window.model_dump(mode="python"))
    with pytest.raises(VoteError, match="REQUEST_MISMATCH"):
        VoteState.open(window).submit(_request(1, 2))
    accepted = VoteState.open(window).submit(_request(1, 2, request_id="active-1"))
    assert accepted.ballot.request_id == "active-1"


def test_same_vote_is_idempotent_and_changed_vote_conflicts() -> None:
    state = VoteState.open(_window())
    accepted = state.submit(_request(1, 2, request_id="req-a"))
    replay = accepted.state.submit(_request(1, 2, request_id="req-a-retry"))
    assert replay.idempotent_replay is True
    assert replay.state == accepted.state
    with pytest.raises(VoteError, match="IDEMPOTENCY_CONFLICT"):
        accepted.state.submit(_request(1, 1, request_id="req-a-change"))


def test_collection_can_accept_independent_votes_concurrently() -> None:
    collector = VoteCollector(_window())
    requests = [_request(1, 2), _request(2, 1), _request(3, 2)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(collector.submit, requests))
    assert all(result.idempotent_replay is False for result in results)
    assert collector.state.is_complete
    assert collector.state.status is VoteStatus.OPEN
    assert collector.state.public_projection() is None


def test_incomplete_collection_requires_moderator_timeout() -> None:
    state = VoteState.open(_window()).submit(_request(1, 2)).state
    with pytest.raises(VoteError, match="INCOMPLETE_VOTE"):
        state.lock()
    state = state.lock(force=True, reason="moderator_timeout")
    assert state.status is VoteStatus.LOCKED
    assert state.missing_voters == (2, 3)


def test_weighted_tally_is_pending_until_gm_confirmation() -> None:
    state = VoteState.open(_window())
    for request in (_request(1, 2), _request(2, 1), _request(3, 2)):
        state = state.submit(request).state
    state = state.finalize_collection()
    assert state.status is VoteStatus.WAITING_GM
    assert state.pending_tally is not None
    assert state.pending_tally.counts == {1: 1.0, 2: 2.5}
    assert state.pending_tally.winner_seat == 2
    assert state.public_projection() is None
    state = state.confirm_tally()
    assert state.status is VoteStatus.RESOLVED
    assert state.public_result is not None
    assert state.public_result.eliminated_seat == 2


def test_tie_policy_is_injected_and_cannot_be_guessed() -> None:
    state = VoteState.open(_window(equal_weights=True, allow_abstain=True))
    for request in (_request(1, 2), _request(2, 1), _request(3, None)):
        state = state.submit(request).state
    with pytest.raises(VoteError, match="TIE_POLICY_REQUIRED"):
        state.finalize_collection()
    state = state.finalize_collection(
        tie_resolver=lambda window, tally: TieDecision(
            action=TieAction.PK,
            candidates=tally.top_candidates,
        )
    )
    assert state.tie_decision is not None
    assert state.tie_decision.action is TieAction.PK
    assert state.confirm_tally().public_result is not None
    assert state.confirm_tally().public_result.eliminated_seat is None


@pytest.mark.parametrize(
    ("policy", "tie_round", "pk_enabled", "expected"),
    [
        ("no_exile_on_tie", 0, False, TieAction.NO_EXILE),
        ("pk_then_no_exile_on_retie", 0, True, TieAction.PK),
        ("pk_then_no_exile_on_retie", 1, True, TieAction.NO_EXILE),
        ("pk_then_no_sheriff_on_retie", 0, True, TieAction.PK),
        ("pk_then_no_sheriff_on_retie", 1, True, TieAction.NO_EXILE),
        ("pk_then_revote", 4, True, TieAction.PK),
        ("revote_until_unique", 4, True, TieAction.PK),
        ("no_sheriff_on_tie", 0, False, TieAction.NO_EXILE),
        ("no_election_on_tie", 0, False, TieAction.NO_EXILE),
    ],
)
def test_resolve_board_tie_maps_day_and_sheriff_policies(
    policy: str,
    tie_round: int,
    pk_enabled: bool,
    expected: TieAction,
) -> None:
    decision = resolve_board_tie(
        policy,
        tie_round,
        (1, 2),
        pk_enabled=pk_enabled,
    )
    assert decision.action is expected
    assert decision.candidates == (1, 2)


def test_resolve_board_tie_rejects_missing_unknown_or_disabled_pk_policy() -> None:
    with pytest.raises(VoteError, match="TIE_POLICY_REQUIRED"):
        resolve_board_tie(None, 0, (1, 2), pk_enabled=False)
    with pytest.raises(VoteError, match="TIE_POLICY_UNSUPPORTED"):
        resolve_board_tie("custom_tie_policy", 0, (1, 2), pk_enabled=False)
    with pytest.raises(VoteError, match="TIE_POLICY_UNSUPPORTED"):
        resolve_board_tie("pk_then_no_exile_on_retie", 0, (1, 2), pk_enabled=False)


def test_abstention_is_board_policy_and_not_an_implicit_candidate() -> None:
    state = VoteState.open(_window(allow_abstain=True))
    state = state.submit(_request(1, None)).state
    with pytest.raises(VoteError, match="ABSTAIN_NOT_ALLOWED"):
        VoteState.open(_window()).submit(_request(1, None))
    assert state.ballots[1].target_seat is None
