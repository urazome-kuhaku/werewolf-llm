from __future__ import annotations

import json

import pytest
from test_sheriff_flow import _board, _manager

from werewolf.domain.enums import GamePhase
from werewolf.game import GameManager, SheriffElectionError, load_action_registry
from werewolf.moderator import ModeratorBadgeError, ModeratorSheriffBadgeFlow, ModeratorSheriffFlow
from werewolf.runtime.player_runtime import (
    Action,
    ActionResponse,
    InitialContext,
    RuntimeConfig,
    Speech,
    SpeechResponse,
    TurnRequest,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime


async def _runtimes(*, pk: bool = False) -> dict[int, ScriptedRuntime]:
    runtimes: dict[int, ScriptedRuntime] = {}
    for seat in (1, 2, 3):

        def response(request: TurnRequest, *, seat: int = seat) -> object:
            if request.expected_kind.value == "speech":
                return SpeechResponse(
                    request_id=request.request_id,
                    speech=Speech(text=f"{seat}号竞选发言。"),
                )
            window_id = request.action_window.window_id if request.action_window else ""
            if pk and window_id.endswith("-pk1"):
                if seat == 3:
                    return ActionResponse(
                        request_id=request.request_id,
                        actions=[Action(action_code=202, targets=[])],
                    )
                target = 2
            else:
                if pk and seat == 3:
                    return ActionResponse(
                        request_id=request.request_id,
                        actions=[Action(action_code=202, targets=[])],
                    )
                target = 2 if (not pk or seat == 1) else 1
            return ActionResponse(
                request_id=request.request_id,
                actions=[Action(action_code=201, targets=[target])],
            )

        runtime = ScriptedRuntime([response] * 8)
        await runtime.start(
            RuntimeConfig(session_id=f"moderator-sheriff-{seat}"),
            InitialContext(game_id="sheriff-manager-game", seat=seat, session_epoch=0),
        )
        runtimes[seat] = runtime
    return runtimes


@pytest.mark.asyncio
async def test_moderator_sheriff_flow_runs_speech_vote_confirm_and_transfer() -> None:
    manager = _manager()
    flow = ModeratorSheriffFlow(manager, _board(), await _runtimes())

    started = await flow.start((1, 2))
    assert started.phase is GamePhase.SHERIFF_ELECTION_SPEECH

    await flow.speech_next()
    await flow.speech_next()
    opened = await flow.open_vote()
    assert opened.phase is GamePhase.SHERIFF_ELECTION

    for seat in (1, 2, 3):
        await flow.vote_next(seat)
    progress = flow.progress()
    encoded = json.dumps(progress, ensure_ascii=False)
    assert "ballots" not in encoded
    assert "target_seat" not in encoded
    assert progress["vote"]["submitted_count"] == 3  # type: ignore[index]

    pending = await flow.collect()
    assert pending.sheriff_election is not None
    assert pending.sheriff_election["decision"]["action"] == "ELECT"
    confirmed = await flow.confirm()
    assert confirmed.phase is GamePhase.SHERIFF_TRANSFER
    assert confirmed.sheriff_seat == 2
    assert (await flow.transfer()).phase is GamePhase.DAY_SPEECH


@pytest.mark.asyncio
async def test_moderator_sheriff_flow_runs_authoritative_pk_round() -> None:
    manager = _manager()
    board_data = _board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True).model_dump(
        mode="python"
    )
    board_data["day_flow"]["vote"]["allow_abstain"] = True
    board = _board(tie_policy="pk_then_no_sheriff_on_retie", pk_enabled=True).model_validate(
        board_data
    )
    flow = ModeratorSheriffFlow(manager, board, await _runtimes(pk=True))
    await flow.start((1, 2))
    await flow.speech_next()
    await flow.speech_next()
    await flow.open_vote()

    for seat in (1, 2, 3):
        await flow.vote_next(seat)
    pending = await flow.collect()
    assert pending.phase is GamePhase.SHERIFF_ELECTION
    pk_speech = await flow.confirm()
    assert pk_speech.phase is GamePhase.SHERIFF_ELECTION_PK_SPEECH
    assert flow.progress()["election"]["tie_round"] == 1  # type: ignore[index]
    await flow.speech_next()
    await flow.speech_next()
    opened = await flow.open_vote()
    assert opened.sheriff_election["vote"]["window"]["window_id"] == "sheriff-vote-d1-pk1"
    for seat in (1, 2, 3):
        await flow.vote_next(seat)
    await flow.collect()
    confirmed = await flow.confirm()
    assert confirmed.phase is GamePhase.SHERIFF_TRANSFER
    assert confirmed.sheriff_seat == 2


def _dead_sheriff_day_speech_manager(*, election: bool) -> GameManager:
    base = _manager()
    players = dict(base.state.players)
    players[1] = players[1].model_copy(update={"alive": False, "can_vote": False})
    state = base.state.model_copy(
        update={
            "phase": GamePhase.DAY_SPEECH,
            "players": players,
            "sheriff_seat": 1,
            "sheriff_election": {"status": "RESOLVED"} if election else None,
        }
    )
    return GameManager(state, registry=load_action_registry())


def _badge_board():
    data = _board().model_dump(mode="python")
    data["day_flow"]["sheriff"].update(
        transfer_enabled=True,
        transfer_on_death=True,
        transfer_on_resignation=True,
    )
    return _board().model_validate(data)


async def _badge_runtime(manager: GameManager) -> ScriptedRuntime:
    def response(request):
        return ActionResponse(
            request_id=request.request_id,
            actions=[Action(action_code=201, targets=[2])],
        )

    runtime = ScriptedRuntime([response])
    await runtime.start(
        RuntimeConfig(session_id="badge-day-speech"),
        InitialContext(game_id=manager.state.game_id, seat=1, session_epoch=0),
    )
    return runtime


@pytest.mark.asyncio
async def test_dead_first_day_sheriff_badge_runs_through_manager_and_flow_in_day_speech() -> None:
    manager = _dead_sheriff_day_speech_manager(election=True)
    flow = ModeratorSheriffBadgeFlow(
        manager,
        _badge_board(),
        {1: await _badge_runtime(manager)},
    )

    opened = await flow.open()
    assert opened["phase"] == GamePhase.DAY_SPEECH.value
    assert opened["status"] == "OPEN"

    submitted = await flow.next()
    request_id = submitted["action"]["request_id"]  # type: ignore[index]
    decided = await flow.resolve(request_id=request_id)  # type: ignore[arg-type]
    assert decided.phase is GamePhase.DAY_SPEECH
    assert decided.sheriff_seat == 2
    assert decided.sheriff_badge is not None
    assert decided.sheriff_badge["status"] == "COMPLETE"

    finished = await flow.finish()
    assert finished.phase is GamePhase.DAY_SPEECH


@pytest.mark.asyncio
async def test_day_speech_badge_requires_first_day_election_context() -> None:
    manager = _dead_sheriff_day_speech_manager(election=False)
    flow = ModeratorSheriffBadgeFlow(
        manager,
        _board(),
        {1: await _badge_runtime(manager)},
    )

    with pytest.raises(ModeratorBadgeError, match="BADGE_PHASE_REQUIRED"):
        await flow.open()


@pytest.mark.asyncio
async def test_manager_rejects_badge_commit_and_finish_without_election_context() -> None:
    manager = _dead_sheriff_day_speech_manager(election=False)

    with pytest.raises(SheriffElectionError, match="PHASE_NOT_ALLOWED"):
        await manager.commit_sheriff_badge_decision(
            _badge_board(),
            request_id="badge-without-election",
        )
    with pytest.raises(SheriffElectionError, match="PHASE_NOT_ALLOWED"):
        await manager.complete_sheriff_badge()
