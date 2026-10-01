"""Integration tests for restoring a query service from a game snapshot."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_knowledge_compiler import _write_compilable_package
from test_knowledge_package_loader import BOARD

from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.runtime_loader import (
    RuntimeKnowledgeBundle,
    load_runtime_knowledge_bundle_from_snapshot,
    load_service_from_snapshot,
)
from werewolf.knowledge.service import QueryContext
from werewolf.knowledge.snapshot import (
    CorruptKnowledgeSnapshotError,
    KnowledgeSnapshotBuilder,
)


async def _create_snapshot(tmp_path: Path):
    source_root = tmp_path / "source"
    _write_compilable_package(source_root)
    package = KnowledgePackageCompiler().compile(
        await KnowledgePackageLoader(source_root).load(BOARD)
    )
    compiled_store = CompiledKnowledgeStore(tmp_path / "compiled")
    await compiled_store.publish(package)
    builder = KnowledgeSnapshotBuilder(compiled_store, tmp_path / "games")
    snapshot = await builder.create("game-001", BOARD)
    return snapshot, builder, source_root, compiled_store.root


@pytest.mark.asyncio
async def test_service_restores_queries_from_snapshot_after_source_cleanup(
    tmp_path: Path,
) -> None:
    snapshot, _, source_root, compiled_root = await _create_snapshot(tmp_path)
    shutil.rmtree(source_root)
    shutil.rmtree(compiled_root)

    service = await load_service_from_snapshot(snapshot)
    context = QueryContext(
        game_id="game-001",
        snapshot_id=snapshot.snapshot_id,
        seat=1,
        session_epoch=0,
    )

    role = service.get_role(context, "wolf")
    board = service.get_board(context)
    assert service.snapshot_id == snapshot.snapshot_id
    assert role.document.id == "wolf"
    assert role.document.effective_rules == {"can_self_heal": False}
    assert board.document.id == "test-board"


@pytest.mark.asyncio
async def test_runtime_bundle_restores_the_frozen_board_definition(
    tmp_path: Path,
) -> None:
    snapshot, _, source_root, compiled_root = await _create_snapshot(tmp_path)
    shutil.rmtree(source_root)
    shutil.rmtree(compiled_root)

    bundle = await load_runtime_knowledge_bundle_from_snapshot(snapshot)

    assert isinstance(bundle, RuntimeKnowledgeBundle)
    assert bundle.service.snapshot_id == snapshot.snapshot_id
    assert bundle.board.board_ref.format() == BOARD
    assert bundle.board.status == "published"
    assert bundle.board.reviewed_by == "reviewer"
    assert bundle.package.package_payload["board_definition"] == (
        bundle.board.model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_runtime_loader_explicitly_rejects_a_legacy_package_without_board_definition(
    tmp_path: Path,
) -> None:
    snapshot, _, _, _ = await _create_snapshot(tmp_path)
    package_json = snapshot.root / "package.json"
    payload = json.loads(package_json.read_text(encoding="utf-8"))
    payload.pop("board_definition")
    package_json.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(CorruptKnowledgeSnapshotError):
        await load_service_from_snapshot(snapshot)


@pytest.mark.asyncio
async def test_service_rejects_snapshot_tampering_before_query_restore(tmp_path: Path) -> None:
    snapshot, _, _, _ = await _create_snapshot(tmp_path)
    package_json = snapshot.root / "package.json"
    package_json.write_bytes(package_json.read_bytes() + b" ")

    with pytest.raises(CorruptKnowledgeSnapshotError):
        await load_service_from_snapshot(snapshot)
