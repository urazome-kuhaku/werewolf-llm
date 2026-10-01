"""Contract tests for the read-only top-level command entry points."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from werewolf.cli import app
from werewolf.cli_support.diagnostics import json_report
from werewolf.domain.enums import GamePhase
from werewolf.game import GameState, PlayerState, RulesetRef
from werewolf.persistence import GameArchiveStore, GameSnapshotStore

runner = CliRunner()
_NOW = datetime(2026, 9, 28, 18, 30, 12, tzinfo=UTC)


def _make_archive(tmp_path: Path) -> tuple[Path, Path]:
    active = tmp_path / "games" / "active" / "game-1"
    state = GameState(
        game_id="game-1",
        created_at=_NOW,
        updated_at=_NOW,
        phase=GamePhase.FINISHED,
        round_no=2,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-rules",
            manifest_sha256="a" * 64,
        ),
        players={
            1: PlayerState(seat=1, role_id="wolf", faction_id="wolves", runtime_ref="pi-1"),
            2: PlayerState(seat=2, role_id="seer", faction_id="village", runtime_ref="pi-2"),
        },
    )
    snapshot = asyncio.run(GameSnapshotStore(active).create(state, created_at=_NOW))
    archive = asyncio.run(GameArchiveStore(tmp_path / "games", clock=lambda: _NOW).create(active))
    return archive.path, snapshot.path


def test_help_is_available() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Werewolf Arena" in result.stdout


def test_version_is_available() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


def test_json_report_is_ascii_safe_and_preserves_unicode_values() -> None:
    report = {"public": "中文发言 🐺", "summary": {"winner": "村民"}}

    serialized = json_report(report)

    serialized.encode("ascii")
    assert json.loads(serialized) == report


def test_config_validate_accepts_v1_configuration() -> None:
    result = runner.invoke(app, ["config", "validate", "config/game.example.yaml"])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["status"] == "valid"
    assert report["board"]["id"] == "classic_12_seer_witch_hunter_idiot"


def test_config_validate_rejects_invalid_configuration(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: 2\n", encoding="utf-8")

    result = runner.invoke(app, ["config", "validate", str(path)])

    assert result.exit_code == 1
    assert "schema_version" in result.stderr


def test_rules_validate_accepts_candidate_board() -> None:
    path = (
        Path("vault")
        / "_workbench"
        / "official_12_20260928"
        / "draft"
        / "boards"
        / "classic_12_seer_witch_hunter_idiot"
        / "1.0.0"
        / "board.md"
    )

    result = runner.invoke(app, ["rules", "validate", str(path)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["dependency_closure"] == "validated"


def test_play_init_builds_experimental_setup(tmp_path: Path) -> None:
    output = tmp_path / "classic-play"
    result = runner.invoke(
        app,
        ["play", "init", "--output", str(output), "--pi-seats", "1,2,3"],
    )

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["status"] == "EXPERIMENTAL_CANDIDATE"
    assert report["pi_seats"] == [1, 2, 3]
    assert (output / "game.yaml").is_file()
    assert (output / "preview" / "compiled").is_dir()


def test_archive_verify_rejects_missing_manifest(tmp_path: Path) -> None:
    result = runner.invoke(app, ["archive", "verify", str(tmp_path)])

    assert result.exit_code == 1
    assert "archive verification failed" in result.stderr


def test_archive_verify_accepts_archive_manifest_and_strict_inventory(tmp_path: Path) -> None:
    archive_path, snapshot_path = _make_archive(tmp_path)

    result = runner.invoke(app, ["archive", "verify", str(archive_path)])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["kind"] == "archive"
    assert report["archive_id"] == archive_path.name
    assert (
        report["snapshot_id"]
        == json.loads((snapshot_path / "snapshot_manifest.json").read_text(encoding="utf-8"))[
            "snapshot_id"
        ]
    )

    (archive_path / "public.md").write_text("tampered\n", encoding="utf-8")
    corrupted = runner.invoke(app, ["archive", "verify", str(archive_path)])

    assert corrupted.exit_code == 1
    assert "archive verification failed" in corrupted.stderr
    assert "hash mismatch" in corrupted.stderr


def test_archive_verify_keeps_snapshot_directory_compatibility(tmp_path: Path) -> None:
    _, snapshot_path = _make_archive(tmp_path)

    result = runner.invoke(app, ["archive", "verify", str(snapshot_path)])

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert (
        report["snapshot_id"]
        == json.loads((snapshot_path / "snapshot_manifest.json").read_text(encoding="utf-8"))[
            "snapshot_id"
        ]
    )


def test_pi_doctor_reports_unresolved_executable(monkeypatch) -> None:
    monkeypatch.delenv("WEREWOLF_PI_EXECUTABLE", raising=False)
    monkeypatch.setattr("werewolf.cli_support.diagnostics.shutil.which", lambda _: None)

    result = runner.invoke(app, ["pi", "doctor"])

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["status"] == "failed"
    assert report["credentials"] == "redacted"
