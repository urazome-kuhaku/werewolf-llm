"""Executable death triggers use the pinned rules package and manager boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_hunter_trigger import _state as legacy_hunter_state

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionRequest,
    ActionResolution,
    GameManager,
    RulesetRef,
    load_action_registry,
)
from werewolf.game.state import RuleFactRecord, RuleLedgerEntry
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiler import CompiledKnowledgePackage, KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.moderator.trigger_flow import ModeratorTriggerError, ModeratorTriggerFlow
from werewolf.rules.models import ExecutionPackage
from werewolf.runtime.player_runtime import ActionResponse, InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
NOW = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)


async def _compiled_classic() -> CompiledKnowledgePackage:
    package = await KnowledgePackageLoader(PUBLISHED_ROOT).load(BOARD_REF)
    return KnowledgePackageCompiler().compile(package)


def _execution_and_board(
    compiled: CompiledKnowledgePackage,
    *,
    unknown_ids: bool = False,
) -> tuple[ExecutionPackage, BoardDefinition]:
    execution = compiled.execution
    if execution is None:
        raise AssertionError("classic compatibility did not produce an execution package")
    board = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    if unknown_ids:
        new_board_id = "moonlit_sentinel_board"
        new_skill_id = "unheard_death_choice"
        execution = execution.model_copy(
            update={
                "board_id": new_board_id,
                "skills": tuple(
                    item.model_copy(update={"skill_id": new_skill_id})
                    if item.action_code == 105
                    else item
                    for item in execution.skills
                ),
            }
        )
        board = board.model_copy(update={"board_id": new_board_id})
    return execution, board


def _manager(
    compiled: CompiledKnowledgePackage,
    execution: ExecutionPackage,
    *,
    game_id: str = "hunter-game",
) -> GameManager:
    registry = compiled.action_registry or load_action_registry()
    original = legacy_hunter_state()
    base = original.model_copy(
        update={
            "game_id": game_id,
            "ruleset": RulesetRef(
                board_id=execution.board_id,
                version=execution.board_version,
                snapshot_id=f"ruleset-{compiled.package_identity}",
                manifest_sha256=compiled.manifest_sha256,
            ),
        }
    )
    manager = GameManager(base, registry=registry, execution_package=execution)

    # Seed the already-confirmed source death as prior package output. The
    # trigger interpreter requires a confirmed-death fact with its rule tag.
    prior_death = RuleFactRecord(
        fact_id="prior-confirmed-exile",
        fact_type="death_confirmed",
        source_rule_id="classic_final_death_fact",
        target_seat=1,
        tags=("exiled",),
    )
    prior_ledger = RuleLedgerEntry(
        batch_id="prior-exile-batch",
        package_id=execution.package_id,
        group_id="prior-exile",
        timing="DAY_RESOLVE",
        read_revision=0,
        committed_revision=0,
        round_no=0,
        request_ids=(),
        actor_seats=(),
        skill_ids=(),
        action_codes=(),
        facts=(prior_death,),
        outcome_digest="a" * 64,
        created_at=NOW,
    )
    manager._state = manager.state.model_copy(update={"rule_ledger": (prior_ledger,)})
    return manager


async def _runtime(*responses: object) -> ScriptedRuntime:
    runtime = ScriptedRuntime(responses)
    await runtime.start(
        RuntimeConfig(session_id="rules-trigger-session"),
        InitialContext(game_id="hunter-game", seat=1, session_epoch=2),
    )
    return runtime


def _request_from_state(manager: GameManager, window_id: str) -> ActionRequest:
    records = [
        payload
        for payload in manager.state.action_requests.values()
        if payload.get("window_id") == window_id and payload.get("status") == "PENDING"
    ]
    assert len(records) == 1
    payload = records[0]
    data = {key: payload[key] for key in ActionRequest.model_fields if key in payload}
    if isinstance(data.get("phase"), str):
        data["phase"] = GamePhase(data["phase"])
    return ActionRequest.model_validate(data)


@pytest.mark.asyncio
async def test_executable_hunter_request_uses_package_and_matches_classic_oracle() -> None:
    compiled = await _compiled_classic()
    execution, board = _execution_and_board(compiled)
    manager = _manager(compiled, execution)
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 105, "targets": [2]}],
        )
    )
    flow = ModeratorTriggerFlow(manager, board, {1: runtime}, clock=lambda: NOW)
    try:
        opened = await flow.open(now=NOW)
        assert opened.action_window.window_id == "exile-1-trigger-death-shot"
        turn = await flow.next()
        assert turn["status"] == "accepted"
        before_commit = manager.state
        request = _request_from_state(manager, opened.action_window.window_id)

        # Keep the legacy adjudicator as a differential oracle in this test
        # only. Production executable resolution goes through the interpreter.
        from werewolf.moderator.classic_resolution import build_classic_trigger_resolution

        legacy_oracle_state = before_commit.model_copy(
            update={
                "execution_identity": None,
                "ability_instances": (),
                "rule_state": (),
                "rule_ledger": (),
                "rule_receipts": (),
            }
        )
        oracle_resolution = build_classic_trigger_resolution(
            legacy_oracle_state,
            request,
            registry=compiled.action_registry,
            now=NOW,
        )
        oracle_manager = GameManager(
            legacy_oracle_state,
            registry=compiled.action_registry or load_action_registry(),
        )
        oracle_state = await oracle_manager.commit_action_resolution(
            oracle_resolution,
            expected_revision=before_commit.state_revision,
            now=NOW,
        )

        result = await flow.auto_resolve()
        committed = manager.state
        assert result["phase"] == GamePhase.TRIGGER_ACTION.value
        assert committed.players[2].alive is False
        assert committed.players[2].death_cause == "hunter_shot"
        assert (committed.players[2].alive, committed.players[2].death_cause) == (
            oracle_state.players[2].alive,
            oracle_state.players[2].death_cause,
        )
        assert committed.players[1].granted_trigger_abilities[0].consumed is True
        trigger_instance = next(
            item
            for item in committed.ability_instances
            if item.actor_seat == 1 and item.action_code == 105
        )
        assert trigger_instance.consumed is True
        assert trigger_instance.uses_consumed == 1
        assert committed.pending_resolution is None
        assert committed.action_windows[opened.action_window.window_id]["closed_at"] is not None
    finally:
        await runtime.close("rules trigger test complete")


@pytest.mark.asyncio
async def test_trigger_pass_retry_is_idempotent_and_does_not_use_skill_charge() -> None:
    compiled = await _compiled_classic()
    execution, board = _execution_and_board(compiled)
    manager = _manager(compiled, execution)
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 105, "targets": [1]}],
        ),
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 299}],
        ),
    )
    flow = ModeratorTriggerFlow(manager, board, {1: runtime}, clock=lambda: NOW)
    try:
        await flow.open(now=NOW)
        with pytest.raises(ModeratorTriggerError, match="TARGET_NOT_ALLOWED"):
            await flow.next()
        retry = await flow.retry()
        assert retry["attempt_no"] == 2
        assert len(runtime.requests) == 2
        assert runtime.requests[0].logical_request_id == runtime.requests[1].logical_request_id

        await flow.auto_resolve()
        committed = manager.state
        assert committed.players[2].alive is True
        assert committed.players[1].granted_trigger_abilities[0].consumed is True
        trigger_instance = next(
            item for item in committed.ability_instances if item.grant_kind == "TRIGGER"
        )
        assert trigger_instance.uses_consumed == 0
        assert trigger_instance.consumed is True
        receipt = ActionResolution.model_validate(committed.resolutions[-1])

        replay = await manager.commit_action_resolutions(
            (receipt,),
            use_rules_engine=True,
            expected_revision=committed.state_revision,
            now=NOW,
        )
        assert replay is committed
        replayed_trigger = next(
            item
            for item in replay.ability_instances
            if item.actor_seat == 1 and item.action_code == 105
        )
        assert replayed_trigger.uses_consumed == 0
        assert len(replay.rule_receipts) == 1
    finally:
        await runtime.close("rules trigger test complete")


@pytest.mark.asyncio
async def test_executable_trigger_accepts_unrecognized_board_and_skill_ids() -> None:
    compiled = await _compiled_classic()
    execution, board = _execution_and_board(compiled, unknown_ids=True)
    manager = _manager(compiled, execution)
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 105, "targets": [2]}],
        )
    )
    flow = ModeratorTriggerFlow(manager, board, {1: runtime}, clock=lambda: NOW)
    try:
        await flow.auto_resolve()
        assert manager.execution_package is not None
        assert manager.execution_package.board_id == "moonlit_sentinel_board"
        assert any(
            item.skill_id == "unheard_death_choice" for item in manager.state.ability_instances
        )
        assert manager.state.players[2].alive is False
        assert manager.state.players[2].death_cause == "hunter_shot"
    finally:
        await runtime.close("rules trigger test complete")


@pytest.mark.asyncio
async def test_bound_missing_window_is_never_fabricated_by_auto_resolve() -> None:
    compiled = await _compiled_classic()
    execution, board = _execution_and_board(compiled)
    manager = _manager(compiled, execution)
    pending = dict(manager.state.pending_resolution or {})
    pending["window_id"] = "forged-trigger-window"
    manager._state = manager.state.model_copy(update={"pending_resolution": pending})
    flow = ModeratorTriggerFlow(manager, board, {}, clock=lambda: NOW)

    with pytest.raises(ModeratorTriggerError, match="TRIGGER_WINDOW_NOT_OPEN"):
        await flow.auto_resolve()
    assert "forged-trigger-window" not in manager.state.action_windows
    assert manager.state.action_requests == {}


@pytest.mark.asyncio
async def test_missing_pinned_execution_package_does_not_fall_back_to_classic() -> None:
    compiled = await _compiled_classic()
    execution, board = _execution_and_board(compiled)
    pinned_manager = _manager(compiled, execution)
    original_state = pinned_manager.state
    original_state_dump = original_state.model_dump(mode="python")

    with pytest.raises(ValueError, match="execution identity but no frozen execution package"):
        GameManager(
            original_state,
            registry=compiled.action_registry or load_action_registry(),
        )

    assert pinned_manager.state is original_state
    assert pinned_manager.state.model_dump(mode="python") == original_state_dump
    assert pinned_manager.state.action_windows == original_state.action_windows
    assert pinned_manager.state.action_requests == original_state.action_requests
