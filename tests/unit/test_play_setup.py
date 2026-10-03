"""Focused checks for the experimental classic playable setup builder."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from werewolf.cli_support.play_setup import PlaySetupError, build_play_setup
from werewolf.knowledge.preview import experimental_preview
from werewolf.knowledge.runtime_loader import load_runtime_knowledge_bundle_from_snapshot
from werewolf.moderator import ModeratorShell

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_builds_isolated_scripted_preview_and_config(tmp_path: Path) -> None:
    output = tmp_path / "play"
    compiled_root = PROJECT_ROOT / "vault" / "compiled"
    before_compiled = {
        path.relative_to(compiled_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in compiled_root.rglob("*")
        if path.is_file()
    }

    report = build_play_setup(output, all_scripted=True, project_root=PROJECT_ROOT)

    assert report["status"] == "EXPERIMENTAL_CANDIDATE"
    assert report["candidate_status"] == "CANDIDATE_PENDING_HUMAN_REVIEW"
    assert report["source_files_verified"] == 13
    assert report["action_contract"] == "validated"
    assert (output / "preview" / "compiled").is_dir()
    after_compiled = {
        path.relative_to(compiled_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in compiled_root.rglob("*")
        if path.is_file()
    }
    assert after_compiled == before_compiled

    config = yaml.safe_load((output / "game.yaml").read_text(encoding="utf-8"))
    assert config["paths"] == {"compiled_root": "preview/compiled", "games_root": "games"}
    assert len(config["players"]) == 12
    assert {player["runtime"] for player in config["players"]} == {"scripted"}
    persisted = json.loads((output / "setup-report.json").read_text(encoding="utf-8"))
    assert persisted["source_manifest_sha256"] == report["source_manifest_sha256"]


def test_builds_three_pi_seat_mixed_config(tmp_path: Path) -> None:
    output = tmp_path / "mixed"

    report = build_play_setup(
        output,
        pi_seats=(1, 3, 7),
        provider="github-copilot",
        model="gpt-6-luna",
        project_root=PROJECT_ROOT,
    )

    assert report["pi_seats"] == [1, 3, 7]
    assert "--experimental-preview" in report["launch"]
    assert f'"{output / "game.yaml"}"' in report["launch"]
    config = yaml.safe_load((output / "game.yaml").read_text(encoding="utf-8"))
    assert [player["seat"] for player in config["players"] if player["runtime"] == "pi"] == [
        1,
        3,
        7,
    ]
    assert all(
        player["provider"] == "github-copilot" and player["model"] == "gpt-6-luna"
        for player in config["players"]
        if player["runtime"] == "pi"
    )


def test_rejects_existing_output_and_does_not_replace_it(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(PlaySetupError, match="already exists"):
        build_play_setup(output, all_scripted=True, project_root=PROJECT_ROOT)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_rejects_duplicate_pi_seats(tmp_path: Path) -> None:
    with pytest.raises(PlaySetupError, match="duplicates"):
        build_play_setup(tmp_path / "duplicate", pi_seats=(1, 1), project_root=PROJECT_ROOT)


def test_manifest_tampering_fails_without_partial_output(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    source = PROJECT_ROOT / "vault" / "_workbench" / "official_12_20260928"
    target = root / "vault" / "_workbench" / "official_12_20260928"
    shutil.copytree(source, target)
    board = (
        target / "draft" / "boards" / "classic_12_seer_witch_hunter_idiot" / "1.0.0" / "board.md"
    )
    board.write_text(board.read_text(encoding="utf-8") + "\n tampered\n", encoding="utf-8")

    output = tmp_path / "broken"
    with pytest.raises(PlaySetupError, match="hash mismatch"):
        build_play_setup(output, all_scripted=True, project_root=root)
    assert not output.exists()


@pytest.mark.asyncio
async def test_candidate_runtime_bundle_requires_explicit_preview_scope(tmp_path: Path) -> None:
    output = tmp_path / "runtime"
    await asyncio.to_thread(
        build_play_setup,
        output,
        all_scripted=True,
        project_root=PROJECT_ROOT,
    )
    shell = ModeratorShell(output / "game.yaml")
    await shell.new()
    assert shell.ruleset_snapshot is not None

    with pytest.raises(ValueError, match="awaiting human review"):
        await load_runtime_knowledge_bundle_from_snapshot(shell.ruleset_snapshot)
    with experimental_preview():
        bundle = await load_runtime_knowledge_bundle_from_snapshot(shell.ruleset_snapshot)
    assert bundle.board.reviewed_by == "pending-human-review"
