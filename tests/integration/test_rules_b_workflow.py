"""Real manager acceptance for resumable data-defined death workflows."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionRequest,
    ActionValidationContext,
    ActionWindow,
    GameManager,
    GameState,
    PlayerState,
    RulesetRef,
)
from werewolf.game.manager import EventCommitError, ResolutionError
from werewolf.game.state import RuleWorkflowCursor
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.rules.compiler import execution_windows_from_board, validate_execution_package

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
GAME_ID = "generic-workflow-chain-game"
SNAPSHOT_ID = "generic-workflow-chain-snapshot"
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"

MASS_ACTION = 981
AUTO_ACTION = 982
CHOICE_ACTION = 983


def _ref(source: str, name: str) -> dict[str, object]:
    return {"op": "ref", "source": source, "name": name}


def _literal(value: object) -> dict[str, object]:
    return {"op": "literal", "value": value}


def _compare(op: str, left: object, right: object) -> dict[str, object]:
    return {"op": op, "left": left, "right": right}


def _selector(
    *,
    alive_only: bool = False,
    exclude_actor: bool = False,
    seats: tuple[int, ...] | None = None,
) -> dict[str, object]:
    conditions: list[dict[str, object]] = []
    if alive_only:
        conditions.append(_compare("eq", _ref("item", "alive"), _literal(True)))
    if exclude_actor:
        conditions.append(_compare("ne", _ref("item", "seat"), _ref("actor", "seat")))
    if seats is not None:
        conditions.extend(
            (
                _compare("ge", _ref("item", "seat"), _literal(min(seats))),
                _compare("le", _ref("item", "seat"), _literal(max(seats))),
            )
        )
    where: dict[str, object] | None = None
    if len(conditions) == 1:
        where = conditions[0]
    elif conditions:
        where = {"op": "and", "values": conditions}
    return {
        "op": "select",
        "source": "players",
        **({"where": where} if where is not None else {}),
        "map": _ref("item", "seat"),
    }


def _execution_definition(
    board_id: str,
    board_version: str,
    *,
    cyclic_auto: bool,
    settlement_groups: dict[str, str],
) -> dict[str, object]:
    mass_target_count = 1 if cyclic_auto else 2
    mass_effects = [
        {
            "effect_id": "generic-mass-casualty",
            "effect_type": "DAMAGE",
            "target": _ref("target", "seat"),
            "tags": ["generic_mass_casualty"],
        }
    ]
    auto_facts = (
        (
            "DEATH_CONFIRMED",
            "generic_auto_echo",
        )
        if cyclic_auto
        else ("DEATH_CONFIRMED",)
    )
    auto_usage = {"scope": "GAME"} if cyclic_auto else {"max_uses": 1, "scope": "GAME"}
    own_death_condition = _compare(
        "eq",
        _ref("source_fact", "target_seat"),
        _ref("actor", "seat"),
    )
    return {
        "schema_version": 1,
        "execution": {
            "board_id": board_id,
            "board_version": board_version,
            "actions": [
                {"action_code": MASS_ACTION, "action_id": "GENERIC_MASS_CASUALTY"},
                {"action_code": AUTO_ACTION, "action_id": "GENERIC_AUTO_ECHO"},
                {"action_code": CHOICE_ACTION, "action_id": "GENERIC_CHOICE_STRIKE"},
            ],
            "skills": [
                {
                    "skill_id": "generic_mass_casualty_skill",
                    "action_code": MASS_ACTION,
                    "grants": [
                        {
                            "grant_id": "generic_mass_casualty_grant",
                            "actor_selector": {
                                "op": "select",
                                "source": "players",
                                "where": _compare("eq", _ref("item", "seat"), _literal(1)),
                                "map": _ref("item", "seat"),
                            },
                        }
                    ],
                    "timing": ["NIGHT_ACTION"],
                    "targets": {
                        "min_targets": mass_target_count,
                        "max_targets": mass_target_count,
                        "selector": _selector(alive_only=True, exclude_actor=True),
                        "allow_self": False,
                    },
                    "usage": {"max_uses": 1, "scope": "GAME"},
                    "effects": mass_effects,
                },
                {
                    "skill_id": "generic_automatic_echo_skill",
                    "mode": "AUTOMATIC",
                    "action_code": AUTO_ACTION,
                    "grants": [
                        {
                            "grant_id": "a_generic_auto_grant",
                            "actor_selector": _selector(seats=(2, 3, 4)),
                        }
                    ],
                    "timing": ["TRIGGER_ACTION"],
                    "trigger": {
                        "fact_types": list(auto_facts),
                        "mode": "AUTOMATIC",
                        "condition": own_death_condition,
                    },
                    "targets": {
                        "min_targets": 0,
                        "max_targets": 0,
                        "selector": _selector(),
                    },
                    "usage": auto_usage,
                    "effects": [
                        {
                            "effect_id": "emit-generic-auto-echo",
                            "effect_type": "FACT",
                            "target": _ref("actor", "seat"),
                            "fact_type": "generic_auto_echo",
                        }
                    ],
                },
                {
                    "skill_id": "generic_player_choice_skill",
                    "action_code": CHOICE_ACTION,
                    "grants": [
                        {
                            "grant_id": "z_generic_choice_grant",
                            "actor_selector": _selector(seats=(2, 3, 4)),
                        }
                    ],
                    "timing": ["TRIGGER_ACTION"],
                    "trigger": {
                        "fact_types": ["DEATH_CONFIRMED"],
                        "mode": "PLAYER_CHOICE",
                        "condition": own_death_condition,
                    },
                    "targets": {
                        "min_targets": 1,
                        "max_targets": 1,
                        "selector": _selector(alive_only=True, exclude_actor=True),
                        "allow_self": False,
                    },
                    "usage": {
                        "max_uses": 1,
                        "scope": "GAME",
                        "costs": [{"resource_id": "charge", "amount": 1}],
                    },
                    "effects": [
                        {
                            "effect_id": "generic-choice-strike",
                            "effect_type": "DAMAGE",
                            "target": _ref("target", "seat"),
                            "tags": ["generic_choice_strike"],
                        }
                    ],
                },
            ],
            "resource_declarations": [{"resource_id": "charge", "min_value": 0, "max_value": 4}],
            "window_settlement_groups": settlement_groups,
            "interactions": [
                {
                    "interaction_id": "confirm-generic-mass-death",
                    "rule_type": "CONFIRM_DEATH",
                    "damage_tags": ["generic_mass_casualty"],
                    "death_cause": "generic_chain_death",
                },
                {
                    "interaction_id": "confirm-generic-choice-death",
                    "rule_type": "CONFIRM_DEATH",
                    "damage_tags": ["generic_choice_strike"],
                    "death_cause": "generic_chain_death",
                },
            ],
        },
        "action_definitions": [
            {
                "action_code": MASS_ACTION,
                "action_name": "GENERIC_MASS_CASUALTY",
                "target_policy": "candidate",
                "target_count": mass_target_count,
            },
            {
                "action_code": AUTO_ACTION,
                "action_name": "GENERIC_AUTO_ECHO",
                "target_policy": "none",
                "target_count": 0,
            },
            {
                "action_code": CHOICE_ACTION,
                "action_name": "GENERIC_CHOICE_STRIKE",
                "target_policy": "candidate",
                "target_count": 1,
            },
        ],
    }


async def _compiled_workflow_fixture(*, cyclic_auto: bool = False):
    """Compile a complete data package against a reviewed package dependency closure."""

    source_package = await KnowledgePackageLoader(PUBLISHED_ROOT).load(BOARD_REF)
    board = source_package.board.model
    windows = execution_windows_from_board(board)
    execution_definition = _execution_definition(
        board.board_id,
        board.version,
        cyclic_auto=cyclic_auto,
        settlement_groups={item.window_id: f"group-{item.window_id}" for item in windows},
    )
    test_package = replace(
        source_package,
        execution_definition=execution_definition,
    )
    compiled = KnowledgePackageCompiler().compile(test_package)
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    validate_execution_package(
        compiled.execution,
        compiled.action_registry,
        available_windows=compiled.execution.window_metadata,
        expected_boundary_policy=compiled.execution.boundary_policy,
    )
    return compiled, board


async def _manager(
    *, budget_limit: int = 512, cyclic_auto: bool = False
) -> tuple[GameManager, BoardDefinition]:
    compiled, board = await _compiled_workflow_fixture(cyclic_auto=cyclic_auto)
    role_by_seat = {
        1: "villager",
        2: "wolf",
        3: "wolf",
        4: "wolf",
        5: "wolf",
        6: "seer",
        7: "villager",
        8: "villager",
        9: "villager",
        10: "witch",
        11: "hunter",
        12: "idiot",
    }
    assert {
        role_id: sum(candidate == role_id for candidate in role_by_seat.values())
        for role_id in set(role_by_seat.values())
    } == {binding.role_ref.id: binding.count for binding in board.role_bindings}
    players = {
        seat: PlayerState(
            seat=seat,
            role_id=role_id,
            faction_id="wolf" if role_id == "wolf" else "good",
            session_epoch=7,
            skill_resources={"charge": 2},
        )
        for seat, role_id in role_by_seat.items()
    }
    state = GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        round_no=1,
        day_no=1,
        ruleset=RulesetRef(
            board_id=compiled.board_ref.id,
            version=compiled.board_ref.version,
            snapshot_id=SNAPSHOT_ID,
            manifest_sha256=compiled.manifest_sha256,
        ),
        players=players,
        current_queue=(),
        rule_workflow_cursor=RuleWorkflowCursor(budget_limit=budget_limit),
    )
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    return (
        GameManager(
            state,
            registry=compiled.action_registry,
            execution_package=compiled.execution,
        ),
        board,
    )


async def _settle_empty_frozen_window(
    manager: GameManager,
    *,
    logical_window_id: str,
) -> GameState:
    package = manager.execution_package
    assert package is not None
    row = next(item for item in package.window_metadata if item.window_id == logical_window_id)
    group_id = (
        f"night:{manager.state.round_no}:{package.window_settlement_groups[logical_window_id]}"
    )
    next_row = next(
        (item for item in package.window_metadata if item.order == row.order + 1),
        None,
    )
    window_id = f"empty-window-{logical_window_id}"
    await manager.commit_action_window(
        ActionWindow(
            window_id=window_id,
            game_id=GAME_ID,
            session_epoch=7,
            phase=GamePhase(row.phase),
            allowed_seats=(),
            allowed_action_codes=(),
            min_actions=0,
            max_actions=0,
            settlement_group_id=group_id,
            logical_window_id=logical_window_id,
            next_window_id=next_row.window_id if next_row is not None else None,
            collection_only=True,
            opened_at=NOW,
        ),
        now=NOW,
    )
    await manager.complete_rule_window(
        window_id,
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    await manager.commit_rule_group(
        group_id,
        request_ids=(),
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    progress = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert progress.kind == "RETURN"
    assert not progress.queue_pending
    return manager.state


async def _settle_initial_multi_death(
    manager: GameManager,
    *,
    targets: tuple[int, ...] = (2, 3),
) -> GameState:
    package = manager.execution_package
    assert package is not None
    team_chat = next(
        item for item in package.window_metadata if item.phase == GamePhase.NIGHT_TEAM_CHAT.value
    )
    await _settle_empty_frozen_window(manager, logical_window_id=team_chat.window_id)
    assert manager.state.phase is GamePhase.NIGHT_ACTION
    action_row = next(
        item for item in package.window_metadata if item.phase == GamePhase.NIGHT_ACTION.value
    )
    group_id = (
        f"night:{manager.state.round_no}:{package.window_settlement_groups[action_row.window_id]}"
    )
    next_row = next(
        (item for item in package.window_metadata if item.order == action_row.order + 1),
        None,
    )
    window_id = "workflow-mass-death-window"
    request_id = "workflow-mass-death-request"
    window = ActionWindow(
        window_id=window_id,
        game_id=GAME_ID,
        session_epoch=7,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_action_codes=(MASS_ACTION,),
        settlement_group_id=group_id,
        logical_window_id=action_row.window_id,
        next_window_id=next_row.window_id if next_row is not None else None,
        opened_at=NOW,
    )
    await manager.commit_action_window(window, now=NOW)
    await manager.begin_action_turn(
        1,
        7,
        window_id=window_id,
        request_id=request_id,
        now=NOW,
    )
    request = ActionRequest(
        request_id=request_id,
        game_id=GAME_ID,
        window_id=window_id,
        seat=1,
        session_epoch=7,
        phase=GamePhase.NIGHT_ACTION,
        actions=(Action(action_code=MASS_ACTION, targets=targets),),
    )
    await manager.commit_action_request(
        request,
        ActionValidationContext(
            game_id=GAME_ID,
            session_epoch=7,
            active_request_id=request_id,
            authorized_action_codes=(MASS_ACTION,),
            eligible_targets_by_action={MASS_ACTION: targets},
        ),
        now=NOW,
    )
    await manager.complete_rule_window(
        window_id,
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    committed = await manager.commit_rule_group(
        group_id,
        request_ids=(request_id,),
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    confirmed = [
        fact
        for ledger in committed.rule_ledger
        for fact in ledger.facts
        if fact.fact_type == "DEATH_CONFIRMED"
    ]
    fact_sources = [
        (
            item.fact_id,
            item.fact_type,
            item.target_seat,
            item.source_request_id,
            item.source_rule_id,
        )
        for item in confirmed
    ]
    assert len(confirmed) == len(targets), (
        "each mortality outcome must contribute one trigger source fact; "
        f"observed facts were {fact_sources!r}"
    )
    assert {seat for seat, player in committed.players.items() if not player.alive} == set(targets)
    return committed


async def _submit_choice(
    manager: GameManager,
    *,
    occurrence_id: str,
    actor_seat: int,
    target_seat: int,
) -> GameState:
    step = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert step.kind == "PLAYER_CHOICE"
    assert step.occurrence_id == occurrence_id
    assert step.actor_seat == actor_seat
    assert step.action_window is not None
    window = step.action_window
    request_id = f"choice-request-{occurrence_id}"
    await manager.begin_action_turn(
        actor_seat,
        manager.state.players[actor_seat].session_epoch,
        window_id=window.window_id,
        request_id=request_id,
        now=NOW,
    )
    request = ActionRequest(
        request_id=request_id,
        game_id=GAME_ID,
        window_id=window.window_id,
        seat=actor_seat,
        session_epoch=manager.state.players[actor_seat].session_epoch,
        phase=GamePhase.TRIGGER_ACTION,
        actions=(Action(action_code=CHOICE_ACTION, targets=(target_seat,)),),
    )
    await manager.commit_action_request(
        request,
        ActionValidationContext(
            game_id=GAME_ID,
            session_epoch=request.session_epoch,
            active_request_id=request_id,
            authorized_action_codes=(CHOICE_ACTION,),
            eligible_targets_by_action={CHOICE_ACTION: (target_seat,)},
        ),
        now=NOW,
    )
    await manager.complete_rule_window(
        window.window_id,
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    return await manager.commit_rule_group(
        window.settlement_group_id or "",
        request_ids=(request_id,),
        occurrence_ids=(occurrence_id,),
        expected_revision=manager.state.state_revision,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_real_manager_drains_multi_death_auto_choice_chain_after_snapshot_restore() -> None:
    """Mixed trigger queues survive restore and execute each bound manager step once."""

    manager, board = await _manager()
    committed = await _settle_initial_multi_death(manager)
    assert len(committed.rule_trigger_queue) == 4
    assert {item.mode for item in committed.rule_trigger_queue} == {
        "AUTOMATIC",
        "PLAYER_CHOICE",
    }

    queued_auto = next(item for item in committed.rule_trigger_queue if item.mode == "AUTOMATIC")
    with pytest.raises(ResolutionError, match="RULE_OCCURRENCE_INVALID"):
        await manager.commit_rule_group(
            f"automatic-{queued_auto.occurrence_id}",
            occurrence_ids=(queued_auto.occurrence_id,),
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
    with pytest.raises(ResolutionError, match="RULE_OCCURRENCE_INVALID"):
        await manager.commit_rule_group(
            "forged-trigger-group",
            occurrence_ids=("not-a-queued-occurrence",),
            expected_revision=manager.state.state_revision,
            now=NOW,
        )

    with pytest.raises(EventCommitError, match="PHASE_BLOCKED"):
        await manager.commit_phase_transition(
            GamePhase.NIGHT_RESOLVE,
            expected_revision=manager.state.state_revision,
            now=NOW,
        )

    # The persisted queue, active source facts, and exact frozen package are
    # the only inputs used to reconstruct the resumed manager.
    restored_state = GameState.model_validate_json(manager.state.model_dump_json())
    restored_manager = GameManager(
        restored_state,
        registry=manager.registry,
        execution_package=manager.execution_package,
    )

    targets_by_actor = {2: 4, 3: 5, 4: 6}
    choice_receipts_before: dict[str, int] = {}
    while True:
        state_before_poll = restored_manager.state
        step = await restored_manager.advance_rule_workflow(
            expected_revision=state_before_poll.state_revision,
            now=NOW,
        )
        if step.kind == "RETURN":
            break
        assert step.queue_pending or step.kind in {"AUTOMATIC", "PLAYER_CHOICE"}

        state_after_poll = restored_manager.state
        cursor = state_after_poll.rule_workflow_cursor
        assert cursor is not None
        budget_after_first_poll = cursor.steps_used
        if step.kind == "AUTOMATIC":
            assert step.occurrence_id is not None
            repeat = await restored_manager.advance_rule_workflow(
                expected_revision=restored_manager.state.state_revision,
                now=NOW,
            )
            assert repeat.kind == "AUTOMATIC"
            assert repeat.occurrence_id == step.occurrence_id
            assert restored_manager.state.rule_workflow_cursor is not None
            assert restored_manager.state.rule_workflow_cursor.steps_used == budget_after_first_poll
            prior_state = restored_manager.state
            auto_group_id = f"automatic-{step.occurrence_id}"
            await restored_manager.commit_rule_group(
                auto_group_id,
                occurrence_ids=(step.occurrence_id,),
                expected_revision=prior_state.state_revision,
                now=NOW,
            )
            replayed = await restored_manager.commit_rule_group(
                auto_group_id,
                occurrence_ids=(step.occurrence_id,),
                expected_revision=restored_manager.state.state_revision,
                now=NOW,
            )
            assert replayed.state_revision == restored_manager.state.state_revision
            assert sum(item.group_id == auto_group_id for item in replayed.rule_receipts) == 1
        else:
            assert step.kind == "PLAYER_CHOICE"
            assert step.occurrence_id is not None
            assert step.actor_seat in targets_by_actor
            assert step.action_window is not None
            repeated_wait = await restored_manager.advance_rule_workflow(
                expected_revision=restored_manager.state.state_revision,
                now=NOW,
            )
            assert repeated_wait.kind == "PLAYER_CHOICE"
            assert repeated_wait.occurrence_id == step.occurrence_id
            assert repeated_wait.action_window == step.action_window
            assert restored_manager.state.rule_workflow_cursor is not None
            assert restored_manager.state.rule_workflow_cursor.steps_used == budget_after_first_poll
            restored_manager = GameManager(
                GameState.model_validate_json(restored_manager.state.model_dump_json()),
                registry=restored_manager.registry,
                execution_package=restored_manager.execution_package,
            )
            settled = await _submit_choice(
                restored_manager,
                occurrence_id=step.occurrence_id,
                actor_seat=step.actor_seat,
                target_seat=targets_by_actor[step.actor_seat],
            )
            choice_group_id = step.action_window.settlement_group_id
            assert choice_group_id is not None
            player = settled.players[step.actor_seat]
            assert player.skill_resources["charge"] == 1
            instance = next(
                item
                for item in settled.ability_instances
                if item.actor_seat == step.actor_seat and item.action_code == CHOICE_ACTION
            )
            assert instance.uses_consumed == 1
            assert sum(item.group_id == choice_group_id for item in settled.rule_receipts) == 1
            choice_receipts_before[choice_group_id] = sum(
                item.group_id == choice_group_id for item in settled.rule_receipts
            )
            replayed_choice = await restored_manager.commit_rule_group(
                choice_group_id,
                request_ids=(f"choice-request-{step.occurrence_id}",),
                occurrence_ids=(step.occurrence_id,),
                expected_revision=restored_manager.state.state_revision,
                now=NOW,
            )
            assert replayed_choice.players[step.actor_seat].skill_resources["charge"] == 1
            assert (
                sum(item.group_id == choice_group_id for item in replayed_choice.rule_receipts)
                == choice_receipts_before[choice_group_id]
            )

    final_state = restored_manager.state
    assert final_state.phase is GamePhase.NIGHT_RESOLVE
    assert final_state.rule_workflow_cursor is not None
    assert final_state.rule_workflow_cursor.status == "IDLE"
    assert all(item.status == "COMPLETED" for item in final_state.rule_trigger_queue)
    assert all(not item.is_pending for item in final_state.rule_boundaries)
    assert (
        sum(
            fact.fact_type == "generic_auto_echo"
            for ledger in final_state.rule_ledger
            for fact in ledger.facts
        )
        == 3
    )
    assert {seat for seat, player in final_state.players.items() if not player.alive} == {
        2,
        3,
        4,
        5,
        6,
    }
    assert all(not final_state.players[seat].alive for seat in (2, 3, 4, 5))
    assert final_state.players[6].role_id == "seer"

    resolve_row = next(
        item
        for item in restored_manager.execution_package.window_metadata
        if item.phase == GamePhase.NIGHT_RESOLVE.value
    )
    after_resolve = await _settle_empty_frozen_window(
        restored_manager,
        logical_window_id=resolve_row.window_id,
    )
    assert after_resolve.phase is GamePhase.DAY_ANNOUNCE
    for phase in (
        GamePhase.DAY_SPEECH,
        GamePhase.VOTE,
        GamePhase.DAY_RESOLVE,
        GamePhase.VICTORY_CHECK,
    ):
        await restored_manager.commit_phase_transition(
            phase,
            expected_revision=restored_manager.state.state_revision,
            now=NOW,
        )
    won = await restored_manager.commit_victory_check(
        board,
        expected_revision=restored_manager.state.state_revision,
        now=NOW,
    )
    assert won.phase is GamePhase.FINISHED
    assert won.winner is not None


@pytest.mark.asyncio
async def test_cyclic_auto_trigger_budget_error_keeps_audited_queue_and_blocks_exit() -> None:
    manager, _board = await _manager(budget_limit=3, cyclic_auto=True)
    committed = await _settle_initial_multi_death(manager, targets=(2,))
    assert len(committed.rule_trigger_queue) == 2
    assert committed.rule_workflow_cursor is not None
    assert committed.rule_workflow_cursor.budget_limit == 3

    first = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert first.kind == "AUTOMATIC"
    assert first.occurrence_id is not None
    await manager.commit_rule_group(
        f"automatic-{first.occurrence_id}",
        occurrence_ids=(first.occurrence_id,),
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    second = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert second.kind == "PLAYER_CHOICE"
    assert second.occurrence_id is not None
    assert second.actor_seat == 2
    await _submit_choice(
        manager,
        occurrence_id=second.occurrence_id,
        actor_seat=2,
        target_seat=4,
    )
    cycle_step = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert cycle_step.kind == "AUTOMATIC"
    assert cycle_step.occurrence_id != first.occurrence_id
    await manager.commit_rule_group(
        f"automatic-{cycle_step.occurrence_id}",
        occurrence_ids=(cycle_step.occurrence_id,),
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    exhausted = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert exhausted.kind == "IDLE"
    assert exhausted.queue_pending is True
    cursor = manager.state.rule_workflow_cursor
    assert cursor is not None
    assert cursor.status == "ERROR"
    assert cursor.error_code == "RULE_WORKFLOW_BUDGET_EXHAUSTED"
    queued_before = tuple(
        item.occurrence_id
        for item in manager.state.rule_trigger_queue
        if item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
    )
    assert queued_before
    audit = next(
        item
        for item in reversed(manager.state.moderator_audit)
        if item.get("operation") == "RULE_WORKFLOW_ERROR"
    )
    assert audit["error_code"] == "RULE_WORKFLOW_BUDGET_EXHAUSTED"
    assert tuple(audit["pending_occurrence_ids"]) == queued_before

    repeat = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert repeat.kind == "IDLE"
    assert repeat.queue_pending is True
    assert (
        tuple(
            item.occurrence_id
            for item in manager.state.rule_trigger_queue
            if item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
        )
        == queued_before
    )
    with pytest.raises(EventCommitError, match="PHASE_BLOCKED"):
        await manager.commit_phase_transition(
            GamePhase.NIGHT_RESOLVE,
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
