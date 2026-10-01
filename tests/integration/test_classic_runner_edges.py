"""Focused command-order coverage for the classic moderator play runner."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.moderator.play_runner import ClassicPlayRunner


def _runner(
    phase: GamePhase,
) -> tuple[ClassicPlayRunner, SimpleNamespace, list[str]]:
    state = SimpleNamespace(
        phase=phase,
        sheriff_seat=1,
        players={
            1: SimpleNamespace(alive=False, can_vote=False),
            2: SimpleNamespace(alive=True, can_vote=True),
        },
    )
    sheriff = SimpleNamespace(
        enabled=True,
        transfer_enabled=True,
        transfer_on_death=True,
        transfer_on_resignation=True,
    )
    board = SimpleNamespace(day_flow=SimpleNamespace(sheriff=sheriff))
    runtime_bundle = SimpleNamespace(board=board)
    shell = SimpleNamespace(state=state, runtime_bundle=runtime_bundle)
    runner = ClassicPlayRunner("unused-config.yaml")
    setattr(runner, "shell", shell)
    commands: list[str] = []
    return runner, state, commands


def _install_command_fake(
    runner: ClassicPlayRunner,
    state: SimpleNamespace,
    commands: list[str],
    *,
    operation: str | None = None,
    last_words: list[int] | None = None,
) -> None:
    pending_last_words = [] if last_words is None else list(last_words)

    async def command(line: str) -> Mapping[str, object]:
        commands.append(line)
        if line == "last-words status":
            return {"last_words": {"pending_seats": list(pending_last_words)}}
        if line.startswith("last-words next"):
            pending_last_words.clear()
            return {"last_words": {"pending_seats": []}}
        if line == "trigger pending":
            return {
                "trigger": {
                    "operation": operation,
                    "pending_requests": [],
                }
            }
        if line == "sheriff transfer":
            # The manager's first-day transfer completes into DAY_SPEECH.
            state.phase = GamePhase.DAY_SPEECH
        elif line == "day speech open":
            state.phase = GamePhase.DAY_SPEECH
        elif line == "sheriff badge finish" and operation == "DAY_EXILE":
            state.phase = GamePhase.VICTORY_CHECK
        elif line == "trigger finish":
            state.phase = GamePhase.DAY_ANNOUNCE
        return {}

    setattr(runner, "command", command)


@pytest.mark.asyncio
async def test_sheriff_transfer_dead_sheriff_completes_badge_before_day_speech() -> None:
    runner, state, commands = _runner(GamePhase.SHERIFF_TRANSFER)
    _install_command_fake(runner, state, commands, last_words=[2])

    await runner._sheriff()

    assert commands == [
        "sheriff transfer",
        "day announce",
        "sheriff badge open",
        "sheriff badge next",
        "sheriff badge resolve",
        "sheriff badge finish",
        "last-words status",
        "last-words next 2",
        "day speech open",
    ]
    assert state.phase is GamePhase.DAY_SPEECH


@pytest.mark.asyncio
async def test_night_resolution_trigger_finishes_without_day_badge_or_speech() -> None:
    runner, state, commands = _runner(GamePhase.TRIGGER_ACTION)
    _install_command_fake(
        runner,
        state,
        commands,
        operation="NIGHT_RESOLUTION",
    )

    await runner._trigger()

    assert commands == [
        "trigger open",
        "trigger pending",
        "trigger next",
        "trigger auto-resolve",
        "trigger finish",
    ]
    assert state.phase is GamePhase.DAY_ANNOUNCE
    assert not any(command.startswith("sheriff badge") for command in commands)
    assert "victory check" not in commands
    assert "day speech open" not in commands


@pytest.mark.asyncio
async def test_day_exile_trigger_finishes_last_words_then_badge_at_victory_boundary() -> None:
    runner, state, commands = _runner(GamePhase.TRIGGER_ACTION)
    _install_command_fake(
        runner,
        state,
        commands,
        operation="DAY_EXILE",
        last_words=[2],
    )

    await runner._trigger()

    assert commands == [
        "trigger open",
        "trigger pending",
        "trigger next",
        "trigger auto-resolve",
        "last-words status",
        "last-words next 2",
        "sheriff badge open",
        "sheriff badge next",
        "sheriff badge resolve",
        "sheriff badge finish",
    ]
    assert commands.index("last-words status") < commands.index("sheriff badge open")
    assert "trigger finish" not in commands
    assert "day speech open" not in commands
    assert state.phase is GamePhase.VICTORY_CHECK
