"""Integration coverage for immutable end-of-game archives."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import GameState, PlayerState, RulesetRef
from werewolf.persistence import (
    ArchiveInputError,
    ArchiveSecurityError,
    CorruptArchiveError,
    GameArchiveStore,
    GameSnapshotStore,
)
from werewolf.persistence.archive import _source_files

NOW = datetime(2026, 9, 28, 18, 30, 12, tzinfo=UTC)


def _ruleset() -> RulesetRef:
    return RulesetRef(
        board_id="classic-12",
        version="1.0.0",
        snapshot_id="snapshot-rules",
        manifest_sha256="a" * 64,
    )


def _state() -> GameState:
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.FINISHED,
        round_no=2,
        ruleset=_ruleset(),
        players={
            1: PlayerState(seat=1, role_id="wolf", faction_id="wolves", runtime_ref="pi-1"),
            2: PlayerState(seat=2, role_id="seer", faction_id="village", runtime_ref="pi-2"),
        },
    )


async def _make_snapshot(tmp_path: Path):
    active = tmp_path / "games" / "active" / "game-1"
    snapshot = await GameSnapshotStore(active).create(_state(), created_at=NOW)
    return active, snapshot


@pytest.mark.asyncio
async def test_archive_copies_verified_game_and_keeps_active_directory(tmp_path: Path) -> None:
    active, snapshot = await _make_snapshot(tmp_path)
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)

    result = await store.create(active)

    assert result.path.name == "20260928T183012Z_game-1"
    assert result.path.joinpath("state.json").is_file()
    assert result.path.joinpath("snapshots", snapshot.path.name).is_dir()
    assert active.is_dir()
    manifest = await store.verify(result.path)
    assert manifest.final_snapshot_id == snapshot.snapshot_id
    assert manifest.file_count == len(manifest.files)
    assert manifest.total_size == sum(item.size for item in manifest.files)


@pytest.mark.asyncio
async def test_archive_verify_rejects_tampering_and_missing_files(tmp_path: Path) -> None:
    active, _ = await _make_snapshot(tmp_path)
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)
    result = await store.create(active)

    result.path.joinpath("public.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(CorruptArchiveError, match="hash mismatch"):
        await store.verify(result.path)
    result.path.joinpath("public.md").unlink()
    with pytest.raises(CorruptArchiveError, match="file set is incomplete"):
        await store.verify(result.path)


@pytest.mark.asyncio
async def test_archive_requires_a_verified_final_snapshot(tmp_path: Path) -> None:
    active, snapshot = await _make_snapshot(tmp_path)
    snapshot.path.joinpath("snapshot_manifest.json").unlink()
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)

    with pytest.raises(Exception, match="final snapshot"):
        await store.create(active, snapshot.path)
    assert not (tmp_path / "games" / "archive").exists()


@pytest.mark.asyncio
async def test_archive_failure_cleans_staging_and_rejects_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, _ = await _make_snapshot(tmp_path)
    active.joinpath("config.json").write_text('{"api_key":"secret"}\n', encoding="utf-8")
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)
    with pytest.raises(ArchiveSecurityError):
        await store.create(active)
    archive_root = tmp_path / "games" / "archive"
    assert not list(archive_root.glob(".staging-*")) if archive_root.exists() else True

    active.joinpath("config.json").unlink()
    original = __import__("werewolf.persistence.archive", fromlist=["atomic_write_bytes"])
    original_write = original.atomic_write_bytes

    async def fail_manifest(path: object, data: bytes) -> None:
        if str(path).endswith("archive_manifest.json"):
            raise OSError("simulated disk failure")
        await original_write(path, data)

    monkeypatch.setattr(original, "atomic_write_bytes", fail_manifest)
    with pytest.raises(OSError, match="simulated disk failure"):
        await store.create(active)
    assert not list(archive_root.glob(".staging-*"))
    assert not list(archive_root.glob("*game-1"))


@pytest.mark.asyncio
async def test_archive_excludes_live_runtime_tree_and_keeps_manifest_clean(tmp_path: Path) -> None:
    active, _ = await _make_snapshot(tmp_path)
    runtime_dir = active / ".runtime" / "players" / "session"
    runtime_dir.mkdir(parents=True)
    runtime_dir.joinpath("dummysecretmarker.txt").write_text(
        "raw Pi model history and identity prompt marker\n", encoding="utf-8"
    )
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)

    result = await store.create(active)
    manifest = await store.verify(result.path)

    assert all(not item.relative_path.startswith(".runtime/") for item in manifest.files)
    assert not result.path.joinpath(".runtime").exists()
    assert all("dummysecretmarker" not in item.relative_path for item in manifest.files)


@pytest.mark.asyncio
async def test_archive_rejects_runtime_file_and_active_root_symlink(tmp_path: Path) -> None:
    active, _ = await _make_snapshot(tmp_path)
    runtime_path = active / ".runtime"
    runtime_path.write_text("not a directory\n", encoding="utf-8")
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)

    with pytest.raises(ArchiveSecurityError, match="excluded path"):
        await store.create(active)

    runtime_path.unlink()
    active_link = tmp_path / "active-link"
    try:
        active_link.symlink_to(active, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(ArchiveInputError, match="real directory"):
        _source_files(active_link)


@pytest.mark.asyncio
async def test_archive_requires_active_public_projection_from_final_snapshot(
    tmp_path: Path,
) -> None:
    active, _ = await _make_snapshot(tmp_path)
    active.joinpath("public.md").write_text("# forged public history\n", encoding="utf-8")
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)

    with pytest.raises(CorruptArchiveError, match="public.md"):
        await store.create(active)


@pytest.mark.asyncio
async def test_archive_requires_runtime_refs_and_rules_to_match_final_snapshot(
    tmp_path: Path,
) -> None:
    active, snapshot = await _make_snapshot(tmp_path)
    private = active / "private"
    private.mkdir()
    private.joinpath("runtime_refs.json").write_text("[]\n", encoding="utf-8")
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)

    with pytest.raises(CorruptArchiveError, match="runtime_refs"):
        await store.create(active)

    private.joinpath("runtime_refs.json").unlink()
    state_path = active / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["ruleset"]["manifest_sha256"] = "b" * 64
    state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    with pytest.raises(CorruptArchiveError, match="rules|snapshot"):
        await store.create(active, snapshot.path)


@pytest.mark.asyncio
async def test_recovery_requires_live_session_proof_and_checks_epochs(tmp_path: Path) -> None:
    active, _ = await _make_snapshot(tmp_path)
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)
    result = await store.create(active)

    rebuild = await store.assess_recovery(result.path)
    assert rebuild.decision == "rebuild-sessions"
    probe = {
        1: {"runtime_ref": "pi-1", "session_epoch": 0, "last_task_ref": None},
        2: {"runtime_ref": "pi-2", "session_epoch": 0, "last_task_ref": None},
    }
    assert (await store.assess_recovery(result.path, session_probe=probe)).decision == "resume"
    probe[1]["session_epoch"] = 99
    assert (
        await store.assess_recovery(result.path, session_probe=probe)
    ).decision == "rebuild-sessions"


@pytest.mark.asyncio
async def test_recovery_abandons_when_persisted_runtime_refs_are_corrupt(tmp_path: Path) -> None:
    active, _ = await _make_snapshot(tmp_path)
    store = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)
    result = await store.create(active)
    refs = result.path.joinpath(
        "snapshots",
        next(p.name for p in result.path.joinpath("snapshots").iterdir()),
        "private",
        "runtime_refs.json",
    )
    payload = json.loads(refs.read_text(encoding="utf-8"))
    payload[0]["session_epoch"] = 99
    refs.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(CorruptArchiveError):
        await store.verify(result.path)
