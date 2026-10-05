"""The whole-game runner drains only durable rule work."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.moderator.play_runner import ClassicPlayRunner


def _runner_with_workflow(
    phase: GamePhase,
    *,
    queue_status: str | None = None,
    cursor_status: str = "IDLE",
) -> tuple[ClassicPlayRunner, list[str]]:
    occurrence_queue = () if queue_status is None else (SimpleNamespace(status=queue_status),)
    cursor = SimpleNamespace(
        status=cursor_status,
        active_occurrence_id=None,
        pending_boundary_id=None,
        pending_flow_action=None,
    )
    state = SimpleNamespace(
        phase=phase,
        state_revision=0,
        rule_workflow_cursor=cursor,
        rule_trigger_queue=occurrence_queue,
        rule_boundaries=(),
    )
    shell = SimpleNamespace(
        state=state,
        manager=SimpleNamespace(execution_package=object()),
    )
    runner = ClassicPlayRunner("unused-config.yaml")
    setattr(runner, "shell", shell)
    commands: list[str] = []

    async def command(line: str) -> Mapping[str, object]:
        commands.append(line)
        if line == "trigger auto-resolve":
            # Model the durable completion written by TriggerFlow so the
            # runner's no-progress guard observes a real revision change.
            state.rule_trigger_queue = ()
            state.rule_workflow_cursor = SimpleNamespace(
                status="IDLE",
                active_occurrence_id=None,
                pending_boundary_id=None,
                pending_flow_action=None,
            )
            state.state_revision += 1
        return {}

    setattr(runner, "command", command)
    return runner, commands


@pytest.mark.asyncio
async def test_night_runner_skips_trigger_auto_resolve_without_durable_work() -> None:
    runner, commands = _runner_with_workflow(GamePhase.DAY_ANNOUNCE)

    await runner._drain_pending_rule_work()

    assert commands == []


@pytest.mark.asyncio
async def test_night_runner_drains_queued_work_outside_trigger_phase() -> None:
    runner, commands = _runner_with_workflow(
        GamePhase.DAY_ANNOUNCE,
        queue_status="QUEUED",
    )

    await runner._drain_pending_rule_work()

    assert commands == ["trigger auto-resolve"]


@pytest.mark.asyncio
async def test_night_runner_leaves_trigger_phase_to_phase_dispatch() -> None:
    runner, commands = _runner_with_workflow(
        GamePhase.TRIGGER_ACTION,
        queue_status="WAITING_CHOICE",
        cursor_status="WAITING_CHOICE",
    )

    await runner._drain_pending_rule_work()

    assert commands == []
