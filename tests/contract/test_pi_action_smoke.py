"""Contract proof for PiRuntime -> ActionTurnScheduler -> GameManager."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionTurnScheduler,
    ActionValidationContext,
    ActionWindow,
    GameManager,
    GameState,
    GrantedAbility,
    PlayerState,
    RulesetRef,
    load_action_registry,
)
from werewolf.knowledge.role import TargetKind, TargetRule
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import InitialContext, RuntimeConfig


class _FakePiActionProcess:
    """Minimal Pi RPC process that emits one deterministic action proposal."""

    started = False
    closed = False

    def __init__(self) -> None:
        self.records: asyncio.Queue[object | None] = asyncio.Queue()
        self.writes: list[dict[str, Any]] = []
        self.pid = 9876
        self.returncode: int | None = None

    async def start(self) -> _FakePiActionProcess:
        self.started = True
        return self

    async def send_record(self, record: dict[str, Any]) -> None:
        self.writes.append(record)
        command = record["type"]
        if command in {
            "get_state",
            "set_steering_mode",
            "set_follow_up_mode",
            "set_auto_compaction",
            "set_auto_retry",
        }:
            await self.records.put(
                {"type": "response", "id": record["id"], "command": command, "success": True}
            )
        elif command == "prompt":
            payload = json.loads(record["message"])
            request_id = payload["request_id"]
            response = json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "kind": "action",
                    "actions": [{"action_code": 102, "targets": [2]}],
                },
                separators=(",", ":"),
            )
            await self.records.put(
                {"type": "response", "id": record["id"], "command": "prompt", "success": True}
            )
            await self.records.put({"type": "agent_start"})
            await self.records.put(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": response}],
                    },
                }
            )
            await self.records.put({"type": "agent_settled"})
        elif command == "get_last_assistant_text":
            prompt = next(record for record in reversed(self.writes) if record["type"] == "prompt")
            payload = json.loads(prompt["message"])
            response = json.dumps(
                {
                    "schema_version": 1,
                    "request_id": payload["request_id"],
                    "kind": "action",
                    "actions": [{"action_code": 102, "targets": [2]}],
                },
                separators=(",", ":"),
            )
            await self.records.put(
                {
                    "type": "response",
                    "id": record["id"],
                    "command": command,
                    "success": True,
                    "data": {"text": response},
                }
            )

    async def read_record(self) -> object | None:
        return await self.records.get()

    async def close(self, reason: str = "normal shutdown") -> None:
        del reason
        self.closed = True
        self.returncode = 0


def _setup() -> tuple[GameManager, ActionWindow, ActionValidationContext]:
    now = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
    window = ActionWindow(
        window_id="pi-contract-window",
        game_id="pi-contract-game",
        session_epoch=2,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_role_ids=("seer",),
        allowed_action_codes=(102,),
        opened_at=now,
        visible_context={"candidate_seats": [2]},
    )
    state = GameState(
        game_id="pi-contract-game",
        created_at=now,
        updated_at=now,
        phase=GamePhase.NIGHT_ACTION,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="pi-contract",
            manifest_sha256="a" * 64,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id="seer",
                faction_id="town",
                session_epoch=2,
                granted_abilities=(
                    GrantedAbility(
                        ability_id="inspect",
                        action_code=102,
                        timing=GamePhase.NIGHT_ACTION,
                        allowed_phases=(GamePhase.NIGHT_ACTION,),
                        target_rule=TargetRule(
                            kind=TargetKind.PLAYER,
                            min_targets=1,
                            max_targets=1,
                        ),
                    ),
                ),
            ),
            2: PlayerState(seat=2, role_id="villager", faction_id="town", session_epoch=2),
        },
        action_windows={window.window_id: window.model_dump(mode="json")},
    )
    context = ActionValidationContext(
        game_id="pi-contract-game",
        session_epoch=2,
        active_request_id="placeholder",
        authorized_action_codes=(102,),
        alive_seats=(1, 2),
        eligible_targets_by_action={102: (2,)},
    )
    return GameManager(state, registry=load_action_registry()), window, context


@pytest.mark.asyncio
async def test_real_runtime_boundary_submits_action_without_resolving_state() -> None:
    manager, window, context = _setup()
    process = _FakePiActionProcess()
    runtime = PiRuntime(process=process, command_timeout_seconds=1)
    await runtime.start(
        RuntimeConfig(session_id="pi-contract-session"),
        InitialContext(game_id="pi-contract-game", seat=1, session_epoch=2, role_id="seer"),
    )
    try:
        before = await manager.snapshot()
        result = await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)
        after = result.state
        assert len(after.action_requests) == 1
        assert after.players[1].alive == before.players[1].alive
        assert after.players[2].alive == before.players[2].alive
        assert after.phase is before.phase
        assert after.delivery_cursors[1].in_flight_request_id is None
        assert result.runtime_result.response.kind == "action"
        assert process.writes[0]["type"] == "get_state"
        assert process.writes[-1]["type"] == "get_last_assistant_text"
    finally:
        await runtime.close("contract complete")
    assert process.closed
    assert process.returncode == 0
