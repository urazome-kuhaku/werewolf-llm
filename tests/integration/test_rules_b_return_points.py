"""Return-point provenance across the day exile Hunter workflow."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from werewolf.cli_support.play_setup import build_play_setup
from werewolf.domain.enums import GamePhase
from werewolf.game.manager import GameManager
from werewolf.game.state import GameState
from werewolf.moderator.play_runner import ClassicPlayRunner
from werewolf.persistence.archive import GameArchiveStore


async def _build_setup(output: Path) -> None:
    await asyncio.to_thread(build_play_setup, output, all_scripted=True)


@pytest.mark.asyncio
async def test_day_exile_hunter_resumes_current_day_and_finishes_archived_game(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle prior-night cursor cannot redirect a host day-exile trigger."""

    setup = tmp_path / "classic-return-point"
    await _build_setup(setup)

    observed_host_returns: list[dict[str, object]] = []
    original_return_point = GameManager._rule_return_point

    def observe_return_point(
        manager: GameManager,
        state: object,
        requests: tuple[object, ...],
        bindings: tuple[object, ...],
    ) -> object:
        return_point = original_return_point(manager, state, requests, bindings)  # type: ignore[arg-type]
        host_requests = tuple(
            request
            for request in requests
            if getattr(request, "origin", None) == "HOST"
            and getattr(request, "skill_id", None) == "exile_resolution"
        )
        if host_requests:
            cursor = getattr(state, "rule_workflow_cursor")
            observed_host_returns.append(
                {
                    "phase": getattr(state, "phase"),
                    "round_no": getattr(state, "round_no"),
                    "day_no": getattr(state, "day_no"),
                    "state_revision": getattr(state, "state_revision"),
                    "cursor_status": getattr(cursor, "status"),
                    "cursor_group": getattr(cursor, "settlement_group_id"),
                    "cursor_return_point": getattr(cursor, "return_point"),
                    "request_actor": getattr(host_requests[0], "actor_seat"),
                    "request_action_code": getattr(host_requests[0], "action_code"),
                    "return_point": return_point,
                }
            )
        return return_point

    monkeypatch.setattr(GameManager, "_rule_return_point", observe_return_point)

    runner = ClassicPlayRunner(setup / "game.yaml", max_rounds=20)
    history: list[tuple[str, GamePhase, int, int, int]] = []
    restored_source_points: list[tuple[GamePhase, int, int, int]] = []
    original_command = runner.command

    async def record_command(line: str) -> object:
        result = await original_command(line)
        state = runner.shell.state
        history.append((line, state.phase, state.round_no, state.day_no, state.state_revision))
        if line == "day confirm-exile 8" and state.phase is GamePhase.TRIGGER_ACTION:
            restored = GameState.model_validate_json(state.model_dump_json())
            cursor = restored.rule_workflow_cursor
            assert cursor is not None and cursor.return_point is not None
            restored_source_points.append(
                (
                    cursor.return_point.phase,
                    cursor.return_point.day_no,
                    state.round_no,
                    state.day_no,
                )
            )
        return result

    monkeypatch.setattr(runner, "command", record_command)

    summary = await runner.run(experimental_preview_enabled=True)

    matching_host_returns = [
        item
        for item in observed_host_returns
        if item["round_no"] == 2 and item["day_no"] == 3 and item["request_actor"] == 8
    ]
    assert len(matching_host_returns) == 1
    observed = matching_host_returns[0]
    assert observed["phase"] is GamePhase.DAY_RESOLVE
    assert observed["round_no"] == 2
    assert observed["day_no"] == 3
    assert observed["cursor_status"] == "IDLE"
    assert observed["cursor_group"] == "night:2"
    stale_point = observed["cursor_return_point"]
    assert getattr(stale_point, "day_no") == 2
    assert observed["request_actor"] == 8
    assert observed["request_action_code"] == 203

    return_point = observed["return_point"]
    assert getattr(return_point, "phase") is GamePhase.DAY_RESOLVE
    assert getattr(return_point, "day_no") == 3
    assert restored_source_points == [(GamePhase.DAY_RESOLVE, 3, 2, 3)]

    exile_index = next(
        index
        for index, (line, phase, _round_no, _day_no, revision) in enumerate(history)
        if line == "day confirm-exile 8"
        and revision == int(observed["state_revision"]) + 1
        and phase is GamePhase.TRIGGER_ACTION
    )
    later = history[exile_index + 1 :]
    day_finish_index = next(
        index for index, (line, *_rest) in enumerate(later) if line == "day finish"
    )
    last_words_index = next(
        index for index, (line, *_rest) in enumerate(later) if line == "last-words next 8"
    )
    victory_index = next(
        index for index, (line, *_rest) in enumerate(later) if line == "victory check"
    )
    next_night_index = next(
        index
        for index, (line, phase, round_no, _day_no, _revision) in enumerate(later)
        if line == "night open" and round_no == 3 and phase is GamePhase.NIGHT_TEAM_CHAT
    )
    next_dawn = next(
        (phase, round_no, day_no)
        for _line, phase, round_no, day_no, _revision in later[next_night_index + 1 :]
        if phase is GamePhase.DAY_ANNOUNCE and round_no == 3
    )
    assert last_words_index < day_finish_index < victory_index < next_night_index
    exile_replays = [
        (phase, round_no, day_no)
        for line, phase, round_no, day_no, _revision in later
        if line == "day confirm-exile 8"
    ]
    assert exile_replays == [
        (GamePhase.DAY_RESOLVE, 2, 3),
    ]
    assert next_dawn == (GamePhase.DAY_ANNOUNCE, 3, 4)
    assert all(
        not (phase is GamePhase.DAY_ANNOUNCE and day_no >= 3)
        for _line, phase, _round_no, day_no, _revision in later[: victory_index + 1]
    )
    assert later[victory_index][2:4] == (3, 3)

    assert summary["status"] == "finished"
    assert summary["phase"] == "FINISHED"
    assert summary["run_status"] == "CLOSED"
    archive_path = summary["archive_path"]
    assert isinstance(archive_path, str)
    manifest = await GameArchiveStore(setup / "games").verify(archive_path)
    assert manifest.game_id == "classic-play-001"
    assert runner.shell.session_service is None
