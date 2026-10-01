"""Unit tests for the shared player-runtime protocol."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.runtime.player_runtime import (
    Action,
    ActionResponse,
    ActionWindowView,
    Deadline,
    InitialContext,
    Observation,
    ReadyResponse,
    ResponseKind,
    RuntimeConfig,
    RuntimeTurnResult,
    TurnRequest,
    build_turn_response_schema,
    validate_turn_response,
)


def _deadline() -> Deadline:
    start = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
    return Deadline(soft_at=start, hard_at=start + timedelta(seconds=30))


def _request(
    *,
    request_id: str = "g001-r1-seat04-a1",
    expected_kind: ResponseKind = ResponseKind.SPEECH,
    action_window: ActionWindowView | None = None,
) -> TurnRequest:
    return TurnRequest(
        request_id=request_id,
        logical_request_id="g001-r1-seat04-speech",
        attempt_no=1,
        game_id="game-1",
        session_epoch=2,
        phase=GamePhase.DAY_SPEECH,
        expected_kind=expected_kind,
        action_window=action_window,
        observation=Observation(summary="visible events"),
        output_schema={"type": "object"},
        deadline=_deadline(),
    )


def test_turn_request_is_strict_and_reuses_game_phase() -> None:
    request = _request()

    assert request.schema_version == 1
    assert request.phase is GamePhase.DAY_SPEECH
    assert request.expected_kind is ResponseKind.SPEECH

    with pytest.raises(ValidationError):
        TurnRequest.model_validate({**request.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({**request.model_dump(), "attempt_no": "1"})


def test_turn_response_is_a_discriminated_union() -> None:
    response = validate_turn_response(
        {
            "schema_version": 1,
            "request_id": "g001-r1-seat04-a1",
            "kind": "action",
            "actions": [
                {"action_code": 201, "targets": [7], "parameters": {}},
            ],
        }
    )

    assert isinstance(response, ActionResponse)
    assert response.actions[0].action_code == 201

    with pytest.raises(ValidationError):
        validate_turn_response(
            {
                "schema_version": 1,
                "request_id": "g001-r1-seat04-a1",
                "kind": "speech",
                "speech": {"text": "发言"},
                "actions": [],
            }
        )
    with pytest.raises(ValidationError):
        validate_turn_response(
            {
                "schema_version": 1,
                "request_id": "g001-r1-seat04-a1",
                "kind": "unknown",
            }
        )


def test_turn_response_schema_is_complete_and_request_bound() -> None:
    ready_schema = build_turn_response_schema(ResponseKind.READY, "prepare-1")
    assert set(ready_schema["required"]) == {
        "schema_version",
        "request_id",
        "kind",
        "ready",
    }
    assert ready_schema["properties"]["schema_version"]["const"] == 1
    assert ready_schema["properties"]["kind"]["const"] == "ready"
    assert ready_schema["properties"]["request_id"]["const"] == "prepare-1"
    ready_payload = ready_schema["$defs"]["Ready"]["properties"]["knowledge_receipts"]
    assert ready_payload["type"] == "array"
    assert ready_payload["minItems"] == 1

    ready = validate_turn_response(
        {
            "schema_version": 1,
            "request_id": "prepare-1",
            "kind": "ready",
            "ready": {"knowledge_receipts": ["receipt-board", "receipt-role"]},
        }
    )
    assert isinstance(ready, ReadyResponse)

    with pytest.raises(ValidationError):
        validate_turn_response(
            {
                "schema_version": 1,
                "kind": "ready",
                "ready": {"knowledge_receipts": ["receipt-board"]},
            }
        )


def test_action_window_and_result_recheck_shape() -> None:
    window = ActionWindowView(
        window_id="vote-r1",
        allowed_action_codes=[201],
        candidate_seats=[2, 7],
    )
    request = _request(
        request_id="g001-r1-seat04-vote-a1",
        expected_kind=ResponseKind.ACTION,
        action_window=window,
    )
    response = ActionResponse(
        request_id=request.request_id,
        actions=[Action(action_code=201, targets=[7])],
    )
    result = RuntimeTurnResult(
        request_id=request.request_id,
        logical_request_id=request.logical_request_id,
        attempt_no=request.attempt_no,
        response=response,
    )

    assert result.response is response
    with pytest.raises(ValidationError, match="response.request_id"):
        RuntimeTurnResult(
            request_id=request.request_id,
            logical_request_id=request.logical_request_id,
            attempt_no=1,
            response=response.model_copy(update={"request_id": "other"}),
        )


def test_deadline_rejects_reversed_bounds() -> None:
    start = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="soft_deadline"):
        Deadline(soft_deadline=start + timedelta(seconds=1), hard_deadline=start)


def test_protocol_config_and_context_are_strict() -> None:
    config = RuntimeConfig(session_id="session-1", model="test-model")
    context = InitialContext(game_id="game-1", seat=4, session_epoch=2)

    assert config.session_id == "session-1"
    assert context.seat == 4
    with pytest.raises(ValidationError):
        InitialContext(game_id="game-1", seat=True, session_epoch=2)
