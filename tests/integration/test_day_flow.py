import json
from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionWindow,
    DayCoordinator,
    GameManager,
    GameState,
    PlayerState,
    RulesetRef,
    TieAction,
    TieDecision,
    VoteRequest,
    VoteState,
    load_action_registry,
)
from werewolf.knowledge.board import BoardDefinition
from werewolf.runtime.player_runtime import InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _board(*, pk_enabled: bool = True) -> BoardDefinition:
    return BoardDefinition.model_validate(
        {
            "schema_version": 1,
            "kind": "board",
            "id": "day-flow-board",
            "version": "1.0.0",
            "name": "白天流程测试板",
            "aliases": [],
            "locale": "zh-CN",
            "status": "published",
            "reviewed_by": "GM",
            "reviewed_at": "2026-09-27",
            "summary": "用于白天协调器集成测试。",
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
            "night_windows": [
                {"window_id": "wolf_team_chat", "phase": "NIGHT_TEAM_CHAT"},
            ],
            "day_flow": {
                "vote": {
                    "visibility_during_collection": "secret",
                    "reveal_after_close": "totals_only",
                    "tie_policy": "pk_then_no_exile_on_retie" if pk_enabled else "no_exile_on_tie",
                },
                "pk": {"enabled": pk_enabled, "candidate_count": 2},
            },
            "mechanics": [],
            "interactions": [],
            "reading_plan": {
                "board_ref": {"id": "day-flow-board", "version": "1.0.0"},
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


def _state() -> GameState:
    return GameState(
        game_id="day-flow-game",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.DAY_ANNOUNCE,
        day_no=1,
        ruleset=RulesetRef(
            board_id="day-flow-board",
            version="1.0.0",
            snapshot_id="day-flow-snapshot",
            manifest_sha256="a" * 64,
        ),
        players={
            seat: PlayerState(
                seat=seat,
                role_id="wolf" if seat <= 2 else "villager",
                faction_id="wolf" if seat <= 2 else "town",
            )
            for seat in range(1, 5)
        },
    )


async def _runtimes() -> dict[int, ScriptedRuntime]:
    runtimes: dict[int, ScriptedRuntime] = {}
    for seat in range(1, 5):
        runtime = ScriptedRuntime()
        await runtime.start(
            RuntimeConfig(session_id=f"day-session-{seat}"),
            InitialContext(game_id="day-flow-game", seat=seat, session_epoch=0),
        )
        runtimes[seat] = runtime
    return runtimes


def _vote_request(progress: object, seat: int, target: int, suffix: str) -> VoteRequest:
    window = progress.window
    return VoteRequest(
        request_id=f"{window.window_id}-{suffix}-{seat}",
        game_id=window.game_id,
        window_id=window.window_id,
        seat=seat,
        session_epoch=window.session_epoch,
        observation_revision=window.observation_revision,
        target_seat=target,
    )


@pytest.mark.asyncio
async def test_day_coordinator_runs_announce_speech_vote_resolve_and_victory_boundary() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    coordinator = DayCoordinator(manager, _board(pk_enabled=False), await _runtimes())

    await coordinator.announce("昨夜平安无事。", now=NOW)
    await coordinator.open_speech()
    for _ in range(4):
        await coordinator.run_next_speech()
    assert (await manager.snapshot()).phase is GamePhase.DAY_SPEECH
    await coordinator.advance_from_speech(now=NOW)

    progress = await coordinator.open_vote(now=NOW)
    for seat in progress.window.eligible_voters:
        await coordinator.submit_vote(_vote_request(progress, seat, 3, "normal"), now=NOW)
    await coordinator.finalize_vote(now=NOW)
    state = await coordinator.confirm_vote(now=NOW)
    assert state.phase is GamePhase.DAY_RESOLVE
    # A vote result is a moderator-visible fact; exile effects are a separate
    # board resolution and must not be guessed by the generic coordinator.
    assert state.players[3].alive is True
    assert (await coordinator.finish_resolution(now=NOW)).phase is GamePhase.VICTORY_CHECK


@pytest.mark.asyncio
async def test_day_coordinator_routes_board_declared_pk_and_reopens_vote_window() -> None:
    manager = GameManager(_state(), registry=load_action_registry())

    def tie_policy(window: object, tally: object) -> TieDecision:
        del window
        candidates = tuple(tally.top_candidates)
        return TieDecision(action=TieAction.PK, candidates=candidates)

    coordinator = DayCoordinator(
        manager,
        _board(pk_enabled=True),
        await _runtimes(),
        tie_resolver=tie_policy,
    )
    await coordinator.announce(now=NOW)
    await coordinator.open_speech()
    for _ in range(4):
        await coordinator.run_next_speech()
    await coordinator.advance_from_speech(now=NOW)

    progress = await coordinator.open_vote(now=NOW)
    targets = (3, 4, 3, 4)
    for seat, target in zip(progress.window.eligible_voters, targets, strict=True):
        await coordinator.submit_vote(_vote_request(progress, seat, target, "first"), now=NOW)
    await coordinator.finalize_vote(now=NOW)
    assert (await coordinator.confirm_vote(now=NOW)).phase is GamePhase.VOTE_PK_SPEECH

    await coordinator.open_speech(is_pk=True)
    for _ in range(2):
        await coordinator.run_next_speech()
    await coordinator.advance_from_pk_speech(now=NOW)
    pk = await coordinator.open_vote(is_pk=True, now=NOW)
    for seat in pk.window.eligible_voters:
        await coordinator.submit_vote(_vote_request(pk, seat, 3, "pk"), now=NOW)
    await coordinator.finalize_vote(now=NOW)
    assert (await coordinator.confirm_vote(now=NOW)).phase is GamePhase.DAY_RESOLVE


@pytest.mark.asyncio
async def test_board_tie_policy_drives_pk_and_unique_revote_without_injected_resolver() -> None:
    """The frozen board policy supplies both ordinary and PK tie rounds."""

    manager = GameManager(_state(), registry=load_action_registry())
    coordinator = DayCoordinator(manager, _board(pk_enabled=True), await _runtimes())
    await coordinator.announce(now=NOW)
    await coordinator.open_speech()
    for _ in range(4):
        await coordinator.run_next_speech()
    await coordinator.advance_from_speech(now=NOW)

    first = await coordinator.open_vote(now=NOW)
    for seat, target in zip(first.window.eligible_voters, (3, 4, 3, 4), strict=True):
        await coordinator.submit_vote(_vote_request(first, seat, target, "default-first"), now=NOW)
    await coordinator.finalize_vote(now=NOW)
    assert (await coordinator.confirm_vote(now=NOW)).phase is GamePhase.VOTE_PK_SPEECH

    await coordinator.open_speech(is_pk=True)
    for _ in range(2):
        await coordinator.run_next_speech()
    await coordinator.advance_from_pk_speech(now=NOW)

    pk = await coordinator.open_vote(is_pk=True, now=NOW)
    for seat in pk.window.eligible_voters:
        await coordinator.submit_vote(_vote_request(pk, seat, 3, "default-pk"), now=NOW)
    await coordinator.finalize_vote(now=NOW)
    resolved = await coordinator.confirm_vote(now=NOW)
    assert resolved.phase is GamePhase.DAY_RESOLVE
    vote_state = VoteState.model_validate_json(json.dumps(resolved.vote_state))
    assert vote_state.public_result is not None
    assert vote_state.public_result.eliminated_seat == 3


@pytest.mark.asyncio
async def test_board_tie_policy_ends_repeated_pk_tie_with_no_exile() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    coordinator = DayCoordinator(manager, _board(pk_enabled=True), await _runtimes())
    await coordinator.announce(now=NOW)
    await coordinator.open_speech()
    for _ in range(4):
        await coordinator.run_next_speech()
    await coordinator.advance_from_speech(now=NOW)

    first = await coordinator.open_vote(now=NOW)
    for seat, target in zip(first.window.eligible_voters, (3, 4, 3, 4), strict=True):
        await coordinator.submit_vote(_vote_request(first, seat, target, "retie-first"), now=NOW)
    await coordinator.finalize_vote(now=NOW)
    assert (await coordinator.confirm_vote(now=NOW)).phase is GamePhase.VOTE_PK_SPEECH

    await coordinator.open_speech(is_pk=True)
    for _ in range(2):
        await coordinator.run_next_speech()
    await coordinator.advance_from_pk_speech(now=NOW)

    pk = await coordinator.open_vote(is_pk=True, now=NOW)
    for seat, target in zip(pk.window.eligible_voters, (3, 4, 3, 4), strict=True):
        await coordinator.submit_vote(_vote_request(pk, seat, target, "retie-pk"), now=NOW)
    await coordinator.finalize_vote(now=NOW)
    resolved = await coordinator.confirm_vote(now=NOW)
    result = VoteState.model_validate_json(json.dumps(resolved.vote_state)).public_result
    assert resolved.phase is GamePhase.DAY_RESOLVE
    assert result is not None
    assert result.eliminated_seat is None
    assert all(player.alive for player in resolved.players.values())


def _night_announcement_state(*, round_no: int, cause: str, trigger: bool = False) -> GameState:
    """Build a compact post-night snapshot for dawn announcement coverage."""

    state = _state().model_copy(update={"round_no": round_no, "day_no": round_no + 1})
    action_window_id = "wolf_kill" if round_no == 0 else f"wolf_kill-r{round_no}"
    resolution_id = f"night-resolution-r{round_no}"
    target = 3
    players = dict(state.players)
    players[target] = players[target].model_copy(update={"alive": False, "death_cause": cause})
    action_window = ActionWindow(
        window_id=action_window_id,
        game_id=state.game_id,
        session_epoch=0,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_action_codes=(101,),
        min_actions=1,
        max_actions=1,
        opened_at=NOW,
        visible_context={"night_round": round_no},
    )
    action_resolution = {
        "resolution_id": resolution_id,
        "window_id": action_window_id,
        "actions": (
            {
                "effects": (
                    {"effect_type": "SET_ALIVE", "target_seat": target, "value": False},
                    {
                        "effect_type": "SET_DEATH_CAUSE",
                        "target_seat": target,
                        "value": cause,
                    },
                )
            },
        ),
    }
    windows = {action_window_id: action_window.model_dump(mode="json")}
    resolutions: list[dict[str, object]] = [action_resolution]
    if trigger:
        trigger_window_id = f"{resolution_id}-trigger-hunter"
        trigger_window = ActionWindow(
            window_id=trigger_window_id,
            game_id=state.game_id,
            session_epoch=0,
            phase=GamePhase.TRIGGER_ACTION,
            allowed_seats=(3,),
            allowed_action_codes=(105, 299),
            min_actions=1,
            max_actions=1,
            allow_pass=True,
            opened_at=NOW,
            visible_context={
                "operation": "NIGHT_RESOLUTION",
                "resolution_id": resolution_id,
            },
        )
        windows[trigger_window_id] = trigger_window.model_dump(mode="json")
        players[4] = players[4].model_copy(update={"alive": False, "death_cause": "hunter_shot"})
        resolutions.append(
            {
                "resolution_id": "hunter-trigger-resolution",
                "window_id": trigger_window_id,
                "actions": (
                    {
                        "effects": (
                            {"effect_type": "SET_ALIVE", "target_seat": 4, "value": False},
                            {
                                "effect_type": "SET_DEATH_CAUSE",
                                "target_seat": 4,
                                "value": "hunter_shot",
                            },
                        )
                    },
                ),
            }
        )
    return state.model_copy(
        update={
            "phase": GamePhase.DAY_ANNOUNCE,
            "players": players,
            "action_windows": windows,
            "resolutions": tuple(resolutions),
        }
    )


@pytest.mark.asyncio
async def test_announcement_includes_second_night_death_with_first_night_last_words() -> None:
    board = _board().model_copy(
        update={
            "day_flow": _board().day_flow.model_copy(
                update={
                    "announce_deaths": True,
                    "last_words": _board().day_flow.last_words.model_copy(
                        update={"enabled": True, "night_death_policy": "first_night_only"}
                    ),
                }
            )
        }
    )
    state = _night_announcement_state(round_no=1, cause="wolf_kill")
    manager = GameManager(state, registry=load_action_registry())

    announced = await DayCoordinator(manager, board).commit_announcement(now=NOW)

    event = announced.events[-1]
    assert event.payload.content == "第 2 天开始。昨夜死亡座位：3。"


@pytest.mark.asyncio
async def test_announcement_includes_first_night_poison_death_without_granting_last_words() -> None:
    board = _board().model_copy(
        update={
            "day_flow": _board().day_flow.model_copy(
                update={
                    "announce_deaths": True,
                    "last_words": _board().day_flow.last_words.model_copy(
                        update={"enabled": True, "eligible_death_causes": ["night_kill"]}
                    ),
                }
            )
        }
    )
    state = _night_announcement_state(round_no=0, cause="witch_poison")
    manager = GameManager(state, registry=load_action_registry())

    announced = await DayCoordinator(manager, board).commit_announcement(now=NOW)

    assert announced.events[-1].payload.content == "第 1 天开始。昨夜死亡座位：3。"


@pytest.mark.asyncio
async def test_announcement_includes_current_night_hunter_shot_and_excludes_historical_night() -> (
    None
):
    board = _board()
    state = _night_announcement_state(round_no=1, cause="wolf_kill", trigger=True)
    manager = GameManager(state, registry=load_action_registry())

    announced = await DayCoordinator(manager, board).commit_announcement(now=NOW)

    assert announced.events[-1].payload.content == "第 2 天开始。昨夜死亡座位：3、4。"


@pytest.mark.asyncio
async def test_announcement_does_not_reannounce_an_older_night_resolution() -> None:
    board = _board()
    state = _night_announcement_state(round_no=1, cause="wolf_kill")
    old_window = dict(state.action_windows["wolf_kill-r1"])
    old_window["visible_context"] = {"night_round": 0}
    historical = state.resolutions[0]
    manager = GameManager(
        state.model_copy(
            update={
                "action_windows": {"wolf_kill-r1": old_window},
                "resolutions": (historical,),
            }
        ),
        registry=load_action_registry(),
    )

    announced = await DayCoordinator(manager, board).commit_announcement(now=NOW)

    assert announced.events[-1].payload.content == "第 2 天开始。昨夜平安。"
