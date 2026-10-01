"""Focused contract coverage for the board-defined last-words boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionWindow,
    GameManager,
    GameState,
    PlayerState,
    RulesetRef,
    load_action_registry,
)
from werewolf.knowledge.board import BoardDefinition
from werewolf.moderator import LastWordsError, LastWordsFlow
from werewolf.runtime.player_runtime import InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def night_board() -> BoardDefinition:
    return BoardDefinition.model_validate(
        {
            "schema_version": 1,
            "kind": "board",
            "id": "fictional-board",
            "version": "1.0.0",
            "name": "遗言测试板",
            "aliases": [],
            "locale": "zh-CN",
            "status": "published",
            "reviewed_by": "GM",
            "reviewed_at": "2026-09-27",
            "summary": "用于遗言流程测试。",
            "seat_count": 3,
            "factions": {"town": 2, "wolf": 1},
            "roles": [
                {
                    "role_ref": {"id": "wolf", "version": "1.0.0"},
                    "count": 1,
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
            "knife_rule": {
                "selection_mode": "consensus",
                "target_visibility": "wolf_team",
                "available_after_window": "wolf_kill",
            },
            "night_windows": [
                {"window_id": "wolf_team_chat"},
                {"window_id": "wolf_kill"},
            ],
            "day_flow": {
                "announce_deaths": True,
                "vote": {
                    "visibility_during_collection": "secret",
                    "reveal_after_close": "ballots_and_totals",
                    "tie_policy": "pk_then_no_exile_on_retie",
                },
                # ``pk_then_no_exile_on_retie`` is only a valid contract
                # when the board enables a PK round.  Keep this fixture
                # internally consistent so failures exercise last-words
                # behavior rather than board validation.
                "pk": {"enabled": True},
                "last_words": {
                    "enabled": True,
                    "eligible_death_causes": ["night_kill", "day_exile"],
                },
            },
            "mechanics": [],
            "interactions": [],
            "reading_plan": {
                "board_ref": {"id": "fictional-board", "version": "1.0.0"},
                "bootstrap_topics": ["board:overview"],
                "role_required_topics": {},
                "phase_topics": {},
                "high_risk_topics": ["board:overview"],
                "suggested_queries": [],
            },
            "claim_refs": ["claim-test"],
            "source_refs": ["source-test"],
        }
    )


def night_state(*, phase: GamePhase = GamePhase.DAY_ANNOUNCE, round_no: int = 0) -> GameState:
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        round_no=round_no,
        day_no=round_no + 1,
        ruleset=RulesetRef(
            board_id="fictional-board",
            version="1.0.0",
            snapshot_id="ruleset-test",
            manifest_sha256="a" * 64,
        ),
        players={
            1: PlayerState(seat=1, role_id="wolf", faction_id="wolf", session_epoch=1),
            2: PlayerState(seat=2, role_id="villager", faction_id="town", session_epoch=1),
            3: PlayerState(seat=3, role_id="villager", faction_id="town", session_epoch=1),
        },
    )


def _night_dead_state(*, phase: GamePhase = GamePhase.DAY_ANNOUNCE, round_no: int = 0):
    state = night_state(phase=phase)
    dead = state.players[3].model_copy(update={"alive": False, "death_cause": "night_kill"})
    window = ActionWindow(
        window_id="wolf_kill",
        game_id=state.game_id,
        session_epoch=1,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_action_codes=(101,),
        min_actions=1,
        max_actions=1,
        opened_at=NOW,
        visible_context={"night_round": round_no},
    )
    return state.model_copy(
        update={
            "phase": phase,
            "round_no": round_no,
            "players": {
                seat: (dead if seat == 3 else player) for seat, player in state.players.items()
            },
            "action_windows": {"wolf_kill": window.model_dump(mode="json")},
            "resolutions": (
                {
                    "window_id": "wolf_kill",
                    "actions": (
                        {
                            "effects": (
                                {
                                    "effect_type": "SET_ALIVE",
                                    "target_seat": 3,
                                    "value": False,
                                },
                                {
                                    "effect_type": "SET_DEATH_CAUSE",
                                    "target_seat": 3,
                                    "value": "night_kill",
                                },
                            )
                        },
                    ),
                },
            ),
        }
    )


async def _runtime(seat: int, *, game_id: str = "game-1") -> ScriptedRuntime:
    runtime = ScriptedRuntime()
    await runtime.start(
        RuntimeConfig(session_id=f"last-words-{seat}"),
        InitialContext(game_id=game_id, seat=seat, session_epoch=1, role_id="villager"),
    )
    return runtime


def _manager(state):
    return GameManager(state, registry=load_action_registry())


@pytest.mark.asyncio
async def test_last_words_runs_for_current_real_night_death_and_deduplicates() -> None:
    state = _night_dead_state()
    manager = _manager(state)
    runtime = await _runtime(3)
    flow = LastWordsFlow(manager, night_board(), {3: runtime})

    result = await flow.next(3)

    assert result.event.payload.speaker_seat == 3
    assert "last-words" in result.request.logical_request_id
    assert flow.status()["pending_seats"] == []
    assert any(
        item.get("operation") == "LAST_WORDS_COMPLETE" for item in manager.state.moderator_audit
    )
    with pytest.raises(LastWordsError, match="LAST_WORDS_COMPLETE"):
        await flow.next(3)


@pytest.mark.asyncio
async def test_last_words_retries_active_request_without_duplicate_speech() -> None:
    class RetryRuntime(ScriptedRuntime):
        first = True

        async def run_turn(self, request):
            if self.first:
                self.first = False
                self._active_request_id = request.request_id
                self._requests.append(request)
                await asyncio.Future()
            return await super().run_turn(request)

        async def abort(self, request_id: str) -> None:
            await super().abort(request_id)
            self._active_request_id = None

    state = _night_dead_state()
    manager = _manager(state)
    runtime = RetryRuntime()
    await runtime.start(
        RuntimeConfig(session_id="last-words-retry"),
        InitialContext(game_id="game-1", seat=3, session_epoch=1, role_id="villager"),
    )
    flow = LastWordsFlow(manager, night_board(), {3: runtime}, timeout_seconds=0.01)

    with pytest.raises(LastWordsError, match="timed out"):
        await flow.next(3)
    result = await flow.retry(3)

    assert result.request.attempt_no == 2
    assert len([event for event in manager.state.events if event.channel.value == "PUBLIC"]) == 1
    assert flow.status()["pending_seats"] == []


def test_historical_night_resolution_is_excluded_from_current_last_words() -> None:
    state = _night_dead_state(round_no=1)
    state = state.model_copy(
        update={
            "action_windows": {
                "wolf_kill": {
                    **state.action_windows["wolf_kill"],
                    "visible_context": {"night_round": 0},
                }
            }
        }
    )
    manager = _manager(state)
    flow = LastWordsFlow(manager, night_board(), {})

    assert flow.status()["source"] is None
    assert flow.status()["pending_seats"] == []


def test_last_words_still_excludes_a_poison_death_when_announcement_includes_it() -> None:
    state = _night_dead_state()
    dead = state.players[3].model_copy(update={"death_cause": "witch_poison"})
    resolution = dict(state.resolutions[0])
    actions = list(resolution["actions"])
    actions[0] = {
        **actions[0],
        "effects": tuple(
            {
                **effect,
                "value": "witch_poison"
                if effect["effect_type"] == "SET_DEATH_CAUSE"
                else effect["value"],
            }
            for effect in actions[0]["effects"]
        ),
    }
    state = state.model_copy(
        update={
            "players": {**state.players, 3: dead},
            "resolutions": (resolution | {"actions": tuple(actions)},),
        }
    )
    manager = _manager(state)
    flow = LastWordsFlow(manager, night_board(), {})

    assert flow.status()["source"] is None
    assert flow.status()["pending_seats"] == []


def test_last_words_does_not_take_over_an_existing_ordinary_speech_queue() -> None:
    state = _night_dead_state(phase=GamePhase.DAY_SPEECH)
    state = state.model_copy(update={"current_queue": (1,)})
    manager = _manager(state)
    flow = LastWordsFlow(manager, night_board(), {})

    with pytest.raises(LastWordsError, match="LAST_WORDS_QUEUE_CONFLICT"):
        asyncio.run(flow.next(3))
    assert manager.state.current_queue == (1,)
