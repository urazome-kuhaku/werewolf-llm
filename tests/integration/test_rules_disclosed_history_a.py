"""Only roster notices delivered in this runtime session narrow wolf targets."""

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
    PlayerState,
    RulesetRef,
    SerialTurnScheduler,
)
from werewolf.game.action_turn import _disclosed_team_seats
from werewolf.game.actions import ActionValidationContext, ActionWindow
from werewolf.game.events import (
    DeliveryCursor,
    EventType,
    GameEvent,
    PublicAnnouncementPayload,
    TeamNoticePayload,
)
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.rules.compat import CLASSIC_BOARD_ID, compile_legacy_execution
from werewolf.runtime.player_runtime import (
    ActionResponse,
    InitialContext,
    RuntimeConfig,
    Speech,
    SpeechResponse,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime

PROJECT_ROOT = Path(__file__).parents[2]
GAME_ID = "disclosed-history-a"
NOW = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)
SESSION_EPOCH = 7
ROSTER_JSON = json.dumps(
    {"kind": "chat_group_roster", "member_seats": [1, 2]},
    separators=(",", ":"),
)


async def _classic_execution():
    package = await KnowledgePackageLoader(PROJECT_ROOT / "vault" / "published").load(
        f"{CLASSIC_BOARD_ID}@1.0.0"
    )
    return compile_legacy_execution(package)


def _roster_notice(*, audience: tuple[int, ...] = (1, 2), round_no: int = 2) -> GameEvent:
    return GameEvent.team(
        event_id=1,
        game_id=GAME_ID,
        state_revision=1,
        round_no=round_no,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        created_at=NOW,
        event_type=EventType.TEAM_NOTICE,
        authorized_seats=audience,
        correlation_id=f"night-wolf-roster-r{round_no}",
        payload=TeamNoticePayload(content=ROSTER_JSON),
    )


def _team_chat_state() -> GameState:
    window = ActionWindow(
        window_id="wolf-team-chat-r2",
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        allowed_seats=(1, 2),
        allowed_action_codes=(299,),
        min_actions=0,
        max_actions=0,
        allow_pass=True,
        opened_at=NOW,
    )
    return GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        round_no=2,
        ruleset=RulesetRef(
            board_id=CLASSIC_BOARD_ID,
            version="1.0.0",
            snapshot_id="disclosed-history-snapshot",
            manifest_sha256="a" * 64,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id="wolf",
                faction_id="wolf",
                chat_group_ids=("wolf-chat",),
                session_epoch=SESSION_EPOCH,
            ),
            2: PlayerState(
                seat=2,
                role_id="wolf",
                faction_id="wolf",
                chat_group_ids=("wolf-chat",),
                session_epoch=SESSION_EPOCH,
            ),
            3: PlayerState(
                seat=3,
                role_id="villager",
                faction_id="good",
                session_epoch=SESSION_EPOCH,
            ),
            4: PlayerState(
                seat=4,
                role_id="villager",
                faction_id="good",
                session_epoch=SESSION_EPOCH,
            ),
        },
        action_windows={window.window_id: window.model_dump(mode="json")},
    )


@pytest.mark.asyncio
async def test_serially_acknowledged_roster_excludes_teammate_from_night_targets() -> None:
    compiled = await _classic_execution()
    manager = GameManager(
        _team_chat_state(),
        registry=compiled.action_registry,
        execution_package=compiled.execution,
    )
    runtime = ScriptedRuntime(
        [
            lambda request: SpeechResponse(
                request_id=request.request_id,
                speech=Speech(text="收到队伍名单"),
            ),
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [4]}],
            ),
        ]
    )
    await runtime.start(
        RuntimeConfig(session_id="disclosed-history-session"),
        InitialContext(
            game_id=GAME_ID,
            seat=1,
            session_epoch=SESSION_EPOCH,
            role_id="wolf",
        ),
    )
    notice = _roster_notice()
    await manager.commit_events((notice,), now=NOW)

    team_scheduler = SerialTurnScheduler(
        manager,
        {1: runtime},
        queue=(1,),
        phase=GamePhase.NIGHT_TEAM_CHAT,
    )
    await team_scheduler.start()
    team_result = await team_scheduler.run_next()

    cursor = manager.state.delivery_cursors[1]
    assert team_result.request.observation.events[0].event_id == notice.event_id
    assert cursor.session_epoch == SESSION_EPOCH
    assert cursor.committed_event_id == notice.event_id
    assert cursor.in_flight_request_id is None

    await manager.commit_phase_transition(GamePhase.NIGHT_ACTION, now=NOW)
    action_window = ActionWindow(
        window_id="wolf-kill-r2",
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_role_ids=("wolf",),
        allowed_action_codes=(101,),
        opened_at=NOW,
        visible_context={"candidate_seats": [1, 2, 3, 4]},
    )
    await manager.commit_action_window(action_window, now=NOW)
    context = ActionValidationContext(
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        active_request_id="coordinator-placeholder",
        role_id="wolf",
        authorized_action_codes=(101,),
        alive_seats=(1, 2, 3, 4),
        eligible_targets_by_action={101: (4,)},
    )

    await ActionTurnScheduler(manager, {1: runtime}).run_turn(action_window, 1, context)

    request = runtime.requests[1]
    assert request.action_window is not None
    assert request.action_window.candidate_seats == [1, 2, 3, 4]
    assert request.action_window.visible_context["targets_by_action"] == {"101": [3, 4]}


@pytest.mark.parametrize(
    ("event", "cursor_epoch", "committed_event_id"),
    [
        (None, SESSION_EPOCH, 0),
        (_roster_notice(), SESSION_EPOCH, 0),  # Not in the in-flight batch or ack history.
        (_roster_notice(audience=(2,)), SESSION_EPOCH, 1),
        (_roster_notice(round_no=1), SESSION_EPOCH, 1),
        (
            GameEvent.public(
                event_id=1,
                game_id=GAME_ID,
                state_revision=1,
                round_no=2,
                phase=GamePhase.NIGHT_TEAM_CHAT,
                created_at=NOW,
                event_type=EventType.TEAM_NOTICE,
                eligible_seats=(1, 2),
                payload=PublicAnnouncementPayload(content=ROSTER_JSON),
            ),
            SESSION_EPOCH,
            1,
        ),  # A public-channel payload cannot authorize team membership.
        (_roster_notice(), SESSION_EPOCH - 1, 1),
    ],
    ids=("absent", "undelivered", "wrong-audience", "wrong-round", "wrong-channel", "old-session"),
)
def test_disclosed_history_requires_ack_cursor_and_authorized_team_notice(
    event: GameEvent | None,
    cursor_epoch: int,
    committed_event_id: int,
) -> None:
    state = GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_ACTION,
        round_no=2,
        players={
            seat: PlayerState(
                seat=seat,
                role_id="wolf" if seat == 1 else "villager",
                faction_id="wolf" if seat == 1 else "good",
                session_epoch=SESSION_EPOCH,
            )
            for seat in (1, 2)
        },
        events=() if event is None else (event,),
        delivery_cursors={
            1: DeliveryCursor(
                session_epoch=cursor_epoch,
                committed_event_id=committed_event_id,
            )
        },
    )

    assert (
        _disclosed_team_seats(
            (),
            state=state,
            seat=1,
            session_epoch=SESSION_EPOCH,
            request_id="action-request",
        )
        is None
    )
