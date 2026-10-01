"""Offline acceptance checks for the generated classic playable setup."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from werewolf.cli_support.play_setup import build_play_setup
from werewolf.moderator.play_runner import ClassicPlayRunner, PlayRunnerError
from werewolf.persistence.archive import GameArchiveStore


async def _build_setup(output: Path) -> None:
    await asyncio.to_thread(build_play_setup, output, all_scripted=True)


@pytest.mark.asyncio
async def test_classic_twelve_scripted_players_reach_final_archive(tmp_path: Path) -> None:
    setup = tmp_path / "classic-play"
    await _build_setup(setup)
    public_events: list[dict[str, object]] = []

    runner = ClassicPlayRunner(
        setup / "game.yaml",
        max_rounds=20,
        output=lambda item: public_events.append(dict(item)),
    )
    summary = await runner.run(experimental_preview_enabled=True)

    assert summary["status"] == "finished"
    assert summary["phase"] == "FINISHED"
    assert summary["run_status"] == "CLOSED"
    archive_path = summary["archive_path"]
    assert isinstance(archive_path, str)
    manifest = await GameArchiveStore(setup / "games").verify(archive_path)
    assert manifest.game_id == "classic-play-001"
    assert public_events
    assert all(item.get("event") in {"progress", "public"} for item in public_events)
    assert all("players" not in item for item in public_events)
    assert not any(
        isinstance(item.get("payload"), dict) and item["payload"].get("kind") == "seer_result"
        for item in public_events
    )
    assert runner.shell.session_service is None


@pytest.mark.asyncio
async def test_classic_runner_stops_at_round_limit_and_closes_sessions(tmp_path: Path) -> None:
    setup = tmp_path / "classic-stop"
    await _build_setup(setup)
    runner = ClassicPlayRunner(setup / "game.yaml", max_rounds=1)

    with pytest.raises(PlayRunnerError, match="round limit reached"):
        await runner.run(experimental_preview_enabled=True)

    assert runner.shell.session_service is None
