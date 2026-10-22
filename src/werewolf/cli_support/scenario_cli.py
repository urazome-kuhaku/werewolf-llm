"""Synchronous CLI adapter for the asynchronous scenario runner."""

from __future__ import annotations

import asyncio
from pathlib import Path

from werewolf.moderator.scenario_runner import run_scenario


def execute_scenario(
    config_path: str | Path,
    scenario_path: str | Path,
    output_path: str | Path,
    *,
    experimental_preview_enabled: bool = False,
) -> dict[str, object]:
    """Run a scenario from the synchronous Typer command boundary."""

    return asyncio.run(
        run_scenario(
            config_path,
            scenario_path,
            output_path,
            experimental_preview_enabled=experimental_preview_enabled,
        )
    )


__all__ = ["execute_scenario"]
