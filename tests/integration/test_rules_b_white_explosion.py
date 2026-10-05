"""Experimental white-wolf self-explosion through the B rule workflow.

The reference at ``doc/验收规则参考/网易狼人杀-12人白狼王守卫.md`` leaves the
ordinary-speech timing, legal target set, and white-wolf-to-Hunter chain open.
This test declares ordinary-speech BEFORE and AFTER hooks, exercises an AFTER
choice, selects one other living target, uses ``self_explosion`` as both deaths'
cause, and enables a Hunter choice from the generic death fact. These are
explicit test-package choices, not official-rule claims.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from werewolf.domain.enums import Channel, GamePhase
from werewolf.game import GameManager, GameState, PlayerState, RulesetRef, load_action_registry
from werewolf.game.actions import ActionDefinition, ActionRegistry
from werewolf.game.day import DayCoordinator, DayCoordinatorError
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiler import CompiledKnowledgePackage, KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.moderator import LastWordsFlow, ModeratorTriggerError, ModeratorTriggerFlow
from werewolf.moderator.night_flow import ModeratorNightFlow
from werewolf.rules.compiler import execution_windows_from_board, validate_execution_package
from werewolf.rules.models import BoundaryPolicy, ExecutionPackage
from werewolf.runtime.player_runtime import (
    ActionResponse,
    InitialContext,
    RuntimeConfig,
    RuntimeTurnResult,
    SpeechResponse,
    TurnRequest,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
PUBLISHED_ROOT = Path(__file__).parents[2] / "vault" / "published"
GAME_ID = "experimental-white-explosion-game"
SNAPSHOT_ID = "experimental-white-explosion-snapshot"
SUNBURST_ACTION = 907
EXTERNAL_DEATH_ACTION = 991


def _ref(source: str, name: str) -> dict[str, object]:
    return {"op": "ref", "source": source, "name": name}


def _literal(value: object) -> dict[str, object]:
    return {"op": "literal", "value": value}


def _compare(op: str, left: object, right: object) -> dict[str, object]:
    return {"op": op, "left": left, "right": right}


def _select_seats(*conditions: dict[str, object]) -> dict[str, object]:
    where: dict[str, object] | None = None
    if len(conditions) == 1:
        where = conditions[0]
    elif conditions:
        where = {"op": "and", "values": list(conditions)}
    return {
        "op": "select",
        "source": "players",
        **({"where": where} if where is not None else {}),
        "map": _ref("item", "seat"),
    }


def _seat_selector(seat: int) -> dict[str, object]:
    return _select_seats(_compare("eq", _ref("item", "seat"), _literal(seat)))


def _role_selector(role_id: str) -> dict[str, object]:
    return _select_seats(
        _compare("eq", _ref("item", "role_id"), _literal(role_id)),
    )


def _living_other_selector() -> dict[str, object]:
    return _select_seats(
        _compare("eq", _ref("item", "alive"), _literal(True)),
        _compare("ne", _ref("item", "seat"), _ref("actor", "seat")),
    )


def _all_living_selector() -> dict[str, object]:
    return _select_seats(_compare("eq", _ref("item", "alive"), _literal(True)))


def _boundary_policy(board: BoardDefinition) -> BoundaryPolicy:
    words = board.day_flow.last_words
    sheriff = board.day_flow.sheriff
    return BoundaryPolicy(
        last_words_enabled=words.enabled,
        eligible_death_causes=tuple(words.eligible_death_causes),
        before_reveal=words.before_reveal,
        night_death_policy=words.night_death_policy,
        day_death_policy=words.day_death_policy,
        sheriff_enabled=sheriff.enabled,
        badge_transfer_enabled=sheriff.transfer_enabled,
        badge_transfer_on_death=sheriff.transfer_on_death,
    )


def _experimental_board(
    compiled: CompiledKnowledgePackage,
    *,
    external_night_attack: bool,
) -> BoardDefinition:
    """Clone the loaded classic fixture with only this test's frozen choices."""

    payload = json.loads(json.dumps(compiled.package_payload["board_definition"]))
    payload["day_flow"]["last_words"]["eligible_death_causes"] = [
        "self_explosion",
        "hunter_shot",
        "wolf_kill",
    ]
    if external_night_attack:
        payload["day_flow"]["last_words"]["night_death_policy"] = "every_night"
        payload["wolf_team_visibility"]["discussion_enabled"] = False
        payload["night_windows"] = [
            {"window_id": "experimental_attack", "order": 1, "phase": "NIGHT_ACTION"},
            {
                "window_id": "night_resolve",
                "order": 2,
                "phase": "NIGHT_RESOLVE",
                "depends_on": ["experimental_attack"],
            },
        ]
        payload["knife_rule"]["available_after_window"] = "experimental_attack"
        payload["knife_rule"]["plan_confirmation_required"] = False
    return BoardDefinition.model_validate(payload)


def _execution(
    compiled: CompiledKnowledgePackage,
    board: BoardDefinition,
    *,
    external_night_attack: bool,
) -> tuple[ExecutionPackage, ActionRegistry]:
    """Build a complete test-only data package without role-name dispatch."""

    assert compiled.execution is not None
    raw = json.loads(compiled.execution.model_dump_json())
    action_rows: list[dict[str, object]] = [
        {"action_code": SUNBURST_ACTION, "action_id": "EXPERIMENTAL_PUBLIC_TAKE"},
        {"action_code": 105, "action_id": "HUNTER_SHOOT"},
    ]
    skills: list[dict[str, object]] = [
        {
            "skill_id": "experimental_sunburst_skill",
            "action_code": SUNBURST_ACTION,
            "grants": [
                {"grant_id": "experimental_sunburst_grant", "actor_selector": _seat_selector(2)}
            ],
            "timing": ["DAY_SPEECH"],
            "hook_ids": ["DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"],
            "targets": {
                "min_targets": 2,
                "max_targets": 2,
                "selector": _all_living_selector(),
                "allow_self": True,
            },
            "usage": {
                "max_uses": 1,
                "scope": "GAME",
                "costs": [{"resource_id": "charge", "amount": 1}],
            },
            "effects": [
                {
                    "effect_id": "experimental-self-death",
                    "effect_type": "DAMAGE",
                    "target": _ref("actor", "seat"),
                    "tags": ["sunburst_damage"],
                    "condition": _compare("eq", _ref("target", "seat"), _ref("actor", "seat")),
                },
                {
                    "effect_id": "experimental-taken-death",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["sunburst_damage"],
                    "condition": _compare("ne", _ref("target", "seat"), _ref("actor", "seat")),
                },
                {
                    "effect_id": "experimental-self-explosion-fact",
                    "effect_type": "FACT",
                    "target": _ref("actor", "seat"),
                    "fact_type": "SELF_EXPLOSION",
                    "condition": _compare("eq", _ref("target", "seat"), _ref("actor", "seat")),
                },
                {
                    "effect_id": "experimental-day-exit",
                    "effect_type": "FLOW",
                    "flow_action": "ADVANCE_TO_NIGHT",
                    "condition": _compare(
                        "gt",
                        {
                            "op": "count",
                            "selector": {
                                "op": "select",
                                "source": "request_targets",
                                "where": _compare(
                                    "eq", _ref("item", "seat"), _ref("actor", "seat")
                                ),
                            },
                        },
                        _literal(0),
                    ),
                },
            ],
            "disclosures": [
                {
                    "disclosure_id": "experimental-public-sunburst-result",
                    "audience": "ALL",
                    "fields": [],
                    "values": {
                        "actor_seat": _ref("actor", "seat"),
                        "taken_seats": {
                            "op": "map",
                            "selector": {
                                "op": "select",
                                "source": "request_targets",
                                "where": _compare(
                                    "ne", _ref("item", "seat"), _ref("actor", "seat")
                                ),
                            },
                            "value": _ref("item", "seat"),
                        },
                    },
                    "hook": "DAY_SPEECH_AFTER",
                    "event_type": "experimental_sunburst_result",
                }
            ],
        },
        {
            "skill_id": "experimental_taken_hunter_shot",
            "mode": "PLAYER",
            "action_code": 105,
            "grants": [
                {
                    "grant_id": "experimental_taken_hunter_grant",
                    "actor_selector": _role_selector("hunter"),
                }
            ],
            "timing": ["TRIGGER_ACTION"],
            "trigger": {
                "fact_types": ["DEATH_CONFIRMED"],
                "mode": "PLAYER_CHOICE",
                "condition": {
                    "op": "and",
                    "values": [
                        _compare("eq", _ref("source_fact", "target_seat"), _ref("actor", "seat")),
                        _compare(
                            "eq", _ref("source_fact", "death_cause"), _literal("self_explosion")
                        ),
                    ],
                },
            },
            "targets": {
                "min_targets": 1,
                "max_targets": 1,
                "selector": _living_other_selector(),
                "allow_self": False,
            },
            "usage": {"max_uses": 1, "scope": "GAME"},
            "effects": [
                {
                    "effect_id": "experimental-hunter-shot",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["taken_hunter_shot"],
                }
            ],
        },
    ]
    if external_night_attack:
        action_rows.append(
            {"action_code": EXTERNAL_DEATH_ACTION, "action_id": "EXPERIMENTAL_NIGHT_STRIKE"}
        )
        skills.append(
            {
                "skill_id": "experimental_unrelated_night_death",
                "action_code": EXTERNAL_DEATH_ACTION,
                "grants": [
                    {"grant_id": "experimental_night_grant", "actor_selector": _seat_selector(1)}
                ],
                "timing": ["NIGHT_ACTION"],
                "window_ids": ["experimental_attack"],
                "targets": {
                    "min_targets": 1,
                    "max_targets": 1,
                    "selector": _living_other_selector(),
                    "allow_self": False,
                },
                "usage": {"max_uses": 1, "scope": "GAME"},
                "effects": [
                    {
                        "effect_id": "experimental-unrelated-death",
                        "effect_type": "DAMAGE",
                        "target": _ref("target", "seat"),
                        "tags": ["external_night_damage"],
                    }
                ],
            }
        )

    windows = execution_windows_from_board(board)
    raw.update(
        {
            "board_id": board.board_id,
            "board_version": board.version,
            "actions": action_rows,
            "skills": skills,
            "resource_declarations": [{"resource_id": "charge", "min_value": 0, "max_value": 1}],
            "window_metadata": [item.model_dump(mode="json") for item in windows],
            "window_settlement_groups": (
                {item.window_id: "experimental_night" for item in windows}
                if external_night_attack
                else {item.window_id: f"experimental-{item.window_id}" for item in windows}
            ),
            "boundary_policy": _boundary_policy(board).model_dump(mode="json"),
            "interactions": [
                {
                    "interaction_id": "experimental-confirm-sunburst-deaths",
                    "rule_type": "CONFIRM_DEATH",
                    "damage_tags": ["sunburst_damage"],
                    "death_cause": "self_explosion",
                },
                {
                    "interaction_id": "experimental-confirm-night-death",
                    "rule_type": "CONFIRM_DEATH",
                    "damage_tags": ["external_night_damage"],
                    "death_cause": "wolf_kill",
                },
                {
                    "interaction_id": "experimental-confirm-taken-hunter-shot",
                    "rule_type": "CONFIRM_DEATH",
                    "damage_tags": ["taken_hunter_shot"],
                    "death_cause": "hunter_shot",
                },
            ],
        }
    )
    execution = ExecutionPackage.model_validate(raw)
    base = load_action_registry()
    registry = ActionRegistry(
        actions=(
            *base.actions,
            ActionDefinition(
                action_code=SUNBURST_ACTION,
                action_name="EXPERIMENTAL_PUBLIC_TAKE",
                target_policy="self_and_other_alive",
                target_count=2,
            ),
            ActionDefinition(
                action_code=EXTERNAL_DEATH_ACTION,
                action_name="EXPERIMENTAL_NIGHT_STRIKE",
                target_policy="other_alive",
                target_count=1,
            ),
        )
    )
    validate_execution_package(
        execution,
        registry,
        available_windows=windows,
        expected_boundary_policy=_boundary_policy(board),
    )
    return execution, registry


def _manager(
    compiled: CompiledKnowledgePackage,
    board: BoardDefinition,
    execution: ExecutionPackage,
    registry: ActionRegistry,
    *,
    phase: GamePhase,
    game_id: str = GAME_ID,
) -> GameManager:
    role_by_seat = {
        1: "wolf",
        2: "wolf",
        3: "villager",
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
    players = {
        seat: PlayerState(
            seat=seat,
            role_id=role_id,
            faction_id="wolf" if role_id == "wolf" else "good",
            session_epoch=7,
            skill_resources={"charge": 1} if seat == 2 else {},
        )
        for seat, role_id in role_by_seat.items()
    }
    state = GameState(
        game_id=game_id,
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        round_no=1,
        day_no=1,
        ruleset=RulesetRef(
            board_id=compiled.board_ref.id,
            version=compiled.board_ref.version,
            snapshot_id=SNAPSHOT_ID,
            manifest_sha256=compiled.manifest_sha256,
        ),
        players=players,
    )
    return GameManager(state, registry=registry, execution_package=execution)


async def _runtime(
    seat: int,
    role_id: str,
    *,
    script: tuple[object, ...] = (),
    game_id: str = GAME_ID,
) -> ScriptedRuntime:
    runtime = ScriptedRuntime(script)
    await runtime.start(
        RuntimeConfig(session_id=f"{game_id}-seat-{seat}"),
        InitialContext(game_id=game_id, seat=seat, session_epoch=7, role_id=role_id),
    )
    return runtime


async def _compiled_classic() -> CompiledKnowledgePackage:
    package = await KnowledgePackageLoader(PUBLISHED_ROOT).load(BOARD_REF)
    return KnowledgePackageCompiler().compile(package)


def _custom_action_response(action_code: int, targets: tuple[int, ...]):
    def response(request: Any) -> ActionResponse:
        return ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": action_code, "targets": list(targets)}],
        )

    return response


@pytest.mark.asyncio
async def test_self_explosion_drains_death_work_before_cancelling_day_and_advancing() -> None:
    """A data-defined explosion settles deaths, Hunter, last words, then exits day."""

    compiled = await _compiled_classic()
    board = _experimental_board(compiled, external_night_attack=False)
    execution, registry = _execution(compiled, board, external_night_attack=False)
    manager = _manager(
        compiled,
        board,
        execution,
        registry,
        phase=GamePhase.DAY_ANNOUNCE,
    )
    speech = await _runtime(
        1,
        "wolf",
        script=(
            lambda request: SpeechResponse(
                request_id=request.request_id, speech={"text": "我先发言。"}
            ),
        ),
    )
    sunburst = await _runtime(
        2,
        "wolf",
        script=(
            _custom_action_response(SUNBURST_ACTION, (3, 11)),
            _custom_action_response(SUNBURST_ACTION, (2, 11)),
        ),
    )
    hunter = await _runtime(11, "hunter", script=(_custom_action_response(105, (3,)),))
    taken = await _runtime(3, "villager")
    runtimes = {1: speech, 2: sunburst, 3: taken, 11: hunter}

    try:
        day = DayCoordinator(manager, board, runtimes)
        await day.announce(now=NOW)
        await day.open_speech(now=NOW)
        spoken = await day.run_next_speech()
        assert spoken.event.payload.speaker_seat == 1
        ordinary_suffix = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)
        assert manager.state.current_queue == ordinary_suffix
        completed_turn = manager.state.last_serial_turn
        assert completed_turn is not None
        assert completed_turn.seat == 1
        assert completed_turn.request_id == spoken.request.request_id

        triggers = ModeratorTriggerFlow(
            manager, board, {2: sunburst, 11: hunter}, clock=lambda: NOW
        )
        hook = await triggers.poll_hook("DAY_SPEECH_AFTER")
        assert hook["status"] == "choice_pending"
        assert manager.state.phase is GamePhase.TRIGGER_ACTION
        cursor = manager.state.rule_workflow_cursor
        assert cursor is not None and cursor.return_point is not None
        return_point = cursor.return_point
        assert return_point.phase is GamePhase.DAY_SPEECH
        assert return_point.hook_id == "DAY_SPEECH_AFTER"
        assert return_point.speaker_seat == 1
        assert return_point.serial_turn_id == completed_turn.request_id
        assert return_point.event_ids == completed_turn.event_ids
        occurrence_id = hook["occurrence_id"]
        assert isinstance(occurrence_id, str)

        with pytest.raises(ModeratorTriggerError, match="ACTOR_TARGET_REQUIRED"):
            await triggers.next(2)
        before_retry = manager.state
        assert before_retry.players[2].alive
        assert before_retry.players[11].alive
        assert before_retry.players[2].skill_resources["charge"] == 1
        sunburst_ability = next(
            item
            for item in manager.state.ability_instances
            if item.actor_seat == 2 and item.skill_id == "experimental_sunburst_skill"
        )
        assert sunburst_ability.uses_consumed == 0

        # The actor's rejected proposal is persisted as a retry boundary. Restore
        # both the manager and trigger adapter before retrying the valid pair.
        manager = GameManager(
            GameState.model_validate_json(before_retry.model_dump_json()),
            registry=registry,
            execution_package=execution,
        )
        day = DayCoordinator(manager, board, runtimes)
        triggers = ModeratorTriggerFlow(
            manager, board, {2: sunburst, 11: hunter}, clock=lambda: NOW
        )
        accepted = await triggers.retry(2)
        assert accepted["status"] == "accepted"
        assert sunburst.requests[-1].attempt_no == 2
        assert manager.state.rule_workflow_cursor is not None
        assert manager.state.rule_workflow_cursor.pending_flow_action == "ADVANCE_TO_NIGHT"
        assert manager.state.current_queue == ordinary_suffix
        assert not manager.state.players[2].alive
        assert manager.state.players[2].death_cause == "self_explosion"
        assert not manager.state.players[11].alive
        assert manager.state.players[11].death_cause == "self_explosion"
        assert manager.state.players[2].skill_resources["charge"] == 0

        ledger_facts = tuple(fact for entry in manager.state.rule_ledger for fact in entry.facts)
        confirmed = [fact for fact in ledger_facts if fact.fact_type == "DEATH_CONFIRMED"]
        assert {(fact.target_seat, fact.death_cause) for fact in confirmed} == {
            (2, "self_explosion"),
            (11, "self_explosion"),
        }
        assert any(
            fact.fact_type == "SELF_EXPLOSION" and fact.target_seat == 2 for fact in ledger_facts
        )

        public_results = [
            event
            for event in manager.state.events
            if getattr(getattr(event, "event_type", None), "value", None)
            == "experimental_sunburst_result"
        ]
        assert len(public_results) == 1
        assert public_results[0].channel is Channel.PUBLIC
        assert '"taken_seats":[11]' in public_results[0].payload.content

        sunburst_receipt = next(
            receipt
            for receipt in manager.state.rule_receipts
            if occurrence_id in receipt.occurrence_ids
        )
        assert (
            len(
                [
                    receipt
                    for receipt in manager.state.rule_receipts
                    if occurrence_id in receipt.occurrence_ids
                ]
            )
            == 1
        )
        ability = next(
            item
            for item in manager.state.ability_instances
            if item.actor_seat == 2 and item.skill_id == "experimental_sunburst_skill"
        )
        assert ability.uses_consumed == 1

        # A restored replay of the same rule group is an idempotent receipt,
        # including its synthetic one-charge accounting probe.
        restored_state = GameState.model_validate_json(manager.state.model_dump_json())
        manager = GameManager(restored_state, registry=registry, execution_package=execution)
        assert manager.state.rule_workflow_cursor is not None
        assert manager.state.rule_workflow_cursor.pending_flow_action == "ADVANCE_TO_NIGHT"
        revision_before_replay = manager.state.state_revision
        replayed = await manager.commit_rule_group(
            sunburst_receipt.group_id,
            request_ids=sunburst_receipt.request_ids,
            occurrence_ids=sunburst_receipt.occurrence_ids,
            expected_revision=revision_before_replay,
            now=NOW,
        )
        assert replayed.state_revision == revision_before_replay
        assert replayed.players[2].skill_resources["charge"] == 0
        assert replayed.rule_workflow_cursor is not None
        assert replayed.rule_workflow_cursor.pending_flow_action == "ADVANCE_TO_NIGHT"
        assert (
            len(
                [
                    receipt
                    for receipt in replayed.rule_receipts
                    if occurrence_id in receipt.occurrence_ids
                ]
            )
            == 1
        )
        assert (
            len(
                [
                    fact
                    for entry in replayed.rule_ledger
                    for fact in entry.facts
                    if fact.fact_type == "DEATH_CONFIRMED"
                ]
            )
            == 2
        )

        # This is the explicit experimental taken-Hunter default: a Hunter
        # killed by self_explosion gets one ordinary choice through generic facts.
        triggers = ModeratorTriggerFlow(
            manager, board, {2: sunburst, 11: hunter}, clock=lambda: NOW
        )
        hunter_progress = await triggers.open(now=NOW)
        assert hunter_progress.action_window is not None
        assert hunter_progress.action_window.allowed_seats == (11,)
        hunter_choice = await triggers.next(11)
        assert hunter_choice["status"] == "accepted"
        assert manager.state.rule_workflow_cursor is not None
        assert manager.state.rule_workflow_cursor.pending_flow_action == "ADVANCE_TO_NIGHT"
        assert manager.state.players[3].alive is False
        assert manager.state.players[3].death_cause == "hunter_shot"
        assert manager.state.current_queue == ordinary_suffix
        hunter_skill = next(
            item
            for item in manager.state.ability_instances
            if item.actor_seat == 11 and item.skill_id == "experimental_taken_hunter_shot"
        )
        assert hunter_skill.uses_consumed == 1

        words = LastWordsFlow(manager, board, runtimes)
        completed_boundary_turns: list[tuple[str, int, int, str]] = []
        for runtime in runtimes.values():
            run_turn = runtime.run_turn

            async def observe_boundary_binding(
                request: TurnRequest,
                *,
                _run_turn: Callable[[TurnRequest], Awaitable[RuntimeTurnResult]] = run_turn,
            ) -> RuntimeTurnResult:
                if "-last-words-" in request.logical_request_id:
                    active = manager.state.serial_turn
                    assert active is not None
                    completed_boundary_turns.append(
                        (
                            active.rule_boundary_id,
                            active.seat,
                            active.session_epoch,
                            active.request_id,
                        )
                    )
                return await _run_turn(request)

            runtime.run_turn = observe_boundary_binding

        while any(boundary.is_pending for boundary in manager.state.rule_boundaries):
            source = words.status()
            pending_seats = source["pending_seats"]
            assert pending_seats
            boundary_id = source["source"]
            assert isinstance(boundary_id, str)
            speaker = pending_seats[0]
            boundary_before = next(
                item for item in manager.state.rule_boundaries if item.boundary_id == boundary_id
            )
            assert speaker in boundary_before.last_words_seats
            expected_epoch = manager.state.players[speaker].session_epoch
            result = await words.next()
            assert result.event.payload.speaker_seat == speaker
            assert result.event.actor_seat == speaker
            assert result.event.channel is Channel.PUBLIC
            assert result.event.event_type.value.lower() == "speech"
            assert result.event.payload.content.strip()
            assert result.event in manager.state.events
            assert result.event.phase is result.request.phase
            assert result.request.game_id == GAME_ID
            assert result.request.session_epoch == expected_epoch
            assert result.request.session_epoch == runtimes[speaker].get_session_ref().session_epoch
            assert result.runtime_result.request_id == result.request.request_id
            assert result.event.correlation_id == result.request.logical_request_id
            assert result.request.logical_request_id == f"{GAME_ID}-r1-last-words-s{speaker}"
            assert completed_boundary_turns[-1] == (
                boundary_id,
                speaker,
                expected_epoch,
                result.request.request_id,
            )
            assert manager.state.serial_turn is None
            assert manager.state.current_queue == ordinary_suffix
            boundary_after = next(
                item for item in manager.state.rule_boundaries if item.boundary_id == boundary_id
            )
            assert speaker in boundary_after.last_words_completed_seats
            boundary_speech_audits = [
                audit
                for audit in manager.state.moderator_audit
                if audit.get("operation") == "RULE_BOUNDARY_LAST_WORDS_SPEECH_COMPLETE"
                and audit.get("boundary_id") == boundary_id
                and audit.get("seat") == speaker
            ]
            assert len(boundary_speech_audits) == 1
            boundary_speech_audit = boundary_speech_audits[0]
            assert boundary_speech_audit["session_epoch"] == expected_epoch
            assert boundary_speech_audit["request_id"] == result.request.request_id
            assert boundary_speech_audit["logical_request_id"] == result.request.logical_request_id
            assert boundary_speech_audit["attempt_no"] == result.request.attempt_no
            assert boundary_speech_audit["speech_event_id"] == result.event.event_id
            assert boundary_speech_audit["speech_event_revision"] == result.event.state_revision
            assert boundary_speech_audit["committed_revision"] == result.event.state_revision
            assert boundary_speech_audit["phase"] == result.event.phase.value
            assert result.event.event_type.value.lower() == "speech"
            assert result.event.channel is Channel.PUBLIC
            completion_audits = [
                audit
                for audit in manager.state.moderator_audit
                if audit.get("operation") == "LAST_WORDS_COMPLETE"
                and audit.get("reason") == f"source={boundary_id};seat={speaker}"
            ]
            assert len(completion_audits) == 1
            completion_audit = completion_audits[0]
            assert completion_audit["base_revision"] == result.event.state_revision
            assert completion_audit["committed_revision"] > result.event.state_revision

        assert len(completed_boundary_turns) == sum(
            len(boundary.last_words_seats)
            for boundary in manager.state.rule_boundaries
            if boundary.last_words_required
        )
        boundary_completion_audits = [
            audit
            for audit in manager.state.moderator_audit
            if audit.get("operation") == "RULE_BOUNDARY_LAST_WORDS_SPEECH_COMPLETE"
        ]
        assert len(boundary_completion_audits) == len(completed_boundary_turns)
        assert all(
            boundary.last_words_completed_seats == boundary.last_words_seats
            for boundary in manager.state.rule_boundaries
            if boundary.last_words_required
        )
        assert manager.state.rule_workflow_cursor is not None
        assert manager.state.rule_workflow_cursor.pending_flow_action == "ADVANCE_TO_NIGHT"
        triggers = ModeratorTriggerFlow(
            manager, board, {2: sunburst, 11: hunter}, clock=lambda: NOW
        )
        finished = await triggers.finish(now=NOW)
        assert finished.phase is GamePhase.VICTORY_CHECK, (
            f"phase={finished.phase.value}; cursor={finished.rule_workflow_cursor!r}; "
            f"boundaries={finished.rule_boundaries!r}"
        )
        assert finished.current_queue == ()
        assert finished.vote_state is None
        assert all(not boundary.is_pending for boundary in finished.rule_boundaries)
        with pytest.raises(DayCoordinatorError, match="PHASE_NOT_ALLOWED"):
            await day.run_next_speech()
        with pytest.raises(DayCoordinatorError, match="PHASE_NOT_ALLOWED"):
            await day.open_vote()

        next_night = await manager.commit_victory_check(
            board,
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
        assert next_night.phase is GamePhase.NIGHT_TEAM_CHAT
        assert next_night.round_no == 2
    finally:
        for runtime in runtimes.values():
            await runtime.close("white-wolf B acceptance complete")


@pytest.mark.asyncio
async def test_unrelated_death_does_not_open_the_sunburst_speech_choice() -> None:
    """Only the live speech hook can explode; an ordinary death cannot replay it."""

    compiled = await _compiled_classic()
    board = _experimental_board(compiled, external_night_attack=True)
    execution, registry = _execution(compiled, board, external_night_attack=True)
    manager = _manager(
        compiled,
        board,
        execution,
        registry,
        phase=GamePhase.NIGHT_ACTION,
        game_id="experimental-white-external-death-game",
    )
    wolf = await _runtime(
        1,
        "wolf",
        script=(_custom_action_response(EXTERNAL_DEATH_ACTION, (2,)),),
        game_id="experimental-white-external-death-game",
    )
    sunburst = await _runtime(
        2,
        "wolf",
        game_id="experimental-white-external-death-game",
    )
    runtimes = {1: wolf, 2: sunburst}
    try:
        day_before_night = manager.state.day_no
        round_before_night = manager.state.round_no
        night = ModeratorNightFlow(manager, board, runtimes, clock=lambda: NOW)
        opened = await night.open()
        assert opened.action_window is not None
        assert opened.action_window.allowed_seats == (1,)
        assert (await night.action_next(1))["status"] == "accepted"
        await night.advance()
        resolve = await night.open()
        assert resolve.action_window is not None and resolve.action_window.collection_only
        settled = await night.resolve()
        assert not settled.players[2].alive
        assert settled.players[2].death_cause == "wolf_kill"
        white_ability = next(
            item
            for item in settled.ability_instances
            if item.actor_seat == 2 and item.skill_id == "experimental_sunburst_skill"
        )
        assert white_ability.uses_consumed == 0
        assert settled.players[2].skill_resources["charge"] == 1
        assert not any(
            item.skill_id == "experimental_sunburst_skill"
            and item.actor_seat == 2
            and item.status in {"QUEUED", "READY", "WAITING_CHOICE"}
            for item in settled.rule_trigger_queue
        )
        triggers = ModeratorTriggerFlow(manager, board, {2: sunburst}, clock=lambda: NOW)
        words = LastWordsFlow(manager, board, runtimes)
        pending_boundary = next(item for item in manager.state.rule_boundaries if item.is_pending)
        assert pending_boundary.last_words_required
        assert pending_boundary.last_words_seats == (2,)

        boundary_progress = await triggers.auto_resolve()
        assert boundary_progress["next_step"] == "RETURN"
        assert boundary_progress["boundary_id"] == pending_boundary.boundary_id
        staged_phase = manager.state.phase
        staged_day_no = manager.state.day_no
        staged_round_no = manager.state.round_no
        staged_cursor = manager.state.rule_workflow_cursor
        assert staged_phase is GamePhase.DAY_ANNOUNCE
        assert staged_cursor is not None
        assert staged_cursor.status == "WAITING_BOUNDARY"
        assert staged_cursor.pending_boundary_id == pending_boundary.boundary_id
        repeated_progress = await triggers.auto_resolve()
        assert repeated_progress["next_step"] == "RETURN"
        assert repeated_progress["boundary_id"] == pending_boundary.boundary_id
        repeated_cursor = manager.state.rule_workflow_cursor
        assert repeated_cursor is not None
        assert repeated_cursor.status == "WAITING_BOUNDARY"
        assert repeated_cursor.pending_boundary_id == staged_cursor.pending_boundary_id
        assert repeated_cursor.return_point == staged_cursor.return_point
        assert repeated_cursor.steps_used == staged_cursor.steps_used
        assert manager.state.phase is staged_phase
        assert manager.state.day_no == staged_day_no
        assert manager.state.round_no == staged_round_no
        pending_words = words.status()
        assert pending_words["pending_seats"] == [2]
        spoken = await words.next(2)
        assert spoken.event.payload.speaker_seat == 2
        assert spoken.event.actor_seat == 2
        assert spoken.event.channel is Channel.PUBLIC
        assert spoken.event in manager.state.events
        assert spoken.request.session_epoch == manager.state.players[2].session_epoch
        completed_boundary = next(
            item
            for item in manager.state.rule_boundaries
            if item.boundary_id == pending_boundary.boundary_id
        )
        assert completed_boundary.last_words_completed_seats == (2,)

        for _ in range(4):
            cursor = manager.state.rule_workflow_cursor
            if cursor is not None and cursor.status == "RETURN_READY":
                assert manager.state.phase is GamePhase.DAY_ANNOUNCE, (
                    f"phase={manager.state.phase.value}; cursor={cursor!r}; "
                    f"boundaries={manager.state.rule_boundaries!r}; "
                    f"ledger={manager.state.rule_ledger!r}"
                )
            if manager.state.phase is GamePhase.DAY_ANNOUNCE and (
                cursor is None or cursor.status == "IDLE"
            ):
                break
            await triggers.auto_resolve()

        assert manager.state.phase is GamePhase.DAY_ANNOUNCE
        assert all(not boundary.is_pending for boundary in manager.state.rule_boundaries)
        assert manager.state.day_no == day_before_night + 1
        assert manager.state.round_no == round_before_night
        dawn_revision = manager.state.state_revision
        idle_step = await manager.advance_rule_workflow(
            expected_revision=dawn_revision,
            now=NOW,
        )
        assert idle_step.kind == "IDLE"
        assert not idle_step.queue_pending
        assert manager.state.state_revision == dawn_revision
        assert manager.state.phase is GamePhase.DAY_ANNOUNCE
        assert manager.state.day_no == day_before_night + 1
        assert manager.state.round_no == round_before_night
        day = DayCoordinator(manager, board, runtimes)
        await day.announce(now=NOW)
        await day.open_speech(now=NOW)
        hook = await triggers.poll_hook("DAY_SPEECH_BEFORE")
        assert hook["status"] == "idle"
        assert manager.state.phase is GamePhase.DAY_SPEECH
        assert manager.state.rule_workflow_cursor is not None
        assert manager.state.rule_workflow_cursor.status == "IDLE"
        assert not any(
            item.skill_id == "experimental_sunburst_skill"
            and item.actor_seat == 2
            and item.status == "WAITING_CHOICE"
            for item in manager.state.rule_trigger_queue
        )
    finally:
        for runtime in runtimes.values():
            await runtime.close("white-wolf unrelated-death acceptance complete")
