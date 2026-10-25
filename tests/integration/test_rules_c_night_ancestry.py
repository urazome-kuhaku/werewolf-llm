"""Real manager ancestry checks for chained night deaths and dawn boundaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from test_rules_b_workflow import (
    AUTO_ACTION,
    CHOICE_ACTION,
    MASS_ACTION,
    NOW,
    _manager as _base_manager,
    _settle_initial_multi_death,
    _submit_choice,
)

from werewolf.domain.enums import GamePhase
from werewolf.game.actions import load_action_registry
from werewolf.game.manager import EventCommitError, GameManager
from werewolf.game.state import GameState
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.refs import VersionedRef
from werewolf.cli_support.play_setup import build_play_setup
from werewolf.moderator.play_runner import ClassicPlayRunner
from werewolf.rules.models import ExecutionPackage
from werewolf.runtime.player_runtime import (
    Action,
    ActionResponse,
    TurnRequest,
)
from werewolf.runtime.demo_runtime import DemoRuntime

PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"


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


def _all_living_except_actor() -> dict[str, object]:
    return {
        "op": "select",
        "source": "players",
        "where": {
            "op": "and",
            "values": [
                _compare("eq", _ref("item", "alive"), _literal(True)),
                _compare("ne", _ref("item", "seat"), _ref("actor", "seat")),
            ],
        },
        "map": _ref("item", "seat"),
    }


def _fixed_live_targets(*seats: int) -> dict[str, object]:
    return {
        "op": "select",
        "source": "players",
        "where": {
            "op": "and",
            "values": [
                _compare("eq", _ref("item", "alive"), _literal(True)),
                {
                    "op": "or",
                    "values": [
                        _compare("eq", _ref("item", "seat"), _literal(seat))
                        for seat in seats
                    ],
                },
            ],
        },
        "map": _ref("item", "seat"),
    }


class _CandidateSelectingDemoRuntime(DemoRuntime):
    def _action_for(self, request: TurnRequest, status: dict[str, Any]) -> ActionResponse:
        window = request.action_window
        if window is not None and any(
            code in window.allowed_action_codes for code in (MASS_ACTION, CHOICE_ACTION)
        ):
            action_code = next(
                code
                for code in (MASS_ACTION, CHOICE_ACTION)
                if code in window.allowed_action_codes
            )
            targets = (3, 4) if action_code == MASS_ACTION else (1,)
            return ActionResponse(
                request_id=request.request_id,
                actions=[Action(action_code=action_code, targets=list(targets))],
            )
        return super()._action_for(request, status)


async def _build_scheduled_ancestry_setup(output: Path) -> None:
    from test_rules_c_scheduled_night_flow import _scheduled_published_root

    published_root = _scheduled_published_root(output.parent)
    source_package = await KnowledgePackageLoader(published_root).load(
        VersionedRef.parse(BOARD_REF)
    )
    compiled = KnowledgePackageCompiler().compile(source_package)
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    for role_path in (published_root / "roles").glob("*/1.0.0/role.md"):
        source = role_path.read_text(encoding="utf-8")
        sections = source.split("---", 2)
        assert len(sections) == 3
        role_data = yaml.safe_load(sections[1])
        assert isinstance(role_data, dict)
        role_data["abilities"] = []
        role_path.write_text(
            f"---\n{yaml.safe_dump(role_data, allow_unicode=True, sort_keys=False)}---\n"
            f"{sections[2].lstrip()}",
            encoding="utf-8",
        )
    execution = compiled.execution
    payload = execution.model_dump(mode="json")
    payload.pop("night_boundary_schedule", None)
    payload["interactions"] = [
        {
            "interaction_id": "c-confirm-night-chain-death",
            "rule_type": "CONFIRM_DEATH",
            "damage_tags": ["c_night_chain_death"],
            "death_cause": "wolf_kill",
        }
    ]
    payload["actions"].extend(
        [
            {"action_code": MASS_ACTION, "action_id": "C_NIGHT_MASS_DEATH"},
            {"action_code": AUTO_ACTION, "action_id": "C_NIGHT_AUTO_DEATH"},
            {"action_code": CHOICE_ACTION, "action_id": "C_NIGHT_CHOICE_DEATH"},
        ]
    )
    payload["skills"] = [
        {
            "skill_id": "c_night_physical_group_death",
            "action_code": MASS_ACTION,
            "grants": [
                {"grant_id": "c_night_physical_grant", "actor_selector": _seat_selector(1)}
            ],
            "timing": ["NIGHT_ACTION"],
            "window_ids": ["night_actions"],
            "targets": {
                "min_targets": 2,
                "max_targets": 2,
                "selector": _fixed_live_targets(3, 4),
                "allow_self": False,
            },
            "usage": {"max_uses": 1, "scope": "GAME"},
            "effects": [
                {
                    "effect_id": "c-night-physical-damage",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["c_night_chain_death"],
                }
            ],
        },
        {
            "skill_id": "c_night_automatic_death",
            "mode": "AUTOMATIC",
            "action_code": AUTO_ACTION,
            "grants": [
                {"grant_id": "c_night_auto_grant", "actor_selector": _seat_selector(2)}
            ],
            "timing": ["TRIGGER_ACTION"],
            "trigger": {
                "fact_types": ["DEATH_CONFIRMED"],
                "mode": "AUTOMATIC",
                "condition": _compare(
                    "ne", _ref("source_fact", "target_seat"), _ref("actor", "seat")
                ),
            },
            "targets": {"min_targets": 0, "max_targets": 0, "selector": _all_living_except_actor()},
            "usage": {"max_uses": 1, "scope": "GAME"},
            "effects": [
                {
                    "effect_id": "c-night-auto-damage",
                    "effect_type": "DAMAGE",
                    "target": _ref("actor", "seat"),
                    "tags": ["c_night_chain_death"],
                }
            ],
        },
        {
            "skill_id": "c_night_choice_death",
            "action_code": CHOICE_ACTION,
            "grants": [
                {"grant_id": "c_night_choice_grant", "actor_selector": _seat_selector(2)}
            ],
            "timing": ["TRIGGER_ACTION"],
            "trigger": {
                "fact_types": ["DEATH_CONFIRMED"],
                "mode": "PLAYER_CHOICE",
                "condition": _compare(
                    "eq", _ref("source_fact", "target_seat"), _ref("actor", "seat")
                ),
            },
            "targets": {
                "min_targets": 1,
                "max_targets": 1,
                "selector": _all_living_except_actor(),
                "allow_self": False,
            },
            "usage": {"max_uses": 1, "scope": "GAME"},
            "effects": [
                {
                    "effect_id": "c-night-choice-damage",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["c_night_chain_death"],
                }
            ],
        },
    ]
    base_codes = {item.action_code for item in load_action_registry().actions}
    action_definitions = [
        item.model_dump(mode="json")
        for item in compiled.action_registry.actions
        if item.action_code not in base_codes
    ]
    action_definitions.extend(
        [
            {
                "action_code": MASS_ACTION,
                "action_name": "C_NIGHT_MASS_DEATH",
                "target_policy": "candidate",
                "target_count": 2,
            },
            {
                "action_code": AUTO_ACTION,
                "action_name": "C_NIGHT_AUTO_DEATH",
                "target_policy": "none",
                "target_count": 0,
            },
            {
                "action_code": CHOICE_ACTION,
                "action_name": "C_NIGHT_CHOICE_DEATH",
                "target_policy": "candidate",
                "target_count": 1,
            },
        ]
    )
    board_path = (
        published_root
        / "boards"
        / "classic_12_seer_witch_hunter_idiot"
        / "1.0.0"
        / "execution.yaml"
    )
    board_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "execution": payload,
                "action_definitions": action_definitions,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    await asyncio.to_thread(
        build_play_setup,
        output,
        all_scripted=True,
        game_id="c-night-ancestry-game",
        seed=20260930,
        board_ref=BOARD_REF,
        published_root=published_root,
        project_root=PROJECT_ROOT,
    )


@pytest.mark.asyncio
async def test_n0_scheduled_runtime_chain_finishes_deaths_words_badge_and_speech(
    tmp_path: Path,
) -> None:
    setup = tmp_path / "night-chain"
    await _build_scheduled_ancestry_setup(setup)
    runner = ClassicPlayRunner(
        setup / "game.yaml",
        max_rounds=1,
        sheriff_candidates=(2,),
        runtime_factory=lambda _player, gateway_url, token: _CandidateSelectingDemoRuntime(
            gateway_url,
            token,
        ),
    )
    history: list[tuple[str, GamePhase, int, int]] = []
    command_history: list[str] = []
    original_command = runner.command

    async def record_command(line: str) -> Any:
        result = await original_command(line)
        command_history.append(line)
        return result

    runner.command = record_command  # type: ignore[method-assign]
    waiting_choice: GameState | None = None
    try:
        await runner.command("new")
        await runner.command("next")
        await runner.command("next")
        await runner.command("start")
        await runner._prepare()
        await runner.command("next")
        manager = runner.shell.manager
        assert manager is not None
        assert manager.execution_package is not None
        assert manager.execution_package.night_boundary_schedule is not None

        original_advance = manager.advance_rule_workflow

        async def capture_choice(*args: Any, **kwargs: Any) -> Any:
            nonlocal waiting_choice
            step = await original_advance(*args, **kwargs)
            if step.kind == "PLAYER_CHOICE" and waiting_choice is None:
                waiting_choice = GameState.model_validate_json(manager.state.model_dump_json())
            return step

        manager.advance_rule_workflow = capture_choice  # type: ignore[method-assign]
        while runner.shell.state.phase is not GamePhase.DAY_SPEECH:
            phase = runner.shell.state.phase
            if phase is GamePhase.NIGHT_TEAM_CHAT:
                await runner._night_team()
            elif phase is GamePhase.NIGHT_ACTION:
                await runner._night_actions()
            elif phase is GamePhase.NIGHT_RESOLVE:
                await runner._night_resolve()
            elif phase is GamePhase.DAY_ANNOUNCE:
                await runner._day_announce()
            elif phase in {
                GamePhase.SHERIFF_ELECTION_SPEECH,
                GamePhase.SHERIFF_ELECTION,
                GamePhase.SHERIFF_ELECTION_PK_SPEECH,
                GamePhase.SHERIFF_ELECTION_PK,
                GamePhase.SHERIFF_TRANSFER,
            }:
                await runner._sheriff()
            else:
                raise AssertionError(f"unexpected phase before dawn speech: {phase.value}")
            history.append(
                (
                    phase.value,
                    runner.shell.state.phase,
                    runner.shell.state.round_no,
                    runner.shell.state.day_no,
                )
            )
        state = runner.shell.state
        assert waiting_choice is not None
        assert {seat for seat, player in state.players.items() if not player.alive} >= {
            2,
            3,
            4,
        }
        auto_occurrence = next(
            item
            for item in state.rule_trigger_queue
            if item.mode == "AUTOMATIC" and item.status == "COMPLETED"
        )
        auto_group = f"automatic-{auto_occurrence.occurrence_id}"
        auto_ledger = next(item for item in state.rule_ledger if item.group_id == auto_group)
        auto_receipt = next(item for item in state.rule_receipts if item.group_id == auto_group)
        assert auto_receipt.occurrence_ids == (auto_occurrence.occurrence_id,)
        auto_death = next(
            fact
            for fact in auto_ledger.facts
            if fact.fact_type == "DEATH_CONFIRMED" and fact.target_seat == 2
        )
        assert auto_death.source_request_id == f"automatic-{auto_occurrence.occurrence_id}"
        assert auto_death.data["source_request_ids"] == [
            f"automatic-{auto_occurrence.occurrence_id}"
        ]
        choice_occurrence = next(
            item
            for item in state.rule_trigger_queue
            if item.mode == "PLAYER_CHOICE" and item.skill_id == "c_night_choice_death"
        )
        assert choice_occurrence.status == "COMPLETED"
        assert choice_occurrence.source_fact_id == auto_death.fact_id
        assert choice_occurrence.source_batch_id == auto_ledger.batch_id
        choice_ledger = next(
            item
            for item in state.rule_ledger
            if item.timing == GamePhase.TRIGGER_ACTION.value
            and "c_night_choice_death" in item.skill_ids
        )
        choice_receipt = next(
            item for item in state.rule_receipts if item.batch_id == choice_ledger.batch_id
        )
        assert choice_receipt.request_ids == choice_ledger.request_ids
        assert choice_ledger.request_ids
        assert any(
            fact.fact_type == "DEATH_CONFIRMED" and fact.target_seat not in {2, 3, 4}
            for fact in choice_ledger.facts
        )
        assert {seat for item in state.rule_boundaries for seat in item.death_seats} >= {
            2,
            3,
            4,
        }
        assert all(not item.is_pending for item in state.rule_boundaries)

        schedule = manager.execution_package.night_boundary_schedule
        assert schedule.required_for_round(0) == (
            "FIRST_DAY_SHERIFF_ELECTION",
            "DAY_ANNOUNCEMENT",
        )
        assert schedule.required_for_round(1) == ("DAY_ANNOUNCEMENT",)
        prerequisites = manager.night_boundary_prerequisites(state, round_no=0)
        assert [(item.prerequisite, item.satisfied) for item in prerequisites] == [
            ("FIRST_DAY_SHERIFF_ELECTION", True),
            ("DAY_ANNOUNCEMENT", True),
        ]

        # Waiting-choice restoration rechecks the actual AUTO receipt chain.
        checkpoint_manager = GameManager(
            waiting_choice,
            registry=manager.registry,
            execution_package=manager.execution_package,
            package_identity=manager.package_identity,
            legacy_compatibility=manager.legacy_compatibility,
        )
        await checkpoint_manager.assert_private_checkpoint_waiting_choice(
            checkpoint_manager.state
        )
        missing_auto_receipt = waiting_choice.model_copy(
            update={
                "rule_receipts": tuple(
                    item
                    for item in waiting_choice.rule_receipts
                    if item.group_id != auto_group
                )
            }
        )
        missing_receipt_manager = GameManager(
            missing_auto_receipt,
            registry=manager.registry,
            execution_package=manager.execution_package,
            package_identity=manager.package_identity,
            legacy_compatibility=manager.legacy_compatibility,
        )
        with pytest.raises(EventCommitError, match="CHECKPOINT_SOURCE"):
            await missing_receipt_manager.assert_private_checkpoint_waiting_choice(
                missing_receipt_manager.state
            )

        physical_fact = next(
            fact
            for ledger in waiting_choice.rule_ledger
            if ledger.group_id == "night:0"
            for fact in ledger.facts
            if fact.fact_type == "DEATH_CONFIRMED"
        )
        forged_queue = tuple(
            item.model_copy(
                update={
                    "source_fact_id": physical_fact.fact_id,
                    "source_batch_id": next(
                        ledger.batch_id
                        for ledger in waiting_choice.rule_ledger
                        if ledger.group_id == "night:0"
                    ),
                }
            )
            if item.mode == "PLAYER_CHOICE"
            and item.skill_id == "c_night_choice_death"
            else item
            for item in waiting_choice.rule_trigger_queue
        )
        forged_ancestry = waiting_choice.model_copy(update={"rule_trigger_queue": forged_queue})
        forged_manager = GameManager(
            forged_ancestry,
            registry=manager.registry,
            execution_package=manager.execution_package,
            package_identity=manager.package_identity,
            legacy_compatibility=manager.legacy_compatibility,
        )
        with pytest.raises(EventCommitError, match="CHECKPOINT_SOURCE"):
            await forged_manager.assert_private_checkpoint_waiting_choice(forged_manager.state)

        assert runner.shell.state.phase is GamePhase.DAY_SPEECH
        assert runner.shell.state.sheriff_election is not None
        assert runner.shell.state.sheriff_election["sheriff_seat"] == 2
        announce_index = command_history.index("day announce")
        badge_index = command_history.index("sheriff badge open")
        words_index = command_history.index("last-words next 2")
        night_check_index = command_history.index("victory night-check")
        speech_index = command_history.index("day speech open")
        assert announce_index < badge_index < words_index < night_check_index < speech_index
        announcements = [
            event
            for event in state.events
            if getattr(event, "event_type", None).value == "ANNOUNCEMENT"
            if getattr(event, "correlation_id", None) == "day-announce-r0-d1"
        ]
        assert len(announcements) == 1
    finally:
        if runner.shell.session_service is not None:
            await runner.shell.session_service.close("scheduled night ancestry test complete")


async def _night_chain_manager() -> GameManager:
    manager, _board = await _base_manager()
    package = manager.execution_package
    assert package is not None
    payload = json.loads(package.model_dump_json())
    skills = {row["skill_id"]: row for row in payload["skills"]}

    # Only seat 2 may emit the automatic follow-up, and only seat 4 may
    # answer that follow-up. Both seats are produced as deaths by the chain;
    # no test state marks them dead directly.
    automatic = skills["generic_automatic_echo_skill"]
    automatic["grants"] = [
        {"grant_id": "a_generic_auto_grant", "actor_selector": _seat_selector(2)}
    ]
    automatic["trigger"]["condition"] = _compare(
        "ne", _ref("source_fact", "target_seat"), _ref("actor", "seat")
    )
    automatic["effects"] = [
        {
            "effect_id": "night-auto-follow-up-death",
            "effect_type": "DAMAGE",
            "target": _ref("actor", "seat"),
            "tags": ["generic_auto_casualty"],
        }
    ]
    choice = skills["generic_player_choice_skill"]
    choice["grants"] = [
        {"grant_id": "z_generic_choice_grant", "actor_selector": _seat_selector(2)}
    ]
    choice["effects"] = [
        {
            "effect_id": "night-choice-follow-up-death",
            "effect_type": "DAMAGE",
            "target": _ref("target", "seat"),
            "tags": ["generic_choice_casualty"],
        }
    ]
    payload["interactions"].extend(
        [
            {
                "interaction_id": "confirm-night-auto-casualty",
                "rule_type": "CONFIRM_DEATH",
                "damage_tags": ["generic_auto_casualty"],
                "death_cause": "generic_chain_death",
            },
            {
                "interaction_id": "confirm-night-choice-casualty",
                "rule_type": "CONFIRM_DEATH",
                "damage_tags": ["generic_choice_casualty"],
                "death_cause": "generic_chain_death",
            },
        ]
    )
    boundary_policy = payload["boundary_policy"]
    boundary_policy["eligible_death_causes"] = ["generic_chain_death"]
    boundary_policy["night_death_policy"] = "every_night"
    execution = ExecutionPackage.model_validate_json(json.dumps(payload))

    # Rebind a clean setup state to the test package. The live manager will
    # rebuild granted abilities and every subsequent death from real commits.
    source_state = manager.state
    clean_state = source_state.model_copy(
        update={
            "execution_identity": None,
            "ability_instances": (),
            "rule_state": (),
            "rule_relations": (),
            "rule_deferred_disclosures": (),
            "rule_trigger_queue": (),
            "rule_workflow_cursor": source_state.rule_workflow_cursor.model_copy(
                update={
                    "cursor_id": None,
                    "settlement_group_id": None,
                    "active_window_ids": (),
                    "completed_collection_window_ids": (),
                    "active_occurrence_id": None,
                    "return_point": None,
                    "pending_flow_action": None,
                    "next_logical_window_id": None,
                    "pending_boundary_id": None,
                    "steps_used": 0,
                    "status": "IDLE",
                    "error_code": None,
                }
            ),
            "rule_boundaries": (),
            "rule_ledger": (),
            "rule_receipts": (),
            "action_windows": {},
            "action_requests": {},
        }
    )
    return GameManager(
        clean_state,
        registry=manager.registry,
        execution_package=execution,
    )


@pytest.mark.asyncio
async def test_night_physical_auto_and_choice_deaths_keep_exact_ancestry() -> None:
    manager = await _night_chain_manager()

    physical = await _settle_initial_multi_death(manager, targets=(3, 4))
    assert physical.phase is GamePhase.NIGHT_ACTION
    assert {seat for seat, player in physical.players.items() if not player.alive} == {3, 4}
    physical_ledger = next(
        item
        for item in physical.rule_ledger
        if item.timing == GamePhase.NIGHT_ACTION.value
        and item.request_ids == ("workflow-mass-death-request",)
    )
    physical_receipt = next(
        item for item in physical.rule_receipts if item.batch_id == physical_ledger.batch_id
    )
    assert physical_receipt.request_ids == physical_ledger.request_ids
    assert {
        fact.target_seat for fact in physical_ledger.facts if fact.fact_type == "DEATH_CONFIRMED"
    } == {3, 4}

    automatic_step = None
    choice_step = None
    while choice_step is None:
        step = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
        if step.kind == "AUTOMATIC":
            assert step.actor_seat == 2
            assert step.occurrence_id is not None
            automatic_step = automatic_step or step
            await manager.commit_rule_group(
                f"automatic-{step.occurrence_id}",
                occurrence_ids=(step.occurrence_id,),
                expected_revision=manager.state.state_revision,
                now=NOW,
            )
        elif step.kind == "PLAYER_CHOICE":
            choice_step = step
        else:
            assert step.kind == "IDLE" and step.queue_pending

    assert automatic_step is not None
    assert choice_step is not None
    assert not manager.state.players[2].alive
    automatic_group = f"automatic-{automatic_step.occurrence_id}"
    automatic_occurrence = next(
        item
        for item in manager.state.rule_trigger_queue
        if item.occurrence_id == automatic_step.occurrence_id
    )
    automatic_state = manager.state
    assert automatic_occurrence.status == "COMPLETED"
    automatic_ledger = next(
        item for item in automatic_state.rule_ledger if item.group_id == automatic_group
    )
    automatic_receipt = next(
        item for item in automatic_state.rule_receipts if item.group_id == automatic_group
    )
    assert automatic_receipt.occurrence_ids == (automatic_occurrence.occurrence_id,)
    auto_death = next(
        item
        for item in automatic_ledger.facts
        if item.fact_type == "DEATH_CONFIRMED" and item.target_seat == 2
    )
    assert auto_death.source_request_id == f"automatic-{automatic_occurrence.occurrence_id}"
    assert auto_death.data["source_request_ids"] == [
        f"automatic-{automatic_occurrence.occurrence_id}"
    ]

    assert choice_step.actor_seat == 2
    assert choice_step.occurrence_id is not None
    choice_occurrence = next(
        item
        for item in manager.state.rule_trigger_queue
        if item.occurrence_id == choice_step.occurrence_id
    )
    assert choice_occurrence.source_fact_id == auto_death.fact_id
    assert choice_occurrence.source_batch_id == automatic_ledger.batch_id

    # Removing the virtual AUTO receipt from a serialized copy cannot retain
    # the descendant choice's authority, even though its actor is already dead.
    no_auto_receipt_state = manager.state.model_copy(
        update={
            "rule_receipts": tuple(
                receipt
                for receipt in manager.state.rule_receipts
                if receipt.group_id != automatic_group
            )
        }
    )
    receipt_probe = GameManager(
        GameState.model_validate_json(no_auto_receipt_state.model_dump_json()),
        registry=manager.registry,
        execution_package=manager.execution_package,
    )
    with pytest.raises(EventCommitError, match="CHECKPOINT_SOURCE"):
        await receipt_probe.assert_private_checkpoint_waiting_choice(receipt_probe.state)

    choice_state = await _submit_choice(
        manager,
        occurrence_id=choice_step.occurrence_id,
        actor_seat=2,
        target_seat=5,
    )
    while True:
        step = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
        if step.kind == "RETURN":
            break
        assert step.kind == "IDLE" and step.queue_pending
        choice_state = manager.state
    choice_state = manager.state
    assert {seat for seat, player in choice_state.players.items() if not player.alive} == {
        3,
        4,
        2,
        5,
    }
    choice_ledger = next(
        item
        for item in choice_state.rule_ledger
        if item.timing == GamePhase.TRIGGER_ACTION.value
        and item.group_id != automatic_group
        and item.actor_seats == (2,)
    )
    choice_receipt = next(
        item for item in choice_state.rule_receipts if item.batch_id == choice_ledger.batch_id
    )
    assert choice_receipt.request_ids == choice_ledger.request_ids
    assert choice_ledger.request_ids == (f"choice-request-{choice_step.occurrence_id}",)
    assert {
        fact.target_seat for fact in choice_ledger.facts if fact.fact_type == "DEATH_CONFIRMED"
    } == {5}

    # Physical, AUTO-derived, and player-choice deaths each retain a separate
    # closed rule boundary; no point string or alive=False marker proves any.
    boundaries = choice_state.rule_boundaries
    assert {seat for item in boundaries for seat in item.death_seats} == {2, 3, 4, 5}
    assert len({item.source_group_id for item in boundaries}) >= 3
    assert all(not item.is_pending for item in boundaries)


@pytest.mark.asyncio
async def test_previous_day_host_exile_boundary_does_not_block_later_scheduled_night(
    tmp_path: Path,
) -> None:
    from test_rules_c_scheduled_night_flow import _build_scheduled_setup

    setup = tmp_path / "later-night-scheduled"
    await _build_scheduled_setup(setup)
    runner = __import__(
        "werewolf.moderator.play_runner", fromlist=["ClassicPlayRunner"]
    ).ClassicPlayRunner(setup / "game.yaml", max_rounds=20)
    history: list[tuple[str, GamePhase, int, int, int]] = []
    original_command = runner.command

    async def record_command(line: str) -> Any:
        result = await original_command(line)
        state = runner.shell.state
        history.append(
            (
                line,
                state.phase,
                state.round_no,
                state.day_no,
                sum(not player.alive for player in state.players.values()),
            )
        )
        return result

    runner.command = record_command  # type: ignore[method-assign]
    try:
        summary = await runner.run()
    finally:
        if runner.shell.session_service is not None:
            await runner.shell.session_service.close("night ancestry test complete")

    assert summary["status"] == "finished"
    assert summary["phase"] == GamePhase.FINISHED.value
    first_exile = next(
        index
        for index, row in enumerate(history)
        if row[0].startswith("day confirm-exile ") and row[2] == 0
    )
    later_night = next(
        index
        for index, row in enumerate(history)
        if row[0] == "night auto-resolve" and row[2] == 1
    )
    later_announcement = next(
        index
        for index, row in enumerate(history)
        if row[0] == "day announce" and row[2] == 1
    )
    later_night_check = next(
        index
        for index, row in enumerate(history)
        if row[0] == "victory night-check" and row[2] == 1
    )
    assert first_exile < later_night < later_announcement < later_night_check
    assert history[later_night][3] == history[later_night][2]
    assert history[later_night][4] > history[first_exile][4]

    state = runner.shell.state
    assert any(
        item.return_point.phase is GamePhase.DAY_RESOLVE
        and item.return_point.day_no == 1
        and not item.is_pending
        for item in state.rule_boundaries
    )
    assert any(
        item.source_group_id == "night:1"
        and item.return_point.phase is GamePhase.DAY_ANNOUNCE
        and not item.is_pending
        for item in state.rule_boundaries
    )
    # The winner reached through the later terminal night boundary proves the
    # previous DAY_RESOLVE boundary did not steal the new round's ancestry.
    assert state.winner is not None
    assert state.winner.resolved_by == "night_boundary"
