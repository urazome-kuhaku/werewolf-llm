"""Classic schema-1 Hunter compatibility through the executable night flow."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_rules_scripted_runtime import _compiled_classic

from werewolf.domain.enums import GamePhase
from werewolf.game.manager import GameManager
from werewolf.game.setup import build_role_assignment_plan
from werewolf.game.state import GameState, RulesetRef
from werewolf.knowledge.board import BoardDefinition
from werewolf.moderator.night_flow import ModeratorNightFlow
from werewolf.moderator.trigger_flow import ModeratorTriggerFlow
from werewolf.runtime.player_runtime import ActionResponse, InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

GAME_NOW = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("actor_role", "expected_cause", "shot_opens"),
    [
        ("wolf", "wolf_kill", True),
        ("witch", "witch_poison", False),
    ],
)
@pytest.mark.asyncio
async def test_classic_night_flow_preserves_hunter_trigger_causes(
    actor_role: str,
    expected_cause: str,
    shot_opens: bool,
) -> None:
    """Only a wolf kill opens the classic Hunter shot after B night settlement."""

    compiled = await _compiled_classic()
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    board_payload = json.loads(json.dumps(compiled.package_payload["board_definition"]))
    board_payload["night_windows"] = [
        {"window_id": "night_actions", "order": 1, "phase": "NIGHT_ACTION"},
        {
            "window_id": "night_resolve",
            "order": 2,
            "phase": "NIGHT_RESOLVE",
            "depends_on": ["night_actions"],
        },
    ]
    board_payload["knife_rule"]["available_after_window"] = "night_actions"
    board_payload["wolf_team_visibility"]["discussion_enabled"] = False
    board_payload["day_flow"]["last_words"]["enabled"] = False
    board = BoardDefinition.model_validate(board_payload)
    assignment = build_role_assignment_plan(
        board,
        compiled,
        seed=20261004,
        seats=tuple(range(1, board.seat_count + 1)),
    )
    by_role = {
        role_id: next(player for player in assignment.players.values() if player.role_id == role_id)
        for role_id in ("wolf", "witch", "hunter", "villager")
    }
    if actor_role == "wolf":
        role_seats = {1: "wolf", 2: "hunter"}
    else:
        role_seats = {1: "wolf", 2: "witch", 3: "hunter", 4: "villager"}
    players = {
        seat: by_role[role_id].model_copy(update={"seat": seat, "current_request_id": None})
        for seat, role_id in role_seats.items()
    }
    game_id = f"classic-hunter-{actor_role}"
    snapshot_id = f"classic-hunter-snapshot-{actor_role}"
    state = GameState(
        game_id=game_id,
        created_at=GAME_NOW,
        updated_at=GAME_NOW,
        phase=GamePhase.NIGHT_ACTION,
        round_no=1,
        day_no=1,
        ruleset=RulesetRef(
            board_id=board.board_id,
            version=board.version,
            snapshot_id=snapshot_id,
            manifest_sha256=compiled.manifest_sha256,
        ),
        players=players,
    )
    manager = GameManager(
        state,
        registry=compiled.action_registry,
        execution_package=compiled.execution,
        legacy_compatibility=True,
    )
    runtimes: dict[int, ScriptedRuntime] = {}
    action_by_seat = {1: (101, 2 if actor_role == "wolf" else 4)}
    if actor_role == "witch":
        action_by_seat[2] = (103, 3)
    for seat, (action_code, target_seat) in action_by_seat.items():
        runtime = ScriptedRuntime(
            [
                lambda request, action_code=action_code, target_seat=target_seat: ActionResponse(
                    request_id=request.request_id,
                    actions=[{"action_code": action_code, "targets": [target_seat]}],
                )
            ]
        )
        await runtime.start(
            RuntimeConfig(session_id=f"{game_id}-seat-{seat}"),
            InitialContext(
                game_id=game_id,
                seat=seat,
                session_epoch=players[seat].session_epoch,
                role_id=players[seat].role_id,
            ),
        )
        runtimes[seat] = runtime
    try:
        flow = ModeratorNightFlow(manager, board, runtimes, clock=lambda: GAME_NOW)
        opened = await flow.open()
        assert opened.action_window.allowed_seats == tuple(action_by_seat)
        assert all(
            action_code in opened.action_window.allowed_action_codes
            for action_code, _target in action_by_seat.values()
        )
        assert (await flow.action_next(1))["status"] == "accepted"
        if actor_role == "witch":
            assert (await flow.action_next(2))["status"] == "accepted"
        await flow.advance()
        assert manager.state.phase is GamePhase.NIGHT_RESOLVE
        resolve_window = await flow.open()
        assert resolve_window.action_window.collection_only is True
        settled = await flow.resolve()

        hunter_seat = 2 if actor_role == "wolf" else 3
        assert settled.players[hunter_seat].alive is False
        assert settled.players[hunter_seat].death_cause == expected_cause
        assert (settled.phase is GamePhase.TRIGGER_ACTION) is shot_opens
        if shot_opens:
            trigger = ModeratorTriggerFlow(manager, board, {})
            pending = await trigger.open(now=GAME_NOW)
            assert pending.action_window is not None
            assert pending.action_window.allowed_seats == (hunter_seat,)
            assert 105 in pending.action_window.allowed_action_codes
        else:
            assert settled.pending_resolution is None
            hunter_trigger = settled.players[hunter_seat].granted_trigger_abilities[0]
            assert hunter_trigger.consumed is False
    finally:
        for runtime in runtimes.values():
            await runtime.close("classic Hunter night-flow regression complete")
