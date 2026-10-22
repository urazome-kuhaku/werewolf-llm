from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.domain.victory_rules import (
    VictoryAliveCount,
    VictoryBoolean,
    VictoryCompare,
    VictoryCondition,
    VictoryGameState,
    VictoryLiteral,
    VictoryProgram,
)
from werewolf.game import EventCommitError, GameManager, load_action_registry
from werewolf.game.state import (
    GameState,
    PlayerState,
    RuleBoundary,
    RuleReturnPoint,
    RuleWorkflowCursor,
)
from werewolf.game.victory import RulesetMismatchError, evaluate_victory
from werewolf.knowledge.board import BoardDefinition, VictoryDefinition
from werewolf.knowledge.models import BoardRoleBinding
from werewolf.knowledge.refs import VersionedRef
from werewolf.rules.compiler import ExecutionCompilerError, validate_execution_package
from werewolf.rules.models import ActionSpec, ExecutionPackage, StateDeclaration

NOW = datetime(2026, 10, 22, tzinfo=UTC)
REGISTRY = load_action_registry()


def _board(program: VictoryProgram) -> BoardDefinition:
    return BoardDefinition.model_construct(
        board_id="declarative-test",
        version="1.0.0",
        status="published",
        reviewed_by="human-reviewer",
        factions={"wolf": 1, "good": 1},
        role_bindings=[
            BoardRoleBinding(
                role_ref=VersionedRef(id="wolf-role", version="1.0.0"),
                count=1,
                effective_rules={},
                override_claim_refs=[],
            ),
            BoardRoleBinding(
                role_ref=VersionedRef(id="citizen-role", version="1.0.0"),
                count=1,
                effective_rules={},
                override_claim_refs=[],
            ),
        ],
        victory=VictoryDefinition(
            mode="declarative",
            winning_sides=["good", "wolf"],
            program=program,
        ),
    )


def _program(
    *conditions: VictoryCondition,
    conflict_policy: str = "DRAW",
) -> VictoryProgram:
    return VictoryProgram(
        player_groups={"wolf-role": "wolf", "citizen-role": "town"},
        conditions=conditions,
        conflict_policy=conflict_policy,  # type: ignore[arg-type]
    )


def _execution(program: VictoryProgram) -> ExecutionPackage:
    return ExecutionPackage(
        board_id="declarative-test",
        board_version="1.0.0",
        actions=(ActionSpec(action_code=299, action_id="PASS", allow_pass=True),),
        skills=(),
        state_declarations=(
            StateDeclaration(
                skill_id="victory-state",
                key="gate",
                value_type="bool",
                initial=True,
                scope="GAME",
            ),
        ),
        victory_program=program,
    )


def _manager(
    execution: ExecutionPackage,
    *,
    players: tuple[PlayerState, ...] = (
        PlayerState(
            seat=1,
            role_id="wolf-role",
            faction_id="wolf",
            victory_group_id="wolf",
        ),
        PlayerState(
            seat=2,
            role_id="citizen-role",
            faction_id="good",
            victory_group_id="wolf",
        ),
    ),
) -> GameManager:
    state = GameState(
        game_id="declarative-victory-test",
        created_at=NOW,
        updated_at=NOW,
        ruleset={
            "board_id": "declarative-test",
            "version": "1.0.0",
            "snapshot_id": "ruleset-" + "a" * 64,
            "manifest_sha256": "b" * 64,
        },
        players={player.seat: player for player in players},
    )
    return GameManager(state, registry=REGISTRY, execution_package=execution)


def _always_true() -> VictoryLiteral:
    return VictoryLiteral(value=True)


def _alive_count(field: str, value: str, count: int) -> VictoryCompare:
    return VictoryCompare(
        op="eq",
        left=VictoryAliveCount(field=field, value=value),  # type: ignore[arg-type]
        right=VictoryLiteral(value=count),
    )


def test_only_highest_priority_conditions_apply_and_same_side_is_deduplicated() -> None:
    program = _program(
        VictoryCondition(condition_id="z-wolf", side_id="wolf", priority=10, when=_always_true()),
        VictoryCondition(condition_id="a-wolf", side_id="wolf", priority=10, when=_always_true()),
        VictoryCondition(
            condition_id="good-lower", side_id="good", priority=9, when=_always_true()
        ),
    )
    manager = _manager(_execution(program))

    result = evaluate_victory(
        manager.state,
        _board(program),
        execution_package=manager.execution_package,
    )

    assert result.status == "WINNER"
    assert result.winner == "wolf"
    assert result.candidate_sides == ("wolf",)
    assert result.candidates[0].condition == "a-wolf"


def test_faction_and_victory_group_counts_read_independent_current_fields() -> None:
    gate = VictoryGameState(key="gate")
    faction_count = VictoryBoolean(
        op="and",
        values=(
            gate,
            _alive_count("faction_id", "wolf", 1),
        ),
    )
    victory_group_count = VictoryBoolean(
        op="and",
        values=(
            gate,
            _alive_count("victory_group_id", "wolf", 2),
        ),
    )
    program = _program(
        VictoryCondition(
            condition_id="faction-count", side_id="good", priority=5, when=faction_count
        ),
        VictoryCondition(
            condition_id="group-count",
            side_id="wolf",
            priority=5,
            when=victory_group_count,
        ),
    )
    manager = _manager(_execution(program))

    result = evaluate_victory(
        manager.state,
        _board(program),
        execution_package=manager.execution_package,
    )

    assert result.status == "DRAW"
    assert result.candidate_sides == ("good", "wolf")
    assert result.reasons == ("TOP_PRIORITY_MULTI_SIDE_DRAW",)


@pytest.mark.parametrize(
    ("conflict_policy", "expected_status", "requires_moderator"),
    [
        ("DRAW", "DRAW", False),
        ("PENDING_MODERATOR", "PENDING_MODERATOR", True),
    ],
)
def test_equal_highest_priority_sides_follow_the_frozen_conflict_policy(
    conflict_policy: str,
    expected_status: str,
    requires_moderator: bool,
) -> None:
    program = _program(
        VictoryCondition(
            condition_id="wolf-condition", side_id="wolf", priority=4, when=_always_true()
        ),
        VictoryCondition(
            condition_id="good-condition", side_id="good", priority=4, when=_always_true()
        ),
        conflict_policy=conflict_policy,
    )
    manager = _manager(_execution(program))

    result = evaluate_victory(
        manager.state,
        _board(program),
        execution_package=manager.execution_package,
    )

    assert result.status == expected_status
    assert result.winner is None
    assert result.candidate_sides == ("good", "wolf")
    assert result.requires_moderator is requires_moderator


def test_declarative_victory_rejects_missing_or_foreign_frozen_execution() -> None:
    program = _program(
        VictoryCondition(
            condition_id="wolf-condition", side_id="wolf", priority=1, when=_always_true()
        )
    )
    execution = _execution(program)
    manager = _manager(execution)

    with pytest.raises(RulesetMismatchError, match="frozen execution package"):
        evaluate_victory(manager.state, _board(program))

    foreign_state = manager.state.model_copy(
        update={
            "execution_identity": manager.state.execution_identity.model_copy(
                update={"package_id": "foreign-execution-package"}
            )
        }
    )
    with pytest.raises(RulesetMismatchError, match="frozen execution identity"):
        evaluate_victory(foreign_state, _board(program), execution_package=execution)

    foreign_execution = execution.model_copy(update={"board_id": "another-board"})
    with pytest.raises(RulesetMismatchError, match="frozen execution identity"):
        evaluate_victory(manager.state, _board(program), execution_package=foreign_execution)


def test_compiler_rejects_unknown_or_undeclared_victory_state_reference() -> None:
    program = _program(
        VictoryCondition(
            condition_id="unknown-state",
            side_id="wolf",
            priority=1,
            when=VictoryGameState(key="not-declared"),
        )
    )
    execution = _execution(program)

    with pytest.raises(ExecutionCompilerError, match="must be declared once as GAME bool"):
        validate_execution_package(execution, REGISTRY, board=_board(program))


def test_victory_ast_rejects_unknown_expression_operations() -> None:
    with pytest.raises(ValidationError):
        VictoryProgram.model_validate(
            {
                "player_groups": {"wolf-role": "wolf"},
                "conditions": [
                    {
                        "condition_id": "dynamic",
                        "side_id": "wolf",
                        "priority": 1,
                        "when": {"op": "python", "source": "__import__('os')"},
                    }
                ],
                "conflict_policy": "DRAW",
            }
        )


@pytest.mark.asyncio
async def test_manager_commits_a_declarative_unique_winner() -> None:
    program = _program(
        VictoryCondition(
            condition_id="wolf-wins",
            side_id="wolf",
            priority=3,
            when=_always_true(),
        )
    )
    manager = _manager(_execution(program))
    manager._state = manager.state.model_copy(update={"phase": GamePhase.VICTORY_CHECK})

    committed = await manager.commit_victory_check(_board(program), now=NOW)

    assert committed.phase is GamePhase.FINISHED
    assert committed.winner is not None
    assert committed.winner["status"] == "WINNER"
    assert committed.winner["side"] == "wolf"
    assert committed.winner["resolved_by"] == "evaluator"


@pytest.mark.asyncio
async def test_manager_does_not_allow_moderator_to_rewrite_a_declared_draw() -> None:
    program = _program(
        VictoryCondition(
            condition_id="wolf-condition",
            side_id="wolf",
            priority=4,
            when=_always_true(),
        ),
        VictoryCondition(
            condition_id="good-condition",
            side_id="good",
            priority=4,
            when=_always_true(),
        ),
    )
    manager = _manager(_execution(program))
    manager._state = manager.state.model_copy(update={"phase": GamePhase.VICTORY_CHECK})
    board = _board(program)

    with pytest.raises(EventCommitError, match="declared draw cannot be rewritten"):
        await manager.commit_victory_check(board, moderator_winner="wolf", now=NOW)

    assert manager.state.phase is GamePhase.VICTORY_CHECK
    assert manager.state.state_revision == 0
    assert manager.state.winner is None

    committed = await manager.commit_victory_check(board, now=NOW)

    assert committed.phase is GamePhase.FINISHED
    assert committed.winner is not None
    assert committed.winner["status"] == "DRAW"
    assert committed.winner["side"] is None


@pytest.mark.asyncio
async def test_pending_declarative_conflict_requires_an_explicit_candidate() -> None:
    program = _program(
        VictoryCondition(
            condition_id="wolf-condition",
            side_id="wolf",
            priority=4,
            when=_always_true(),
        ),
        VictoryCondition(
            condition_id="good-condition",
            side_id="good",
            priority=4,
            when=_always_true(),
        ),
        conflict_policy="PENDING_MODERATOR",
    )
    manager = _manager(_execution(program))
    manager._state = manager.state.model_copy(update={"phase": GamePhase.VICTORY_CHECK})
    board = _board(program)

    with pytest.raises(EventCommitError, match="moderator_winner is required"):
        await manager.commit_victory_check(board, now=NOW)
    with pytest.raises(EventCommitError, match="explicit candidates"):
        await manager.commit_victory_check(board, moderator_winner="neutral", now=NOW)

    committed = await manager.commit_victory_check(board, moderator_winner="good", now=NOW)

    assert committed.phase is GamePhase.FINISHED
    assert committed.winner is not None
    assert committed.winner["status"] == "WINNER"
    assert committed.winner["side"] == "good"
    assert committed.winner["resolved_by"] == "moderator"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["error_cursor", "pending_boundary", "badge_open"])
async def test_manager_rejects_declarative_victory_with_unfinished_lifecycle_work(
    blocker: str,
) -> None:
    program = _program(
        VictoryCondition(
            condition_id="wolf-wins",
            side_id="wolf",
            priority=3,
            when=_always_true(),
        )
    )
    manager = _manager(_execution(program))
    updates: dict[str, object] = {"phase": GamePhase.VICTORY_CHECK}
    if blocker == "error_cursor":
        updates["rule_workflow_cursor"] = RuleWorkflowCursor(
            status="ERROR",
            error_code="RULE_WORKFLOW_BUDGET_EXHAUSTED",
        )
    elif blocker == "pending_boundary":
        updates["rule_boundaries"] = (
            RuleBoundary(
                boundary_id="death-boundary",
                source_group_id="resolve",
                source_batch_id="batch-1",
                death_fact_ids=("death-1",),
                death_seats=(1,),
                return_point=RuleReturnPoint(phase=GamePhase.NIGHT_RESOLVE),
                last_words_required=True,
                last_words_seats=(1,),
                created_at=NOW,
            ),
        )
    else:
        updates["sheriff_badge"] = {"status": "OPEN"}
    manager._state = manager.state.model_copy(update=updates)

    with pytest.raises(EventCommitError, match="pending|choice"):
        await manager.commit_victory_check(_board(program), now=NOW)

    assert manager.state.phase is GamePhase.VICTORY_CHECK
    assert manager.state.state_revision == 0
    assert manager.state.winner is None
