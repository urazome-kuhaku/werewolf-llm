"""Private waiting-choice checkpoints remain detached from live packages."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_rules_b_workflow import (
    GAME_ID as WORKFLOW_GAME_ID,
)
from test_rules_b_workflow import (
    _compiled_workflow_fixture,
    _settle_initial_multi_death,
)
from test_rules_b_workflow import (
    _manager as _workflow_manager,
)

from werewolf.domain.enums import GamePhase
from werewolf.game.manager import GameManager
from werewolf.game.state import (
    GameState,
    RuleExecutionIdentity,
    RulesetRef,
)
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.snapshot import KnowledgeSnapshotBuilder
from werewolf.persistence.checkpoint import (
    CorruptPrivateCheckpointError,
    PrivateCheckpointBoundaryError,
    PrivateCheckpointIdentity,
    PrivateCheckpointIdentityError,
    PrivateCheckpointStore,
    _canonical_json_bytes,
    _decode_payload,
)

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
GAME_ID = WORKFLOW_GAME_ID


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@pytest.mark.asyncio
async def test_waiting_choice_checkpoint_restores_frozen_bundle_and_checks_identity(
    tmp_path: Path,
) -> None:
    compiled, _ = await _compiled_workflow_fixture()
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    manager, _ = await _workflow_manager()
    assert manager.execution_package is not None
    assert manager.execution_package.package_id == compiled.execution.package_id
    compiled_store = CompiledKnowledgeStore(tmp_path / "compiled")
    await compiled_store.publish(compiled)
    package_load = await compiled_store.load(compiled.board_ref)

    game_root = tmp_path / "active" / GAME_ID
    game_root.mkdir(parents=True)
    snapshot = await KnowledgeSnapshotBuilder.from_game_root(compiled_store, game_root).create(
        GAME_ID,
        package_load,
    )
    snapshot_manifest = json.loads((snapshot.root / "snapshot.json").read_text(encoding="utf-8"))
    frozen_files = {
        relative: (snapshot.root / relative).read_bytes() for relative in snapshot.file_digests
    }
    execution_digest = compiled.manifest_payload["execution_sha256"]
    assert isinstance(execution_digest, str)
    frozen_ref = RulesetRef(
        board_id=compiled.board_ref.id,
        version=compiled.board_ref.version,
        snapshot_id=snapshot.snapshot_id,
        manifest_sha256=snapshot.manifest_sha256,
    )
    state = GameState.model_validate(
        {
            **manager.state.model_dump(mode="python"),
            "ruleset": frozen_ref.model_dump(mode="python"),
        }
    )
    manager = GameManager(
        state,
        registry=manager.registry,
        execution_package=manager.execution_package,
    )
    await _settle_initial_multi_death(manager)
    automatic = next(item for item in manager.state.rule_trigger_queue if item.mode == "AUTOMATIC")
    auto_step = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
    )
    assert auto_step.kind == "AUTOMATIC"
    assert auto_step.occurrence_id == automatic.occurrence_id
    await manager.commit_rule_group(
        f"automatic-{automatic.occurrence_id}",
        occurrence_ids=(automatic.occurrence_id,),
        expected_revision=manager.state.state_revision,
    )
    choice_step = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
    )
    assert choice_step.kind == "PLAYER_CHOICE"
    state = manager.state
    await manager.assert_private_checkpoint_waiting_choice(state)
    identity = PrivateCheckpointIdentity(
        game_id=GAME_ID,
        board_ref=compiled.board_ref.format(),
        package_id=compiled.package_id,
        execution_package_id=compiled.execution.package_id,
        execution_digest=execution_digest,
        package_identity=compiled.package_identity,
    )
    store = PrivateCheckpointStore(game_root)

    saved = await store.create(
        state,
        manager=manager,
        knowledge_snapshot_manifest=snapshot_manifest,
        frozen_files=frozen_files,
        identity=identity,
        created_at=NOW.isoformat(),
    )
    loaded = await store.verify(saved.manifest.checkpoint_id, expected_identity=identity)

    assert loaded.state == state
    assert loaded.manifest.identity.execution_package_id != identity.package_id
    assert loaded.manifest.identity.execution_digest == execution_digest
    assert loaded.manifest.identity.package_identity == compiled.package_identity
    assert loaded.frozen_files == frozen_files
    assert (
        game_root / "private" / "checkpoints" / saved.manifest.checkpoint_id
    ).stat().st_mode & 0o077 == 0

    original_payload = json.loads((saved.path / "payload.json").read_text(encoding="utf-8"))
    with pytest.raises(CorruptPrivateCheckpointError):
        _decode_payload(
            _canonical_json_bytes({**original_payload, "unknown": "must be rejected"}),
            path=saved.path,
            manifest=saved.manifest,
            expected_identity=identity,
        )
    with pytest.raises(CorruptPrivateCheckpointError):
        _decode_payload(
            _canonical_json_bytes({**original_payload, "schema_version": True}),
            path=saved.path,
            manifest=saved.manifest,
            expected_identity=identity,
        )

    with pytest.raises(PrivateCheckpointIdentityError):
        await store.verify(
            saved.manifest.checkpoint_id,
            expected_identity=PrivateCheckpointIdentity(
                game_id=GAME_ID,
                board_ref=compiled.board_ref.format(),
                package_id=compiled.package_id,
                execution_package_id="f" * 64,
                execution_digest=execution_digest,
                package_identity=compiled.package_identity,
            ),
        )
    payload_path = saved.path / "payload.json"
    payload_path.write_bytes(payload_path.read_bytes() + b" ")
    with pytest.raises(CorruptPrivateCheckpointError):
        await store.verify(saved.manifest.checkpoint_id, expected_identity=identity)


@pytest.mark.asyncio
async def test_private_checkpoint_rejects_unsupported_phase_and_payload_tampering(
    tmp_path: Path,
) -> None:
    compiled, _ = await _compiled_workflow_fixture()
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    compiled_store = CompiledKnowledgeStore(tmp_path / "compiled")
    await compiled_store.publish(compiled)
    package_load = await compiled_store.load(compiled.board_ref)
    game_root = tmp_path / "active" / GAME_ID
    game_root.mkdir(parents=True)
    snapshot = await KnowledgeSnapshotBuilder.from_game_root(compiled_store, game_root).create(
        GAME_ID,
        package_load,
    )
    snapshot_manifest = json.loads((snapshot.root / "snapshot.json").read_text(encoding="utf-8"))
    frozen_files = {
        relative: (snapshot.root / relative).read_bytes() for relative in snapshot.file_digests
    }
    # The public phase gate is checked before any filesystem materialization.
    store = PrivateCheckpointStore(game_root)
    invalid = GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_ACTION,
        ruleset=RulesetRef(
            board_id=compiled.board_ref.id,
            version=compiled.board_ref.version,
            snapshot_id=snapshot.snapshot_id,
            manifest_sha256=snapshot.manifest_sha256,
        ),
        execution_identity=RuleExecutionIdentity(
            package_id=compiled.execution.package_id,
            board_id=compiled.execution.board_id,
            board_version=compiled.execution.board_version,
            execution_digest=compiled.execution.package_id,
            action_registry_digest=_digest(compiled.action_registry.model_dump(mode="json")),
        ),
    )
    invalid_manager = GameManager(
        invalid,
        registry=compiled.action_registry,
        execution_package=compiled.execution,
    )
    with pytest.raises(PrivateCheckpointBoundaryError):
        await store.create(
            invalid,
            manager=invalid_manager,
            knowledge_snapshot_manifest=snapshot_manifest,
            frozen_files=frozen_files,
            created_at=NOW.isoformat(),
        )
    assert not (game_root / "private" / "checkpoints").exists()


@pytest.mark.asyncio
async def test_checkpoint_rejects_symlink_and_unexpected_files(tmp_path: Path) -> None:
    game_root = tmp_path / "active" / GAME_ID
    (game_root / "private" / "checkpoints").mkdir(parents=True)
    store = PrivateCheckpointStore(game_root)
    victim = tmp_path / "victim"
    victim.mkdir()
    link = game_root / "private" / "checkpoints" / ("checkpoint-" + "0" * 64)
    with pytest.raises(CorruptPrivateCheckpointError):
        store._resolve_checkpoint(link)
    if os.name != "nt":
        link.symlink_to(victim, target_is_directory=True)
        with pytest.raises(CorruptPrivateCheckpointError):
            store._resolve_checkpoint(link)
