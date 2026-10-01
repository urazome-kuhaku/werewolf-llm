"""End-to-end coverage for one real moderator controlled game cycle.

The fixture uses the same loopback gateway and ``PlayerRuntime`` boundary as
the moderator's production path.  The players are deterministic, but the
host still has to open every board window, run every runtime turn, and submit
the explicit night ruling before the day can begin.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import yaml

from werewolf.domain.enums import Channel, GamePhase
from werewolf.moderator import ModeratorError, ModeratorShell
from werewolf.runtime.player_runtime import (
    Action,
    ActionResponse,
    Ready,
    ReadyResponse,
    ResponseKind,
    Speech,
    SpeechResponse,
    TurnRequest,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
BOARD = "test-board@1.0.0"
_FRONTMATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)


def _read_frontmatter(path: Path) -> dict[str, Any]:
    match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    assert match is not None
    values = yaml.safe_load(match.group("body"))
    assert isinstance(values, dict)
    return values


def _write_document(path: Path, values: dict[str, Any], body: str) -> None:
    encoded = yaml.safe_dump(values, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{encoded}---\n{body}", encoding="utf-8")


def _wolf_ability() -> dict[str, Any]:
    return {
        "ability_id": "wolf-kill",
        "name": "夜间袭击",
        "action_code": 101,
        "timing": "NIGHT_ACTION",
        "allowed_phases": ["NIGHT_ACTION"],
        "trigger_type": "ACTIVE",
        "target_rule": {
            "kind": "PLAYER",
            "min_targets": 1,
            "max_targets": 1,
            "allow_self": False,
            "allow_dead": False,
        },
        "usage_limit": {"max_uses": 1, "uses_per_round": 1},
        "input_information": [],
        "request_effect": {
            "effect_code": "wolf_kill_requested",
            "description": "提交一名合法的夜间袭击目标。",
            "visibility": "PRIVATE",
        },
        "resolution_effect": {
            "effect_code": "wolf_kill_resolved",
            "description": "由主持人裁定夜间袭击结果。",
            "visibility": "PUBLIC",
        },
        "result_visibility": ["PRIVATE"],
        "failure_rules": [],
    }


def _write_four_seat_package(root: Path) -> None:
    """Turn the small loader fixture into a reviewed four-seat game board."""

    # Pytest's importlib mode does not add sibling test directories to
    # ``sys.path``.  Reuse the established package fixture without making the
    # scenario depend on collection order.
    integration_tests = Path(__file__).parents[1] / "integration"
    if str(integration_tests) not in sys.path:
        sys.path.insert(0, str(integration_tests))
    from test_knowledge_package_loader import _write_package

    _write_package(root)
    # The runtime loader persists a section table for every published
    # document, so give the inherited mechanic/interaction fixture the same
    # anchored Markdown sections used by the compiler integration tests.
    for relative, title, anchor, body in (
        (
            "mechanics/voting/1.0.0/mechanic.md",
            "投票",
            "voting_rules",
            "秘密投票并由主持人确认结算。",
        ),
        (
            "interactions/wolf-voting/1.0.0/interaction.md",
            "狼人投票交互",
            "interaction_rules",
            "狼人夜间提交行动，白天所有存活玩家投票。",
        ),
    ):
        path = root / relative
        _write_document(
            path,
            _read_frontmatter(path),
            f"# {title}\n\n## 规则 {{#{anchor}}}\n\n{body}\n",
        )
    board_path = root / "boards" / "test-board" / "1.0.0" / "board.md"
    board = _read_frontmatter(board_path)
    board.update(
        {
            "seat_count": 4,
            "factions": {"wolf": 2, "town": 2},
            "victory": {
                **dict(board["victory"]),
                "special_conditions": ["good_wins_when_all_wolves_are_dead"],
            },
            "roles": [
                {
                    "role_ref": "wolf@1.0.0",
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
                {
                    "role_ref": "villager@1.0.0",
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
            ],
            "night_windows": [
                {
                    "window_id": "wolf_team_chat",
                    "order": 1,
                    "phase": "NIGHT_TEAM_CHAT",
                    "visible_to": ["wolf"],
                },
                {
                    "window_id": "wolf_kill",
                    "order": 2,
                    "phase": "NIGHT_ACTION",
                    "depends_on": ["wolf_team_chat"],
                },
                {
                    "window_id": "night_resolve",
                    "order": 3,
                    "phase": "NIGHT_RESOLVE",
                    "depends_on": ["wolf_kill"],
                },
            ],
        }
    )
    reading_plan = dict(board["reading_plan"])
    reading_plan["role_required_topics"] = {
        "wolf": ["role:wolf"],
        "villager": ["role:villager"],
    }
    reading_plan["phase_topics"] = {
        **dict(reading_plan["phase_topics"]),
        "NIGHT_ACTION": ["mechanic:voting"],
    }
    board["reading_plan"] = reading_plan
    _write_document(board_path, board, "# 测试板\n\n## 概览 {#overview}\n\n四座位昼夜流程板。\n")

    wolf_path = root / "roles" / "wolf" / "1.0.0" / "role.md"
    wolf = _read_frontmatter(wolf_path)
    wolf["abilities"] = [_wolf_ability()]
    _write_document(
        wolf_path, wolf, "# 狼人\n\n## 角色规则 {#role_rules}\n\n狼人夜间可提交一次袭击。\n"
    )

    villager = {
        "schema_version": 1,
        "kind": "role",
        "id": "villager",
        "name": "村民",
        "aliases": [],
        "version": "1.0.0",
        "status": "published",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-09-27",
        "faction": "GOOD",
        "team": "town",
        "victory_goal": "eliminate_wolf",
        "public_summary": "没有夜间主动技能的好人角色。",
        "private_identity_card": "你是村民，白天通过发言和投票找出狼人。",
        "abilities": [],
        "knowledge_at_start": [],
        "team_visibility": {"channel": "TEAM", "share_identity": False},
        "death_behavior": {
            "active_abilities_allowed": False,
            "passive_abilities_continue": False,
            "death_trigger_fires": False,
            "description": "死亡后不再行动。",
        },
        "board_compatibility": [BOARD],
        "common_mistakes": [],
        "claim_refs": ["claim-villager"],
        "source_refs": ["source-villager"],
    }
    villager_path = root / "roles" / "villager" / "1.0.0" / "role.md"
    villager_path.parent.mkdir(parents=True, exist_ok=True)
    _write_document(
        villager_path,
        villager,
        "# 村民\n\n## 角色规则 {#role_rules}\n\n白天发言并投票。\n",
    )


def _write_config(path: Path, compiled: Path, games: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "game": {
                    "game_id": "scenario-game",
                    "board": {"id": "test-board", "version": "1.0.0"},
                    "seed": 7,
                },
                "paths": {"compiled_root": str(compiled), "games_root": str(games)},
                "players": [{"seat": seat, "runtime": "scripted"} for seat in range(1, 5)],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class _GatewayReadingRuntime(ScriptedRuntime):
    """A deterministic runtime that performs real gateway reads before READY."""

    def __init__(self, gateway_url: str, token: str) -> None:
        super().__init__()
        self.gateway_url = gateway_url
        self.token = token

    async def run_turn(self, request: TurnRequest):
        context = self._context
        assert context is not None
        if request.expected_kind is ResponseKind.READY:
            headers = {"Authorization": f"Bearer {self.token}"}
            async with aiohttp.ClientSession(headers=headers) as client:
                board_response = await client.get(f"{self.gateway_url}/v1/board/current")
                role_response = await client.get(f"{self.gateway_url}/v1/role/{context.role_id}")
                assert board_response.status == 200
                assert role_response.status == 200
                board = await board_response.json()
                role = await role_response.json()
            self._script.appendleft(
                ReadyResponse(
                    request_id=request.request_id,
                    ready=Ready(knowledge_receipts=[board["receipt_id"], role["receipt_id"]]),
                )
            )
        elif request.expected_kind is ResponseKind.SPEECH:
            self._script.appendleft(
                SpeechResponse(
                    request_id=request.request_id,
                    speech=Speech(text=f"{context.seat}号玩家发言：我会结合公屏信息判断阵营。"),
                )
            )
        elif request.expected_kind is ResponseKind.ACTION:
            window = request.action_window
            assert window is not None
            if 101 in window.allowed_action_codes:
                # The coordinator exposes only legal non-wolf candidates here.
                assert window.candidate_seats
                action = Action(action_code=101, targets=[window.candidate_seats[0]])
            else:
                candidates = [seat for seat in window.candidate_seats if seat != context.seat]
                target = candidates[0] if candidates else window.candidate_seats[0]
                action = Action(action_code=201, targets=[target])
            self._script.appendleft(ActionResponse(request_id=request.request_id, actions=[action]))
        return await super().run_turn(request)


async def _compile_package(tmp_path: Path) -> Path:
    from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
    from werewolf.knowledge.compiler import KnowledgePackageCompiler
    from werewolf.knowledge.package_loader import KnowledgePackageLoader

    source = tmp_path / "source"
    _write_four_seat_package(source)
    package = await KnowledgePackageLoader(source).load(BOARD)
    compiled = KnowledgePackageCompiler().compile(package)
    root = tmp_path / "compiled"
    await CompiledKnowledgeStore(root).publish(compiled)
    return root


def _resolution_for_pending(
    pending: dict[str, Any],
    *,
    game_id: str,
    base_revision: int,
) -> tuple[dict[str, Any], int]:
    record = pending["pending_requests"][0]
    requested_action = record["actions"][0]
    target = requested_action["targets"][0]
    return (
        {
            "schema_version": 1,
            "resolution_id": "scenario-resolution-1",
            "bundle_id": record["bundle_id"],
            "game_id": game_id,
            "window_id": record["window_id"],
            "request_id": record["request_id"],
            "session_epoch": record["session_epoch"],
            "base_revision": base_revision,
            "status": "CONFIRMED",
            "actions": [
                {
                    "action_index": 0,
                    "requested_action": requested_action,
                    "resource_cost": 0,
                    "effects": [
                        {
                            "effect_id": "scenario-kill",
                            "action_index": 0,
                            "effect_type": "SET_ALIVE",
                            "target_seat": target,
                            "value": False,
                        },
                        {
                            "effect_id": "scenario-death-cause",
                            "action_index": 0,
                            "effect_type": "SET_DEATH_CAUSE",
                            "target_seat": target,
                            "value": "wolf_kill",
                        },
                    ],
                }
            ],
            "moderator_id": "scenario-moderator",
            "created_at": NOW.isoformat(),
        },
        target,
    )


@pytest.mark.asyncio
async def test_four_seat_moderator_cycle_uses_real_reads_actions_votes_and_snapshot(
    tmp_path: Path,
) -> None:
    compiled = await _compile_package(tmp_path)
    config = tmp_path / "game.yaml"
    games = tmp_path / "games"
    _write_config(config, compiled, games)
    runtimes: dict[int, _GatewayReadingRuntime] = {}

    def factory(player, gateway_url: str, token: str) -> _GatewayReadingRuntime:
        runtime = _GatewayReadingRuntime(gateway_url, token)
        runtimes[player.seat] = runtime
        return runtime

    shell = ModeratorShell(config, runtime_factory=factory, clock=lambda: NOW)
    await shell.new()
    await shell.next()
    await shell.next()
    started = await shell.execute("start")
    assert started is not None
    assert started["phase"] == GamePhase.PLAYER_PREPARE.value

    for _ in range(4):
        prepared = await shell.execute("prepare next")
        assert prepared is not None
    assert all(len(runtime.requests) == 1 for runtime in runtimes.values())
    assert all(shell.state.players[seat].knowledge_receipt_ids for seat in range(1, 5))
    assert (await shell.next())["phase"] == GamePhase.NIGHT_TEAM_CHAT.value

    wolves = tuple(
        seat for seat, player in shell.state.players.items() if player.faction_id == "wolf"
    )
    assert len(wolves) == 2
    assert (await shell.execute("night open"))["phase"] == GamePhase.NIGHT_TEAM_CHAT.value
    for _ in wolves:
        await shell.execute("night team next")
    team_events = [event for event in shell.state.events if event.event_type.value == "team_speech"]
    assert len(team_events) == 2
    assert all(event.channel is Channel.TEAM for event in team_events)
    assert all(event.audience == wolves for event in team_events)
    assert (await shell.execute("night advance"))["phase"] == GamePhase.NIGHT_ACTION.value

    opened_action = await shell.execute("night open")
    assert opened_action is not None
    action_result = await shell.execute("night action next")
    assert action_result is not None
    assert action_result["action"]["status"] == "accepted"  # type: ignore[index]
    assert (await shell.execute("night advance"))["phase"] == GamePhase.NIGHT_RESOLVE.value
    assert (await shell.execute("night open"))["phase"] == GamePhase.NIGHT_RESOLVE.value

    pending_response = await shell.execute("night pending")
    assert pending_response is not None
    assert pending_response["private"] is True
    assert pending_response["sensitive"] is True
    pending = pending_response["night"]  # type: ignore[index]
    assert pending["pending_requests"]  # type: ignore[index]
    resolution, night_target = _resolution_for_pending(
        pending, game_id=shell.state.game_id, base_revision=shell.state.state_revision
    )
    resolution_path = tmp_path / "night-resolution.json"
    resolution_path.write_text(json.dumps([resolution]), encoding="utf-8")
    resolved = await shell.execute(f"night resolve {resolution_path}")
    assert resolved is not None
    assert resolved["phase"] == GamePhase.DAY_ANNOUNCE.value
    assert shell.state.players[night_target].alive is False

    assert (await shell.execute("day announce 昨夜的结果已经公布"))["phase"] == (
        GamePhase.DAY_SPEECH.value
    )
    await shell.execute("day speech open")
    alive = tuple(seat for seat, player in shell.state.players.items() if player.alive)
    for _ in alive:
        await shell.execute("day speech next")
    assert (await shell.execute("day speech close"))["phase"] == GamePhase.VOTE.value

    await shell.execute("day vote open")
    eligible_voters = tuple(
        seat for seat, player in shell.state.players.items() if player.alive and player.can_vote
    )
    for seat in eligible_voters:
        await shell.execute(f"day vote next {seat}")
    assert (await shell.execute("day vote collect"))["phase"] == GamePhase.VOTE.value
    assert (await shell.execute("day vote confirm"))["phase"] == GamePhase.DAY_RESOLVE.value
    vote_state = shell.state.vote_state
    assert isinstance(vote_state, dict)
    result = vote_state["public_result"]
    assert isinstance(result, dict)
    exile_target = result["eliminated_seat"]
    assert isinstance(exile_target, int)
    await shell.execute(f"day confirm-exile {exile_target}")
    finished_day = await shell.execute("day finish")
    assert finished_day is not None
    assert finished_day["phase"] == GamePhase.VICTORY_CHECK.value

    public_status = await shell.status()
    private_status = await shell.status(private=True)
    assert "players" not in public_status
    assert private_status["sensitive"] is True
    assert {item["role_id"] for item in private_status["players"]} <= {"wolf", "villager"}  # type: ignore[index]

    saved = await shell.save()
    snapshot_path = Path(str(saved["path"]))
    assert (snapshot_path / "snapshot_manifest.json").is_file()
    snapshot_state = json.loads((snapshot_path / "state.json").read_text(encoding="utf-8"))
    assert snapshot_state["phase"] == GamePhase.VICTORY_CHECK.value
    assert snapshot_state["round_no"] == shell.state.round_no
    public_projection = (snapshot_path / "public.md").read_text(encoding="utf-8")
    gm_projection = (snapshot_path / "private" / "gm.md").read_text(encoding="utf-8")
    wolf_projection = (snapshot_path / "private" / "channels" / "wolves.md").read_text(
        encoding="utf-8"
    )
    assert "昨夜的结果已经公布" in public_projection
    assert "玩家发言" in public_projection
    assert "夜间行动" not in public_projection
    assert "role_assignment" not in public_projection
    assert "role_assignment" in gm_projection
    assert '"event_type":"team_speech"' in wolf_projection

    # Victory is a board-driven transition.  The generic ``next`` command is
    # intentionally blocked at this boundary, so only the explicit check can
    # select the next-night edge (or FINISHED when the frozen board has a
    # winning candidate).
    assert shell.state.phase is GamePhase.VICTORY_CHECK
    with pytest.raises(ModeratorError, match="VICTORY_CHECK"):
        await shell.next()

    victory_status = await shell.execute("victory status")
    assert victory_status is not None
    assert victory_status["phase"] == GamePhase.VICTORY_CHECK.value
    victory = victory_status["victory"]
    assert isinstance(victory, dict)
    assert victory["status"] == "ONGOING"
    assert victory["winner"] is None

    checked = await shell.execute("victory check")
    assert checked is not None
    assert checked["phase"] == GamePhase.NIGHT_TEAM_CHAT.value
    checked_victory = checked["victory"]
    assert isinstance(checked_victory, dict)
    assert checked_victory["status"] == "ONGOING"
    assert checked_victory["winner"] is None
    assert shell.state.phase is GamePhase.NIGHT_TEAM_CHAT
    assert shell.state.winner is None
    assert shell.state.moderator_audit[-1]["operation"] == "VICTORY_CHECK"
    assert shell.state.moderator_audit[-1]["status"] == "ONGOING"

    # Opening the next board window proves that victory checking really
    # advanced the authoritative state; no generic phase skip is involved.
    next_night = await shell.execute("night open")
    assert next_night is not None
    assert next_night["phase"] == GamePhase.NIGHT_TEAM_CHAT.value
    next_window = next_night["night"]["window"]
    assert isinstance(next_window, dict)
    assert next_window["window_id"] == "wolf_team_chat-r1"
    assert next_window["phase"] == GamePhase.NIGHT_TEAM_CHAT.value
    assert next_window["closed_at"] is None
    await shell.session_service.close()  # type: ignore[union-attr]
