"""Action requests disclose only roster seats delivered to the actor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionTurnScheduler,
    GameManager,
    GameState,
    GrantedAbility,
    PlayerState,
    RulesetRef,
)
from werewolf.game.actions import ActionValidationContext, ActionWindow
from werewolf.game.events import EventType, GameEvent, TeamNoticePayload
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.role import TargetKind, TargetRule
from werewolf.rules.compat import CLASSIC_BOARD_ID, compile_legacy_execution
from werewolf.runtime.player_runtime import ActionResponse, InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

PROJECT_ROOT = Path(__file__).parents[2]
GAME_ID = "action-privacy"
WINDOW_ID = "night-action"
NOW = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)
SESSION_EPOCH = 7


async def _classic_execution():
    package = await KnowledgePackageLoader(PROJECT_ROOT / "vault" / "published").load(
        f"{CLASSIC_BOARD_ID}@1.0.0"
    )
    return compile_legacy_execution(package)


def _state(
    *,
    hidden_seats: tuple[tuple[str, str, tuple[str, ...]], tuple[str, str, tuple[str, ...]]],
    roster: tuple[int, ...] | None,
) -> tuple[GameState, ActionWindow]:
    window = ActionWindow(
        window_id=WINDOW_ID,
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_action_codes=(101, 299),
        allow_pass=True,
        opened_at=NOW,
        visible_context={"candidate_seats": [1, 2, 3, 4]},
    )
    players = {
        1: PlayerState(
            seat=1,
            role_id="wolf",
            faction_id="wolf",
            chat_group_ids=("wolf-chat",),
            session_epoch=SESSION_EPOCH,
            granted_abilities=(
                GrantedAbility(
                    ability_id="wolf_team_shared",
                    action_code=101,
                    timing=GamePhase.NIGHT_ACTION,
                    allowed_phases=(GamePhase.NIGHT_ACTION,),
                    target_rule=TargetRule(
                        kind=TargetKind.PLAYER,
                        min_targets=1,
                        max_targets=1,
                        allow_self=False,
                    ),
                ),
            ),
        )
    }
    for seat, (role_id, faction_id, group_ids) in zip((2, 3), hidden_seats, strict=True):
        players[seat] = PlayerState(
            seat=seat,
            role_id=role_id,
            faction_id=faction_id,
            chat_group_ids=group_ids,
            session_epoch=SESSION_EPOCH,
        )
    players[4] = PlayerState(
        seat=4,
        role_id="villager",
        faction_id="good",
        session_epoch=SESSION_EPOCH,
    )
    events: tuple[GameEvent, ...] = ()
    if roster is not None:
        notice = GameEvent.team(
            event_id=1,
            game_id=GAME_ID,
            state_revision=0,
            round_no=2,
            phase=GamePhase.NIGHT_TEAM_CHAT,
            created_at=NOW,
            event_type=EventType.TEAM_NOTICE,
            authorized_seats=(1, 2),
            correlation_id="night-chat-roster-r2",
            payload=TeamNoticePayload(
                content=json.dumps(
                    {"kind": "chat_group_roster", "member_seats": list(roster)},
                    separators=(",", ":"),
                )
            ),
        )
        events = (notice,)
    state = GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_ACTION,
        round_no=2,
        ruleset=RulesetRef(
            board_id=CLASSIC_BOARD_ID,
            version="1.0.0",
            snapshot_id="action-privacy-snapshot",
            manifest_sha256="a" * 64,
        ),
        players=players,
        events=events,
        action_windows={WINDOW_ID: window.model_dump(mode="json")},
    )
    return state, window


async def _capture_request(
    *,
    hidden_seats: tuple[tuple[str, str, tuple[str, ...]], tuple[str, str, tuple[str, ...]]],
    roster: tuple[int, ...] | None = None,
):
    compiled = await _classic_execution()
    state, window = _state(hidden_seats=hidden_seats, roster=roster)
    manager = GameManager(
        state,
        registry=compiled.action_registry,
        execution_package=compiled.execution,
    )
    runtime = ScriptedRuntime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [4]}],
            )
        ]
    )
    await runtime.start(
        RuntimeConfig(session_id=f"session-{GAME_ID}"),
        InitialContext(
            game_id=GAME_ID,
            seat=1,
            session_epoch=SESSION_EPOCH,
            role_id="wolf",
        ),
    )
    context = ActionValidationContext(
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        active_request_id="coordinator-placeholder",
        role_id="wolf",
        authorized_action_codes=(101,),
        alive_seats=(1, 2, 3, 4),
        eligible_targets_by_action={101: (4,)},
    )

    await ActionTurnScheduler(manager, {1: runtime}).run_turn(window, 1, context)

    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.action_window is not None
    return request


@pytest.mark.asyncio
async def test_hidden_role_and_group_changes_do_not_change_candidates_without_roster() -> None:
    first = await _capture_request(
        hidden_seats=(
            ("wolf", "wolf", ("wolf-chat",)),
            ("villager", "good", ()),
        )
    )
    second = await _capture_request(
        hidden_seats=(
            ("villager", "good", ()),
            ("wolf", "wolf", ("wolf-chat",)),
        )
    )

    assert first.action_window is not None
    assert second.action_window is not None
    assert first.action_window.candidate_seats == [1, 2, 3, 4]
    assert second.action_window.candidate_seats == first.action_window.candidate_seats
    assert first.action_window.visible_context["targets_by_action"] == {"101": [2, 3, 4]}
    assert second.action_window.visible_context["targets_by_action"] == {"101": [2, 3, 4]}


@pytest.mark.asyncio
async def test_delivered_roster_notice_excludes_disclosed_teammates_from_wolf_targets() -> None:
    request = await _capture_request(
        hidden_seats=(
            ("wolf", "wolf", ("wolf-chat",)),
            ("villager", "good", ()),
        ),
        roster=(1, 2),
    )

    assert request.action_window is not None
    assert request.action_window.candidate_seats == [1, 2, 3, 4]
    assert request.action_window.visible_context["targets_by_action"] == {"101": [3, 4]}
