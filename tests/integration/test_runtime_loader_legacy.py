"""Compatibility tests for loading packages frozen before the knife flag."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_knowledge_compiler import _write_compilable_package
from test_knowledge_package_loader import BOARD

from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import CompiledKnowledgePackage, KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.runtime_loader import (
    RuntimeKnowledgeLoaderError,
    load_runtime_knowledge_bundle_from_snapshot,
)
from werewolf.knowledge.snapshot import KnowledgeSnapshotBuilder


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _package_variant(
    package: CompiledKnowledgePackage,
    mutate_board: Callable[[dict[str, Any]], None],
    *,
    mutate_manifest: Callable[[dict[str, Any]], None] | None = None,
) -> CompiledKnowledgePackage:
    """Rebuild one canonical package variant as an on-disk historical package."""

    payload = json.loads(package.canonical_package_json)
    board = payload["board_definition"]
    assert isinstance(board, dict)
    mutate_board(board)
    manifest = payload["manifest"]
    assert isinstance(manifest, dict)
    manifest["board_definition_sha256"] = _sha256_json(board)
    if mutate_manifest is not None:
        mutate_manifest(manifest)

    canonical_manifest = _canonical(manifest)
    canonical_package = _canonical(payload)
    return replace(
        package,
        package_payload=payload,
        manifest_payload=manifest,
        canonical_package_json=canonical_package,
        canonical_manifest_json=canonical_manifest,
        package_identity=hashlib.sha256(canonical_package.encode("utf-8")).hexdigest(),
        manifest_sha256=hashlib.sha256(canonical_manifest.encode("utf-8")).hexdigest(),
    )


async def _snapshot_for_package(tmp_path: Path, package: CompiledKnowledgePackage):
    store = CompiledKnowledgeStore(tmp_path / "compiled")
    await store.publish(package)
    builder = KnowledgeSnapshotBuilder(store, tmp_path / "games")
    return await builder.create("game-001", BOARD)


async def _compiled_package(tmp_path: Path) -> CompiledKnowledgePackage:
    source = tmp_path / "source"
    _write_compilable_package(source)
    loaded = await KnowledgePackageLoader(source).load(BOARD)
    return KnowledgePackageCompiler().compile(loaded)


@pytest.mark.asyncio
async def test_runtime_loader_accepts_materialized_legacy_knife_snapshot(
    tmp_path: Path,
) -> None:
    current = await _compiled_package(tmp_path)
    legacy = _package_variant(
        current,
        lambda board: board["knife_rule"].pop("plan_confirmation_required"),
    )
    snapshot = await _snapshot_for_package(tmp_path, legacy)
    original_identity = snapshot.package_identity
    shutil.rmtree(tmp_path / "source")
    shutil.rmtree(tmp_path / "compiled")

    bundle = await load_runtime_knowledge_bundle_from_snapshot(snapshot)

    assert bundle.package.package_identity == original_identity
    assert bundle.board.knife_rule.plan_confirmation_required is False
    assert (
        "plan_confirmation_required"
        not in bundle.package.package_payload["board_definition"]["knife_rule"]
    )


@pytest.mark.asyncio
async def test_runtime_loader_accepts_explicit_true_knife_flag(tmp_path: Path) -> None:
    current = await _compiled_package(tmp_path)
    package = _package_variant(
        current,
        lambda board: board["knife_rule"].update(plan_confirmation_required=True),
    )
    snapshot = await _snapshot_for_package(tmp_path, package)

    bundle = await load_runtime_knowledge_bundle_from_snapshot(snapshot)

    assert bundle.board.knife_rule.plan_confirmation_required is True


def _legacy_with_malformed_value(board: dict[str, Any]) -> None:
    knife = board["knife_rule"]
    knife.pop("plan_confirmation_required")
    knife["final_target_required"] = "yes"


def _legacy_with_missing_existing_field(board: dict[str, Any]) -> None:
    knife = board["knife_rule"]
    knife.pop("plan_confirmation_required")
    knife.pop("selection_mode")


def _legacy_board_without_flag(board: dict[str, Any]) -> None:
    board["knife_rule"].pop("plan_confirmation_required")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate_board,mutate_manifest",
    [
        (_legacy_with_malformed_value, None),
        (_legacy_with_missing_existing_field, None),
        (
            _legacy_board_without_flag,
            lambda manifest: manifest.update(board_definition_sha256="0" * 64),
        ),
    ],
)
async def test_runtime_loader_rejects_invalid_legacy_board_payload(
    tmp_path: Path,
    mutate_board: Callable[[dict[str, Any]], None],
    mutate_manifest: Callable[[dict[str, Any]], None] | None,
) -> None:
    current = await _compiled_package(tmp_path)
    legacy = _package_variant(current, mutate_board, mutate_manifest=mutate_manifest)
    snapshot = await _snapshot_for_package(tmp_path, legacy)

    with pytest.raises(RuntimeKnowledgeLoaderError):
        await load_runtime_knowledge_bundle_from_snapshot(snapshot)
