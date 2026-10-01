"""Integration coverage for the moderator player-session lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest
from test_knowledge_compiler import _write_compilable_package
from test_knowledge_package_loader import BOARD

from werewolf.game.setup import build_role_assignment_plan
from werewolf.game.state import GameState, RulesetRef
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.runtime_loader import (
    load_runtime_knowledge_bundle_from_snapshot,
)
from werewolf.knowledge.snapshot import KnowledgeSnapshotBuilder
from werewolf.moderator.config import parse_player_configuration
from werewolf.moderator.sessions import PlayerSessionError, PlayerSessionService
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime


async def _bundle(tmp_path: Path):
    source = tmp_path / "source"
    _write_compilable_package(source)
    package = await KnowledgePackageLoader(source).load(BOARD)
    compiled = KnowledgePackageCompiler().compile(package)
    store = CompiledKnowledgeStore(tmp_path / "compiled")
    await store.publish(compiled)
    snapshot = await KnowledgeSnapshotBuilder(store, tmp_path / "games").create("game-001", BOARD)
    return await load_runtime_knowledge_bundle_from_snapshot(snapshot)


def _configuration(bundle, tmp_path: Path):
    return parse_player_configuration(
        {
            "game": {"game_id": "game-001"},
            "players": [{"seat": 1, "runtime": "scripted"}],
        },
        board=bundle.board,
        game_root=tmp_path,
        allow_scripted=True,
    )


@pytest.mark.asyncio
async def test_start_issues_seat_bound_token_and_closes_everything(tmp_path: Path) -> None:
    bundle = await _bundle(tmp_path)
    configuration = _configuration(bundle, tmp_path)
    assignments = build_role_assignment_plan(bundle.board, bundle.package, seats=(1,), seed=7)
    runtimes: list[ScriptedRuntime] = []

    def factory(_config, _url, _token):
        runtime = ScriptedRuntime()
        runtimes.append(runtime)
        return runtime

    service = PlayerSessionService(runtime_factory=factory)
    records = await service.start(bundle, configuration, assignments)

    record = records[1]
    assert record.runtime_ref.seat == 1
    assert record.context.role_id == assignments.players[1].role_id
    assert record.context.system_prompt is not None
    assert record.token not in record.context.system_prompt

    async with aiohttp.ClientSession() as client:
        async with client.get(
            f"{service.gateway_url}/v1/board/current",
            headers={"Authorization": f"Bearer {record.token}"},
        ) as response:
            assert response.status == 200
            payload = await response.json()
    assert payload["snapshot"]["id"] == bundle.service.snapshot_id
    async with aiohttp.ClientSession() as client:
        async with client.get(
            f"{service.gateway_url}/v1/role/{record.context.role_id}",
            headers={"Authorization": f"Bearer {record.token}"},
        ) as response:
            assert response.status == 200
    captured = service.receipts_for_seat(1)
    assert {receipt.tool for receipt in captured} == {"get_board", "get_role"}
    assert all(receipt.seat == 1 for receipt in captured)

    await service.close()
    assert runtimes[0]._closed is True
    with pytest.raises(RuntimeError, match="not been started"):
        _ = service.gateway


@pytest.mark.asyncio
async def test_skill_status_provider_reads_current_seat_state(tmp_path: Path) -> None:
    bundle = await _bundle(tmp_path)
    configuration = _configuration(bundle, tmp_path)
    assignments = build_role_assignment_plan(bundle.board, bundle.package, seats=(1,), seed=7)
    timestamp = datetime.now(UTC)
    state = GameState(
        game_id="game-001",
        created_at=timestamp,
        updated_at=timestamp,
        ruleset=RulesetRef(
            board_id=bundle.board.board_ref.id,
            version=bundle.board.board_ref.version,
            snapshot_id=bundle.service.snapshot_id,
            manifest_sha256="a" * 64,
        ),
        players=dict(assignments.players),
    )

    async def provider() -> GameState:
        return state

    service = PlayerSessionService(
        runtime_factory=lambda *_args: ScriptedRuntime(), state_provider=provider
    )
    records = await service.start(bundle, configuration, assignments)
    async with aiohttp.ClientSession() as client:
        async with client.get(
            f"{service.gateway_url}/v1/game/skills/me",
            headers={"Authorization": f"Bearer {records[1].token}"},
        ) as response:
            assert response.status == 200
            payload = await response.json()
    assert payload["skill_status"]["seat"] == 1
    assert payload["skill_status"]["session_epoch"] == 0
    await service.close()


@pytest.mark.asyncio
async def test_failed_start_revokes_tokens_and_closes_started_runtime(tmp_path: Path) -> None:
    bundle = await _bundle(tmp_path)
    configuration = _configuration(bundle, tmp_path)
    assignments = build_role_assignment_plan(bundle.board, bundle.package, seats=(1,), seed=7)

    class FailingRuntime(ScriptedRuntime):
        async def start(self, config, context):
            del config, context
            raise RuntimeError("injected startup failure")

    service = PlayerSessionService(runtime_factory=lambda *_args: FailingRuntime())
    with pytest.raises(RuntimeError, match="injected startup failure"):
        await service.start(bundle, configuration, assignments)
    assert service.records == {}
    with pytest.raises(RuntimeError, match="not been started"):
        _ = service.gateway_url


def test_reasoning_reaches_pi_process_config(tmp_path: Path) -> None:
    runtime = PiRuntime(knowledge_base_url="http://127.0.0.1:1", knowledge_token="token")
    process = runtime._build_default_process(
        RuntimeConfig(
            session_id="seat-01",
            session_dir=tmp_path,
            provider="provider",
            model="model",
            reasoning="xhigh",
        ),
        InitialContext(game_id="game-001", seat=1, session_epoch=0, role_id="wolf"),
        tmp_path,
    )
    assert process.config.thinking == "xhigh"


@pytest.mark.asyncio
async def test_default_pi_process_receives_and_removes_seat_prompt(tmp_path: Path) -> None:
    prompt = "# seat prompt\n仅供座位 1 使用。"
    runtime = PiRuntime(knowledge_base_url="http://127.0.0.1:1", knowledge_token="secret")
    process = runtime._build_default_process(
        RuntimeConfig(session_id="seat-01", session_dir=tmp_path),
        InitialContext(
            game_id="game-001",
            seat=1,
            session_epoch=0,
            role_id="wolf",
            system_prompt=prompt,
        ),
        tmp_path,
    )
    prompt_path = process.config.append_system_prompt
    assert prompt_path is not None
    assert prompt_path.parent == tmp_path.resolve()
    assert prompt_path.read_text(encoding="utf-8") == prompt
    assert "secret" not in prompt_path.read_text(encoding="utf-8")

    await runtime.close("prompt cleanup")
    assert not prompt_path.exists()


@pytest.mark.asyncio
async def test_close_consumes_session_group_when_runtime_close_fails(tmp_path: Path) -> None:
    bundle = await _bundle(tmp_path)
    configuration = _configuration(bundle, tmp_path)
    assignments = build_role_assignment_plan(bundle.board, bundle.package, seats=(1,), seed=7)

    class FailingCloseRuntime(ScriptedRuntime):
        close_calls = 0

        async def close(self, reason: str) -> None:
            del reason
            self.close_calls += 1
            raise RuntimeError("close failed")

    runtimes: list[FailingCloseRuntime] = []

    def factory(_config, _url, _token):
        runtime = FailingCloseRuntime()
        runtimes.append(runtime)
        return runtime

    service = PlayerSessionService(runtime_factory=factory)
    await service.start(bundle, configuration, assignments)
    with pytest.raises(RuntimeError, match="close failed"):
        await service.close()

    assert service.records == {}
    with pytest.raises(PlayerSessionError, match="have not been started"):
        _ = service.gateway
    await service.close()
    assert runtimes[0].close_calls == 1
