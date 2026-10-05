"""Return-point provenance for frozen windows with no player requests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from werewolf.domain.enums import GamePhase
from werewolf.game.actions import ActionWindow
from werewolf.game.manager import GameManager
from werewolf.game.state import GameState, RuleWorkflowCursor


def test_empty_collecting_window_returns_to_its_frozen_successor() -> None:
    """An empty window still has a source even when it has no request bindings."""

    now = datetime(2026, 10, 5, tzinfo=UTC)
    group_id = "night:1:team-chat"
    window = ActionWindow(
        window_id="empty-team-chat",
        game_id="return-provenance-game",
        session_epoch=1,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        collection_only=True,
        min_actions=0,
        max_actions=0,
        settlement_group_id=group_id,
        logical_window_id="night-team-chat",
        next_window_id="night-action",
        opened_at=now,
        collection_complete_at=now,
    )
    state = GameState(
        game_id="return-provenance-game",
        created_at=now,
        updated_at=now,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        round_no=1,
        day_no=1,
        rule_workflow_cursor=RuleWorkflowCursor(
            settlement_group_id=group_id,
            active_window_ids=(window.window_id,),
            completed_collection_window_ids=(window.window_id,),
            status="COLLECTING",
        ),
        action_windows={window.window_id: window.model_dump(mode="json")},
    )
    manager = object.__new__(GameManager)
    manager._execution_package = SimpleNamespace(
        window_metadata=(
            SimpleNamespace(
                window_id="night-team-chat",
                phase=GamePhase.NIGHT_TEAM_CHAT.value,
                order=0,
            ),
            SimpleNamespace(
                window_id="night-action",
                phase=GamePhase.NIGHT_ACTION.value,
                order=1,
            ),
        )
    )

    return_point = GameManager._rule_return_point(manager, state, (), ())

    assert return_point.phase is GamePhase.NIGHT_ACTION
    assert return_point.window_id == window.window_id
    assert return_point.logical_window_id == "night-action"
    assert return_point.day_no == 1
