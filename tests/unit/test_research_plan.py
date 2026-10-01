"""Tests for deterministic board search plans and Agent Reach preflight."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from werewolf.ruleset_workbench.research_plan import (
    DEFAULT_DOCTOR_TIMEOUT_SECONDS,
    AgentReachDoctorConfig,
    AgentReachDoctorOutputLimitError,
    AgentReachDoctorResponseError,
    AgentReachDoctorTimeoutError,
    AgentReachTransport,
    InvalidResearchPlanError,
    SearchTopic,
    UnsupportedAgentReachBackendError,
    build_search_plan,
    deduplicate_query_texts,
    prepare_research_plan,
    run_agent_reach_doctor,
)


class FakeProcess:
    def __init__(self, output: bytes, *, returncode: int = 0) -> None:
        self.stdout = None
        self.stderr = None
        self.returncode = returncode
        self._output = output
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._output, b""

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class SlowProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(b"", returncode=None)  # type: ignore[arg-type]

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(60)
        return b"", b""

    async def wait(self) -> int:
        return 0


class DelayedProcess(FakeProcess):
    def __init__(self, output: bytes, *, delay_seconds: float) -> None:
        super().__init__(output, returncode=0)
        self.delay_seconds = delay_seconds

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(self.delay_seconds)
        return self._output, b""


def process_factory_for(
    process: FakeProcess,
) -> tuple[Callable[..., Any], list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def factory(*args: Any, **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return process

    return factory, calls


def doctor_wire(
    *,
    active_backend: str = "Exa via mcporter",
    available: bool | None = True,
    status: str | None = None,
    transport: str | None = None,
) -> bytes:
    search: dict[str, Any] = {"active_backend": active_backend}
    if available is not None:
        search["available"] = available
    if status is not None:
        search["status"] = status
    if transport is not None:
        search["transport"] = transport
    return json.dumps({"channels": {"search": search}}).encode()


def test_plan_is_deterministic_and_covers_required_dimensions() -> None:
    first = build_search_plan("  预女猎白\n")
    second = build_search_plan("预女猎白")

    assert first == second
    assert first.board_name == "预女猎白"
    assert len(first.queries) == 8
    assert [item.topic for item in first.queries] == list(SearchTopic)
    assert any("官方" in query for query in first.query_texts)
    assert any("角色技能" in query for query in first.query_texts)
    assert any("投票" in query for query in first.query_texts)
    assert all(item.max_results == 5 for item in first.queries)
    assert len(first.provider_queries) == len(first.queries)
    assert first.is_preflighted is False


def test_query_deduplication_normalizes_whitespace_and_case() -> None:
    assert deduplicate_query_texts(
        ["  Board  Rules ", "board rules", "投票", "投票", "  ", "夜间"],
    ) == ("Board Rules", "投票", "夜间")


def test_doctor_default_timeout_is_bounded_for_slow_agent_reach_startup() -> None:
    assert DEFAULT_DOCTOR_TIMEOUT_SECONDS == 90.0
    assert AgentReachDoctorConfig().timeout_seconds == DEFAULT_DOCTOR_TIMEOUT_SECONDS
    assert AgentReachDoctorConfig(timeout_seconds=1.25).timeout_seconds == 1.25


@pytest.mark.asyncio
async def test_explicit_doctor_timeout_remains_strict_while_default_allows_startup_delay() -> None:
    delayed_default = DelayedProcess(doctor_wire(), delay_seconds=0.02)
    default_factory, _ = process_factory_for(delayed_default)
    status = await run_agent_reach_doctor(process_factory=default_factory)
    assert status.ready is True

    delayed_custom = DelayedProcess(doctor_wire(), delay_seconds=0.02)
    custom_factory, _ = process_factory_for(delayed_custom)
    with pytest.raises(AgentReachDoctorTimeoutError, match="0.001s"):
        await run_agent_reach_doctor(
            timeout_seconds=0.001,
            process_factory=custom_factory,
        )


@pytest.mark.parametrize("value", ["", "   ", "a\x00b", "x" * 129])
def test_invalid_board_name_is_rejected(value: str) -> None:
    with pytest.raises((InvalidResearchPlanError, ValueError)):
        build_search_plan(value)


@pytest.mark.asyncio
async def test_doctor_uses_one_shell_free_call_and_accepts_agent_reach_label() -> None:
    process = FakeProcess(doctor_wire())
    factory, calls = process_factory_for(process)

    status = await run_agent_reach_doctor(
        executable="agent-reach.exe",
        process_factory=factory,
    )

    assert status.active_backend == "Exa via mcporter"
    assert status.available is True
    assert status.transport is AgentReachTransport.MCPORTER_HTTPS
    assert status.ready is True
    args, kwargs = calls[0]
    assert args == ("agent-reach.exe", "doctor", "--json")
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE
    assert "shell" not in kwargs


@pytest.mark.asyncio
async def test_prepare_plan_runs_doctor_once_for_all_queries() -> None:
    process = FakeProcess(doctor_wire())
    factory, calls = process_factory_for(process)

    plan = await prepare_research_plan("狼美人骑士", process_factory=factory)

    assert plan.is_preflighted is True
    assert plan.preflight is not None
    assert len(plan.queries) == 8
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_kwargs", "message"),
    [
        ({"active_backend": "socket_search"}, "unsupported"),
        ({"active_backend": "Exa via socket", "transport": "socket"}, "unsupported"),
        ({"available": False}, "unavailable"),
    ],
)
async def test_doctor_rejects_socket_or_unavailable_routes(
    payload_kwargs: dict[str, Any],
    message: str,
) -> None:
    process = FakeProcess(doctor_wire(**payload_kwargs))
    factory, _ = process_factory_for(process)

    with pytest.raises(UnsupportedAgentReachBackendError, match=message):
        await run_agent_reach_doctor(process_factory=factory)


@pytest.mark.asyncio
async def test_doctor_rejects_missing_availability_and_malformed_json() -> None:
    missing = FakeProcess(doctor_wire(available=None))
    missing_factory, _ = process_factory_for(missing)
    with pytest.raises(AgentReachDoctorResponseError, match="availability"):
        await run_agent_reach_doctor(process_factory=missing_factory)

    malformed = FakeProcess(b"not json")
    malformed_factory, _ = process_factory_for(malformed)
    with pytest.raises(AgentReachDoctorResponseError, match="malformed JSON"):
        await run_agent_reach_doctor(process_factory=malformed_factory)


@pytest.mark.asyncio
async def test_doctor_enforces_timeout_and_cleans_up_process() -> None:
    process = SlowProcess()
    factory, _ = process_factory_for(process)

    with pytest.raises(AgentReachDoctorTimeoutError):
        await run_agent_reach_doctor(
            timeout_seconds=0.01,
            process_factory=factory,
        )

    assert process.terminated is True or process.killed is True


@pytest.mark.asyncio
async def test_doctor_enforces_output_limit_and_cleans_up_process() -> None:
    process = FakeProcess(b"x" * 100)
    factory, _ = process_factory_for(process)

    with pytest.raises(AgentReachDoctorOutputLimitError):
        await run_agent_reach_doctor(
            max_output_bytes=10,
            process_factory=factory,
        )

    assert process.terminated is True or process.killed is True
