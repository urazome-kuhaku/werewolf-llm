"""Moderator command integration for the ordinary daytime path."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_moderator_start import _compiled, _ReadingRuntime, _write_config

from werewolf.domain.enums import GamePhase
from werewolf.game.voting import VoteRequest
from werewolf.moderator import ModeratorError, ModeratorShell


async def _to_day(shell: ModeratorShell) -> None:
    await shell.new()
    await shell.next()  # RULESET_READY
    await shell.next()  # ASSIGNED
    await shell.execute("start")
    await shell.execute("prepare next")
    await shell.next()  # NIGHT_TEAM_CHAT
    await shell.next()  # NIGHT_ACTION
    await shell.next()  # NIGHT_RESOLVE
    await shell.next()  # DAY_ANNOUNCE


@pytest.mark.asyncio
async def test_day_commands_drive_announce_speech_vote_and_exile(tmp_path: Path) -> None:
    compiled = await _compiled(tmp_path)
    config = tmp_path / "game.yaml"
    _write_config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(
        config,
        runtime_factory=lambda _player, gateway_url, token: _ReadingRuntime(gateway_url, token),
    )

    await _to_day(shell)

    announced = await shell.execute("day announce 昨夜平安")
    assert announced is not None
    assert announced["phase"] == GamePhase.DAY_SPEECH.value

    opened = await shell.execute("day speech open")
    assert opened is not None
    assert opened["day"]["speech"]["queue"] == [1]  # type: ignore[index]

    spoken = await shell.execute("day speech next")
    assert spoken is not None
    assert spoken["phase"] == GamePhase.DAY_SPEECH.value
    closed = await shell.execute("day speech close")
    assert closed is not None
    assert closed["phase"] == GamePhase.VOTE.value

    opened_vote = await shell.execute("day vote open")
    assert opened_vote is not None
    assert opened_vote["day"]["vote"]["window_id"] == "day-vote-r0-d1"  # type: ignore[index]

    assert shell._day_flow is not None
    progress = shell._day_flow.coordinator.vote_progress
    assert progress is not None
    window = progress.window
    await shell._day_flow.coordinator.submit_vote(
        VoteRequest(
            request_id="day-vote-seat-1",
            game_id=window.game_id,
            window_id=window.window_id,
            seat=1,
            session_epoch=window.session_epoch,
            observation_revision=window.observation_revision,
            target_seat=1,
        )
    )
    collected = await shell.execute("day vote collect")
    assert collected is not None
    assert collected["phase"] == GamePhase.VOTE.value
    confirmed = await shell.execute("day vote confirm")
    assert confirmed is not None
    assert confirmed["phase"] == GamePhase.DAY_RESOLVE.value

    exiled = await shell.execute("day confirm-exile 1")
    assert exiled is not None
    assert exiled["phase"] == GamePhase.DAY_RESOLVE.value
    finished = await shell.execute("day finish")
    assert finished is not None
    assert finished["phase"] == GamePhase.VICTORY_CHECK.value


@pytest.mark.asyncio
async def test_day_commands_fail_closed_before_sessions_and_keep_retryable_speech_boundary(
    tmp_path: Path,
) -> None:
    compiled = await _compiled(tmp_path)
    config = tmp_path / "game.yaml"
    _write_config(config, compiled, tmp_path / "games")
    shell = ModeratorShell(config)

    await shell.new()
    help_result = await shell.execute("day help")
    assert help_result is not None
    assert "day speech retry" in help_result["commands"]  # type: ignore[operator]
    with pytest.raises(ModeratorError, match="started player sessions"):
        await shell.execute("day status")
