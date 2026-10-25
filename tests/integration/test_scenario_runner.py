"""Real CLI integration coverage for unfamiliar source-package scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from typer.testing import CliRunner

from werewolf.cli import app  # type: ignore[import-untyped]
from werewolf.cli_support.play_setup import build_play_setup  # type: ignore[import-untyped]

PROJECT_ROOT = Path(__file__).parents[2]
SCENARIO_SOURCE = PROJECT_ROOT / "tests" / "fixtures" / "scenario_quasar"
SCENARIOS = PROJECT_ROOT / "tests" / "fixtures" / "scenarios"
_RUNNER = CliRunner()


def _setup(tmp_path: Path, *, label: str) -> Path:
    config = build_play_setup(
        tmp_path / label,
        all_scripted=True,
        board_ref="quasar_echo_lab@1.0.0",
        published_root=SCENARIO_SOURCE,
        project_root=PROJECT_ROOT,
        game_id=f"c-scenario-{label}-{uuid4().hex[:8]}",
    )
    return Path(str(config["config_path"]))


def _run(config: Path, scenario_name: str, tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    report_path = tmp_path / f"{scenario_name}-{uuid4().hex}.json"
    result = _RUNNER.invoke(
        app,
        [
            "scenario",
            "run",
            "--config",
            str(config),
            "--scenario",
            str(SCENARIOS / f"{scenario_name}.yaml"),
            "--output",
            str(report_path),
        ],
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return result, report


def test_unknown_source_package_full_game_uses_real_cast_gateway_and_archive(
    tmp_path: Path,
) -> None:
    config = _setup(tmp_path, label="full")
    result, report = _run(config, "quasar-source-full", tmp_path)

    assert result.exit_code == 0, result.stdout
    assert report["status"] == "PASS"
    assert report["board_ref"] == "quasar_echo_lab@1.0.0"
    assert report["proof_summary"]["required_steps_consumed"] == 6
    assert report["proof_summary"]["gateway_reads"] == 6
    assert report["proof_summary"]["private_skill_queries"] == 1
    assert report["player_casts"][0]["skill_id"] == "quasar_echo_unfamiliar"
    assert report["game_result"]["phase"] == "FINISHED"
    assert report["game_result"]["run_status"] == "CLOSED"
    assert report["archive"]["archive_verified"] is True
    assert report["archive"]["snapshot_verified"] is True
    assert report["assertions"][0]["actual"] is False
    assert report["assertions"][1]["actual"] is False


def test_command_scenario_runs_host_boundaries_and_positional_assertions(
    tmp_path: Path,
) -> None:
    config = _setup(tmp_path, label="command")
    result, report = _run(config, "quasar-command-positive", tmp_path)

    assert result.exit_code == 0, result.stdout
    assert report["status"] == "PASS"
    assert report["proof_summary"]["required_steps_consumed"] == 12
    assert report["proof_summary"]["gateway_reads"] == 6
    assert [item["actual"] for item in report["assertions"]] == [
        "CREATED",
        "ASSIGNED",
        "PLAYER_PREPARE",
        "NIGHT_TEAM_CHAT",
    ]
    assert report["host_commands"][0]["post_state_proof"]["creation_proved"] is True


def test_command_scenario_rejects_failed_assertions_unused_steps_and_budget_exhaustion(
    tmp_path: Path,
) -> None:
    cases = (
        ("quasar-command-assertion-fail", "assertion failed"),
        ("quasar-command-assert-missing-seat", "assertion failed"),
        ("quasar-command-assert-unknown-state", "assertion failed"),
        ("quasar-command-unused", "unused-player-action"),
        ("quasar-command-budget", "budget exhausted"),
        ("quasar-command-round-budget", "round budget exhausted"),
    )
    for index, (scenario_name, expected_failure) in enumerate(cases):
        config = _setup(tmp_path, label=f"negative-{index}")
        result, report = _run(config, scenario_name, tmp_path)

        assert result.exit_code == 1, result.stdout
        assert report["status"] == "FAIL"
        assert any(expected_failure in failure for failure in report["failures"])
        if scenario_name in {
            "quasar-command-assert-missing-seat",
            "quasar-command-assert-unknown-state",
        }:
            assert report["assertions"][0]["actual"] == "UNAVAILABLE"
            assert report["assertions"][0]["passed"] is False
        if scenario_name == "quasar-command-round-budget":
            assert len(report["failures"]) == 1
            assert [item["actual"] for item in report["assertions"]] == [
                1,
                "VICTORY_CHECK",
            ]
            assert all(item["consumed"] for item in report["steps"] if item["required"])
            assert report["host_commands"][-1]["command"] == "day finish"
            assert report["host_commands"][-1]["post_state_proof"]["phase_after"] == (
                "VICTORY_CHECK"
            )
