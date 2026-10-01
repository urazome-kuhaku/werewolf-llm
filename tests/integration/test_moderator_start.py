from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import aiohttp
import pytest
import yaml
from test_knowledge_compiler import _write_compilable_package
from test_knowledge_package_loader import BOARD

from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.moderator import ModeratorError, ModeratorShell
from werewolf.moderator.prepare_flow import PlayerPrepareError, PlayerPrepareFlow
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import Ready, ReadyResponse, ResponseKind, RuntimeTurnResult
from werewolf.runtime.scripted_runtime import ScriptedRuntime


async def _compiled(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    _write_compilable_package(source)
    package = await KnowledgePackageLoader(source).load(BOARD)
    compiled = KnowledgePackageCompiler().compile(package)
    root = tmp_path / "compiled"
    await CompiledKnowledgeStore(root).publish(compiled)
    return root


def _write_config(path: Path, compiled: Path, games: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "game": {
                    "game_id": "game-001",
                    "board": {"id": "test-board", "version": "1.0.0"},
                    "seed": 7,
                },
                "paths": {"compiled_root": str(compiled), "games_root": str(games)},
                "players": [{"seat": 1, "runtime": "scripted"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class _ReadingRuntime(ScriptedRuntime):
    """Scripted Pi that performs the real loopback reads before READY."""

    def __init__(self, gateway_url: str, token: str) -> None:
        super().__init__()
        self.gateway_url = gateway_url
        self.token = token
        self.forge_receipts = False

    async def run_turn(self, request):
        if request.expected_kind is ResponseKind.READY:
            if self.forge_receipts:
                receipt_ids = ["receipt-forged"]
            else:
                assert self._context is not None
                headers = {"Authorization": f"Bearer {self.token}"}
                async with aiohttp.ClientSession(headers=headers) as client:
                    board = await client.get(f"{self.gateway_url}/v1/board/current")
                    role = await client.get(f"{self.gateway_url}/v1/role/{self._context.role_id}")
                    assert board.status == 200
                    assert role.status == 200
                    board_payload = await board.json()
                    role_payload = await role.json()
                receipt_ids = [board_payload["receipt_id"], role_payload["receipt_id"]]
            self._script.appendleft(
                ReadyResponse(
                    request_id=request.request_id,
                    ready=Ready(knowledge_receipts=receipt_ids),
                )
            )
        return await super().run_turn(request)


class _ForgedMetadataRuntime(_ReadingRuntime):
    """Return real receipts while forging one physical READY correlation field."""

    def __init__(self, gateway_url: str, token: str, field: str) -> None:
        super().__init__(gateway_url, token)
        self.field = field

    async def run_turn(self, request):
        result = await super().run_turn(request)
        if request.expected_kind is not ResponseKind.READY:
            return result
        if self.field == "request_id":
            response = result.response.model_copy(update={"request_id": "late-ready"})
            return RuntimeTurnResult(
                request_id="late-ready",
                logical_request_id=result.logical_request_id,
                attempt_no=result.attempt_no,
                response=response,
            )
        if self.field == "logical_request_id":
            return result.model_copy(update={"logical_request_id": "late-logical"})
        if self.field == "attempt_no":
            return result.model_copy(update={"attempt_no": result.attempt_no + 1})
        if self.field == "response_request_id":
            response = result.response.model_copy(update={"request_id": "late-response"})
            return result.model_copy(update={"response": response})
        raise AssertionError(f"unknown forged field: {self.field}")


class _TimeoutThenReadingRuntime(_ReadingRuntime):
    """Keep the first request active so prepare retry must explicitly abort it."""

    async def run_turn(self, request):
        if request.expected_kind is ResponseKind.READY and request.attempt_no == 1:
            self._active_request_id = request.request_id
            self._requests.append(request)
            await asyncio.Future()
        return await super().run_turn(request)

    async def abort(self, request_id: str) -> None:
        await super().abort(request_id)
        self._active_request_id = None


@pytest.mark.asyncio
async def test_start_assigns_roles_starts_runtime_and_enters_prepare(tmp_path: Path) -> None:
    compiled = await _compiled(tmp_path)
    config = tmp_path / "game.yaml"
    _write_config(config, compiled, tmp_path / "games")
    runtime = ScriptedRuntime()
    shell = ModeratorShell(config, runtime_factory=lambda *_args: runtime)

    await shell.new()
    await shell.next()
    await shell.next()
    started = await shell.execute("start")

    assert started is not None
    assert started["phase"] == "PLAYER_PREPARE"
    assert started["run_status"] == "RUNNING"
    assert shell.state.players[1].runtime_ref == "game-001-seat-01"
    assert shell.session_refs == {1: "game-001-seat-01"}
    assert all(event.channel.value == "PRIVATE" for event in shell.state.events)
    assert "players" not in await shell.status()

    assert shell.session_service is not None
    await shell.session_service.close()


@pytest.mark.asyncio
async def test_prepare_requires_real_seat_receipts_before_next(tmp_path: Path) -> None:
    compiled = await _compiled(tmp_path)
    config = tmp_path / "game.yaml"
    _write_config(config, compiled, tmp_path / "games")
    runtimes: list[_ReadingRuntime] = []

    def factory(_config, gateway_url, token):
        runtime = _ReadingRuntime(gateway_url, token)
        runtime.forge_receipts = True
        runtimes.append(runtime)
        return runtime

    shell = ModeratorShell(config, runtime_factory=factory)
    await shell.new()
    await shell.next()
    await shell.next()
    await shell.execute("start")

    with pytest.raises(ModeratorError, match="PLAYER_PREPARE"):
        await shell.next()
    status = await shell.execute("prepare status")
    assert status is not None
    assert status["players"] == [  # type: ignore[index]
        {"seat": 1, "ready": False, "receipt_count": 0, "attempts": 0, "runtime_started": True}
    ]

    with pytest.raises(ModeratorError, match="KNOWLEDGE_NOT_READY"):
        await shell.execute("prepare next")
    request = runtimes[0].requests[0]
    prompt = json.loads(PiRuntime()._build_prompt_message(request))
    schema = prompt["output_schema"]
    assert schema["properties"]["request_id"]["const"] == request.request_id
    assert schema["properties"]["kind"]["const"] == "ready"
    assert set(schema["required"]) >= {"schema_version", "request_id", "kind", "ready"}
    assert schema["$defs"]["Ready"]["properties"]["knowledge_receipts"]["type"] == "array"
    runtimes[0].forge_receipts = False
    prepared = await shell.execute("prepare retry 1")
    assert prepared is not None
    assert prepared["prepare"]["all_ready"] is True  # type: ignore[index]
    assert len(shell.state.players[1].knowledge_receipt_ids) == 2

    advanced = await shell.next()
    assert advanced["phase"] == "NIGHT_TEAM_CHAT"
    assert shell.session_service is not None
    await shell.session_service.close()


@pytest.mark.parametrize(
    "field",
    ["request_id", "logical_request_id", "attempt_no", "response_request_id"],
)
@pytest.mark.asyncio
async def test_prepare_rejects_forged_runtime_correlation_metadata(
    tmp_path: Path, field: str
) -> None:
    compiled = await _compiled(tmp_path)
    config = tmp_path / "game.yaml"
    _write_config(config, compiled, tmp_path / "games")
    runtimes: list[_ForgedMetadataRuntime] = []

    def factory(_config, gateway_url, token):
        runtime = _ForgedMetadataRuntime(gateway_url, token, field)
        runtimes.append(runtime)
        return runtime

    shell = ModeratorShell(config, runtime_factory=factory)
    await shell.new()
    await shell.next()
    await shell.next()
    await shell.execute("start")

    with pytest.raises(ModeratorError, match="REQUEST_MISMATCH"):
        await shell.execute("prepare next")
    assert shell.state.players[1].knowledge_receipt_ids == ()
    assert runtimes[0].requests[0].attempt_no == 1
    assert shell.session_service is not None
    await shell.session_service.close()


@pytest.mark.asyncio
async def test_prepare_retry_aborts_timed_out_request_before_replacement(tmp_path: Path) -> None:
    compiled = await _compiled(tmp_path)
    config = tmp_path / "game.yaml"
    _write_config(config, compiled, tmp_path / "games")
    runtimes: list[_TimeoutThenReadingRuntime] = []

    def factory(_config, gateway_url, token):
        runtime = _TimeoutThenReadingRuntime(gateway_url, token)
        runtimes.append(runtime)
        return runtime

    shell = ModeratorShell(config, runtime_factory=factory)
    await shell.new()
    await shell.next()
    await shell.next()
    await shell.execute("start")
    assert shell.manager is not None
    assert shell.session_service is not None
    flow = PlayerPrepareFlow(
        shell.manager,
        shell.session_service,
        clock=shell.clock,
        turn_timeout=timedelta(seconds=1),
    )

    with pytest.raises(PlayerPrepareError, match="timed out"):
        await flow.next(1)
    retried = await flow.retry(1)

    assert retried["attempt_no"] == 2
    assert runtimes[0].aborts == ("prepare-seat-1-attempt-1",)
    assert len(shell.state.players[1].knowledge_receipt_ids) == 2
    await shell.session_service.close()
