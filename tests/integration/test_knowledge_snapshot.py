"""Integration tests for immutable per-game knowledge snapshots."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_knowledge_compiler import _write_compilable_package
from test_knowledge_package_loader import BOARD

from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.snapshot import (
    CorruptKnowledgeSnapshotError,
    KnowledgeSnapshotBuilder,
    KnowledgeSnapshotPathError,
    SnapshotAlreadyExistsError,
)


async def _compile(source_root: Path):
    _write_compilable_package(source_root)
    package = await KnowledgePackageLoader(source_root).load(BOARD)
    return KnowledgePackageCompiler().compile(package)


@pytest.mark.asyncio
async def test_create_and_load_snapshot_is_self_contained(tmp_path: Path) -> None:
    package = await _compile(tmp_path / "source")
    compiled_store = CompiledKnowledgeStore(tmp_path / "compiled")
    await compiled_store.publish(package)
    builder = KnowledgeSnapshotBuilder(compiled_store, tmp_path / "games")

    snapshot = await builder.create("game-001", BOARD, created_at="2026-09-28T00:00:00Z")
    loaded = await builder.load("game-001")

    assert snapshot.snapshot_id == loaded.snapshot_id
    assert loaded.package_id == BOARD
    assert loaded.board_ref_text == BOARD
    assert loaded.created_at == "2026-09-28T00:00:00Z"
    assert loaded.path == tmp_path / "games" / "active" / "game-001" / "ruleset"
    assert (loaded.path / "snapshot.json").is_file()
    assert "manifest.json" in loaded.file_digests


@pytest.mark.asyncio
async def test_tampering_and_incomplete_snapshot_fail_closed(tmp_path: Path) -> None:
    package = await _compile(tmp_path / "source")
    compiled_store = CompiledKnowledgeStore(tmp_path / "compiled")
    await compiled_store.publish(package)
    builder = KnowledgeSnapshotBuilder(compiled_store, tmp_path / "games")
    snapshot = await builder.create("game-001", BOARD)

    package_json = snapshot.path / "package.json"
    original_package_json = package_json.read_bytes()
    package_json.write_bytes(original_package_json + b" ")
    with pytest.raises(CorruptKnowledgeSnapshotError, match="hash mismatch"):
        await builder.load("game-001")

    package_json.write_bytes(original_package_json[:-1])
    with pytest.raises(CorruptKnowledgeSnapshotError):
        await builder.load("game-001")

    shutil.rmtree(snapshot.path)
    await builder.create("game-001", BOARD)
    (snapshot.path / "documents.jsonl").unlink()
    with pytest.raises(CorruptKnowledgeSnapshotError, match="incomplete"):
        await builder.load("game-001")


@pytest.mark.asyncio
async def test_different_content_cannot_replace_existing_snapshot(tmp_path: Path) -> None:
    first_source = tmp_path / "source-one"
    first_package = await _compile(first_source)
    first_store = CompiledKnowledgeStore(tmp_path / "compiled-one")
    await first_store.publish(first_package)
    builder = KnowledgeSnapshotBuilder(first_store, tmp_path / "games")
    first = await builder.create("game-001", BOARD)

    second_source = tmp_path / "source-two"
    await _compile(second_source)
    role_path = second_source / "roles" / "wolf" / "1.0.0" / "role.md"
    role_path.write_text(role_path.read_text(encoding="utf-8") + "\n补充规则。\n", encoding="utf-8")
    second_loaded = await KnowledgePackageLoader(second_source).load(BOARD)
    second_package = KnowledgePackageCompiler().compile(second_loaded)
    second_store = CompiledKnowledgeStore(tmp_path / "compiled-two")
    await second_store.publish(second_package)
    second_builder = KnowledgeSnapshotBuilder(second_store, tmp_path / "games")

    with pytest.raises(SnapshotAlreadyExistsError, match="different snapshot"):
        await second_builder.create("game-001", BOARD)
    assert (await builder.load("game-001")).snapshot_id == first.snapshot_id


@pytest.mark.asyncio
async def test_game_id_is_contained_and_game_root_mode_is_supported(tmp_path: Path) -> None:
    package = await _compile(tmp_path / "source")
    compiled_store = CompiledKnowledgeStore(tmp_path / "compiled")
    await compiled_store.publish(package)
    builder = KnowledgeSnapshotBuilder(compiled_store, tmp_path / "games")

    with pytest.raises(KnowledgeSnapshotPathError):
        await builder.create("../outside", BOARD)
    assert not (tmp_path / "outside").exists()

    game_root = tmp_path / "games" / "active" / "game-002"
    game_builder = KnowledgeSnapshotBuilder.from_game_root(compiled_store, game_root)
    loaded = await game_builder.create("game-002", BOARD)
    assert loaded.path == game_root / "ruleset"
    with pytest.raises(KnowledgeSnapshotPathError):
        await game_builder.load("game-003")


@pytest.mark.asyncio
async def test_source_changes_after_creation_do_not_change_snapshot(tmp_path: Path) -> None:
    package = await _compile(tmp_path / "source")
    compiled_store = CompiledKnowledgeStore(tmp_path / "compiled")
    await compiled_store.publish(package)
    builder = KnowledgeSnapshotBuilder(compiled_store, tmp_path / "games")
    snapshot = await builder.create("game-001", BOARD)
    before = {path.name: path.read_bytes() for path in snapshot.path.iterdir()}

    (tmp_path / "source" / "roles" / "wolf" / "1.0.0" / "role.md").write_text(
        "changed source after snapshot\n", encoding="utf-8"
    )
    loaded = await builder.load("game-001")
    after = {path.name: path.read_bytes() for path in loaded.path.iterdir()}
    assert after == before
