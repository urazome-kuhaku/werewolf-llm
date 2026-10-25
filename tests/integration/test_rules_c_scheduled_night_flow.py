"""Scheduled night boundaries traverse the real scripted game lifecycle."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml

from werewolf.cli_support.play_setup import build_play_setup
from werewolf.domain.enums import GamePhase
from werewolf.game.actions import load_action_registry
from werewolf.game.events import EventType
from werewolf.game.manager import EventCommitError, GameManager
from werewolf.game.state import GameState
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.refs import VersionedRef
from werewolf.moderator.play_runner import ClassicPlayRunner

PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"


def _scheduled_published_root(output: Path) -> Path:
    root = output / "published"
    shutil.copytree(PUBLISHED_ROOT, root)
    (root / "manifests" / "classic_12_seer_witch_hunter_idiot@1.0.0.json").unlink()
    board_path = root / "boards" / "classic_12_seer_witch_hunter_idiot" / "1.0.0" / "board.md"
    source = board_path.read_text(encoding="utf-8")
    sections = source.split("---", 2)
    assert len(sections) == 3
    board = yaml.safe_load(sections[1])
    assert isinstance(board, dict)
    board["night_boundary_schedule"] = {
        "prerequisites": ["DAY_ANNOUNCEMENT", "FIRST_DAY_SHERIFF_ELECTION"]
    }
    board_path.write_text(
        f"---\n{yaml.safe_dump(board, allow_unicode=True, sort_keys=False)}---\n"
        f"{sections[2].lstrip()}",
        encoding="utf-8",
    )
    return root


async def _build_scheduled_setup(output: Path) -> None:
    published_root = _scheduled_published_root(output.parent)
    source_package = await KnowledgePackageLoader(published_root).load(
        VersionedRef.parse(BOARD_REF)
    )
    classic_template = KnowledgePackageCompiler().compile(source_package)
    execution = classic_template.execution
    assert execution is not None
    execution_payload = execution.model_dump(mode="json")
    execution_payload.pop("night_boundary_schedule", None)
    execution_payload["interactions"] = [
        item
        for item in execution_payload["interactions"]
        if item["interaction_id"] != "classic_final_death_fact"
    ]
    base_actions = {item.action_code for item in load_action_registry().actions}
    action_definitions = [
        item.model_dump(mode="json")
        for item in classic_template.action_registry.actions
        if item.action_code not in base_actions
    ]
    board_path = (
        published_root
        / "boards"
        / "classic_12_seer_witch_hunter_idiot"
        / "1.0.0"
        / "execution.yaml"
    )
    board_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "execution": execution_payload,
                "action_definitions": action_definitions,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    await asyncio.to_thread(
        build_play_setup,
        output,
        all_scripted=True,
        game_id="scheduled-night-game",
        seed=20260930,
        board_ref=BOARD_REF,
        published_root=published_root,
        project_root=PROJECT_ROOT,
    )


@pytest.mark.asyncio
async def test_first_night_dead_elected_sheriff_completes_announcement_badge_words_then_speech(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = tmp_path / "scheduled-night"
    await _build_scheduled_setup(setup)
    runner = ClassicPlayRunner(setup / "game.yaml", max_rounds=20)
    history: list[tuple[str, GamePhase, int, int]] = []
    first_night_deaths: tuple[int, ...] = ()
    original_command = runner.command
    original_validate = GameManager._validate_rule_batch

    def inspect_batch(manager, state, batch, requests, **kwargs):
        facts = (*batch.outcomes, *batch.facts)
        if any(fact.fact_type.upper() in {"DEATH_CONFIRMED", "RULE_FLOW_QUEUED"} for fact in facts):
            print("RULE_RESERVED_FACTS", [fact.fact_type for fact in facts])
            print("RULE_INTERACTIONS", [item.interaction_id for item in manager.execution_package.interactions])
        return original_validate(manager, state, batch, requests, **kwargs)

    monkeypatch.setattr(GameManager, "_validate_rule_batch", inspect_batch)

    async def record_command(line: str):
        nonlocal first_night_deaths
        result = await original_command(line)
        state = runner.shell.state
        history.append((line, state.phase, state.round_no, state.day_no))
        if (
            line == "night auto-resolve"
            and state.round_no == 0
            and state.phase is GamePhase.DAY_ANNOUNCE
        ):
            first_night_deaths = tuple(
                seat for seat, player in sorted(state.players.items()) if not player.alive
            )
            assert first_night_deaths
            # Bind the election to an actual, proven first-night casualty.
            runner.sheriff_candidates = (first_night_deaths[0],)
        return result

    runner.command = record_command  # type: ignore[method-assign]
    summary = await runner.run()

    assert summary["status"] == "finished"
    assert first_night_deaths
    election = runner.shell.state.sheriff_election
    assert isinstance(election, dict)
    assert election["status"] == "RESOLVED"
    dead_sheriff = election["sheriff_seat"]
    assert dead_sheriff in first_night_deaths
    announcement_index = next(
        index for index, row in enumerate(history) if row[0] == "day announce"
    )
    badge_open_index = next(
        index for index, row in enumerate(history) if row[0] == "sheriff badge open"
    )
    last_words_index = next(
        index
        for index, row in enumerate(history)
        if row[0] == f"last-words next {dead_sheriff}"
    )
    night_check_index = next(
        index for index, row in enumerate(history) if row[0] == "victory night-check"
    )
    speech_open_index = next(
        index for index, row in enumerate(history) if row[0] == "day speech open"
    )
    assert announcement_index < badge_open_index < last_words_index < night_check_index < speech_open_index
    assert history[badge_open_index][1] is GamePhase.DAY_ANNOUNCE
    assert history[last_words_index][1] is GamePhase.DAY_ANNOUNCE
    assert history[night_check_index][1] is GamePhase.DAY_ANNOUNCE
    assert history[speech_open_index][1] is GamePhase.DAY_SPEECH

    announcement_events = [
        event
        for event in runner.shell.state.events
        if getattr(event, "event_type", None) is EventType.ANNOUNCEMENT
        and getattr(event, "correlation_id", None) == "day-announce-r0-d1"
    ]
    assert len(announcement_events) == 1
    state_copy = GameState.model_validate_json(runner.shell.state.model_dump_json())
    assert state_copy.sheriff_election == runner.shell.state.sheriff_election


@pytest.mark.asyncio
async def test_schedule_rejects_a_forged_dawn_speech_edge_before_prerequisites(
    tmp_path: Path,
) -> None:
    setup = tmp_path / "scheduled-edge"
    await _build_scheduled_setup(setup)
    runner = ClassicPlayRunner(setup / "game.yaml", max_rounds=1)

    try:
        await runner.command("new")
        await runner.command("next")
        await runner.command("next")
        await runner.command("start")
        await runner._prepare()
        await runner.command("next")
        assert runner.shell.state.phase is GamePhase.NIGHT_TEAM_CHAT
        active_manager = runner.shell.manager
        assert active_manager is not None
        forged_state = runner.shell.state.model_copy(
            update={"phase": GamePhase.VICTORY_CHECK, "round_no": 1}
        )
        isolated_manager = GameManager(
            forged_state,
            registry=active_manager.registry,
            execution_package=active_manager.execution_package,
            package_identity=active_manager.package_identity,
            legacy_compatibility=active_manager.legacy_compatibility,
        )
        with pytest.raises(EventCommitError, match="prior scheduled night boundary"):
            await isolated_manager.commit_phase_transition(GamePhase.NIGHT_TEAM_CHAT)
    finally:
        if runner.shell.session_service is not None:
            await runner.shell.session_service.close()

