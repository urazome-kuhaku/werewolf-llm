"""Contract tests for the persistent Pi player runtime."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.runtime.pi_runtime import PiRuntime, PiRuntimeHardTimeout
from werewolf.runtime.player_runtime import (
    Deadline,
    InitialContext,
    Observation,
    ResponseKind,
    RuntimeConfig,
    RuntimeRequestMismatchError,
    TurnRequest,
    build_turn_response_schema,
)


def _message_end(text: str) -> dict[str, object]:
    return {
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


class _FakePi:
    started = False
    closed = False

    def __init__(self, *, response_request_id: str | None = None) -> None:
        self.records: asyncio.Queue[object | None] = asyncio.Queue()
        self.writes: list[dict[str, Any]] = []
        self.prompt_no = 0
        self.response_request_id = response_request_id

    async def start(self) -> _FakePi:
        self.started = True
        return self

    async def send_record(self, record: dict[str, Any]) -> None:
        self.writes.append(record)
        command = record["type"]
        if command in {
            "get_state",
            "set_steering_mode",
            "set_follow_up_mode",
            "set_auto_compaction",
            "set_auto_retry",
            "clear_queue",
            "abort",
        }:
            await self.records.put(
                {"type": "response", "id": record["id"], "command": command, "success": True}
            )
        elif command == "prompt":
            self.prompt_no += 1
            request = json.loads(record["message"])
            text = json.dumps(
                {
                    "schema_version": 1,
                    "request_id": self.response_request_id or request["request_id"],
                    "kind": "speech",
                    "speech": {"text": f"第{self.prompt_no}轮"},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await self.records.put(
                {"type": "response", "id": record["id"], "command": "prompt", "success": True}
            )
            await self.records.put({"type": "agent_start"})
            await self.records.put(_message_end(text))
            await self.records.put({"type": "agent_settled"})
        elif command == "get_last_assistant_text":
            message = self.writes[-2]["message"]
            payload = json.loads(message)
            text = json.dumps(
                {
                    "schema_version": 1,
                    "request_id": self.response_request_id or payload["request_id"],
                    "kind": "speech",
                    "speech": {"text": f"第{self.prompt_no}轮"},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await self.records.put(
                {
                    "type": "response",
                    "id": record["id"],
                    "command": command,
                    "success": True,
                    "data": {"text": text},
                }
            )
        elif command == "steer":
            await self.records.put(
                {"type": "response", "id": record["id"], "command": command, "success": True}
            )

    async def read_record(self) -> object | None:
        return await self.records.get()

    async def close(self, reason: str = "normal shutdown") -> None:
        self.closed = True


def _request(request_id: str, *, seconds: float = 10.0) -> TurnRequest:
    now = datetime.now(UTC)
    return TurnRequest(
        request_id=request_id,
        logical_request_id=request_id,
        attempt_no=1,
        game_id="game-1",
        session_epoch=2,
        phase=GamePhase.DAY_SPEECH,
        expected_kind=ResponseKind.SPEECH,
        observation=Observation(summary="公开观察"),
        output_schema=build_turn_response_schema(ResponseKind.SPEECH, request_id),
        deadline=Deadline(
            soft_deadline=now + timedelta(seconds=seconds / 2),
            hard_deadline=now + timedelta(seconds=seconds),
        ),
    )


async def _started(fake: _FakePi) -> PiRuntime:
    runtime = PiRuntime(process=fake, command_timeout_seconds=1)
    await runtime.start(
        RuntimeConfig(session_id="session-1"),
        InitialContext(game_id="game-1", seat=4, session_epoch=2),
    )
    return runtime


@pytest.mark.asyncio
async def test_persistent_session_handshakes_and_completes_two_turns() -> None:
    fake = _FakePi()
    runtime = await _started(fake)
    first = await runtime.run_turn(_request("turn-1"))
    second = await runtime.run_turn(_request("turn-2"))

    assert first.response.speech is not None
    assert second.response.speech is not None
    assert second.response.speech.text == "第2轮"
    assert [record["type"] for record in fake.writes[:5]] == [
        "get_state",
        "set_steering_mode",
        "set_follow_up_mode",
        "set_auto_compaction",
        "set_auto_retry",
    ]
    await runtime.close("test complete")
    assert fake.closed


def test_prompt_message_carries_complete_request_bound_schema() -> None:
    request = _request("turn-1")
    payload = json.loads(PiRuntime()._build_prompt_message(request))

    schema = payload["output_schema"]
    assert set(schema["required"]) == {"schema_version", "request_id", "kind", "speech"}
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["kind"]["const"] == "speech"
    assert schema["properties"]["request_id"]["const"] == "turn-1"
    assert payload["instruction"].startswith("Return exactly one complete JSON response object")
    assert "Set request_id exactly to the current request_id" in payload["instruction"]


@pytest.mark.asyncio
async def test_wrong_response_request_id_is_still_rejected() -> None:
    fake = _FakePi(response_request_id="other-turn")
    runtime = await _started(fake)

    with pytest.raises(RuntimeRequestMismatchError):
        await runtime.run_turn(_request("turn-1"))
    await runtime.close("test complete")


@pytest.mark.asyncio
async def test_wrong_rpc_response_is_discarded_and_never_submitted() -> None:
    fake = _FakePi()
    runtime = await _started(fake)
    original = fake.send_record

    async def send(record: dict[str, Any]) -> None:
        if record["type"] == "prompt":
            await fake.records.put(
                {"type": "response", "id": "stale", "command": "prompt", "success": True}
            )
        await original(record)

    fake.send_record = send  # type: ignore[method-assign]
    result = await runtime.run_turn(_request("turn-1"))
    assert result.response.request_id == "turn-1"
    await runtime.close("test complete")


@pytest.mark.asyncio
async def test_hard_timeout_does_not_advance_and_abort_requires_settle() -> None:
    fake = _FakePi()
    runtime = await _started(fake)

    async def hanging(record: dict[str, Any]) -> None:
        fake.writes.append(record)
        if record["type"] in {
            "get_state",
            "set_steering_mode",
            "set_follow_up_mode",
            "set_auto_compaction",
            "set_auto_retry",
        }:
            await fake.records.put(
                {"type": "response", "id": record["id"], "command": record["type"], "success": True}
            )
        elif record["type"] == "prompt":
            await fake.records.put(
                {"type": "response", "id": record["id"], "command": "prompt", "success": True}
            )
        elif record["type"] == "clear_queue":
            await fake.records.put(
                {"type": "response", "id": record["id"], "command": "clear_queue", "success": True}
            )
        elif record["type"] == "abort":
            await fake.records.put(
                {"type": "response", "id": record["id"], "command": "abort", "success": True}
            )
            await fake.records.put({"type": "agent_settled"})

    fake.send_record = hanging  # type: ignore[method-assign]
    with pytest.raises(PiRuntimeHardTimeout):
        await runtime.run_turn(_request("turn-1", seconds=0.05))

    await runtime.abort("turn-1")
    # The abort path may be observed by run_turn's pending consumer, but it
    # never yields a RuntimeTurnResult.
    assert not any(record.get("type") == "get_last_assistant_text" for record in fake.writes)
    await runtime.close("test complete")
