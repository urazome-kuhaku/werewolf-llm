"""Private waiting-choice checkpoints remain detached from live packages."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_rules_b_workflow import _compiled_workflow_fixture

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.game.actions import ActionWindow
from werewolf.game.state import (
    AbilityInstanceState,
    GameState,
    PlayerState,
    RuleCommitReceipt,
    RuleExecutionIdentity,
    RuleFactRecord,
    RuleLedgerEntry,
    RulesetRef,
    RuleTriggerOccurrence,
    RuleWorkflowCursor,
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
GAME_ID = "private-choice-checkpoint"


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
    executable_wrapper = compiled.package_payload["executable"]
    assert isinstance(executable_wrapper, dict)
    execution_digest = compiled.manifest_payload["execution_sha256"]
    assert isinstance(execution_digest, str)
    action_registry_digest = _digest(compiled.action_registry.model_dump(mode="json"))
    skill = next(
        item
        for item in compiled.execution.skills
        if item.trigger is not None and item.trigger.mode == "PLAYER_CHOICE"
    )
    assert skill.trigger is not None
    source_fact_id = "confirmed-death-fact-1"
    source_batch_id = "source-rule-batch-1"
    source_request_id = "source-internal-request-1"
    occurrence_id = "hunter-choice-1"
    window_id = "hunter-choice-window-1"
    ability_id = "hunter-trigger-instance-1"
    action_code = skill.action_code
    source_fact = RuleFactRecord(
        fact_id=source_fact_id,
        fact_type=skill.trigger.fact_types[0],
        source_request_id=source_request_id,
        actor_seat=2,
        target_seat=2,
        death_cause="white_wolf_explosion",
    )
    trigger_window = ActionWindow(
        window_id=window_id,
        game_id=GAME_ID,
        session_epoch=4,
        phase=GamePhase.TRIGGER_ACTION,
        allowed_seats=(2,),
        allowed_action_codes=(action_code,),
        opened_at=NOW,
        settlement_group_id="trigger-group-1",
        logical_window_id="hunter-choice",
        visible_context={
            "rule_occurrence_id": occurrence_id,
            "rule_source_fact_id": source_fact_id,
            "rule_actor_seat": 2,
            "rule_ability_instance_id": ability_id,
            "rule_skill_id": skill.skill_id,
            "rule_action_code": action_code,
        },
    )
    closed_source_window = ActionWindow(
        window_id="source-window-1",
        game_id=GAME_ID,
        session_epoch=4,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(2,),
        allowed_action_codes=(299,),
        opened_at=NOW,
        closed_at=NOW,
        settlement_group_id="source-group-1",
    )
    ledger = RuleLedgerEntry(
        batch_id=source_batch_id,
        package_id=compiled.execution.package_id,
        group_id="source-group-1",
        timing="NIGHT_ACTION",
        read_revision=10,
        committed_revision=11,
        round_no=1,
        request_ids=("source-physical-request-1",),
        actor_seats=(2,),
        skill_ids=("source-skill-1",),
        action_codes=(299,),
        facts=(source_fact,),
        outcome_digest="a" * 64,
        created_at=NOW,
    )
    receipt = RuleCommitReceipt(
        batch_id=source_batch_id,
        package_id=compiled.execution.package_id,
        group_id="source-group-1",
        timing="NIGHT_ACTION",
        read_revision=10,
        committed_revision=11,
        request_ids=("source-physical-request-1",),
        outcome_digest="a" * 64,
    )
    state = GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.TRIGGER_ACTION,
        run_status=RunStatus.RUNNING,
        round_no=1,
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
            action_registry_digest=action_registry_digest,
        ),
        ability_instances=(
            AbilityInstanceState(
                ability_instance_id=ability_id,
                skill_id=skill.skill_id,
                grant_id=skill.grants[0].grant_id,
                action_code=action_code,
                actor_seat=2,
                grant_kind="TRIGGER",
            ),
        ),
        rule_trigger_queue=(
            RuleTriggerOccurrence(
                occurrence_id=occurrence_id,
                kind="TRIGGER",
                source_fact_id=source_fact_id,
                source_batch_id=source_batch_id,
                ability_instance_id=ability_id,
                skill_id=skill.skill_id,
                actor_seat=2,
                mode="PLAYER_CHOICE",
                order=1,
                status="WAITING_CHOICE",
                window_id=window_id,
                logical_window_id="hunter-choice",
            ),
        ),
        rule_workflow_cursor=RuleWorkflowCursor(
            cursor_id="cursor-1",
            settlement_group_id="trigger-group-1",
            active_window_ids=(window_id,),
            active_occurrence_id=occurrence_id,
            status="WAITING_CHOICE",
        ),
        rule_ledger=(ledger,),
        rule_receipts=(receipt,),
        players={
            1: PlayerState(
                seat=1,
                role_id="villager",
                faction_id="good",
                runtime_ref="old-runtime-1",
                session_epoch=4,
            ),
            2: PlayerState(
                seat=2,
                role_id="wolf",
                faction_id="wolf",
                alive=False,
                death_cause="white_wolf_explosion",
                runtime_ref="old-runtime-1",
                session_epoch=4,
            ),
        },
        action_windows={
            window_id: trigger_window.model_dump(mode="json"),
            closed_source_window.window_id: closed_source_window.model_dump(mode="json"),
        },
        action_requests={
            "source-physical-request-1": {
                "request_id": "source-physical-request-1",
                "window_id": "source-window-1",
                "seat": 2,
                "status": "CONFIRMED",
            }
        },
        resolutions=({"request_id": "source-physical-request-1", "status": "CONFIRMED"},),
        pending_resolution={
            "operation": "RULE_TRIGGER",
            "status": "RULE_TRIGGER_ACTION_REQUIRED",
            "occurrence_id": occurrence_id,
            "window_id": window_id,
            "source_fact_id": source_fact_id,
            "actor_seat": 2,
        },
    )
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
    with pytest.raises(PrivateCheckpointBoundaryError):
        await store.create(
            invalid,
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
