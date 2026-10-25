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
    _settle_initial_multi_death,
    _submit_choice,
)
from test_rules_b_workflow import (
    _manager as _base_manager,
)

from werewolf.cli_support.play_setup import build_play_setup
from werewolf.domain.enums import GamePhase
from werewolf.domain.sheriff_eligibility import first_day_sheriff_participants
from werewolf.game.actions import load_action_registry
from werewolf.game.events import EventType
from werewolf.game.manager import EventCommitError, GameManager
from werewolf.game.sheriff import SheriffElectionError
from werewolf.game.state import GameState
from werewolf.game.vote_turn import ABSTAIN_ACTION_CODE, VOTE_ACTION_CODE
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.refs import VersionedRef
from werewolf.moderator.play_runner import ClassicPlayRunner
from werewolf.rules.models import ExecutionPackage
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.player_runtime import (
    Action,
    ActionResponse,
    TurnRequest,
)

PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
N0_PROTECT_ACTION = 984


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
                        _compare("eq", _ref("item", "seat"), _literal(seat)) for seat in seats
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
                code for code in (MASS_ACTION, CHOICE_ACTION) if code in window.allowed_action_codes
            )
            targets = (3,) if action_code == MASS_ACTION else (1,)
            return ActionResponse(
                request_id=request.request_id,
                actions=[Action(action_code=action_code, targets=list(targets))],
            )
        return super()._action_for(request, status)


class _LaterNightDemoRuntime(DemoRuntime):
    def _action_for(self, request: TurnRequest, status: dict[str, Any]) -> ActionResponse:
        if request.phase is GamePhase.VOTE:
            seat = request.observation.payload.get("seat")
            if seat in {8, 12}:
                return ActionResponse(
                    request_id=request.request_id,
                    actions=[Action(action_code=ABSTAIN_ACTION_CODE)],
                )
            assert type(seat) is int
            target = 1 if seat in {1, 2, 3, 4, 5} else 2
            return ActionResponse(
                request_id=request.request_id,
                actions=[Action(action_code=VOTE_ACTION_CODE, targets=[target])],
            )
        window = request.action_window
        if window is not None:
            round_no = request.observation.payload.get("round_no")
            if round_no == 0 and N0_PROTECT_ACTION in window.allowed_action_codes:
                return ActionResponse(
                    request_id=request.request_id,
                    actions=[Action(action_code=N0_PROTECT_ACTION, targets=[3])],
                )
            action_code = next(
                (
                    code
                    for code in (MASS_ACTION, CHOICE_ACTION)
                    if code in window.allowed_action_codes
                ),
                None,
            )
            if action_code is not None:
                target = 3 if action_code == MASS_ACTION else min(window.candidate_seats)
                return ActionResponse(
                    request_id=request.request_id,
                    actions=[Action(action_code=action_code, targets=[target])],
                )
        return super()._action_for(request, status)


async def _assert_scheduled_badge_rejects_broken_choice_ancestry(
    manager: GameManager,
) -> None:
    state = manager.state
    assert isinstance(state.sheriff_badge, dict)
    assert state.sheriff_badge.get("status") == "COMPLETE"
    choice_occurrence = next(
        item
        for item in state.rule_trigger_queue
        if item.skill_id == "c_night_choice_death" and item.mode == "PLAYER_CHOICE"
    )
    choice_ledger = next(
        item
        for item in state.rule_ledger
        if "c_night_choice_death" in item.skill_ids
        and item.timing == GamePhase.TRIGGER_ACTION.value
    )
    physical_fact = next(
        fact
        for item in state.rule_ledger
        for fact in item.facts
        if fact.fact_type == "DEATH_CONFIRMED" and fact.death_cause == "wolf_kill"
    )

    tampered_receipt_state = state.model_copy(
        update={
            "rule_receipts": tuple(
                item for item in state.rule_receipts if item.batch_id != choice_ledger.batch_id
            )
        }
    )
    tampered_source_queue = tuple(
        item.model_copy(update={"source_fact_id": physical_fact.fact_id})
        if item.occurrence_id == choice_occurrence.occurrence_id
        else item
        for item in state.rule_trigger_queue
    )
    tampered_source_state = state.model_copy(update={"rule_trigger_queue": tampered_source_queue})
    for tampered_state in (tampered_receipt_state, tampered_source_state):
        probe = GameManager(
            GameState.model_validate_json(tampered_state.model_dump_json()),
            registry=manager.registry,
            execution_package=manager.execution_package,
            package_identity=manager.package_identity,
            legacy_compatibility=manager.legacy_compatibility,
        )
        with pytest.raises(SheriffElectionError):
            await probe.complete_sheriff_badge(
                expected_revision=probe.state.state_revision,
                now=NOW,
            )


async def _build_scheduled_ancestry_setup(
    output: Path, *, exercise_later_night: bool = False
) -> None:
    from test_rules_c_scheduled_night_flow import _scheduled_published_root

    published_root = _scheduled_published_root(output.parent)
    board_path = (
        published_root / "boards" / "classic_12_seer_witch_hunter_idiot" / "1.0.0" / "board.md"
    )
    board_source = board_path.read_text(encoding="utf-8")
    board_sections = board_source.split("---", 2)
    assert len(board_sections) == 3
    board_data = yaml.safe_load(board_sections[1])
    assert isinstance(board_data, dict)
    flow = board_data.get("day_flow")
    assert isinstance(flow, dict)
    if exercise_later_night:
        vote = flow.get("vote")
        pk = flow.get("pk")
        assert isinstance(vote, dict)
        assert isinstance(pk, dict)
        vote["tie_policy"] = "no_exile_on_tie"
        pk["enabled"] = False
    last_words = flow.get("last_words")
    assert isinstance(last_words, dict)
    last_words["eligible_death_causes"] = [
        "wolf_kill",
        "c_night_auto_death",
        "c_night_choice_death",
    ]
    board_path.write_text(
        f"---\n{yaml.safe_dump(board_data, allow_unicode=True, sort_keys=False)}---\n"
        f"{board_sections[2].lstrip()}",
        encoding="utf-8",
    )
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
    usage_scope = "ROUND" if exercise_later_night else "GAME"
    payload["interactions"] = [
        {
            "interaction_id": "c-confirm-night-physical-death",
            "rule_type": "CONFIRM_DEATH",
            "damage_tags": ["c_night_physical_death"],
            "death_cause": "wolf_kill",
        },
        {
            "interaction_id": "c-confirm-night-auto-death",
            "rule_type": "CONFIRM_DEATH",
            "damage_tags": ["c_night_auto_death"],
            "death_cause": "c_night_auto_death",
        },
        {
            "interaction_id": "c-confirm-night-choice-death",
            "rule_type": "CONFIRM_DEATH",
            "damage_tags": ["c_night_choice_death"],
            "death_cause": "c_night_choice_death",
        },
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
            "grants": [{"grant_id": "c_night_physical_grant", "actor_selector": _seat_selector(1)}],
            "timing": ["NIGHT_ACTION"],
            "window_ids": ["night_actions"],
            "targets": {
                "min_targets": 1,
                "max_targets": 1,
                "selector": _fixed_live_targets(3),
                "allow_self": False,
            },
            "usage": {"max_uses": 1, "scope": usage_scope},
            "effects": [
                {
                    "effect_id": "c-night-physical-damage",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["c_night_physical_death"],
                }
            ],
        },
        {
            "skill_id": "c_night_automatic_death",
            "mode": "AUTOMATIC",
            "action_code": AUTO_ACTION,
            "grants": [{"grant_id": "c_night_auto_grant", "actor_selector": _seat_selector(2)}],
            "timing": ["TRIGGER_ACTION"],
            "trigger": {
                "fact_types": ["DEATH_CONFIRMED"],
                "mode": "AUTOMATIC",
                "condition": _compare(
                    "eq",
                    _ref("source_fact", "death_cause"),
                    _literal("wolf_kill"),
                ),
            },
            "targets": {"min_targets": 0, "max_targets": 0, "selector": _all_living_except_actor()},
            "usage": {"max_uses": 1, "scope": usage_scope},
            "effects": [
                {
                    "effect_id": "c-night-auto-damage",
                    "effect_type": "DAMAGE",
                    "target": _ref("actor", "seat"),
                    "tags": ["c_night_auto_death"],
                }
            ],
        },
        {
            "skill_id": "c_night_choice_death",
            "action_code": CHOICE_ACTION,
            "grants": [{"grant_id": "c_night_choice_grant", "actor_selector": _seat_selector(2)}],
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
            "usage": {"max_uses": 1, "scope": usage_scope},
            "effects": [
                {
                    "effect_id": "c-night-choice-damage",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["c_night_choice_death"],
                }
            ],
        },
    ]
    if exercise_later_night:
        payload["skills"].append(
            {
                "skill_id": "c_night_n0_prevent_death",
                "action_code": N0_PROTECT_ACTION,
                "grants": [
                    {
                        "grant_id": "c_night_n0_protect_grant",
                        "actor_selector": _seat_selector(11),
                    }
                ],
                "timing": ["NIGHT_ACTION"],
                "window_ids": ["night_actions"],
                "targets": {
                    "min_targets": 1,
                    "max_targets": 1,
                    "selector": _fixed_live_targets(3),
                    "allow_self": False,
                },
                "usage": {"max_uses": 1, "scope": "GAME"},
                "effects": [
                    {
                        "effect_id": "c-night-n0-prevent-death",
                        "effect_type": "PREVENT_DEATH",
                        "target": _ref("target", "seat"),
                    }
                ],
            }
        )
        payload["actions"].append(
            {"action_code": N0_PROTECT_ACTION, "action_id": "C_NIGHT_N0_PREVENT_DEATH"}
        )
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
                "target_count": 1,
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
    if exercise_later_night:
        action_definitions.append(
            {
                "action_code": N0_PROTECT_ACTION,
                "action_name": "C_NIGHT_N0_PREVENT_DEATH",
                "target_policy": "candidate",
                "target_count": 1,
            }
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
        runtime_factory=lambda _player, gateway_url, token: _CandidateSelectingDemoRuntime(
            gateway_url,
            token,
        ),
    )
    history: list[tuple[str, GamePhase, int, int]] = []
    dead_sheriff: int | None = None
    command_history: list[str] = []
    original_command = runner.command

    async def record_command(line: str) -> Any:
        if line == "sheriff badge finish":
            assert manager is not None
            await _assert_scheduled_badge_rejects_broken_choice_ancestry(manager)
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
        for _ in range(200):
            if runner.shell.state.phase is GamePhase.DAY_SPEECH:
                break
            phase = runner.shell.state.phase
            if phase is GamePhase.NIGHT_TEAM_CHAT:
                await runner._night_team()
            elif phase is GamePhase.NIGHT_ACTION:
                await runner._night_actions()
            elif phase is GamePhase.NIGHT_RESOLVE:
                await runner._night_resolve()
            elif phase is GamePhase.DAY_ANNOUNCE:
                if runner.shell.state.round_no == 0 and runner.sheriff_candidates is None:
                    last_words_seats = tuple(
                        seat
                        for boundary in runner.shell.state.rule_boundaries
                        if boundary.return_point.phase is GamePhase.DAY_ANNOUNCE
                        and boundary.return_point.day_no == 0
                        for seat in boundary.last_words_seats
                    )
                    sheriff_candidates = tuple(
                        seat
                        for seat in last_words_seats
                        if not runner.shell.state.players[seat].alive
                    )
                    eligible_sheriffs = first_day_sheriff_participants(
                        runner.shell.state,
                        execution_package=manager.execution_package,
                    )
                    assert sheriff_candidates
                    assert sheriff_candidates[0] in eligible_sheriffs
                    dead_sheriff = sheriff_candidates[0]
                    runner.sheriff_candidates = (dead_sheriff,)
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
        else:
            raise AssertionError("N0 scheduled dawn did not reach ordinary day speech")
        state = runner.shell.state
        assert waiting_choice is not None
        assert {seat for seat, player in state.players.items() if not player.alive} >= {
            2,
            3,
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
        assert auto_death.data["source_request_ids"] == (
            f"automatic-{auto_occurrence.occurrence_id}",
        )
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
        await checkpoint_manager.assert_private_checkpoint_waiting_choice(checkpoint_manager.state)
        missing_auto_receipt = waiting_choice.model_copy(
            update={
                "rule_receipts": tuple(
                    item for item in waiting_choice.rule_receipts if item.group_id != auto_group
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
            if item.mode == "PLAYER_CHOICE" and item.skill_id == "c_night_choice_death"
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
        with pytest.raises(EventCommitError, match="CHECKPOINT_BOUNDARY|CHECKPOINT_SOURCE"):
            await forged_manager.assert_private_checkpoint_waiting_choice(forged_manager.state)

        assert runner.shell.state.phase is GamePhase.DAY_SPEECH
        assert runner.shell.state.sheriff_election is not None
        assert dead_sheriff is not None
        assert runner.shell.state.sheriff_election["sheriff_seat"] == dead_sheriff
        announce_index = command_history.index("day announce")
        badge_index = command_history.index("sheriff badge open")
        words_index = command_history.index(f"last-words next {dead_sheriff}")
        night_check_index = command_history.index("victory night-check")
        speech_index = command_history.index("day speech open")
        assert announce_index < badge_index < words_index < night_check_index < speech_index
        announcements = [
            event
            for event in state.events
            if getattr(event, "event_type", None) is EventType.ANNOUNCEMENT
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
    choice["grants"] = [{"grant_id": "z_generic_choice_grant", "actor_selector": _seat_selector(2)}]
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
    for _ in range(256):
        if choice_step is not None:
            break
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
    else:
        raise AssertionError("night ancestry workflow did not reach its player choice")

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
    assert auto_death.data["source_request_ids"] == (
        f"automatic-{automatic_occurrence.occurrence_id}",
    )

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
    for _ in range(256):
        step = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
        if step.kind == "RETURN":
            break
        assert step.kind == "IDLE" and step.queue_pending
        choice_state = manager.state
    else:
        raise AssertionError("night ancestry workflow did not return after its player choice")
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
    # host-pending boundary until a real dawn flow completes it.
    boundaries = choice_state.rule_boundaries
    assert {seat for item in boundaries for seat in item.death_seats} == {2, 3, 4, 5}
    assert len({item.source_group_id for item in boundaries}) >= 3
    assert all(item.is_pending for item in boundaries)


@pytest.mark.asyncio
async def test_n1_scheduled_runtime_chain_runs_after_n0_day_without_exile(
    tmp_path: Path,
) -> None:
    setup = tmp_path / "n1-night-chain"
    await _build_scheduled_ancestry_setup(setup, exercise_later_night=True)
    runner = ClassicPlayRunner(
        setup / "game.yaml",
        max_rounds=2,
        runtime_factory=lambda _player, gateway_url, token: _LaterNightDemoRuntime(
            gateway_url,
            token,
        ),
    )
    history: list[tuple[str, GamePhase, int, int]] = []
    original_command = runner.command

    async def record_command(line: str) -> Any:
        result = await original_command(line)
        state = runner.shell.state
        history.append((line, state.phase, state.round_no, state.day_no))
        return result

    runner.command = record_command  # type: ignore[method-assign]
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
        schedule = manager.execution_package.night_boundary_schedule
        assert schedule is not None
        assert schedule.required_for_round(0) == (
            "FIRST_DAY_SHERIFF_ELECTION",
            "DAY_ANNOUNCEMENT",
        )
        assert schedule.required_for_round(1) == ("DAY_ANNOUNCEMENT",)
        # Seat 8 survives N0 and is elected before the first announcement.
        runner.sheriff_candidates = (8,)

        for _ in range(200):
            if (
                runner.shell.state.round_no == 1
                and runner.shell.state.phase is GamePhase.NIGHT_TEAM_CHAT
            ):
                break
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
            elif phase in {
                GamePhase.DAY_SPEECH,
                GamePhase.VOTE,
                GamePhase.VOTE_PK_SPEECH,
                GamePhase.VOTE_PK,
            }:
                await runner._day_vote()
            elif phase is GamePhase.DAY_RESOLVE:
                assert runner.shell.state.round_no == 0
                await runner.command("day confirm-exile none")
                assert all(player.alive for player in runner.shell.state.players.values())
                await runner._last_words()
                await runner.command("day finish")
            elif phase is GamePhase.VICTORY_CHECK:
                await runner.command("victory check")
            else:
                raise AssertionError(f"unexpected phase before N1 scheduled night: {phase.value}")
        else:
            raise AssertionError("N0/day/no-exile path did not enter the N1 scheduled night")

        assert runner.shell.state.round_no == 1
        assert runner.shell.state.phase is GamePhase.NIGHT_TEAM_CHAT
        assert all(player.alive for player in runner.shell.state.players.values())
        assert runner.shell.state.sheriff_seat == 8
        n0_night_ledger = next(
            item
            for item in runner.shell.state.rule_ledger
            if item.group_id == "night:0"
            and item.round_no == 0
            and item.timing == GamePhase.NIGHT_ACTION.value
        )
        assert {
            "c_night_n0_prevent_death",
            "c_night_physical_group_death",
        } <= set(n0_night_ledger.skill_ids)
        assert not any(fact.fact_type == "DEATH_CONFIRMED" for fact in n0_night_ledger.facts)
        assert not any(
            item.skill_id in {"c_night_automatic_death", "c_night_choice_death"}
            for item in runner.shell.state.rule_trigger_queue
        )
        assert any(row[0] == "day confirm-exile none" and row[2] == 0 for row in history)

        await runner._night_team()
        await runner._night_actions()
        physical_anchor_proof_checked = False
        original_terminal_proof = manager._has_terminal_night_boundary_source

        def verify_physical_anchor_proof(
            state: GameState,
            boundary: Any,
            *,
            depth: int = 0,
            seen: frozenset[str] = frozenset(),
        ) -> bool:
            nonlocal physical_anchor_proof_checked
            proved = original_terminal_proof(
                state,
                boundary,
                depth=depth,
                seen=seen,
            )
            physical_ledger = next(
                (
                    item
                    for item in state.rule_ledger
                    if item.batch_id == boundary.source_batch_id
                    and item.group_id == f"night:{state.round_no}"
                    and item.round_no == state.round_no
                    and item.timing == GamePhase.NIGHT_ACTION.value
                    and "c_night_physical_group_death" in item.skill_ids
                ),
                None,
            )
            if (
                not physical_anchor_proof_checked
                and state.round_no == 1
                and boundary.source_group_id == "night:1"
                and physical_ledger is not None
                and proved
            ):
                tampered_receipts = tuple(
                    receipt.model_copy(update={"package_id": "forged-package"})
                    if receipt.batch_id == physical_ledger.batch_id
                    else receipt
                    for receipt in state.rule_receipts
                )
                tampered_receipt_state = state.model_copy(
                    update={"rule_receipts": tampered_receipts}
                )
                assert not original_terminal_proof(tampered_receipt_state, boundary)
                assert state.execution_identity is not None
                tampered_identity_state = state.model_copy(
                    update={
                        "execution_identity": state.execution_identity.model_copy(
                            update={"package_id": "forged-package"}
                        )
                    }
                )
                assert not original_terminal_proof(tampered_identity_state, boundary)
                physical_anchor_proof_checked = True
            return proved

        manager._has_terminal_night_boundary_source = verify_physical_anchor_proof  # type: ignore[method-assign]
        try:
            await runner._night_resolve()
        finally:
            manager._has_terminal_night_boundary_source = original_terminal_proof  # type: ignore[method-assign]
        assert physical_anchor_proof_checked
        assert runner.shell.state.round_no == 1
        assert runner.shell.state.phase is GamePhase.DAY_ANNOUNCE
        assert {
            seat for seat, player in runner.shell.state.players.items() if not player.alive
        } >= {
            2,
            3,
        }

        physical_ledger = next(
            item
            for item in runner.shell.state.rule_ledger
            if item.group_id == "night:1"
            and item.timing == GamePhase.NIGHT_ACTION.value
            and "c_night_physical_group_death" in item.skill_ids
        )
        physical_death = next(
            fact
            for fact in physical_ledger.facts
            if fact.fact_type == "DEATH_CONFIRMED" and fact.target_seat == 3
        )
        auto_occurrence = next(
            item
            for item in runner.shell.state.rule_trigger_queue
            if item.skill_id == "c_night_automatic_death"
            and item.mode == "AUTOMATIC"
            and item.status == "COMPLETED"
        )
        assert auto_occurrence.source_fact_id == physical_death.fact_id
        auto_ledger = next(
            item
            for item in runner.shell.state.rule_ledger
            if item.group_id == f"automatic-{auto_occurrence.occurrence_id}"
        )
        auto_death = next(
            fact
            for fact in auto_ledger.facts
            if fact.fact_type == "DEATH_CONFIRMED" and fact.target_seat == 2
        )
        choice_occurrence = next(
            item
            for item in runner.shell.state.rule_trigger_queue
            if item.skill_id == "c_night_choice_death" and item.mode == "PLAYER_CHOICE"
        )
        assert choice_occurrence.status == "COMPLETED"
        assert choice_occurrence.source_fact_id == auto_death.fact_id
        assert choice_occurrence.source_batch_id == auto_ledger.batch_id
        choice_ledger = next(
            item
            for item in runner.shell.state.rule_ledger
            if "c_night_choice_death" in item.skill_ids
            and item.timing == GamePhase.TRIGGER_ACTION.value
        )
        choice_death = next(
            fact for fact in choice_ledger.facts if fact.fact_type == "DEATH_CONFIRMED"
        )
        assert choice_death.target_seat not in {2, 3}
        assert {physical_death.target_seat, auto_death.target_seat, choice_death.target_seat} <= {
            seat for boundary in runner.shell.state.rule_boundaries for seat in boundary.death_seats
        }
        assert physical_ledger.round_no == 1
        assert auto_ledger.round_no == 1
        assert choice_ledger.round_no == 1

        await runner._day_announce()
        state = runner.shell.state
        assert state.phase is GamePhase.DAY_SPEECH
        assert state.round_no == 1
        assert state.day_no == 2
        assert state.rule_boundaries
        assert all(not item.is_pending for item in state.rule_boundaries)
        assert any(
            getattr(event, "event_type", None) is EventType.ANNOUNCEMENT
            and getattr(event, "correlation_id", None) == "day-announce-r1-d2"
            for event in state.events
        )
    finally:
        if runner.shell.session_service is not None:
            await runner.shell.session_service.close("N1 scheduled ancestry test complete")
