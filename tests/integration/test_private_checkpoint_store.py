"""Private waiting-choice checkpoints remain detached from live packages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from dataclasses import replace
from collections.abc import Mapping
from typing import Any

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
from werewolf.game.actions import load_action_registry
from werewolf.game.manager import EventCommitError, GameManager
from werewolf.game.state import (
    GameState,
    PlayerState,
    RandomStateRef,
    RuleExecutionIdentity,
    RulesetRef,
)
from werewolf.game.setup import PlayerAssignmentPlan
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.runtime_loader import load_runtime_knowledge_bundle_from_snapshot
from werewolf.knowledge.snapshot import KnowledgeSnapshotBuilder
from werewolf.knowledge.refs import VersionedRef
from werewolf.moderator import (
    LastWordsFlow,
    ModeratorSheriffBadgeFlow,
    ModeratorTriggerFlow,
    ModeratorShell,
)
from werewolf.moderator.shell import ModeratorError
from werewolf.moderator.config import parse_player_configuration
from werewolf.moderator.day_flow import DayCoordinator
from werewolf.moderator.sessions import PlayerSessionService
from werewolf.persistence.checkpoint import (
    CorruptPrivateCheckpointError,
    PrivateCheckpointBoundaryError,
    PrivateCheckpointIdentity,
    PrivateCheckpointIdentityError,
    PrivateCheckpointStore,
    _canonical_json_bytes,
    _decode_payload,
)
from werewolf.persistence.archive import GameArchiveStore
from werewolf.persistence.snapshot import GameSnapshotStore
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.player_runtime import ActionResponse, ResponseKind, SpeechResponse
from werewolf.runtime.scripted_runtime import ScriptedRuntime
from test_rules_b_white_explosion import (
    BOARD_REF as WHITE_EXPLOSION_BOARD_REF,
)
from test_rules_b_white_explosion import (
    PUBLISHED_ROOT as WHITE_EXPLOSION_PUBLISHED_ROOT,
)
from test_rules_b_white_explosion import (
    SUNBURST_ACTION,
    _compiled_classic,
    _execution as _white_explosion_execution,
    _experimental_board,
    _manager as _white_explosion_manager,
)
from werewolf.cli_support.checkpoint_cli import verify_private_checkpoint

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
GAME_ID = WORKFLOW_GAME_ID


class ObservedDemoRuntime(DemoRuntime):
    """Keep evidence of the skill status read through this runtime's token."""

    def __init__(self, knowledge_base_url: str, knowledge_token: str) -> None:
        super().__init__(knowledge_base_url, knowledge_token)
        self.skill_status_reads: list[Mapping[str, Any]] = []

    async def _read_skill_status(self) -> Mapping[str, Any]:
        status = await super()._read_skill_status()
        self.skill_status_reads.append(status)
        return status

    def _action_for(self, request: Any, status: Mapping[str, Any]) -> ActionResponse:
        window = request.action_window
        if window is None:
            raise RuntimeError("Hunter action request has no window")
        ability = next(
            (
                item
                for item in status.get("abilities", ())
                if isinstance(item, Mapping)
                and item.get("skill_id") == "experimental_taken_hunter_shot"
                and item.get("enabled") is True
                and item.get("consumed") is False
            ),
            None,
        )
        if not isinstance(ability, Mapping):
            return super()._action_for(request, status)
        action_code = ability.get("action_code")
        if type(action_code) is not int or action_code not in window.allowed_action_codes:
            return super()._action_for(request, status)
        if 3 not in window.candidate_seats:
            raise RuntimeError("fixture target is absent from the runtime-visible candidate set")
        return ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": action_code, "targets": [3]}],
        )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _response_for_white_explosion(request: object) -> object:
    expected_kind = getattr(request, "expected_kind", None)
    request_id = getattr(request, "request_id", None)
    if expected_kind is ResponseKind.SPEECH:
        return SpeechResponse(request_id=request_id, speech={"text": "我已完成发言。"})
    window = getattr(request, "action_window", None)
    allowed = tuple(getattr(window, "allowed_action_codes", ()))
    if SUNBURST_ACTION in allowed:
        code, targets = SUNBURST_ACTION, [2, 11]
    elif 105 in allowed:
        code, targets = 105, [3]
    elif 201 in allowed:
        code, targets = 201, [1]
    elif 202 in allowed:
        code, targets = 202, []
    elif allowed:
        code, targets = allowed[0], []
    else:
        return ActionResponse(request_id=request_id, actions=[])
    return ActionResponse(
        request_id=request_id,
        actions=[{"action_code": code, "targets": targets}],
    )


async def _compiled_white_explosion_package():
    base = await _compiled_classic()
    board = _experimental_board(base, external_night_attack=False)
    execution, registry = _white_explosion_execution(
        base, board, external_night_attack=False
    )
    source = await KnowledgePackageLoader(WHITE_EXPLOSION_PUBLISHED_ROOT).load(
        WHITE_EXPLOSION_BOARD_REF
    )
    execution_payload = json.loads(execution.model_dump_json())
    for derived_field in (
        "window_metadata",
        "boundary_policy",
        "victory_program",
        "night_boundary_schedule",
    ):
        execution_payload.pop(derived_field, None)
    base_codes = {item.action_code for item in load_action_registry().actions}
    action_definitions = [
        item.model_dump(mode="json")
        for item in registry.actions
        if item.action_code not in base_codes
    ]
    source = replace(
        source,
        board=replace(source.board, model=board),
        execution_definition={
            "schema_version": 1,
            "execution": execution_payload,
            "action_definitions": action_definitions,
        },
    )
    compiled = KnowledgePackageCompiler().compile(source)
    assert compiled.execution is not None
    assert compiled.action_registry == registry
    return compiled, board


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


@pytest.mark.asyncio
async def test_white_wolf_hunter_choice_restores_with_fresh_gateway_and_finishes_archive(
    tmp_path: Path,
) -> None:
    """A real white-wolf FLOW and Hunter choice survive a process replacement."""

    compiled, board = await _compiled_white_explosion_package()
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    game_id = "checkpoint-white-explosion"
    seed = 20261025
    compiled_root = tmp_path / "compiled-live"
    compiled_store = CompiledKnowledgeStore(compiled_root)
    await compiled_store.publish(compiled)
    package_load = await compiled_store.load(compiled.board_ref)

    games_root = tmp_path / "games"
    active_root = games_root / "active" / game_id
    active_root.mkdir(parents=True)
    snapshot = await KnowledgeSnapshotBuilder.from_game_root(
        compiled_store,
        active_root,
        game_id=game_id,
    ).create(game_id, package_load)
    bundle = await load_runtime_knowledge_bundle_from_snapshot(snapshot)

    config: dict[str, object] = {
        "schema_version": 1,
        "game": {
            "game_id": game_id,
            "seed": seed,
            "board": {"id": compiled.board_ref.id, "version": compiled.board_ref.version},
        },
        "paths": {"compiled_root": str(compiled_root), "games_root": str(games_root)},
        "players": [{"seat": seat, "runtime": "scripted"} for seat in range(1, 13)],
    }
    config_path = tmp_path / "game.yaml"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    configuration = parse_player_configuration(
        config,
        board=bundle.board,
        game_root=active_root,
        game_id=game_id,
        allow_scripted=True,
    )

    manager_seed = _white_explosion_manager(
        compiled,
        board,
        compiled.execution,
        compiled.action_registry,
        phase=GamePhase.DAY_ANNOUNCE,
        game_id=game_id,
    )
    seed_players = dict(manager_seed.state.players)
    for seat in (1, 3, 4, 5):
        seed_players[seat] = seed_players[seat].model_copy(
            update={"role_id": "villager", "faction_id": "good"}
        )
    assignment_seats = tuple(sorted(seed_players))
    assignments = PlayerAssignmentPlan(
        board_ref=compiled.board_ref,
        seed=seed,
        seats=assignment_seats,
        players={
            seat: seed_players[seat].model_copy(
                update={"runtime_ref": None, "current_request_id": None}
            )
            for seat in assignment_seats
        },
    )
    initial_runtime_map: dict[int, ScriptedRuntime] = {}

    def initial_runtime_factory(player: Any, _gateway_url: str, _token: str) -> ScriptedRuntime:
        runtime = ScriptedRuntime([_response_for_white_explosion] * 64)
        initial_runtime_map[player.seat] = runtime
        return runtime

    manager_box: dict[str, GameManager] = {}

    async def state_provider() -> GameState:
        return await manager_box["manager"].snapshot()

    initial_service = PlayerSessionService(
        runtime_factory=initial_runtime_factory,
        state_provider=state_provider,
    )
    initial_records = await initial_service.start(bundle, configuration, assignments)
    initial_refs = {
        seat: record.runtime_ref.session_id for seat, record in initial_records.items()
    }
    initial_players = {
        seat: assignments.players[seat].model_copy(update={"runtime_ref": initial_refs[seat]})
        for seat in assignment_seats
    }
    manager_state = manager_seed.state.model_copy(
        update={
            "players": initial_players,
            "ruleset": RulesetRef(
                board_id=compiled.board_ref.id,
                version=compiled.board_ref.version,
                snapshot_id=snapshot.snapshot_id,
                manifest_sha256=snapshot.manifest_sha256,
            ),
            "rng": RandomStateRef(seed=seed),
            "sheriff_seat": 11,
        }
    )
    manager = GameManager(
        manager_state,
        registry=compiled.action_registry,
        execution_package=compiled.execution,
        package_identity=compiled.package_identity,
    )
    manager_box["manager"] = manager

    initial_shell = ModeratorShell(config_path, clock=lambda: NOW)
    initial_shell.config = config
    initial_shell.games_root = games_root
    initial_shell.active_root = active_root
    initial_shell.ruleset_snapshot = snapshot
    initial_shell.runtime_bundle = bundle
    initial_shell.manager = manager
    initial_shell.assignment_plan = assignments
    initial_shell.session_refs = initial_refs
    initial_shell.session_service = initial_service
    initial_shell.snapshot_store = GameSnapshotStore(active_root)
    initial_shell.archive_store = GameArchiveStore(games_root)
    baseline_manager = _white_explosion_manager(
        compiled,
        board,
        compiled.execution,
        compiled.action_registry,
        phase=GamePhase.VICTORY_CHECK,
        game_id=game_id,
    )
    baseline = await baseline_manager.commit_victory_check(
        board,
        expected_revision=baseline_manager.state.state_revision,
        now=NOW,
    )
    assert baseline.phase is GamePhase.NIGHT_TEAM_CHAT
    await initial_shell.snapshot_store.create(baseline, created_at=NOW)

    try:
        day = DayCoordinator(manager, board, initial_service.runtimes)
        await day.announce(now=NOW)
        await day.open_speech(now=NOW)
        await day.run_next_speech()
        triggers = ModeratorTriggerFlow(
            manager,
            board,
            {2: initial_service.runtimes[2], 11: initial_service.runtimes[11]},
            clock=lambda: NOW,
        )
        hook = await triggers.poll_hook("DAY_SPEECH_AFTER")
        assert hook["status"] == "choice_pending"
        explosion = await triggers.next(2)
        assert explosion["status"] == "accepted"
        assert manager.state.players[2].death_cause == "self_explosion"
        assert manager.state.players[11].death_cause == "self_explosion"

        hunter_pending = await triggers.open(now=NOW)
        assert hunter_pending.action_window is not None
        assert hunter_pending.action_window.allowed_seats == (11,)
        saved = await initial_shell.checkpoint_save()
        checkpoint_id = saved["checkpoint_id"]
        assert isinstance(checkpoint_id, str)
        assert manager.state.rule_workflow_cursor is not None
        assert manager.state.rule_workflow_cursor.status == "WAITING_CHOICE"
        assert manager.state.rule_workflow_cursor.pending_flow_action == "ADVANCE_TO_NIGHT"

        old_epochs = {seat: player.session_epoch for seat, player in manager.state.players.items()}
        await initial_service.close("checkpoint process replaced")
        shutil.rmtree(compiled_root)

        cli_report = await asyncio.to_thread(
            verify_private_checkpoint,
            config_path,
            checkpoint_id,
        )
        assert cli_report["status"] == "verified"
        assert cli_report["checkpoint_id"] == checkpoint_id

        failed_runtimes: list[ScriptedRuntime] = []

        class PartialStartupFailure(ScriptedRuntime):
            async def start(self, runtime_config, context):
                result = await super().start(runtime_config, context)
                if context.seat == 6:
                    raise RuntimeError("injected checkpoint startup failure")
                return result

        def failing_runtime_factory(
            _player: Any,
            _gateway_url: str,
            _token: str,
        ) -> ScriptedRuntime:
            runtime = PartialStartupFailure([_response_for_white_explosion] * 64)
            failed_runtimes.append(runtime)
            return runtime

        failed_shell = ModeratorShell(
            config_path,
            runtime_factory=failing_runtime_factory,
            clock=lambda: NOW,
        )
        with pytest.raises(ModeratorError, match="restore failed") as failure:
            await failed_shell.checkpoint_restore(checkpoint_id)
        assert failed_shell.manager is None
        assert failed_shell.session_service is None
        assert failed_shell._checkpoint_bundle_tempdir is None
        assert failed_runtimes and all(runtime._closed for runtime in failed_runtimes), str(
            failure.value
        )
        assert (await failed_shell.checkpoint_verify(checkpoint_id))["status"] == "verified"

        fresh_runtime_map: dict[int, ObservedDemoRuntime] = {}

        def fresh_runtime_factory(
            player: Any,
            _gateway_url: str,
            _token: str,
        ) -> ObservedDemoRuntime:
            runtime = ObservedDemoRuntime(_gateway_url, _token)
            fresh_runtime_map[player.seat] = runtime
            return runtime

        restored_shell = ModeratorShell(
            config_path,
            runtime_factory=fresh_runtime_factory,
            clock=lambda: NOW,
        )
        restored = await restored_shell.checkpoint_restore(checkpoint_id)
        assert restored["status"] == "restored"
        assert restored_shell.manager is not None
        assert restored_shell.session_service is not None
        restored_manager = restored_shell.manager
        assert {
            seat: player.session_epoch for seat, player in restored_manager.state.players.items()
        } == {seat: epoch + 1 for seat, epoch in old_epochs.items()}
        assert all(runtime._context is not None for runtime in fresh_runtime_map.values())

        cursor = restored_manager.state.rule_workflow_cursor
        assert cursor is not None and cursor.active_occurrence_id is not None
        occurrence = next(
            item
            for item in restored_manager.state.rule_trigger_queue
            if item.occurrence_id == cursor.active_occurrence_id
        )
        assert occurrence.actor_seat == 11 and occurrence.window_id is not None
        with pytest.raises(EventCommitError, match="SESSION_MISMATCH"):
            await restored_manager.begin_action_turn(
                11,
                old_epochs[11],
                window_id=occurrence.window_id,
                request_id="stale-pre-restore-hunter-request",
                now=NOW,
            )

        restored_triggers = ModeratorTriggerFlow(
            restored_manager,
            bundle.board,
            restored_shell.session_service.runtimes,
            clock=lambda: NOW,
        )
        alive_before_shot = {
            seat for seat, player in restored_manager.state.players.items() if player.alive
        }
        hunter_action = await restored_triggers.next(11)
        assert hunter_action["status"] == "accepted"
        hunter_runtime = fresh_runtime_map[11]
        assert hunter_runtime.receipts
        assert len(hunter_runtime.skill_status_reads) == 1
        hunter_status = hunter_runtime.skill_status_reads[0]
        assert hunter_status["session_epoch"] == old_epochs[11] + 1
        assert any(
            item["skill_id"] == "experimental_taken_hunter_shot"
            for item in hunter_status["abilities"]
        )
        assert not restored_manager.state.players[3].alive
        hunted_seats = {
            seat
            for seat in alive_before_shot
            if not restored_manager.state.players[seat].alive
        }
        assert len(hunted_seats) == 1
        assert restored_manager.state.players[next(iter(hunted_seats))].faction_id == "good"
        shot_skill = next(
            item
            for item in restored_manager.state.ability_instances
            if item.actor_seat == 11 and item.skill_id == "experimental_taken_hunter_shot"
        )
        assert shot_skill.uses_consumed == 1

        words = LastWordsFlow(
            restored_manager,
            bundle.board,
            restored_shell.session_service.runtimes,
        )
        while words.status()["pending_seats"]:
            result = await words.next()
            assert result.event.payload.content.strip()
            assert result.event.channel.value == "PUBLIC"

        badge = ModeratorSheriffBadgeFlow(
            restored_manager,
            bundle.board,
            restored_shell.session_service.runtimes,
            clock=lambda: NOW,
        )
        await badge.next()
        await badge.resolve()
        await badge.finish()
        assert restored_manager.state.sheriff_seat == 1

        await restored_triggers.finish(now=NOW)
        flow_facts = tuple(
            fact
            for ledger in restored_manager.state.rule_ledger
            for fact in ledger.facts
            if fact.fact_type == "RULE_FLOW_QUEUED"
            and fact.data.get("flow_action") == "ADVANCE_TO_NIGHT"
        )
        assert len(flow_facts) == 1
        flow_audits = tuple(
            audit
            for audit in restored_manager.state.moderator_audit
            if audit.get("operation") == "RULE_FLOW_ACTION_COMPLETED"
            and audit.get("flow_fact_id") == flow_facts[0].fact_id
        )
        assert len(flow_audits) == 1
        victory = await restored_manager.commit_victory_check(
            bundle.board,
            expected_revision=restored_manager.state.state_revision,
            now=NOW,
        )
        assert victory.phase is GamePhase.FINISHED
        assert victory.winner is not None

        finished = await restored_shell.finish()
        archive_path = Path(str(finished["path"]))
        archive_manifest = await GameArchiveStore(games_root).verify(archive_path)
        assert archive_manifest.game_id == game_id
        final_state = GameState.model_validate_json((archive_path / "state.json").read_bytes())
        assert final_state.phase is GamePhase.FINISHED
        assert final_state.run_status.value == "CLOSED"

        replay_shell = ModeratorShell(config_path, clock=lambda: NOW)
        with pytest.raises(ModeratorError, match="blocks checkpoint replay"):
            await replay_shell.checkpoint_restore(checkpoint_id)
    finally:
        await initial_service.close("checkpoint test complete")
        if "restored_shell" in locals() and restored_shell.session_service is not None:
            await restored_shell.session_service.close("checkpoint test complete")
        if "restored_shell" in locals() and restored_shell._checkpoint_bundle_tempdir is not None:
            restored_shell._checkpoint_bundle_tempdir.cleanup()
