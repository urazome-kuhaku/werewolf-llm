from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from werewolf.domain.enums import GamePhase
from werewolf.game.state import GameState
from werewolf.knowledge.gateway import KnowledgeGateway, KnowledgeReceipt
from werewolf.knowledge.service import QueryContext
from werewolf.runtime.player_runtime import Action, ActionResponse
from werewolf.runtime.prompt_composer import compose_system_prompt

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "tools" / "pi_role_smoke.py"
_SCRIPT_SPEC = importlib.util.spec_from_file_location("pi_role_smoke", _SCRIPT_PATH)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"cannot load smoke script: {_SCRIPT_PATH}")
_SMOKE = importlib.util.module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SMOKE
_SCRIPT_SPEC.loader.exec_module(_SMOKE)


async def test_pi_role_smoke_startup_card_and_prompt_are_pinned_without_pi(
    tmp_path: Path,
) -> None:
    """The live probe's setup contract is checked before any Pi process starts."""

    reading_skill = _SMOKE.READING_SKILL_PATH.read_bytes()
    assert hashlib.sha256(reading_skill).hexdigest() == _SMOKE.READING_SKILL_SHA256

    game_id = "pi-role-smoke-prompt-offline"
    snapshot, service = await _SMOKE._build_snapshot(
        workbench=_SMOKE.DEFAULT_WORKBENCH,
        board_ref=_SMOKE.DEFAULT_BOARD_REF,
        game_id=game_id,
        temporary_root=tmp_path,
    )
    context = QueryContext(
        game_id=game_id,
        snapshot_id=snapshot.snapshot_id,
        seat=_SMOKE.SEAT,
        session_epoch=0,
    )
    card = _SMOKE.build_knowledge_bootstrap_card(service, context, _SMOKE.ROLE_ID)
    prompt = compose_system_prompt(
        card,
        _SMOKE.READING_SKILL_SHA256,
        reading_skill_path=_SMOKE.READING_SKILL_PATH,
    )

    assert card.board.id == _SMOKE.DEFAULT_BOARD_REF.split("@", 1)[0]
    assert card.your_role.id == _SMOKE.ROLE_ID
    assert "# Werewolf Arena 玩家系统提示" in prompt
    assert "## 冻结的规则阅读协议" in prompt
    assert "## 当前局知识导航（只读数据）" in prompt
    assert _SMOKE.READING_SKILL_PATH.read_text(encoding="utf-8").rstrip() in prompt


async def test_pi_role_smoke_gateway_projects_private_skill_state(tmp_path: Path) -> None:
    """Exercise the real temporary snapshot and gateway without starting Pi."""

    game_id = "pi-role-smoke-offline"
    snapshot, service = await _SMOKE._build_snapshot(
        workbench=_SMOKE.DEFAULT_WORKBENCH,
        board_ref=_SMOKE.DEFAULT_BOARD_REF,
        game_id=game_id,
        temporary_root=tmp_path,
    )
    state = _SMOKE._build_skill_state(
        game_id=game_id,
        board_ref=_SMOKE.DEFAULT_BOARD_REF,
        snapshot=snapshot,
        seat=7,
        session_epoch=0,
    )

    state_provider_calls = 0

    async def state_provider() -> GameState:
        nonlocal state_provider_calls
        state_provider_calls += 1
        return state

    receipts: list[KnowledgeReceipt] = []

    async def receipt_sink(receipt: KnowledgeReceipt) -> None:
        receipts.append(receipt)

    gateway = KnowledgeGateway(
        service,
        receipt_sink=receipt_sink,
        state_provider=state_provider,
    )
    witch_token = gateway.issue_token(
        game_id=game_id,
        snapshot_id=snapshot.snapshot_id,
        seat=7,
        session_epoch=0,
    )
    villager_token = gateway.issue_token(
        game_id=game_id,
        snapshot_id=snapshot.snapshot_id,
        seat=8,
        session_epoch=0,
    )

    async with TestClient(TestServer(gateway.app)) as client:
        headers = {"Authorization": f"Bearer {witch_token}"}
        skill_status_calls_before = state_provider_calls
        board = await client.get(
            f"/v1/board/{_SMOKE.DEFAULT_BOARD_REF.split('@', 1)[0]}", headers=headers
        )
        role = await client.get("/v1/role/witch", headers=headers)
        skill = await client.get("/v1/game/skills/me", headers=headers)

        assert board.status == 200
        assert role.status == 200
        assert skill.status == 200
        _SMOKE._require_skill_status_call(
            baseline=skill_status_calls_before,
            current=state_provider_calls,
        )
        skill_status = (await skill.json())["skill_status"]
        assert skill_status["seat"] == 7
        assert skill_status["role_id"] == "witch"
        assert skill_status["resources"] == {"witch_heal": 1, "witch_poison": 1}
        assert {item["action_code"] for item in skill_status["abilities"]} == {103, 104}
        assert skill_status["windows"] == [
            {
                "window_id": "pi-role-smoke-witch-window",
                "phase": "NIGHT_ACTION",
                "allowed_action_codes": [103, 299],
                "allow_pass": True,
                "dependencies_satisfied": True,
            }
        ]
        assert "private_marker" not in skill_status["resources"]
        assert "players" not in skill_status
        assert "faction_id" not in skill_status

        other = await client.get(
            "/v1/game/skills/me",
            headers={"Authorization": f"Bearer {villager_token}"},
        )
        assert other.status == 200
        other_status = (await other.json())["skill_status"]
        assert other_status["seat"] == 8
        assert other_status["role_id"] == "villager"
        assert other_status["resources"] == {"private_marker": 1}
        assert other_status["abilities"] == []
        assert other_status["windows"] == []
        assert "witch_poison" not in other_status["resources"]

    # Skill status is deliberately read-only; only the board and role reads
    # produce audit receipts.
    assert {receipt.tool for receipt in receipts} == {"get_board", "get_role"}


def test_pi_role_smoke_rejects_skill_action_without_status_read() -> None:
    """An action-shaped result cannot pass the smoke gate by itself."""

    with pytest.raises(_SMOKE.SmokeTestError, match="get_skill_status"):
        _SMOKE._require_skill_status_call(baseline=3, current=3)

    assert _SMOKE._require_skill_status_call(baseline=3, current=4) == 1


def test_pi_role_smoke_defaults_to_witch_and_preserves_original_window() -> None:
    args = _SMOKE._parse_args([])

    assert args.role == "witch"
    assert args.board_ref == _SMOKE.DEFAULT_BOARD_REF
    request = _SMOKE._skill_request("pi-role-smoke-default", 0, 5)
    assert request.phase is GamePhase.NIGHT_ACTION
    assert request.action_window is not None
    assert request.action_window.window_id == "pi-role-smoke-witch-window"
    assert request.action_window.allowed_action_codes == [103, 299]
    assert request.action_window.candidate_seats == [2, 3]


def test_pi_role_smoke_guard_rejects_self_and_accepts_other_live_target() -> None:
    request = _SMOKE._skill_request("pi-role-smoke-guard", 0, 5, "guard", 7)

    assert request.phase is GamePhase.NIGHT_ACTION
    assert request.action_window is not None
    assert request.action_window.allowed_action_codes == [106, 299]
    assert request.action_window.candidate_seats == [8]

    valid = ActionResponse(
        request_id="pi-role-smoke-skill",
        actions=[Action(action_code=106, targets=[8])],
    )
    assert _SMOKE._assert_skill_response(valid, "guard", 7) == (106, 1, False)

    invalid = ActionResponse(
        request_id="pi-role-smoke-skill",
        actions=[Action(action_code=106, targets=[7])],
    )
    with pytest.raises(_SMOKE.SmokeTestError, match="guard"):
        _SMOKE._assert_skill_response(invalid, "guard", 7)


def test_pi_role_smoke_white_wolf_king_requires_ordered_actor_pair() -> None:
    request = _SMOKE._skill_request("pi-role-smoke-wwk", 0, 5, "white_wolf_king", 7)

    assert request.phase is GamePhase.DAY_SPEECH
    assert request.action_window is not None
    assert request.action_window.allowed_action_codes == [107, 299]
    assert request.action_window.candidate_seats == [7, 8]

    valid = ActionResponse(
        request_id="pi-role-smoke-skill",
        actions=[Action(action_code=107, targets=[7, 8])],
    )
    assert _SMOKE._assert_skill_response(valid, "white_wolf_king", 7) == (107, 2, False)

    reversed_targets = ActionResponse(
        request_id="pi-role-smoke-skill",
        actions=[Action(action_code=107, targets=[8, 7])],
    )
    with pytest.raises(_SMOKE.SmokeTestError, match="actor"):
        _SMOKE._assert_skill_response(reversed_targets, "white_wolf_king", 7)
