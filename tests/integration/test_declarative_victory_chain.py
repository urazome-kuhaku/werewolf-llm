"""A generic death-trigger chain finishes before declarative victory is committed."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_rules_b_workflow import _manager as _base_manager

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
from werewolf.game.actions import ActionRegistry
from werewolf.game.manager import EventCommitError, GameManager
from werewolf.game.state import GameState, PlayerState
from werewolf.game.voting import (
    PublicVoteResult,
    VoteState,
    VoteStatus,
    VoteTally,
    VoteWindow,
)
from werewolf.knowledge.board import BoardDefinition, VictoryDefinition
from werewolf.moderator import LastWordsFlow
from werewolf.rules.compiler import validate_execution_package
from werewolf.rules.models import ExecutionPackage
from werewolf.runtime.player_runtime import InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
EXILE_ACTION = 203
AUTO_ACTION = 982


def _ref(source: str, name: str) -> dict[str, object]:
    return {"op": "ref", "source": source, "name": name}


def _literal(value: object) -> dict[str, object]:
    return {"op": "literal", "value": value}


def _compare(op: str, left: object, right: object) -> dict[str, object]:
    return {"op": op, "left": left, "right": right}


def _seat_selector(seat: int) -> dict[str, object]:
    return {
        "op": "select",
        "source": "players",
        "where": _compare("eq", _ref("item", "seat"), _literal(seat)),
        "map": _ref("item", "seat"),
    }


def _alive_count(field: str, value: str, count: int) -> VictoryCompare:
    return VictoryCompare(
        op="eq",
        left=VictoryAliveCount(field=field, value=value),  # type: ignore[arg-type]
        right=VictoryLiteral(value=count),
    )


async def _exile_chain_fixture() -> tuple[GameManager, BoardDefinition]:
    base_manager, base_board = await _base_manager()
    execution = base_manager.execution_package
    assert execution is not None
    execution_payload = json.loads(execution.model_dump_json())
    skills = {item["skill_id"]: item for item in execution_payload["skills"]}
    execution_payload["skills"] = [
        item
        for item in execution_payload["skills"]
        if item["skill_id"] != "generic_player_choice_skill"
    ]
    charm = skills["generic_automatic_echo_skill"]
    charm["grants"] = [{"grant_id": "generic_charm_grant", "actor_selector": _seat_selector(2)}]
    # The fixture has a single verified death fact: the last wolf's host exile.
    charm["trigger"]["condition"] = None
    charm["effects"] = [
        {
            "effect_id": "mark-generic-charm-complete",
            "effect_type": "STATE_SET",
            "state_key": "charm_complete",
            "value": _literal(True),
        },
        {
            "effect_id": "generic-charm-casualty",
            "effect_type": "DAMAGE",
            "target": _ref("actor", "seat"),
            "tags": ["generic_charm_casualty"],
        },
    ]
    execution_payload["actions"].append(
        {"action_code": EXILE_ACTION, "action_id": "GENERIC_EXILE_RESOLVE"}
    )
    execution_payload["skills"].append(
        {
            "skill_id": "generic_exile_resolution",
            "action_code": EXILE_ACTION,
            "mode": "HOST",
            "grants": [],
            "timing": [GamePhase.DAY_RESOLVE.value],
            "targets": {
                "min_targets": 1,
                "max_targets": 1,
                "selector": {
                    "op": "select",
                    "source": "players",
                    "where": _compare("eq", _ref("item", "alive"), _literal(True)),
                    "map": _ref("item", "seat"),
                },
                "allow_self": True,
            },
            "usage": {"scope": "GAME"},
            "effects": [
                {
                    "effect_id": "generic-exile-damage",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["generic_exile"],
                }
            ],
        }
    )
    execution_payload["interactions"].extend(
        [
            {
                "interaction_id": "confirm-generic-exile-death",
                "rule_type": "CONFIRM_DEATH",
                "damage_tags": ["generic_exile"],
                "death_cause": "exiled",
            },
            {
                "interaction_id": "confirm-generic-charm-death",
                "rule_type": "CONFIRM_DEATH",
                "damage_tags": ["generic_charm_casualty"],
                "death_cause": "charmed",
            },
        ]
    )
    execution_payload["state_declarations"].append(
        {
            "skill_id": "generic_automatic_echo_skill",
            "key": "charm_complete",
            "value_type": "bool",
            "initial": False,
            "scope": "GAME",
        }
    )

    role_groups = {
        binding.role_ref.id: (
            "wolf"
            if binding.role_ref.id == "wolf"
            else "god"
            if binding.role_ref.id in {"seer", "witch", "hunter", "idiot"}
            else "villager"
        )
        for binding in base_board.role_bindings
    }
    program = VictoryProgram(
        player_groups=role_groups,
        conditions=(
            VictoryCondition(
                condition_id="good-wins-after-last-wolf-death",
                side_id="good",
                priority=1,
                when=_alive_count("faction_id", "wolf", 0),
            ),
            VictoryCondition(
                condition_id="wolf-wins-after-charm-chain",
                side_id="wolf",
                priority=10,
                when=VictoryBoolean(
                    op="and",
                    values=(
                        VictoryGameState(key="charm_complete"),
                        _alive_count("victory_group_id", "god", 0),
                    ),
                ),
            ),
        ),
        conflict_policy="DRAW",
    )
    execution_payload["victory_program"] = program.model_dump(mode="json")
    execution = ExecutionPackage.model_validate_json(json.dumps(execution_payload))
    board = base_board.model_copy(
        update={
            "victory": VictoryDefinition(
                mode="declarative",
                winning_sides=["good", "wolf"],
                program=program,
            )
        }
    )

    registry = ActionRegistry.model_validate(
        {
            "schema_version": base_manager.registry.schema_version,
            "actions": [
                *(item.model_dump(mode="json") for item in base_manager.registry.actions),
                {
                    "action_code": EXILE_ACTION,
                    "action_name": "GENERIC_EXILE_RESOLVE",
                    "target_policy": "candidate",
                    "target_count": 1,
                },
            ],
        }
    )
    validate_execution_package(
        execution,
        registry,
        board=board,
        available_windows=execution.window_metadata,
        expected_boundary_policy=execution.boundary_policy,
    )

    alive_players: dict[int, PlayerState] = {}
    for seat, player in base_manager.state.players.items():
        role_id = "seer" if seat == 2 else player.role_id
        faction_id = "wolf" if seat in {3, 4, 5} else "good"
        victory_group_id = "wolf" if seat in {3, 4, 5} else "god" if seat == 2 else "villager"
        alive_players[seat] = player.model_copy(
            update={
                "role_id": role_id,
                "faction_id": faction_id,
                "victory_group_id": victory_group_id,
                "alive": seat in {1, 2, 3},
                "death_cause": None if seat in {1, 2, 3} else "fixture-elimination",
            }
        )

    vote_window = VoteWindow(
        window_id="confirmed-last-wolf-exile",
        game_id=base_manager.state.game_id,
        session_epoch=7,
        observation_revision=0,
        eligible_voters=(1,),
        candidate_seats=(3,),
        vote_weights={1: 1.0},
    )
    tally = VoteTally(
        counts={3: 1.0},
        total_weight=1.0,
        ballots_count=1,
        top_candidates=(3,),
        is_tie=False,
        winner_seat=3,
    )
    vote_state = VoteState(
        window=vote_window,
        status=VoteStatus.RESOLVED,
        public_result=PublicVoteResult(tally=tally, eliminated_seat=3),
    )
    initial_data = base_manager.state.model_dump(mode="python")
    initial_data.update(
        {
            "phase": GamePhase.DAY_RESOLVE,
            "round_no": 0,
            "day_no": 1,
            "players": alive_players,
            "execution_identity": None,
            "ability_instances": (),
            "rule_state": (),
            "rule_relations": (),
            "rule_deferred_disclosures": (),
            "rule_trigger_queue": (),
            "rule_boundaries": (),
            "rule_ledger": (),
            "rule_receipts": (),
            "action_windows": {},
            "action_requests": {},
            "pending_resolution": None,
            "vote_state": vote_state.model_dump(mode="json"),
        }
    )
    state = GameState.model_validate(initial_data)
    return GameManager(state, registry=registry, execution_package=execution), board


@pytest.mark.asyncio
async def test_confirmed_last_wolf_exile_drains_generic_charm_chain_before_priority_victory() -> (
    None
):
    manager, board = await _exile_chain_fixture()

    exiled = await manager.commit_confirmed_vote_exile(
        target_seat=3,
        vote_window_id="confirmed-last-wolf-exile",
        now=NOW,
    )
    assert not exiled.players[3].alive
    assert exiled.players[3].death_cause == "exiled"

    trigger = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert trigger.kind == "AUTOMATIC"
    assert trigger.actor_seat == 2
    assert trigger.occurrence_id is not None

    unresolved_state = manager.state.model_copy(update={"phase": GamePhase.VICTORY_CHECK})
    unresolved_manager = GameManager(
        unresolved_state,
        registry=manager.registry,
        execution_package=manager.execution_package,
    )
    with pytest.raises(EventCommitError, match="VICTORY_BLOCKED: a rule settlement, trigger"):
        await unresolved_manager.commit_victory_check(board, now=NOW)

    while True:
        if trigger.kind == "AUTOMATIC":
            await manager.commit_rule_group(
                f"automatic-{trigger.occurrence_id}",
                occurrence_ids=(trigger.occurrence_id,),
                expected_revision=manager.state.state_revision,
                now=NOW,
            )
        trigger = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
        if trigger.kind == "RETURN":
            break
        assert trigger.kind == "AUTOMATIC"

    drained = manager.state
    assert not drained.players[2].alive
    assert drained.players[2].death_cause == "charmed"
    assert any(
        item.scope == "GAME" and item.key == "charm_complete" and item.value is True
        for item in drained.rule_state
    )
    assert all(item.status == "COMPLETED" for item in drained.rule_trigger_queue)
    pending_boundaries = [item for item in drained.rule_boundaries if item.is_pending]
    assert pending_boundaries
    assert pending_boundaries[0].last_words_seats == (3,)

    runtime = ScriptedRuntime()
    await runtime.start(
        RuntimeConfig(session_id="generic-chain-last-words"),
        InitialContext(
            game_id=drained.game_id,
            seat=3,
            session_epoch=drained.players[3].session_epoch,
            role_id=drained.players[3].role_id,
        ),
    )
    try:
        last_words = await LastWordsFlow(manager, board, {3: runtime}).next(3)
        assert last_words.event.actor_seat == 3
    finally:
        await runtime.close("generic charm chain test complete")

    boundary_return = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert boundary_return.kind == "IDLE"
    assert not boundary_return.queue_pending
    workflow_return = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    assert workflow_return.kind == "RETURN"
    assert all(not item.is_pending for item in manager.state.rule_boundaries)

    await manager.commit_phase_transition(GamePhase.VICTORY_CHECK, now=NOW)
    finished = await manager.commit_victory_check(board, now=NOW)
    assert finished.phase is GamePhase.FINISHED
    assert finished.winner is not None
    assert finished.winner["side"] == "wolf"
    assert finished.winner["condition"] == "wolf-wins-after-charm-chain"
