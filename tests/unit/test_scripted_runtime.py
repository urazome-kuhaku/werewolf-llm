"""Unit tests for deterministic multi-turn runtime behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.runtime.player_runtime import (
    ActionWindowView,
    Deadline,
    InitialContext,
    Observation,
    ResponseKind,
    RuntimeConfig,
    RuntimeLifecycleError,
    RuntimeProtocolError,
    RuntimeRequestMismatchError,
    RuntimeResponseValidationError,
    TurnRequest,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime


def _request(
    request_id: str,
    kind: ResponseKind,
    phase: GamePhase,
    *,
    logical_request_id: str | None = None,
    action_window: ActionWindowView | None = None,
) -> TurnRequest:
    start = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
    return TurnRequest(
        request_id=request_id,
        logical_request_id=logical_request_id or request_id,
        attempt_no=1,
        game_id="game-1",
        session_epoch=2,
        phase=phase,
        expected_kind=kind,
        action_window=action_window,
        observation=Observation(),
        output_schema={"type": "object"},
        deadline=Deadline(soft_deadline=start, hard_deadline=start + timedelta(seconds=30)),
    )


async def _started(script: list[object] | None = None) -> ScriptedRuntime:
    runtime = ScriptedRuntime(script or [])
    await runtime.start(
        RuntimeConfig(session_id="session-1"),
        InitialContext(game_id="game-1", seat=4, session_epoch=2),
    )
    return runtime


@pytest.mark.asyncio
async def test_scripted_runtime_completes_prepare_speech_vote_round() -> None:
    runtime = await _started()
    prepare = _request("prepare-1", ResponseKind.READY, GamePhase.PLAYER_PREPARE)
    speech = _request(
        "speech-1",
        ResponseKind.SPEECH,
        GamePhase.DAY_SPEECH,
        logical_request_id="speech-round-1",
    )
    vote = _request(
        "vote-1",
        ResponseKind.ACTION,
        GamePhase.VOTE,
        logical_request_id="vote-round-1",
        action_window=ActionWindowView(
            window_id="vote-window-1",
            allowed_action_codes=[201],
            candidate_seats=[2, 7],
        ),
    )

    ready = await runtime.run_turn(prepare)
    spoken = await runtime.run_turn(speech)
    voted = await runtime.run_turn(vote)

    assert ready.response.kind == "ready"
    assert spoken.response.kind == "speech"
    assert voted.response.kind == "action"
    assert voted.response.actions[0].action_code == 201
    assert voted.response.actions[0].targets == [2]
    assert [request.request_id for request in runtime.requests] == [
        "prepare-1",
        "speech-1",
        "vote-1",
    ]


@pytest.mark.asyncio
async def test_scripted_runtime_preserves_explicit_request_id_errors() -> None:
    runtime = await _started(
        [
            {
                "schema_version": 1,
                "request_id": "old-request",
                "kind": "speech",
                "speech": {"text": "迟到回包"},
            }
        ]
    )
    request = _request("current-request", ResponseKind.SPEECH, GamePhase.DAY_SPEECH)

    with pytest.raises(RuntimeRequestMismatchError):
        await runtime.run_turn(request)
    assert runtime.remaining_script_items == 0


@pytest.mark.asyncio
async def test_scripted_runtime_rejects_malformed_response_and_kind_mismatch() -> None:
    malformed = await _started([{"schema_version": 1, "request_id": "speech-1"}])
    request = _request("speech-1", ResponseKind.SPEECH, GamePhase.DAY_SPEECH)
    with pytest.raises(RuntimeResponseValidationError):
        await malformed.run_turn(request)

    wrong_kind = await _started(
        [
            {
                "schema_version": 1,
                "request_id": "speech-1",
                "kind": "action",
                "actions": [{"action_code": 201}],
            }
        ]
    )
    with pytest.raises(RuntimeProtocolError, match="does not match"):
        await wrong_kind.run_turn(request)


@pytest.mark.asyncio
async def test_scripted_runtime_enforces_binding_and_close() -> None:
    runtime = ScriptedRuntime()
    with pytest.raises(RuntimeLifecycleError):
        runtime.get_session_ref()
    await runtime.start(
        RuntimeConfig(session_id="session-1"),
        InitialContext(game_id="game-1", seat=4, session_epoch=2),
    )
    foreign = _request("foreign", ResponseKind.SPEECH, GamePhase.DAY_SPEECH).model_copy(
        update={"game_id": "other-game"}
    )
    with pytest.raises(RuntimeProtocolError):
        await runtime.run_turn(foreign)
    with pytest.raises(RuntimeRequestMismatchError):
        await runtime.abort("foreign")
    await runtime.close("test complete")
    with pytest.raises(RuntimeLifecycleError):
        await runtime.run_turn(_request("after-close", ResponseKind.SPEECH, GamePhase.DAY_SPEECH))
