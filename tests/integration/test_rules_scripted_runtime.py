"""Data-only unfamiliar skills traverse the player gateway and game manager."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from aiohttp.test_utils import TestServer

from werewolf.cli_support.play_setup import build_play_setup
from werewolf.domain.enums import Channel, GamePhase
from werewolf.game import (
    ActionTurnScheduler,
    GameManager,
    GameState,
    GrantedAbility,
    PlayerState,
    RulesetRef,
    load_action_registry,
)
from werewolf.game.actions import (
    ActionDefinition,
    ActionRegistry,
    ActionValidationContext,
    ActionWindow,
)
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiler import CompiledKnowledgePackage, KnowledgePackageCompiler
from werewolf.knowledge.gateway import KnowledgeGateway
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.role import TargetKind, TargetRule, UsageLimit
from werewolf.knowledge.service import KnowledgeService
from werewolf.moderator.night_flow import ModeratorNightFlow
from werewolf.moderator.shell import ModeratorShell
from werewolf.rules.compiler import validate_execution_package
from werewolf.rules.models import ExecutionPackage
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.player_runtime import (
    ActionResponse,
    Deadline,
    InitialContext,
    Observation,
    ReadyResponse,
    ResponseKind,
    RuntimeConfig,
    TurnRequest,
)

PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
BOARD_ID = "classic_12_seer_witch_hunter_idiot"
GAME_NOW = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
NOVEL_ACTION_CODE = 987
NOVEL_SKILL_ID = "quasar_echo_unfamiliar"
NOVEL_GRANT_ID = "quasar_echo_grant"
SOURCE_BOARD_ID = "quasar_echo_lab"
SOURCE_ORACLE_ROLE_ID = "quasar_oracle"
SOURCE_CITIZEN_ROLE_ID = "quasar_citizen"


def _ref(source: str, name: str) -> dict[str, object]:
    return {"op": "ref", "source": source, "name": name}


def _literal(value: object) -> dict[str, object]:
    return {"op": "literal", "value": value}


def _compare(op: str, left: object, right: object) -> dict[str, object]:
    return {"op": op, "left": left, "right": right}


def _selector_map(source: str, field: str) -> dict[str, object]:
    return {
        "op": "map",
        "selector": {"op": "select", "source": source},
        "value": _ref("item", field),
    }


def _novel_execution(
    *,
    target_count: int,
    modes: list[str],
    board_id: str = BOARD_ID,
    board_version: str = "1.0.0",
    actor_role_id: str = "seer",
) -> ExecutionPackage:
    alive_players = {
        "op": "count",
        "selector": {
            "op": "select",
            "source": "players",
            "where": _compare("eq", _ref("item", "alive"), _literal(True)),
        },
    }
    actor_selector = {
        "op": "select",
        "source": "players",
        "where": _compare("eq", _ref("item", "role_id"), _literal(actor_role_id)),
        "map": _ref("item", "seat"),
    }
    target_selector = {
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
    parameter_is_strike = _compare(
        "eq",
        _ref("request", "parameter:mode"),
        _literal("strike"),
    )
    parameter_is_scan = _compare(
        "eq",
        _ref("request", "parameter:mode"),
        _literal("scan"),
    )
    return ExecutionPackage.model_validate(
        {
            "board_id": board_id,
            "board_version": board_version,
            "actions": [
                {"action_code": 299, "action_id": "PASS", "allow_pass": True},
                {
                    "action_code": NOVEL_ACTION_CODE,
                    "action_id": "QUASAR_ECHO",
                    "allow_pass": True,
                },
            ],
            "skills": [
                {
                    "skill_id": NOVEL_SKILL_ID,
                    "action_code": NOVEL_ACTION_CODE,
                    "grants": [{"grant_id": NOVEL_GRANT_ID, "actor_selector": actor_selector}],
                    "timing": ["NIGHT_ACTION"],
                    "condition": _compare("gt", alive_players, _literal(2)),
                    "targets": {
                        "min_targets": target_count,
                        "max_targets": target_count,
                        "selector": target_selector,
                        "allow_self": False,
                    },
                    "usage": {
                        "max_uses": 1,
                        "scope": "ROUND",
                        "pass_records": True,
                        "pass_updates_history": False,
                    },
                    "parameters": [
                        {"name": "mode", "value_type": "str", "required": True, "choices": modes}
                    ],
                    "effects": [
                        {
                            "effect_id": "quasar-strike-damage",
                            "effect_type": "DAMAGE",
                            "target": _ref("target", "seat"),
                            "condition": parameter_is_strike,
                        },
                        {
                            "effect_id": "quasar-scan-fact",
                            "effect_type": "FACT",
                            "target": _ref("target", "seat"),
                            "fact_type": "quasar_scan_mark",
                            "condition": parameter_is_scan,
                        },
                    ],
                    "disclosures": [
                        {
                            "disclosure_id": "quasar-self-report",
                            "audience": "SELF",
                            "fields": [],
                            "values": {
                                "mode": _ref("request", "parameter:mode"),
                                "selected_targets": _selector_map("request_targets", "seat"),
                                "alive_count": alive_players,
                            },
                            "event_type": "quasar_skill_report",
                        }
                    ],
                }
            ],
            "interactions": [
                {
                    "interaction_id": "quasar-confirm-damage",
                    "rule_type": "CONFIRM_DEATH",
                }
            ],
        }
    )


async def _compiled_classic() -> CompiledKnowledgePackage:
    package = await KnowledgePackageLoader(PUBLISHED_ROOT).load(BOARD_REF)
    return KnowledgePackageCompiler().compile(package)


def _registry(target_count: int) -> ActionRegistry:
    base = load_action_registry()
    actions = [item for item in base.actions if item.action_code != NOVEL_ACTION_CODE]
    actions.append(
        ActionDefinition(
            action_code=NOVEL_ACTION_CODE,
            action_name="QUASAR_ECHO",
            target_policy="other_alive",
            target_count=target_count,
        )
    )
    return ActionRegistry(actions=tuple(actions))


def _state(
    package: CompiledKnowledgePackage,
    execution: ExecutionPackage,
    registry: ActionRegistry,
    *,
    game_id: str,
    snapshot_id: str,
    target_count: int,
) -> GameManager:
    board = cast(Mapping[str, Any], package.package_payload["board_definition"])
    action_board_window = next(
        item for item in board["night_windows"] if item["phase"] == GamePhase.NIGHT_ACTION.value
    )
    resolve_board_window = next(
        item for item in board["night_windows"] if item["phase"] == GamePhase.NIGHT_RESOLVE.value
    )
    action_id = cast(str, action_board_window["window_id"])
    resolve_id = cast(str, resolve_board_window["window_id"])
    candidate_seats = [2, 3, 4]
    action_window = ActionWindow(
        window_id=action_id,
        game_id=game_id,
        session_epoch=0,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_role_ids=("seer",),
        allowed_action_codes=(NOVEL_ACTION_CODE, 299),
        allow_pass=True,
        opened_at=GAME_NOW,
        visible_context={"candidate_seats": candidate_seats},
    )
    resolve_window = ActionWindow(
        window_id=resolve_id,
        game_id=game_id,
        session_epoch=0,
        phase=GamePhase.NIGHT_RESOLVE,
        allowed_seats=(1,),
        allowed_action_codes=(299,),
        allow_pass=True,
        opened_at=GAME_NOW,
    )
    state = GameState(
        game_id=game_id,
        created_at=GAME_NOW,
        updated_at=GAME_NOW,
        phase=GamePhase.NIGHT_ACTION,
        ruleset=RulesetRef(
            board_id=package.board_ref.id,
            version=package.board_ref.version,
            snapshot_id=snapshot_id,
            manifest_sha256=package.manifest_sha256,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id="seer",
                faction_id="village",
                session_epoch=0,
                granted_abilities=(
                    GrantedAbility(
                        ability_id=NOVEL_GRANT_ID,
                        action_code=NOVEL_ACTION_CODE,
                        timing=GamePhase.NIGHT_ACTION,
                        allowed_phases=(GamePhase.NIGHT_ACTION,),
                        target_rule=TargetRule(
                            kind=TargetKind.PLAYER,
                            min_targets=target_count,
                            max_targets=target_count,
                            allow_self=False,
                        ),
                        usage_limit=UsageLimit(max_uses=1, uses_per_round=1),
                    ),
                ),
            ),
            **{
                seat: PlayerState(
                    seat=seat,
                    role_id="villager",
                    faction_id="village",
                    session_epoch=0,
                )
                for seat in (2, 3, 4)
            },
        },
        action_windows={
            action_id: action_window.model_dump(mode="json"),
            resolve_id: resolve_window.model_dump(mode="json"),
        },
    )
    return GameManager(state, registry=registry, execution_package=execution)


def _ready_request(game_id: str) -> TurnRequest:
    now = datetime.now(UTC)
    return TurnRequest(
        request_id=f"ready-{game_id}",
        logical_request_id=f"ready-{game_id}",
        attempt_no=1,
        game_id=game_id,
        session_epoch=0,
        phase=GamePhase.PLAYER_PREPARE,
        expected_kind=ResponseKind.READY,
        observation=Observation(payload={"seat": 1}),
        output_schema={"type": "object"},
        deadline=Deadline(soft_deadline=now, hard_deadline=now + timedelta(seconds=10)),
    )


async def _start_runtime(
    compiled: CompiledKnowledgePackage,
    manager: GameManager,
    execution: ExecutionPackage,
    registry: ActionRegistry,
    *,
    game_id: str,
    snapshot_id: str,
) -> tuple[KnowledgeGateway, TestServer, DemoRuntime]:
    async def state_provider() -> object:
        return await manager.snapshot()

    gateway = KnowledgeGateway(
        KnowledgeService(compiled, snapshot_id=snapshot_id),
        state_provider=state_provider,
        execution_package=execution,
        action_registry=registry,
    )
    server = TestServer(gateway.app)
    await server.start_server()
    token = gateway.issue_token(
        game_id=game_id,
        snapshot_id=snapshot_id,
        seat=1,
        session_epoch=0,
    )
    runtime = DemoRuntime(str(server.make_url("")), token)
    await runtime.start(
        RuntimeConfig(session_id=f"session-{game_id}"),
        InitialContext(
            game_id=game_id,
            seat=1,
            session_epoch=0,
            role_id="seer",
        ),
    )
    ready = await runtime.run_turn(_ready_request(game_id))
    assert isinstance(ready.response, ReadyResponse)
    assert len(ready.response.ready.knowledge_receipts) == 2
    assert len(runtime.receipts) == 2
    return gateway, server, runtime


def _validation_context(game_id: str) -> ActionValidationContext:
    return ActionValidationContext(
        game_id=game_id,
        session_epoch=0,
        active_request_id=f"coordinator-{game_id}",
        role_id="seer",
        authorized_action_codes=(NOVEL_ACTION_CODE,),
        alive_seats=(1, 2, 3, 4),
        eligible_targets_by_action={NOVEL_ACTION_CODE: (2, 3, 4)},
    )


@pytest.mark.asyncio
async def test_unfamiliar_data_skill_uses_frozen_versions_through_gateway_and_manager() -> None:
    compiled = await _compiled_classic()
    execution_v1 = _novel_execution(target_count=2, modes=["strike", "scan"])
    execution_v2 = _novel_execution(target_count=1, modes=["scan"])
    registry_v1 = _registry(target_count=2)
    registry_v2 = _registry(target_count=1)
    validate_execution_package(execution_v1, registry_v1)
    validate_execution_package(execution_v2, registry_v2)
    game_v1, game_v2 = "quasar-v1-game", "quasar-v2-game"
    snapshot_v1, snapshot_v2 = "quasar-v1-snapshot", "quasar-v2-snapshot"
    manager_v1 = _state(
        compiled,
        execution_v1,
        registry_v1,
        game_id=game_v1,
        snapshot_id=snapshot_v1,
        target_count=2,
    )
    gateway_v1, server_v1, runtime_v1 = await _start_runtime(
        compiled,
        manager_v1,
        execution_v1,
        registry_v1,
        game_id=game_v1,
        snapshot_id=snapshot_v1,
    )
    manager_v2 = _state(
        compiled,
        execution_v2,
        registry_v2,
        game_id=game_v2,
        snapshot_id=snapshot_v2,
        target_count=1,
    )
    gateway_v2, server_v2, runtime_v2 = await _start_runtime(
        compiled,
        manager_v2,
        execution_v2,
        registry_v2,
        game_id=game_v2,
        snapshot_id=snapshot_v2,
    )
    board_definition = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    flow_v1 = ModeratorNightFlow(
        manager_v1,
        board_definition,
        {1: runtime_v1},
        snapshot_id=snapshot_v1,
        clock=lambda: GAME_NOW,
    )
    flow_v2 = ModeratorNightFlow(
        manager_v2,
        board_definition,
        {1: runtime_v2},
        snapshot_id=snapshot_v2,
        clock=lambda: GAME_NOW,
    )
    try:
        status_v1 = await runtime_v1._read_skill_status()
        status_v2 = await runtime_v2._read_skill_status()
        skill_v1 = status_v1["abilities"][0]
        skill_v2 = status_v2["abilities"][0]
        assert skill_v1["target_rule"]["max_targets"] == 2
        assert skill_v1["parameters"][0]["choices"] == ["strike", "scan"]
        assert skill_v2["target_rule"]["max_targets"] == 1
        assert skill_v2["parameters"][0]["choices"] == ["scan"]
        assert manager_v1.state.execution_identity.package_id != (
            manager_v2.state.execution_identity.package_id
        )

        result_v1 = await ActionTurnScheduler(manager_v1, {1: runtime_v1}).run_turn(
            _action_window(manager_v1),
            1,
            _validation_context(game_v1),
        )
        result_v2 = await ActionTurnScheduler(manager_v2, {1: runtime_v2}).run_turn(
            _action_window(manager_v2),
            1,
            _validation_context(game_v2),
        )
        assert isinstance(result_v1.runtime_result.response, ActionResponse)
        assert isinstance(result_v2.runtime_result.response, ActionResponse)
        action_v1 = result_v1.runtime_result.response.actions[0]
        action_v2 = result_v2.runtime_result.response.actions[0]
        assert action_v1.action_code == NOVEL_ACTION_CODE
        assert len(action_v1.targets) == 2
        assert action_v1.parameters == {"mode": "strike"}
        assert action_v2.action_code == NOVEL_ACTION_CODE
        assert len(action_v2.targets) == 1
        assert action_v2.parameters == {"mode": "scan"}

        await flow_v1.advance()
        resolved_v1 = await flow_v1.auto_resolve()
        await flow_v2.advance()
        resolved_v2 = await flow_v2.auto_resolve()
        assert resolved_v1.phase is GamePhase.DAY_ANNOUNCE
        assert resolved_v2.phase is GamePhase.DAY_ANNOUNCE
        assert sum(not player.alive for player in resolved_v1.players.values()) == 2
        assert all(player.alive for player in resolved_v2.players.values())
        assert resolved_v1.ability_instances[0].uses_consumed == 1
        assert resolved_v2.ability_instances[0].uses_consumed == 1
        assert resolved_v1.rule_ledger[0].history_updates[0].targets == tuple(
            sorted(action_v1.targets)
        )
        assert resolved_v2.rule_ledger[0].facts[0].fact_type == "quasar_scan_mark"
        private_report = next(
            event
            for event in resolved_v1.events
            if getattr(event, "event_type", None) == "quasar_skill_report"
        )
        assert private_report.actor_seat == 1
        assert private_report.channel is Channel.PRIVATE
        assert private_report.audience == (1,)
        assert json.loads(private_report.payload.content) == {
            "alive_count": 4,
            "mode": "strike",
            "selected_targets": list(action_v1.targets),
        }
    finally:
        await runtime_v1.close("integration complete")
        await runtime_v2.close("integration complete")
        await gateway_v1.close()
        await gateway_v2.close()
        await server_v1.close()
        await server_v2.close()


def _action_window(manager: GameManager) -> ActionWindow:
    state = manager.state
    matching = [
        ActionWindow.model_validate_json(json.dumps(raw))
        for raw in state.action_windows.values()
        if isinstance(raw, dict) and raw.get("phase") == GamePhase.NIGHT_ACTION.value
    ]
    assert len(matching) == 1
    return matching[0]


def _write_markdown(path: Path, frontmatter: Mapping[str, object], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = yaml.safe_dump(dict(frontmatter), allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{encoded}---\n{body}\n", encoding="utf-8")


def _source_board(version: str) -> dict[str, object]:
    board_ref = f"{SOURCE_BOARD_ID}@{version}"
    return {
        "schema_version": 1,
        "kind": "board",
        "id": SOURCE_BOARD_ID,
        "version": version,
        "name": "Quasar Echo Integration Board",
        "aliases": [],
        "locale": "en-US",
        "status": "published",
        "reviewed_by": "stage-a-integration-fixture",
        "reviewed_at": "2026-10-03",
        "summary": "A three-seat fixture for exercising a data-defined two-mode ability.",
        "seat_count": 3,
        "factions": {"wolf": 1, "good": 2},
        "roles": [
            {
                "role_ref": f"{SOURCE_ORACLE_ROLE_ID}@1.0.0",
                "count": 1,
                "effective_rules": {},
                "override_claim_refs": [],
            },
            {
                "role_ref": f"{SOURCE_CITIZEN_ROLE_ID}@1.0.0",
                "count": 2,
                "effective_rules": {},
                "override_claim_refs": [],
            },
        ],
        "victory": {
            "mode": "eliminate_side",
            "winning_sides": ["good", "wolf"],
            "check_phases": ["NIGHT_RESOLVE", "DAY_RESOLVE", "VICTORY_CHECK"],
            "draw_policy": "no_winner",
            "role_groups": {
                SOURCE_ORACLE_ROLE_ID: "wolf",
                SOURCE_CITIZEN_ROLE_ID: "villager",
            },
        },
        "wolf_team_visibility": {
            "members_know_each_other": True,
            "discussion_enabled": True,
            "identity_visibility": "members",
        },
        "knife_rule": {
            "selection_mode": "consensus",
            "target_visibility": "wolf_team",
            "final_target_required": False,
            "plan_confirmation_required": False,
            "available_after_window": "wolf_team_chat",
        },
        "identity_reveal": {"reveal_on_death": False, "reveal_on_exile": False},
        "night_windows": [
            {
                "window_id": "wolf_team_chat",
                "order": 1,
                "phase": "NIGHT_TEAM_CHAT",
                "visible_to": [SOURCE_ORACLE_ROLE_ID],
            },
            {
                "window_id": "night_actions",
                "order": 2,
                "phase": "NIGHT_ACTION",
                "depends_on": ["wolf_team_chat"],
            },
            {
                "window_id": "night_resolve",
                "order": 3,
                "phase": "NIGHT_RESOLVE",
                "depends_on": ["night_actions"],
            },
        ],
        "day_flow": {
            "vote": {
                "visibility_during_collection": "secret",
                "reveal_after_close": "ballots_and_totals",
                "tie_policy": "no_exile_on_tie",
                "eligible_voters": "alive_with_vote",
                "allow_abstain": True,
            },
            "pk": {"enabled": False},
            "last_words": {"enabled": False},
            "sheriff": {"enabled": False},
        },
        "mechanics": [],
        "interactions": [],
        "reading_plan": {
            "board_ref": board_ref,
            "bootstrap_topics": ["board:overview"],
            "role_required_topics": {
                SOURCE_ORACLE_ROLE_ID: [f"role:{SOURCE_ORACLE_ROLE_ID}"],
                SOURCE_CITIZEN_ROLE_ID: [f"role:{SOURCE_CITIZEN_ROLE_ID}"],
            },
            "phase_topics": {
                "NIGHT_ACTION": ["board:overview"],
                "NIGHT_RESOLVE": ["board:overview"],
            },
            "high_risk_topics": [f"role:{SOURCE_ORACLE_ROLE_ID}"],
            "suggested_queries": ["What does Quasar Echo do?"],
        },
        "claim_refs": ["claim-quasar-echo-fixture"],
        "source_refs": ["source-quasar-echo-fixture"],
    }


def _source_role(
    role_id: str,
    name: str,
    description: str,
    *,
    faction: str,
    team: str,
    team_channel: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "role",
        "id": role_id,
        "name": name,
        "aliases": [],
        "version": "1.0.0",
        "status": "published",
        "reviewed_by": "stage-a-integration-fixture",
        "reviewed_at": "2026-10-03",
        "faction": faction,
        "team": team,
        "victory_goal": "support_good",
        "public_summary": description,
        "private_identity_card": f"You are {name}. {description}",
        "abilities": [],
        "knowledge_at_start": [],
        "team_visibility": {
            "channel": team_channel,
            "share_identity": team_channel == "TEAM",
            "shared_knowledge": [],
        },
        "death_behavior": {
            "active_abilities_allowed": False,
            "passive_abilities_continue": False,
            "death_trigger_fires": False,
            "description": "No abilities continue after death.",
        },
        "board_compatibility": [],
        "common_mistakes": [],
        "claim_refs": [],
        "source_refs": [],
    }


def _write_source_package(
    published_root: Path,
    *,
    version: str,
    target_count: int,
    modes: list[str],
) -> None:
    board = _source_board(version)
    _write_markdown(
        published_root / "boards" / SOURCE_BOARD_ID / version / "board.md",
        board,
        "# Quasar Echo Integration Board\n\n"
        "## Overview {#overview}\n\n"
        "Three players test a new ability contract.",
    )
    for role_id, name, summary, faction, team, team_channel in (
        (
            SOURCE_ORACLE_ROLE_ID,
            "Quasar Oracle",
            "Receives the Quasar Echo ability.",
            "WEREWOLF",
            "wolf",
            "TEAM",
        ),
        (
            SOURCE_CITIZEN_ROLE_ID,
            "Quasar Citizen",
            "Has no active abilities.",
            "GOOD",
            "good",
            "PRIVATE",
        ),
    ):
        role = _source_role(
            role_id,
            name,
            summary,
            faction=faction,
            team=team,
            team_channel=team_channel,
        )
        _write_markdown(
            published_root / "roles" / role_id / "1.0.0" / "role.md",
            role,
            f"# {name}\n\n## Overview {{#overview}}\n\n{summary}",
        )

    execution = _novel_execution(
        target_count=target_count,
        modes=modes,
        board_id=SOURCE_BOARD_ID,
        board_version=version,
        actor_role_id=SOURCE_ORACLE_ROLE_ID,
    )
    envelope = {
        "schema_version": 1,
        "execution": execution.model_dump(mode="json"),
        "action_definitions": [
            {
                "action_code": NOVEL_ACTION_CODE,
                "action_name": "QUASAR_ECHO",
                "target_policy": "other_alive",
                "target_count": target_count,
            }
        ],
    }
    execution_path = published_root / "boards" / SOURCE_BOARD_ID / version / "execution.yaml"
    execution_path.write_text(yaml.safe_dump(envelope, sort_keys=False), encoding="utf-8")


@pytest.mark.asyncio
async def test_source_package_play_init_start_gateway_script_and_auto_resolve_are_versioned(
    tmp_path: Path,
) -> None:
    cases = (
        ("1.0.0", 2, ["strike", "scan"], "strike", 2),
        ("2.0.0", 1, ["scan"], "scan", 0),
    )
    package_ids: list[str] = []
    for version, target_count, modes, expected_mode, expected_deaths in cases:
        published_root = tmp_path / f"published-{version}"
        _write_source_package(
            published_root,
            version=version,
            target_count=target_count,
            modes=modes,
        )
        setup_root = tmp_path / f"play-{version}"
        report = await asyncio.to_thread(
            build_play_setup,
            setup_root,
            all_scripted=True,
            board_ref=f"{SOURCE_BOARD_ID}@{version}",
            published_root=published_root,
            project_root=PROJECT_ROOT,
        )
        assert report["source_kind"] == "published"
        assert report["board_ref"] == f"{SOURCE_BOARD_ID}@{version}"

        shell = ModeratorShell(
            setup_root / "game.yaml",
            clock=lambda: GAME_NOW,
        )
        try:
            await shell.new()
            assert shell.runtime_bundle is not None
            package = shell.runtime_bundle.package
            assert package.board_ref.format() == f"{SOURCE_BOARD_ID}@{version}"
            assert package.execution_source == "declared"
            assert package.execution is not None
            assert package.action_registry is not None
            package_ids.append(package.execution.package_id)

            await shell.next()
            await shell.next()
            await shell.execute("start")
            assert shell.session_service is not None
            oracle_seat = next(
                seat
                for seat, player in shell.state.players.items()
                if player.role_id == SOURCE_ORACLE_ROLE_ID
            )
            assert all(not player.granted_abilities for player in shell.state.players.values())
            assert len(shell.state.ability_instances) == 1
            assert shell.state.ability_instances[0].actor_seat == oracle_seat
            assert shell.state.ability_instances[0].skill_id == NOVEL_SKILL_ID

            for _ in shell.state.players:
                await shell.execute("prepare next")
            assert all(player.knowledge_receipt_ids for player in shell.state.players.values())
            assert len(shell.session_service.receipts_for_seat(oracle_seat)) == 2
            await shell.next()
            assert shell.state.phase is GamePhase.NIGHT_TEAM_CHAT
            await shell.execute("night open")
            await shell.execute("night team next")
            await shell.execute("night advance")
            assert shell.state.phase is GamePhase.NIGHT_ACTION

            oracle_runtime = cast(DemoRuntime, shell.session_service.runtimes[oracle_seat])
            skill_status = await oracle_runtime._read_skill_status()
            novel_ability = next(
                item for item in skill_status["abilities"] if item["skill_id"] == NOVEL_SKILL_ID
            )
            assert novel_ability["target_rule"]["max_targets"] == target_count
            assert novel_ability["parameters"][0]["choices"] == modes

            await shell.execute("night open")
            action_window = next(
                ActionWindow.model_validate_json(json.dumps(raw))
                for raw in shell.state.action_windows.values()
                if isinstance(raw, dict) and raw.get("phase") == GamePhase.NIGHT_ACTION.value
            )
            assert action_window.allowed_seats == (oracle_seat,)
            assert NOVEL_ACTION_CODE in action_window.allowed_action_codes
            action_result = await shell.execute(f"night action next {oracle_seat}")
            assert action_result is not None
            stored_request = next(
                payload
                for payload in shell.state.action_requests.values()
                if isinstance(payload, dict) and payload.get("seat") == oracle_seat
            )
            action = stored_request["actions"][0]
            assert action["action_code"] == NOVEL_ACTION_CODE, (
                f"selected action={action!r}; status={novel_ability!r}; "
                f"visible_context={action_window.visible_context!r}"
            )
            assert len(action["targets"]) == target_count
            assert action["parameters"] == {"mode": expected_mode}
            assert oracle_runtime.requests

            await shell.execute("night advance")
            await shell.execute("night open")
            await shell.execute("night auto-resolve")
            assert shell.state.phase is GamePhase.DAY_ANNOUNCE
            assert sum(not player.alive for player in shell.state.players.values()) == (
                expected_deaths
            )
            assert shell.state.ability_instances[0].uses_consumed == 1
            assert shell.state.rule_ledger[0].history_updates[0].targets == tuple(
                sorted(action["targets"])
            )
            if expected_mode == "scan":
                assert shell.state.rule_ledger[0].facts[0].fact_type == "quasar_scan_mark"
            private_report = next(
                event
                for event in shell.state.events
                if getattr(event, "event_type", None) == "quasar_skill_report"
            )
            assert private_report.actor_seat == oracle_seat
            assert private_report.channel is Channel.PRIVATE
            assert private_report.audience == (oracle_seat,)
            private_content = json.loads(private_report.payload.content)
            assert private_content["alive_count"] == 3
            assert private_content["mode"] == expected_mode
            assert private_content["selected_targets"] == sorted(action["targets"])
        finally:
            if shell.session_service is not None:
                await shell.session_service.close()
    assert len(set(package_ids)) == 2
