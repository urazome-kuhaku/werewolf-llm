import json
from datetime import UTC, datetime

import pytest
from test_day_flow import _board, _state
from test_moderator_start import _ReadingRuntime
from test_sheriff_flow import (
    _board as _sheriff_board,
)
from test_sheriff_flow import (
    _manager as _sheriff_manager,
)
from test_sheriff_flow import (
    _open_vote as _open_sheriff_vote,
)

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    DayCoordinator,
    DayCoordinatorError,
    GameManager,
    SheriffCampaignSpeechRequest,
    SheriffVoteTurnScheduler,
    VoteTurnError,
    build_sheriff_vote_window,
    load_action_registry,
)
from werewolf.moderator import ModeratorShell
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import (
    Action,
    ActionResponse,
    InitialContext,
    RuntimeConfig,
    TurnRequest,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 28, tzinfo=UTC)


async def _runtimes(*, invalid_first_for: int | None = None) -> dict[int, ScriptedRuntime]:
    runtimes: dict[int, ScriptedRuntime] = {}
    for seat in range(1, 5):
        script: list[object] = []
        if seat == invalid_first_for:
            script.extend(
                [
                    lambda request: ActionResponse(
                        request_id=request.request_id,
                        actions=[Action(action_code=201, targets=[99])],
                    ),
                    lambda request: ActionResponse(
                        request_id=request.request_id,
                        actions=[Action(action_code=201, targets=[2])],
                    ),
                ]
            )
        runtime = ScriptedRuntime(script)
        await runtime.start(
            RuntimeConfig(session_id=f"vote-session-{seat}"),
            InitialContext(game_id="day-flow-game", seat=seat, session_epoch=0),
        )
        runtimes[seat] = runtime
    return runtimes


async def _sheriff_runtimes(*, invalid_first_for: int | None = None) -> dict[int, ScriptedRuntime]:
    runtimes: dict[int, ScriptedRuntime] = {}
    for seat in range(1, 4):

        def response(request: TurnRequest, *, seat: int = seat) -> ActionResponse:
            window_id = request.observation.payload["window_id"]
            if window_id == "sheriff-vote-d1" and seat == 3:
                return ActionResponse(
                    request_id=request.request_id,
                    actions=[Action(action_code=202, targets=[])],
                )
            target = 2 if window_id == "sheriff-vote-d1-pk" else seat
            return ActionResponse(
                request_id=request.request_id,
                actions=[Action(action_code=201, targets=[target])],
            )

        script: list[object] = [response, response]
        if seat == invalid_first_for:

            def invalid_response(request: TurnRequest) -> ActionResponse:
                return ActionResponse(
                    request_id=request.request_id,
                    actions=[Action(action_code=201, targets=[99])],
                )

            script.insert(
                0,
                invalid_response,
            )
        runtime = ScriptedRuntime(script)
        await runtime.start(
            RuntimeConfig(session_id=f"sheriff-vote-session-{seat}"),
            InitialContext(game_id="sheriff-manager-game", seat=seat, session_epoch=0),
        )
        runtimes[seat] = runtime
    return runtimes


@pytest.mark.asyncio
async def test_vote_turn_collects_one_runtime_ballot_per_seat_and_retries_invalid_target() -> None:
    manager = GameManager(
        _state().model_copy(update={"phase": GamePhase.VOTE}),
        registry=load_action_registry(),
    )
    runtimes = await _runtimes(invalid_first_for=1)
    coordinator = DayCoordinator(manager, _board(pk_enabled=False), runtimes)

    progress = await coordinator.open_vote(now=NOW)
    with pytest.raises(DayCoordinatorError, match="TARGET_NOT_ALLOWED"):
        await coordinator.run_next_vote(seat=1)

    retried = await coordinator.retry_vote(seat=1)
    assert retried.request.attempt_no == 2
    assert retried.request.action_window is not None
    assert retried.request.action_window.candidate_seats == [1, 2, 3, 4]
    assert (
        retried.request.observation.payload["observation_revision"]
        == progress.window.observation_revision
    )
    assert "ballots" not in retried.request.observation.payload

    # A second request for the accepted seat cannot create a second ballot.
    with pytest.raises(DayCoordinatorError, match="VOTE_ALREADY_SUBMITTED"):
        await coordinator.run_next_vote(seat=1)

    for seat in (2, 3, 4):
        result = await coordinator.run_next_vote()
        assert result.request.observation.payload["seat"] == seat

    state = await manager.snapshot()
    assert state.vote_state is not None
    assert set(state.vote_state["ballots"]) == {"1", "2", "3", "4"}
    assert (await manager.vote_observation(1)).own_target_seat == 2


@pytest.mark.asyncio
async def test_sheriff_vote_turn_collects_private_runtime_ballots_and_retries() -> None:
    manager = _sheriff_manager()
    board = _sheriff_board()
    await manager.start_sheriff_election(board, candidates=(1, 2))
    await _open_sheriff_vote(manager)
    runtimes = await _sheriff_runtimes()
    scheduler = SheriffVoteTurnScheduler(manager, runtimes)

    result = await scheduler.run_next(seat=1)
    prompt = json.loads(PiRuntime()._build_prompt_message(result.request))
    schema = prompt["output_schema"]
    assert schema["properties"]["request_id"]["const"] == result.request.request_id
    assert schema["properties"]["kind"]["const"] == "action"
    assert set(schema["required"]) >= {"schema_version", "request_id", "kind", "actions"}
    action_description = schema["$defs"]["Action"]["properties"]["action_code"]["description"]
    assert "targets has exactly one seat" in action_description
    assert result.request.phase is GamePhase.SHERIFF_ELECTION
    assert result.request.action_window is not None
    assert result.request.action_window.window_id == "sheriff-vote-d1"
    assert result.request.action_window.candidate_seats == [1, 2]
    assert "ballots" not in result.request.observation.payload

    for seat in (2, 3):
        await scheduler.run_next(seat=seat)

    state = await manager.snapshot()
    assert state.sheriff_election is not None
    assert set(state.sheriff_election["vote"]["ballots"]) == {"1", "2", "3"}


@pytest.mark.asyncio
async def test_sheriff_vote_turn_rejects_non_candidate_and_retries_same_seat() -> None:
    manager = _sheriff_manager()
    board = _sheriff_board()
    await manager.start_sheriff_election(board, candidates=(1, 2))
    await _open_sheriff_vote(manager)
    scheduler = SheriffVoteTurnScheduler(manager, await _sheriff_runtimes(invalid_first_for=1))

    with pytest.raises(VoteTurnError, match="TARGET_NOT_ALLOWED"):
        await scheduler.run_next(seat=1)
    retried = await scheduler.retry(seat=1)
    assert retried.request.attempt_no == 2

    for seat in (2, 3):
        await scheduler.run_next(seat=seat)


@pytest.mark.asyncio
async def test_sheriff_vote_turn_uses_a_new_runtime_window_for_pk_revote() -> None:
    manager = _sheriff_manager()
    board = _sheriff_board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True)
    await manager.start_sheriff_election(board, candidates=(1, 2))
    await _open_sheriff_vote(manager)
    runtimes = await _sheriff_runtimes()
    scheduler = SheriffVoteTurnScheduler(manager, runtimes)

    for seat in (1, 2, 3):
        await scheduler.run_next(seat=seat)
    current = await manager.snapshot()
    pending = await manager.finalize_sheriff_election(
        board, expected_revision=current.state_revision
    )
    pk_speech = await manager.confirm_sheriff_election(
        board, expected_revision=pending.state_revision
    )
    assert pk_speech.phase is GamePhase.SHERIFF_ELECTION_PK_SPEECH

    for seat in (1, 2):
        current = await manager.snapshot()
        await manager.submit_sheriff_speech(
            SheriffCampaignSpeechRequest(
                request_id=f"runtime-pk-speech-{seat}",
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
    window = build_sheriff_vote_window(
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
    opened = await manager.open_sheriff_vote_window(
        window, expected_revision=current.state_revision
    )
    assert opened.phase is GamePhase.SHERIFF_ELECTION_PK

    for seat in (1, 2, 3):
        result = await scheduler.run_next(seat=seat)
        assert result.request.phase is GamePhase.SHERIFF_ELECTION_PK
        assert result.request.action_window is not None
        assert result.request.action_window.window_id == "sheriff-vote-d1-pk"
        assert result.request.observation.payload["candidate_seats"] == [1, 2]

    assert all(len(runtime.requests) == 2 for runtime in runtimes.values())


@pytest.mark.asyncio
async def test_moderator_vote_next_and_retry_commands_use_runtime_boundary(tmp_path) -> None:
    # The command integration is intentionally exercised against the existing
    # one-seat moderator fixture; the scheduler test above covers multi-seat
    # collection and target rejection.
    from test_moderator_start import _compiled, _write_config

    compiled = await _compiled(tmp_path)
    config = tmp_path / "game.yaml"
    _write_config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(
        config,
        runtime_factory=lambda _player, gateway_url, token: _ReadingRuntime(gateway_url, token),
    )

    await shell.new()
    await shell.next()
    await shell.next()
    await shell.execute("start")
    await shell.execute("prepare next")
    await shell.next()
    await shell.next()
    await shell.next()
    await shell.next()
    await shell.execute("day announce")
    await shell.execute("day speech open")
    await shell.execute("day speech next")
    await shell.execute("day speech close")
    await shell.execute("day vote open")

    result = await shell.execute("day vote next")
    assert result is not None
    assert result["day"]["vote"]["window_id"] == "day-vote-r0-d1"  # type: ignore[index]
    await shell.execute("day vote collect")
