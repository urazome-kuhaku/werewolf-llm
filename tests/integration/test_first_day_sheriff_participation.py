from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_sheriff_flow import _board, _manager

from werewolf.domain.enums import GamePhase
from werewolf.domain.sheriff_eligibility import first_day_sheriff_participants
from werewolf.game import (
    EventType,
    GameEvent,
    GameManager,
    GameState,
    PublicAnnouncementPayload,
    SheriffCampaignSpeechRequest,
    SheriffElectionError,
    VoteError,
    VoteRequest,
    build_sheriff_vote_window,
    load_action_registry,
)
from werewolf.knowledge.skill_status import project_skill_status

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _first_night_manager(
    *,
    with_provenance: bool = True,
    announced: bool = False,
) -> GameManager:
    base = _manager()
    state = base.state
    players = dict(state.players)
    players[3] = players[3].model_copy(update={"alive": False, "death_cause": "wolf_kill"})
    data = state.model_dump(mode="python")
    data["round_no"] = 0
    data["day_no"] = 1
    data["players"] = players
    data["events"] = ()
    if with_provenance:
        data["action_windows"] = {
            "night-action-r0": {
                "window_id": "night-action-r0",
                "phase": "NIGHT_ACTION",
                "visible_context": {"night_round": 0},
            }
        }
        data["resolutions"] = (
            {
                "resolution_id": "resolution-r0",
                "window_id": "night-action-r0",
                "actions": [
                    {
                        "effects": [
                            {"effect_type": "SET_ALIVE", "target_seat": 3, "value": False},
                            {
                                "effect_type": "SET_DEATH_CAUSE",
                                "target_seat": 3,
                                "value": "wolf_kill",
                            },
                        ]
                    }
                ],
            },
        )
    else:
        data["action_windows"] = {}
        data["resolutions"] = ()
    if announced:
        data["events"] = (
            GameEvent.public(
                event_id=1,
                game_id=state.game_id,
                state_revision=1,
                round_no=0,
                phase=GamePhase.DAY_ANNOUNCE,
                created_at=NOW,
                event_type=EventType.ANNOUNCEMENT,
                eligible_seats=(1, 2, 3),
                payload=PublicAnnouncementPayload(content="昨夜死亡座位：3。"),
                correlation_id="day-announce-r0-d1",
            ),
        )
    return GameManager(GameState.model_validate(data), registry=load_action_registry())


@pytest.mark.asyncio
async def test_first_night_death_can_campaign_and_vote_before_announcement() -> None:
    manager = _first_night_manager()
    board = _board()

    assert first_day_sheriff_participants(manager.state) == (1, 2, 3)
    started = await manager.start_sheriff_election(board, candidates=(3,))
    speech = await manager.submit_sheriff_speech(
        SheriffCampaignSpeechRequest(
            request_id="speech-3",
            game_id=started.game_id,
            day_no=1,
            seat=3,
            session_epoch=0,
            observation_revision=started.state_revision,
            text="3号上警。",
        ),
        expected_revision=started.state_revision,
    )
    window = build_sheriff_vote_window(
        game_id=speech.game_id,
        day_no=1,
        observation_revision=speech.state_revision,
        session_epoch=0,
        eligible_voters=(1, 2, 3),
        candidates=(3,),
        vote_weights={1: 1.0, 2: 1.0, 3: 1.0},
        allow_abstain=True,
    )
    opened = await manager.open_sheriff_vote_window(
        window,
        expected_revision=speech.state_revision,
    )
    await manager.submit_sheriff_vote(
        VoteRequest(
            request_id="vote-3",
            game_id=opened.game_id,
            window_id=window.window_id,
            seat=3,
            session_epoch=0,
            observation_revision=window.observation_revision,
            target_seat=3,
        ),
        expected_revision=opened.state_revision,
    )


@pytest.mark.asyncio
async def test_first_day_exception_requires_provenance_and_ends_at_announcement() -> None:
    board = _board()
    with pytest.raises(SheriffElectionError, match="CANDIDATE_NOT_ELIGIBLE"):
        await _first_night_manager(with_provenance=False).start_sheriff_election(
            board, candidates=(3,)
        )
    with pytest.raises(SheriffElectionError, match="CANDIDATE_NOT_ELIGIBLE"):
        await _first_night_manager(announced=True).start_sheriff_election(board, candidates=(3,))


@pytest.mark.asyncio
async def test_dead_first_night_seat_is_still_rejected_by_ordinary_vote() -> None:
    manager = _first_night_manager()
    current = manager.state
    window = build_sheriff_vote_window(
        game_id=current.game_id,
        day_no=1,
        observation_revision=current.state_revision,
        session_epoch=0,
        eligible_voters=(1, 2, 3),
        candidates=(1, 2),
        vote_weights={1: 1.0, 2: 1.0, 3: 1.0},
        allow_abstain=True,
        window_id="ordinary-vote-r0",
    )
    data = json.loads(current.model_dump_json(warnings=False))
    data["phase"] = GamePhase.VOTE
    ordinary = GameManager(
        GameState.model_validate_json(json.dumps(data)),
        registry=load_action_registry(),
    )
    with pytest.raises(VoteError, match="SEAT_NOT_ELIGIBLE"):
        await ordinary.open_vote_window(window)


def test_skill_status_masks_only_provenance_bound_unannounced_death() -> None:
    manager = _first_night_manager()
    state = manager.state
    masked = project_skill_status(
        state,
        game_id=state.game_id,
        snapshot_id=state.ruleset.snapshot_id,  # type: ignore[union-attr]
        seat=3,
        session_epoch=0,
    )
    assert masked["alive"] is True
    assert masked["death_cause"] is None

    unknown = _first_night_manager(with_provenance=False).state
    unmasked = project_skill_status(
        unknown,
        game_id=unknown.game_id,
        snapshot_id=unknown.ruleset.snapshot_id,  # type: ignore[union-attr]
        seat=3,
        session_epoch=0,
    )
    assert unmasked["alive"] is False
    assert unmasked["death_cause"] == "wolf_kill"


def test_skill_status_projects_only_the_bound_sheriff_badge_window() -> None:
    manager = _manager()
    current = manager.state
    players = dict(current.players)
    players[1] = players[1].model_copy(update={"alive": False, "can_vote": False})
    data = current.model_dump(mode="python")
    data["phase"] = GamePhase.DAY_RESOLVE
    data["players"] = players
    data["sheriff_seat"] = 1
    data["sheriff_badge"] = {
        "status": "OPEN",
        "source_seat": 1,
        "source_session_epoch": 0,
        "office_seat": 1,
        "window_id": "badge-window",
        "candidate_seats": [2],
    }
    data["action_windows"] = {
        "badge-window": {
            "window_id": "badge-window",
            "game_id": current.game_id,
            "session_epoch": 0,
            "phase": GamePhase.DAY_RESOLVE,
            "allowed_seats": [1],
            "allowed_action_codes": [201, 202],
            "min_actions": 1,
            "max_actions": 1,
            "allow_pass": False,
            "visible_context": {
                "kind": "sheriff_badge",
                "source_seat": 1,
                "candidate_seats": [2],
            },
        }
    }
    state = current.model_validate(data)

    status = project_skill_status(
        state,
        game_id=state.game_id,
        snapshot_id=state.ruleset.snapshot_id,  # type: ignore[union-attr]
        seat=1,
        session_epoch=0,
    )
    other_status = project_skill_status(
        state,
        game_id=state.game_id,
        snapshot_id=state.ruleset.snapshot_id,  # type: ignore[union-attr]
        seat=2,
        session_epoch=0,
    )

    assert status["windows"] == [
        {
            "window_id": "badge-window",
            "phase": "DAY_RESOLVE",
            "kind": "SHERIFF_BADGE",
            "allowed_action_codes": [201, 202],
            "allow_pass": False,
            "candidate_seats": [2],
            "dependencies_satisfied": True,
        }
    ]
    assert other_status["windows"] == []


def test_skill_status_rejects_a_forged_badge_marker_or_window_binding() -> None:
    manager = _manager()
    current = manager.state
    data = current.model_dump(mode="python")
    data["sheriff_seat"] = 1
    data["sheriff_badge"] = {
        "status": "OPEN",
        "source_seat": 1,
        "source_session_epoch": 0,
        "office_seat": 1,
        "window_id": "other-window",
        "candidate_seats": [2],
    }
    data["action_windows"] = {
        "forged-window": {
            "window_id": "forged-window",
            "game_id": current.game_id,
            "session_epoch": 0,
            "phase": GamePhase.DAY_ANNOUNCE,
            "allowed_seats": [1],
            "allowed_action_codes": [201, 202],
            "visible_context": {
                "kind": "sheriff_badge",
                "source_seat": 1,
                "candidate_seats": [2],
            },
        }
    }
    state = current.model_validate(data)

    status = project_skill_status(
        state,
        game_id=state.game_id,
        snapshot_id=state.ruleset.snapshot_id,  # type: ignore[union-attr]
        seat=1,
        session_epoch=0,
    )

    assert status["windows"] == []
