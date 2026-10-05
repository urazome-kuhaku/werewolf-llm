"""Gateway-backed recovery for data-defined night death trigger chains."""

from __future__ import annotations

import json

import pytest
from test_rules_b_flow import _boundary_policy
from test_rules_scripted_runtime import (
    GAME_NOW,
    _compiled_classic,
    _novel_execution,
)
from test_rules_scripted_runtime import (
    _registry as _novel_registry,
)
from test_rules_scripted_runtime import (
    _start_runtime as _start_gateway_runtime,
)
from test_rules_scripted_runtime import (
    _state as _novel_state,
)

from werewolf.domain.enums import GamePhase
from werewolf.game.actions import (
    Action,
    ActionDefinition,
    ActionRegistry,
    ActionRequest,
    ActionValidationContext,
    ActionValidationError,
)
from werewolf.game.manager import GameManager
from werewolf.game.state import GameState, PlayerState
from werewolf.knowledge.board import BoardDefinition
from werewolf.moderator.night_flow import ModeratorNightFlow
from werewolf.moderator.trigger_flow import ModeratorTriggerFlow
from werewolf.rules.compiler import execution_windows_from_board, validate_execution_package
from werewolf.rules.models import ExecutionPackage


@pytest.mark.asyncio
async def test_gateway_night_flow_resumes_multi_death_auto_choice_chain_from_snapshots() -> None:
    """Two deaths queue two automatic facts and two resumable player choices."""

    from test_rules_scripted_runtime import _compare, _literal, _ref

    compiled = await _compiled_classic()
    board_payload = json.loads(json.dumps(compiled.package_payload["board_definition"]))
    board_payload["night_windows"] = [
        {"window_id": "quasar_mass_casualty", "order": 1, "phase": "NIGHT_ACTION"}
    ]
    board_payload["knife_rule"]["available_after_window"] = "quasar_mass_casualty"
    board_payload["wolf_team_visibility"]["discussion_enabled"] = False
    board_payload["day_flow"]["last_words"] = {"enabled": False}
    board_payload["day_flow"]["sheriff"] = {"enabled": False}
    board = BoardDefinition.model_validate(board_payload)

    registry_base = _novel_registry(target_count=2)
    registry = ActionRegistry(
        actions=(
            *registry_base.actions,
            ActionDefinition(
                action_code=988,
                action_name="QUASAR_AUTOMATIC_ECHO",
                target_policy="none",
                target_count=0,
            ),
            ActionDefinition(
                action_code=989,
                action_name="QUASAR_CHAIN_CHOICE",
                target_policy="candidate",
                target_count=1,
            ),
        )
    )
    actor_one = {
        "op": "select",
        "source": "players",
        "where": _compare("eq", _ref("item", "seat"), _literal(1)),
        "map": _ref("item", "seat"),
    }
    private_role_target = {
        "op": "select",
        "source": "players",
        "where": {
            "op": "and",
            "values": [
                _compare("eq", _ref("item", "role_id"), _literal("oracle")),
            ],
        },
        "map": _ref("item", "seat"),
    }
    all_seats = {"op": "select", "source": "players", "map": _ref("item", "seat")}
    raw_execution = json.loads(_novel_execution(target_count=2, modes=["strike"]).model_dump_json())
    raw_execution["actions"].extend(
        [
            {"action_code": 988, "action_id": "QUASAR_AUTOMATIC_ECHO"},
            {"action_code": 989, "action_id": "QUASAR_CHAIN_CHOICE"},
        ]
    )
    attack = raw_execution["skills"][0]
    attack["window_ids"] = ["quasar_mass_casualty"]
    attack["parameters"] = []
    attack["disclosures"] = []
    attack["targets"]["min_targets"] = 2
    attack["targets"]["max_targets"] = 2
    attack["targets"]["selector"] = {
        "op": "select",
        "source": "players",
        "where": {
            "op": "or",
            "values": [
                _compare("eq", _ref("item", "seat"), _literal(2)),
                _compare("eq", _ref("item", "seat"), _literal(3)),
            ],
        },
        "map": _ref("item", "seat"),
    }
    attack["effects"] = [
        {
            "effect_id": "quasar-initial-mass-casualty",
            "effect_type": "DAMAGE",
            "target": _ref("target", "seat"),
            "tags": ["quasar_initial_hit"],
        }
    ]
    automatic = {
        "skill_id": "quasar_after_death_echo",
        "mode": "AUTOMATIC",
        "action_code": 988,
        "grants": [{"grant_id": "quasar_auto_grant", "actor_selector": actor_one}],
        "timing": ["TRIGGER_ACTION"],
        "trigger": {
            "fact_types": ["DEATH_CONFIRMED"],
            "mode": "AUTOMATIC",
            "condition": _compare(
                "eq", _ref("source_fact", "death_cause"), _literal("initial_kill")
            ),
        },
        "targets": {"min_targets": 0, "max_targets": 0, "selector": all_seats},
        "usage": {"max_uses": 2, "scope": "GAME"},
        "effects": [
            {
                "effect_id": "quasar-emit-choice-source",
                "effect_type": "FACT",
                "target": _ref("actor", "seat"),
                "fact_type": "QUASAR_CHAIN_READY",
            }
        ],
    }
    choice = {
        "skill_id": "quasar_after_auto_choice",
        "action_code": 989,
        "grants": [{"grant_id": "quasar_choice_grant", "actor_selector": actor_one}],
        "timing": ["TRIGGER_ACTION"],
        "trigger": {"fact_types": ["QUASAR_CHAIN_READY"], "mode": "PLAYER_CHOICE"},
        "targets": {
            "min_targets": 1,
            "max_targets": 1,
            "selector": private_role_target,
            "allow_self": False,
        },
        "usage": {"max_uses": 2, "scope": "GAME"},
        "effects": [
            {
                "effect_id": "quasar-choice-followup-death",
                "effect_type": "FACT",
                "target": _ref("target", "seat"),
                "fact_type": "QUASAR_CHOICE_ACK",
            }
        ],
    }
    raw_execution["skills"] = [attack, automatic, choice]
    raw_execution["interactions"] = [
        {
            "interaction_id": "quasar-confirm-initial-death",
            "rule_type": "CONFIRM_DEATH",
            "damage_tags": ["quasar_initial_hit"],
            "death_cause": "initial_kill",
        },
    ]
    windows = execution_windows_from_board(board)
    raw_execution["window_metadata"] = [item.model_dump(mode="json") for item in windows]
    raw_execution["window_settlement_groups"] = {}
    raw_execution["boundary_policy"] = _boundary_policy(board).model_dump(mode="json")
    execution = ExecutionPackage.model_validate_json(json.dumps(raw_execution))
    validate_execution_package(
        execution,
        registry,
        available_windows=windows,
        expected_boundary_policy=_boundary_policy(board),
    )

    game_id = "quasar-death-chain-game"
    snapshot_id = "quasar-death-chain-snapshot"
    seed_manager = _novel_state(
        compiled,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
        target_count=2,
    )
    players = dict(seed_manager.state.players)
    players[4] = players[4].model_copy(update={"role_id": "oracle"})
    players[5] = PlayerState(seat=5, role_id="villager", faction_id="village", session_epoch=0)
    unbound_state = seed_manager.state.model_copy(
        update={
            "execution_identity": None,
            "ability_instances": (),
            "rule_state": (),
            "rule_relations": (),
            "rule_deferred_disclosures": (),
            "rule_trigger_queue": (),
            "rule_workflow_cursor": None,
            "rule_boundaries": (),
            "rule_ledger": (),
            "rule_receipts": (),
            "phase": GamePhase.NIGHT_ACTION,
            "round_no": 1,
            "day_no": 1,
            "current_queue": None,
            "serial_turn": None,
            "last_serial_turn": None,
            "players": players,
            "action_windows": {},
            "action_requests": {},
        }
    )
    manager = GameManager(unbound_state, registry=registry, execution_package=execution)

    gateway, server, runtime = await _start_gateway_runtime(
        compiled,
        manager,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
    )
    try:
        night = ModeratorNightFlow(manager, board, {1: runtime}, clock=lambda: GAME_NOW)
        opened_night = await night.open()
        assert opened_night.action_window is not None
        assert opened_night.action_window.logical_window_id == "quasar_mass_casualty"
        action = await night.action_next(1)
        assert action["status"] == "accepted"
        await night.advance()
        initial_deaths = {
            seat for seat, player in manager.state.players.items() if not player.alive
        }
        assert len(initial_deaths) == 2
        assert sum(
            player.death_cause == "initial_kill"
            for seat, player in manager.state.players.items()
            if seat in initial_deaths
        ) == len(initial_deaths)

        triggers = ModeratorTriggerFlow(manager, board, {1: runtime}, clock=lambda: GAME_NOW)
        choice = await triggers.open(now=GAME_NOW)
        assert choice.action_window is not None
        assert manager.state.phase is GamePhase.TRIGGER_ACTION
        assert choice.action_window.visible_context["candidate_seats"] == [1, 2, 3, 4, 5]
        assert manager.state.players[4].alive is True
        first_choice_occurrence = next(
            item for item in manager.state.rule_trigger_queue if item.status == "WAITING_CHOICE"
        )
        private_choice_skill = next(
            item for item in execution.skills if item.skill_id == first_choice_occurrence.skill_id
        )
        changed_hidden_roles = dict(manager.state.players)
        changed_hidden_roles[4] = changed_hidden_roles[4].model_copy(update={"role_id": "villager"})
        changed_hidden_roles[5] = changed_hidden_roles[5].model_copy(update={"role_id": "oracle"})
        alternate_hidden_state = manager.state.model_copy(update={"players": changed_hidden_roles})
        alternate_window = manager._build_trigger_action_window(
            alternate_hidden_state,
            first_choice_occurrence,
            private_choice_skill,
            now=GAME_NOW,
        )
        assert alternate_window.visible_context["candidate_seats"] == [1, 2, 3, 4, 5]

        probe_manager = GameManager(
            GameState.model_validate_json(manager.state.model_dump_json()),
            registry=registry,
            execution_package=execution,
        )
        probe_request_id = "quasar-private-target-probe"
        await probe_manager.begin_action_turn(
            1,
            manager.state.players[1].session_epoch,
            window_id=choice.action_window.window_id,
            request_id=probe_request_id,
            expected_revision=probe_manager.state.state_revision,
            now=GAME_NOW,
        )
        probe_context = ActionValidationContext(
            game_id=game_id,
            session_epoch=manager.state.players[1].session_epoch,
            active_request_id=probe_request_id,
            player_alive=True,
            player_qualified=True,
            role_id=manager.state.players[1].role_id,
            authorized_action_codes=(989,),
            alive_seats=(1, 2, 3, 4, 5),
            eligible_targets_by_action={989: (1, 2, 3, 4, 5)},
        )
        unauthorized_target = next(
            seat
            for seat, player in manager.state.players.items()
            if player.alive and seat != 1 and player.role_id != "oracle"
        )
        invalid_private_target = ActionRequest(
            request_id=probe_request_id,
            game_id=game_id,
            window_id=choice.action_window.window_id,
            seat=1,
            session_epoch=manager.state.players[1].session_epoch,
            phase=GamePhase.TRIGGER_ACTION,
            actions=(Action(action_code=989, targets=(unauthorized_target,)),),
        )
        with pytest.raises(ActionValidationError, match="TARGET_NOT_ALLOWED"):
            await probe_manager.commit_action_request(
                invalid_private_target,
                probe_context,
                expected_revision=probe_manager.state.state_revision,
                now=GAME_NOW,
            )
        auto_occurrences = tuple(
            item
            for item in manager.state.rule_trigger_queue
            if item.skill_id == "quasar_after_death_echo"
        )
        assert len(auto_occurrences) == 2
        assert all(item.status == "COMPLETED" for item in auto_occurrences)
        choice_occurrences = tuple(
            item
            for item in manager.state.rule_trigger_queue
            if item.skill_id == "quasar_after_auto_choice"
        )
        assert len(choice_occurrences) == 2
        assert sum(item.status == "WAITING_CHOICE" for item in choice_occurrences) == 1
    finally:
        await runtime.close("B trigger chain snapshot setup complete")
        await gateway.close()
        await server.close()

    restored = GameState.model_validate_json(manager.state.model_dump_json())
    restored_manager = GameManager(
        restored,
        registry=registry,
        execution_package=execution,
    )
    gateway2, server2, runtime2 = await _start_gateway_runtime(
        compiled,
        restored_manager,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
    )
    try:
        triggers2 = ModeratorTriggerFlow(
            restored_manager, board, {1: runtime2}, clock=lambda: GAME_NOW
        )
        resumed = await triggers2.open(now=GAME_NOW)
        assert resumed.action_window is not None
        assert resumed.action_window.window_id == choice.action_window.window_id
        accepted = await triggers2.next(1)
        action_request = next(
            item for item in reversed(runtime2.requests) if item.expected_kind.value == "action"
        )
        assert action_request.action_window is not None
        assert action_request.action_window.candidate_seats == [1, 2, 3, 4, 5]
        assert accepted["status"] == "accepted"
        assert accepted["next_step"] == "PLAYER_CHOICE"
        assert not any(
            player.death_cause == "choice_kill"
            for player in restored_manager.state.players.values()
        )
    finally:
        await runtime2.close("B trigger chain first choice complete")
        await gateway2.close()
        await server2.close()

    restored_again = GameState.model_validate_json(restored_manager.state.model_dump_json())
    manager_after_choice = GameManager(
        restored_again,
        registry=registry,
        execution_package=execution,
    )
    gateway3, server3, runtime3 = await _start_gateway_runtime(
        compiled,
        manager_after_choice,
        execution,
        registry,
        game_id=game_id,
        snapshot_id=snapshot_id,
    )
    try:
        triggers3 = ModeratorTriggerFlow(
            manager_after_choice, board, {1: runtime3}, clock=lambda: GAME_NOW
        )
        final = await triggers3.auto_resolve()
        assert final["phase"] == GamePhase.DAY_ANNOUNCE.value, (
            f"result={final!r}, phase={manager_after_choice.state.phase.value}, "
            f"cursor={manager_after_choice.state.rule_workflow_cursor!r}, "
            f"queue={manager_after_choice.state.rule_trigger_queue!r}"
        )
        assert {
            seat for seat, player in manager_after_choice.state.players.items() if not player.alive
        } == initial_deaths
        assert all(
            item.status == "COMPLETED" for item in manager_after_choice.state.rule_trigger_queue
        )
        assert (
            sum(
                fact.fact_type == "DEATH_CONFIRMED"
                for entry in manager_after_choice.state.rule_ledger
                for fact in entry.facts
            )
            == 2
        )
        assert (
            sum(
                fact.fact_type == "QUASAR_CHAIN_READY"
                for entry in manager_after_choice.state.rule_ledger
                for fact in entry.facts
            )
            == 2
        )
        assert (
            sum(
                fact.fact_type == "QUASAR_CHOICE_ACK"
                for entry in manager_after_choice.state.rule_ledger
                for fact in entry.facts
            )
            == 2
        )
        assert (
            next(
                item
                for item in manager_after_choice.state.ability_instances
                if item.skill_id == "quasar_after_death_echo"
            ).uses_consumed
            == 2
        )
        assert (
            next(
                item
                for item in manager_after_choice.state.ability_instances
                if item.skill_id == "quasar_after_auto_choice"
            ).uses_consumed
            == 2
        )
        before_replay = manager_after_choice.state
        replay = await triggers3.auto_resolve()
        assert replay["status"] == "idle"
        assert manager_after_choice.state.rule_ledger == before_replay.rule_ledger
        assert manager_after_choice.state.rule_trigger_queue == before_replay.rule_trigger_queue
    finally:
        await runtime3.close("B trigger chain recovery complete")
        await gateway3.close()
        await server3.close()
