"""Integration coverage for the generic trigger moderator commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hunter_trigger import _state as hunter_state
from test_moderator_night_flow import _board

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.game import (
    Action,
    ActionResolution,
    ActionResolutionEntry,
    GameManager,
    ResolutionEffect,
    ResolutionStatus,
    load_action_registry,
)
from werewolf.moderator import ModeratorError, ModeratorShell
from werewolf.runtime.player_runtime import ActionResponse, InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 29, tzinfo=UTC)


async def _runtime(response_factory: object, *additional_responses: object) -> ScriptedRuntime:
    runtime = ScriptedRuntime([response_factory, *additional_responses])
    await runtime.start(
        RuntimeConfig(session_id="trigger-shell-session"),
        InitialContext(game_id="hunter-game", seat=1, session_epoch=2),
    )
    return runtime


def _shell(
    state: object,
    runtime: ScriptedRuntime,
    *,
    board: object | None = None,
) -> ModeratorShell:
    shell = ModeratorShell("unused-game-config.yaml", clock=lambda: NOW)
    shell.manager = GameManager(state, registry=load_action_registry())  # type: ignore[arg-type]
    shell.runtime_bundle = SimpleNamespace(board=_board() if board is None else board)
    shell.session_service = SimpleNamespace(runtimes={1: runtime})
    return shell


def _badge_board() -> object:
    data = _board().model_dump(mode="python")
    data["board_id"] = "classic-12"
    data["reading_plan"]["board_ref"] = {"id": "classic-12", "version": "1.0.0"}
    data["day_flow"]["sheriff"].update(
        enabled=True,
        transfer_enabled=True,
        transfer_on_death=True,
        transfer_on_resignation=True,
    )
    return _board().model_validate(data)


def _resolution_file(
    path: Path,
    *,
    pending: dict[str, object],
    base_revision: int,
    requested: Action,
    effects: tuple[ResolutionEffect, ...] = (),
) -> None:
    resolution = ActionResolution(
        resolution_id="shell-trigger-ruling",
        bundle_id=str(pending["bundle_id"]),
        game_id="hunter-game",
        window_id=str(pending["window_id"]),
        request_id=str(pending["request_id"]),
        session_epoch=int(pending["session_epoch"]),
        base_revision=base_revision,
        status=ResolutionStatus.CONFIRMED,
        actions=(
            ActionResolutionEntry(
                action_index=0,
                requested_action=requested,
                effects=effects,
            ),
        ),
        moderator_id="shell-test",
        created_at=NOW,
    )
    path.write_text(json.dumps([resolution.model_dump(mode="json")]), encoding="utf-8")


@pytest.mark.asyncio
async def test_trigger_shell_runs_day_exile_skill_and_marks_private_ruling(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 105, "targets": [2]}],
        )
    )
    shell = _shell(hunter_state(), runtime)

    status = await shell.execute("trigger status")
    assert status["private"] is True
    assert status["sensitive"] is True

    opened = await shell.execute("trigger open")
    assert opened["private"] is True
    assert opened["sensitive"] is True
    assert opened["trigger"]["operation"] == "DAY_EXILE"  # type: ignore[index]
    advanced = await shell.execute("trigger next 1")
    assert advanced["private"] is True
    assert advanced["sensitive"] is True
    pending_result = await shell.execute("trigger pending")
    assert pending_result["private"] is True
    assert pending_result["sensitive"] is True
    pending = pending_result["trigger"]  # type: ignore[index]
    assert isinstance(pending, dict)
    records = pending["pending_requests"]
    assert isinstance(records, list) and len(records) == 1
    request_record = records[0]
    assert isinstance(request_record, dict)
    requested = Action.model_validate(request_record["actions"][0])
    ruling = tmp_path / "day-trigger.json"
    _resolution_file(
        ruling,
        pending=request_record,
        base_revision=shell.state.state_revision,
        requested=requested,
        effects=(
            ResolutionEffect(
                effect_id="trigger-death",
                action_index=0,
                effect_type="SET_ALIVE",
                target_seat=2,
                value=False,
            ),
            ResolutionEffect(
                effect_id="trigger-cause",
                action_index=0,
                effect_type="SET_DEATH_CAUSE",
                target_seat=2,
                value="hunter_shot",
            ),
        ),
    )

    resolved = await shell.execute(f"trigger resolve {ruling}")
    assert resolved["private"] is True
    assert resolved["sensitive"] is True
    finished = await shell.execute("trigger finish")
    assert finished["private"] is True
    assert finished["sensitive"] is True
    assert finished["phase"] == GamePhase.VICTORY_CHECK.value
    assert shell.state.players[2].alive is False


@pytest.mark.asyncio
async def test_trigger_shell_runs_night_resolution_with_explicit_pass(
    tmp_path: Path,
) -> None:
    state = hunter_state()
    state = state.model_copy(
        update={
            "pending_resolution": {
                **state.pending_resolution,  # type: ignore[misc]
                "operation": "NIGHT_RESOLUTION",
            },
            "moderator_audit": ({"operation": "ACTION_RESOLUTION", "resolution_id": "exile-1"},),
        }
    )
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 299}],
        )
    )
    shell = _shell(state, runtime)

    await shell.execute("trigger open")
    await shell.execute("trigger next")
    pending_result = await shell.execute("trigger pending")
    pending = pending_result["trigger"]  # type: ignore[index]
    assert isinstance(pending, dict)
    request_record = pending["pending_requests"][0]
    assert isinstance(request_record, dict)
    requested = Action.model_validate(request_record["actions"][0])
    ruling = tmp_path / "night-trigger-pass.json"
    _resolution_file(
        ruling,
        pending=request_record,
        base_revision=shell.state.state_revision,
        requested=requested,
    )

    resolved = await shell.execute(f"trigger resolve {ruling}")
    assert resolved["resolution"]["count"] == 1  # type: ignore[index]
    finished = await shell.execute("trigger finish")
    assert finished["private"] is True
    assert finished["sensitive"] is True
    assert finished["phase"] == GamePhase.DAY_ANNOUNCE.value


@pytest.mark.asyncio
async def test_night_trigger_finish_skips_dawn_last_words_and_badge_guards(
    tmp_path: Path,
) -> None:
    state = hunter_state()
    state = state.model_copy(
        update={
            "sheriff_seat": 1,
            "pending_resolution": {
                **state.pending_resolution,  # type: ignore[misc]
                "operation": "NIGHT_RESOLUTION",
            },
            "moderator_audit": ({"operation": "ACTION_RESOLUTION", "resolution_id": "exile-1"},),
        }
    )
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 299}],
        )
    )
    shell = _shell(state, runtime, board=_badge_board())

    await shell.execute("trigger open")
    await shell.execute("trigger next")
    pending_result = await shell.execute("trigger pending")
    pending = pending_result["trigger"]  # type: ignore[index]
    assert isinstance(pending, dict)
    request_record = pending["pending_requests"][0]
    assert isinstance(request_record, dict)
    requested = Action.model_validate(request_record["actions"][0])
    ruling = tmp_path / "night-trigger-dead-sheriff.json"
    _resolution_file(
        ruling,
        pending=request_record,
        base_revision=shell.state.state_revision,
        requested=requested,
    )

    await shell.execute(f"trigger resolve {ruling}")
    finished = await shell.execute("trigger finish")
    assert finished["phase"] == GamePhase.DAY_ANNOUNCE.value
    assert shell.state.phase is GamePhase.DAY_ANNOUNCE


@pytest.mark.asyncio
async def test_day_trigger_finish_preserves_last_words_and_badge_order(
    tmp_path: Path,
) -> None:
    state = hunter_state().model_copy(update={"sheriff_seat": 1})
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 105, "targets": [2]}],
        ),
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 201, "targets": [3]}],
        ),
    )
    shell = _shell(state, runtime, board=_badge_board())

    await shell.execute("trigger open")
    await shell.execute("trigger next")
    pending_result = await shell.execute("trigger pending")
    pending = pending_result["trigger"]  # type: ignore[index]
    assert isinstance(pending, dict)
    request_record = pending["pending_requests"][0]
    assert isinstance(request_record, dict)
    requested = Action.model_validate(request_record["actions"][0])
    ruling = tmp_path / "day-trigger-dead-sheriff.json"
    _resolution_file(
        ruling,
        pending=request_record,
        base_revision=shell.state.state_revision,
        requested=requested,
        effects=(
            ResolutionEffect(
                effect_id="trigger-death",
                action_index=0,
                effect_type="SET_ALIVE",
                target_seat=2,
                value=False,
            ),
            ResolutionEffect(
                effect_id="trigger-cause",
                action_index=0,
                effect_type="SET_DEATH_CAUSE",
                target_seat=2,
                value="hunter_shot",
            ),
        ),
    )
    await shell.execute(f"trigger resolve {ruling}")

    with pytest.raises(ModeratorError, match="BADGE_PENDING"):
        await shell.execute("trigger finish")
    assert shell.state.phase is GamePhase.TRIGGER_ACTION

    await shell.execute("sheriff badge open")
    await shell.execute("sheriff badge next")
    await shell.execute("sheriff badge resolve")
    finished = await shell.execute("sheriff badge finish")
    assert finished["phase"] == GamePhase.VICTORY_CHECK.value
    assert shell.state.phase is GamePhase.VICTORY_CHECK


@pytest.mark.asyncio
async def test_trigger_shell_rejects_ambiguous_json_and_implicit_ruling(tmp_path: Path) -> None:
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 299}],
        )
    )
    shell = _shell(hunter_state(), runtime)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('[{"schema_version":1,"schema_version":1}]', encoding="utf-8")

    with pytest.raises(ModeratorError, match="valid JSON"):
        await shell.execute(f"trigger resolve {duplicate}")
    with pytest.raises(ModeratorError, match="trigger syntax"):
        await shell.execute("trigger resolve")
    assert shell.state.state_revision == 0

    shell.manager._state = shell.state.model_copy(update={"run_status": RunStatus.PAUSED})
    with pytest.raises(ModeratorError, match="paused"):
        await shell.execute("trigger open")


@pytest.mark.asyncio
async def test_trigger_shell_help_exposes_generic_command_family(tmp_path: Path) -> None:
    runtime = await _runtime(
        lambda request: ActionResponse(
            request_id=request.request_id,
            actions=[{"action_code": 299}],
        )
    )
    shell = _shell(hunter_state(), runtime)

    help_result = await shell.execute("help")
    commands = help_result["commands"]  # type: ignore[index]
    assert "trigger open" in commands
    assert "trigger resolve <json-file>" in commands
    status = await shell.execute("trigger status")
    assert status["private"] is True
    assert status["sensitive"] is True
