"""Integration tests for deterministic compiled package storage."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_knowledge_compiler import _write_compilable_package
from test_knowledge_package_loader import BOARD

import werewolf.knowledge.compiled_store as compiled_store_module
from werewolf.knowledge.compiled_store import (
    CompiledKnowledgeStore,
    CompiledPackageAlreadyExistsError,
    CorruptCompiledPackageError,
)
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader


async def _compile(source_root: Path):
    _write_compilable_package(source_root)
    package = await KnowledgePackageLoader(source_root).load(BOARD)
    return KnowledgePackageCompiler().compile(package)


@pytest.mark.asyncio
async def test_materialized_bytes_are_deterministic(tmp_path: Path) -> None:
    first_package = await _compile(tmp_path / "source-one")
    second_package = await _compile(tmp_path / "source-two")

    first_path = await CompiledKnowledgeStore(tmp_path / "compiled-one").publish(first_package)
    second_path = await CompiledKnowledgeStore(tmp_path / "compiled-two").publish(second_package)

    filenames = {
        "package.json",
        "documents.jsonl",
        "exact-index.json",
        "alias-index.json",
        "topic-index.json",
        "relation-index.json",
        "text-index.json",
        "manifest.json",
    }
    assert {path.name for path in first_path.iterdir()} == filenames
    assert {path.name for path in second_path.iterdir()} == filenames
    assert {filename: (first_path / filename).read_bytes() for filename in filenames} == {
        filename: (second_path / filename).read_bytes() for filename in filenames
    }


@pytest.mark.asyncio
async def test_publish_is_idempotent_for_the_same_package(tmp_path: Path) -> None:
    package = await _compile(tmp_path / "source")
    store = CompiledKnowledgeStore(tmp_path / "compiled")

    first_path = await store.publish(package)
    first_bytes = {path.name: path.read_bytes() for path in first_path.iterdir()}
    second_path = await store.publish(package)

    assert second_path == first_path
    assert {path.name: path.read_bytes() for path in second_path.iterdir()} == first_bytes
    assert not list((tmp_path / "compiled").glob(".*.staging-*"))


@pytest.mark.asyncio
async def test_publish_rejects_a_different_package_with_the_same_directory_name(
    tmp_path: Path,
) -> None:
    first = await _compile(tmp_path / "source-one")
    second_root = tmp_path / "source-two"
    await _compile(second_root)
    role_path = second_root / "roles" / "wolf" / "1.0.0" / "role.md"
    role_path.write_text(
        role_path.read_text(encoding="utf-8") + "\n补充一条已发布规则。\n",
        encoding="utf-8",
    )
    second_source = await KnowledgePackageLoader(second_root).load(BOARD)
    second = KnowledgePackageCompiler().compile(second_source)
    assert first.package_identity != second.package_identity

    store = CompiledKnowledgeStore(tmp_path / "compiled")
    await store.publish(first)
    with pytest.raises(CompiledPackageAlreadyExistsError, match="different package"):
        await store.publish(second)


@pytest.mark.asyncio
async def test_load_rejects_tampering_without_source_vault_fallback(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    package = await _compile(source_root)
    store = CompiledKnowledgeStore(tmp_path / "compiled")
    package_path = await store.publish(package)

    (package_path / "text-index.json").write_bytes(
        (package_path / "text-index.json").read_bytes() + b"\n"
    )
    with pytest.raises(CorruptCompiledPackageError, match="hash mismatch"):
        await store.load(BOARD)

    shutil.rmtree(source_root)
    with pytest.raises(CorruptCompiledPackageError):
        await store.load(BOARD)


@pytest.mark.asyncio
async def test_incomplete_staging_is_rejected_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = await _compile(tmp_path / "source")
    store = CompiledKnowledgeStore(tmp_path / "compiled")
    original_write = compiled_store_module._write_one_sync

    def skip_text_index(path: Path, data: bytes) -> None:
        if path.name == "text-index.json":
            return
        original_write(path, data)

    monkeypatch.setattr(compiled_store_module, "_write_one_sync", skip_text_index)
    with pytest.raises(CorruptCompiledPackageError, match="incomplete"):
        await store.publish(package)

    assert not (tmp_path / "compiled" / BOARD).exists()
    assert not list((tmp_path / "compiled").glob(".*.staging-*"))
