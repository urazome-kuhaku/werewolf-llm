"""Integration tests for the long lived moderator authority."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_knowledge_compiler import _write_compilable_package
from test_knowledge_package_loader import BOARD

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.moderator import ModeratorError, ModeratorShell


async def _compile_and_publish(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    _write_compilable_package(source)
    package = await KnowledgePackageLoader(source).load(BOARD)
    compiled = KnowledgePackageCompiler().compile(package)
    root = tmp_path / "compiled"
    await CompiledKnowledgeStore(root).publish(compiled)
    return root


def _config(path: Path, compiled: Path, games: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "game": {
                    "game_id": "game-001",
                    "board": {"id": "test-board", "version": "1.0.0"},
                    "seed": 7,
                },
                "paths": {
                    "compiled_root": str(compiled),
                    "games_root": str(games),
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_new_uses_one_manager_and_published_ruleset_snapshot(tmp_path: Path) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    games = tmp_path / "games"
    _config(config, compiled, games)
    shell = ModeratorShell(config, clock=lambda: datetime(2026, 9, 28, tzinfo=UTC))

    created = await shell.new()

    assert shell.manager is not None
    assert created["phase"] == "CREATED"
    assert shell.state.ruleset is not None
    assert (games / "active" / "game-001" / "ruleset" / "snapshot.json").is_file()
    assert await shell.status() == created
    assert "players" not in await shell.status()


@pytest.mark.asyncio
async def test_lifecycle_commands_keep_private_status_and_reject_unwired_actions(
    tmp_path: Path,
) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()

    paused = await shell.pause()
    assert paused["run_status"] == "PAUSED"
    resumed = await shell.resume()
    assert resumed["run_status"] == "READY"
    progressed = await shell.next()
    assert progressed["phase"] == "RULESET_READY"
    with pytest.raises(ModeratorError, match="not connected"):
        await shell.execute("start")
    with pytest.raises(ModeratorError, match="cycle boundary"):
        await shell.save()
    private = await shell.status(private=True)
    assert private["sensitive"] is True
    assert len(private["moderator_audit"]) >= 3


@pytest.mark.asyncio
async def test_night_resolve_command_forwards_json_file_to_night_flow(tmp_path: Path) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()

    class StubNightFlow:
        def __init__(self) -> None:
            self.resolve_args: tuple[str, ...] | None = None

        def progress(self) -> dict[str, object]:
            return {"phase": "NIGHT_RESOLVE"}

        async def resolve(self, args: tuple[str, ...]) -> None:
            self.resolve_args = args

        def pending(self) -> dict[str, object]:
            return {
                "phase": "NIGHT_RESOLVE",
                "pending_requests": [],
                "private": True,
                "sensitive": True,
            }

    flow = StubNightFlow()
    shell.runtime_bundle = SimpleNamespace(board=object())
    shell.session_service = SimpleNamespace(runtimes={})
    shell._night_flow = flow  # type: ignore[assignment]
    resolution_file = tmp_path / "resolution.json"
    resolution_file.write_text("[]", encoding="utf-8")

    result = await shell.execute(f"night resolve {resolution_file}")

    assert flow.resolve_args == (str(resolution_file),)
    assert result["night"] == {"phase": "NIGHT_RESOLVE"}

    pending = await shell.execute("night pending")
    assert pending["private"] is True
    assert pending["sensitive"] is True
    assert pending["night"]["pending_requests"] == []  # type: ignore[index]


@pytest.mark.asyncio
async def test_night_team_commands_forward_only_supported_subcommands(tmp_path: Path) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()

    class StubNightFlow:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def progress(self) -> dict[str, object]:
            return {"phase": "NIGHT_TEAM_CHAT"}

        async def team_next(self) -> dict[str, object]:
            self.calls.append("next")
            return {"status": "accepted", "seat": 1}

        async def team_retry(self) -> dict[str, object]:
            self.calls.append("retry")
            return {"status": "accepted", "seat": 1, "attempt_no": 2}

        async def team_again(self) -> dict[str, object]:
            self.calls.append("again")
            return {"status": "ready", "queue": [1, 2]}

    flow = StubNightFlow()
    shell.runtime_bundle = SimpleNamespace(board=object())
    shell.session_service = SimpleNamespace(runtimes={})
    shell._night_flow = flow  # type: ignore[assignment]

    assert (await shell.execute("night team next"))["team"] == {  # type: ignore[index]
        "status": "accepted",
        "seat": 1,
    }
    assert (await shell.execute("night team retry"))["team"]["attempt_no"] == 2  # type: ignore[index]
    assert (await shell.execute("night team again"))["team"]["queue"] == [1, 2]  # type: ignore[index]
    assert flow.calls == ["next", "retry", "again"]

    with pytest.raises(ModeratorError, match="night team syntax"):
        await shell.execute("night team next 1")


@pytest.mark.asyncio
async def test_save_embeds_verified_ruleset_files_from_active_snapshot(tmp_path: Path) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    games = tmp_path / "games"
    _config(config, compiled, games)
    shell = ModeratorShell(config)
    await shell.new()

    # The game coordinator normally reaches this boundary after a complete
    # cycle.  Use the authoritative state value directly here to isolate the
    # persistence contract from phase orchestration.
    assert shell.manager is not None
    shell.manager._state = shell.state.model_copy(update={"phase": GamePhase.VICTORY_CHECK})

    saved = await shell.save()
    snapshot_path = Path(str(saved["path"]))
    manifest = json.loads(snapshot_path.joinpath("snapshot_manifest.json").read_text())

    ruleset_files = manifest["ruleset"]["files"]
    assert ruleset_files
    for relative_path in ruleset_files:
        assert (snapshot_path / "private" / "ruleset" / "files" / relative_path).is_file()


@pytest.mark.asyncio
async def test_finish_save_failure_rolls_back_closed_status_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()
    assert shell.manager is not None
    assert shell.snapshot_store is not None
    shell.manager._state = shell.state.model_copy(update={"phase": GamePhase.FINISHED})

    async def fail_create(*args: object, **kwargs: object) -> object:
        raise OSError("simulated snapshot failure")

    monkeypatch.setattr(shell.snapshot_store, "create", fail_create)

    with pytest.raises(ModeratorError, match="snapshot"):
        await shell.finish()

    assert shell.state.run_status is RunStatus.READY
    assert any(item["operation"] == "FINISH_ROLLBACK" for item in shell.state.moderator_audit)


@pytest.mark.asyncio
async def test_finish_archive_failure_rolls_back_closed_status_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()
    assert shell.manager is not None
    assert shell.archive_store is not None
    shell.manager._state = shell.state.model_copy(update={"phase": GamePhase.FINISHED})

    async def fail_archive(*args: object, **kwargs: object) -> object:
        raise OSError("simulated archive failure")

    monkeypatch.setattr(shell.archive_store, "finish", fail_archive)

    with pytest.raises(ModeratorError, match="archive"):
        await shell.finish()

    assert shell.state.run_status is RunStatus.READY
    assert any(item["operation"] == "FINISH_ROLLBACK" for item in shell.state.moderator_audit)


@pytest.mark.asyncio
async def test_finish_save_failure_rolls_back_running_status_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()
    assert shell.manager is not None
    assert shell.snapshot_store is not None
    shell.manager._state = shell.state.model_copy(
        update={"phase": GamePhase.FINISHED, "run_status": RunStatus.RUNNING}
    )

    original_create = shell.snapshot_store.create
    failed = True

    async def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal failed
        if failed:
            failed = False
            raise OSError("simulated snapshot failure")
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(shell.snapshot_store, "create", fail_once)

    with pytest.raises(ModeratorError, match="snapshot"):
        await shell.finish()
    assert shell.state.run_status is RunStatus.RUNNING

    result = await shell.finish()
    assert result["status"] == "finished"
    assert shell.state.run_status is RunStatus.CLOSED


@pytest.mark.asyncio
async def test_finish_archive_failure_rolls_back_running_status_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()
    assert shell.manager is not None
    assert shell.archive_store is not None
    shell.manager._state = shell.state.model_copy(
        update={"phase": GamePhase.FINISHED, "run_status": RunStatus.RUNNING}
    )

    original_finish = shell.archive_store.finish
    failed = True

    async def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal failed
        if failed:
            failed = False
            raise OSError("simulated archive failure")
        return await original_finish(*args, **kwargs)

    monkeypatch.setattr(shell.archive_store, "finish", fail_once)

    with pytest.raises(ModeratorError, match="archive"):
        await shell.finish()
    assert shell.state.run_status is RunStatus.RUNNING

    result = await shell.finish()
    assert result["status"] == "finished"
    assert shell.state.run_status is RunStatus.CLOSED


@pytest.mark.asyncio
async def test_sheriff_shell_commands_forward_authoritative_subcommands(tmp_path: Path) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()

    class StubSheriffFlow:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def progress(self) -> dict[str, object]:
            return {"phase": "SHERIFF_ELECTION", "election": None, "vote": None}

        async def start(self, candidates: tuple[int, ...]) -> None:
            self.calls.append(("start", candidates))

        async def speech_next(self) -> object:
            self.calls.append(("speech_next", None))
            return {"status": "accepted"}

        async def speech_retry(self) -> object:
            self.calls.append(("speech_retry", None))
            return {"status": "retried"}

        async def open_vote(self) -> None:
            self.calls.append(("vote_open", None))

        async def vote_next(self, seat: int | None = None) -> object:
            self.calls.append(("vote_next", seat))
            return {"seat": seat}

        async def vote_retry(self, seat: int | None = None) -> object:
            self.calls.append(("vote_retry", seat))
            return {"seat": seat}

        async def collect(self, *, force: bool = False) -> None:
            self.calls.append(("vote_collect", force))

        async def confirm(self) -> None:
            self.calls.append(("vote_confirm", None))

        async def transfer(self) -> None:
            self.calls.append(("transfer", None))

    flow = StubSheriffFlow()
    shell.runtime_bundle = SimpleNamespace(board=object())
    shell.session_service = SimpleNamespace(runtimes={})
    shell._sheriff_flow = flow  # type: ignore[assignment]

    await shell.execute("sheriff start 2 3")
    await shell.execute("sheriff speech next")
    await shell.execute("sheriff speech retry")
    await shell.execute("sheriff vote open")
    assert (await shell.execute("sheriff vote next 2"))["vote"] == {"seat": 2}  # type: ignore[index]
    await shell.execute("sheriff vote retry 3")
    await shell.execute("sheriff vote collect --force")
    await shell.execute("sheriff vote confirm")
    await shell.execute("sheriff transfer")

    assert flow.calls == [
        ("start", (2, 3)),
        ("speech_next", None),
        ("speech_retry", None),
        ("vote_open", None),
        ("vote_next", 2),
        ("vote_retry", 3),
        ("vote_collect", True),
        ("vote_confirm", None),
        ("transfer", None),
    ]


@pytest.mark.asyncio
async def test_day_commands_cannot_skip_required_first_day_sheriff_election(
    tmp_path: Path,
) -> None:
    compiled = await _compile_and_publish(tmp_path)
    config = tmp_path / "game.yaml"
    _config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)
    await shell.new()
    assert shell.manager is not None
    shell.manager._state = shell.state.model_copy(
        update={"phase": GamePhase.DAY_ANNOUNCE, "day_no": 1}
    )
    shell.runtime_bundle = SimpleNamespace(
        board=SimpleNamespace(
            day_flow=SimpleNamespace(sheriff=SimpleNamespace(enabled=True, first_day_election=True))
        )
    )
    shell.session_service = SimpleNamespace(runtimes={})
    shell._day_flow = SimpleNamespace()  # type: ignore[assignment]

    with pytest.raises(ModeratorError, match="FIRST_DAY_SHERIFF_REQUIRED"):
        await shell.execute("day announce")
