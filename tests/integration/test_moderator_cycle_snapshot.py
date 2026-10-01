"""Integration coverage for the automatic victory-cycle snapshot boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import GameManager, GameState, PlayerState, RulesetRef, load_action_registry
from werewolf.knowledge.board import BoardDefinition, VictoryDefinition
from werewolf.persistence import GameSnapshotStore, SnapshotBoundaryError

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _board(*conditions: str) -> BoardDefinition:
    return BoardDefinition.model_construct(
        board_id="classic-12",
        version="1.0.0",
        status="published",
        reviewed_by="human-reviewer",
        factions={"wolf": 1, "good": 1},
        victory=VictoryDefinition(
            mode="eliminate_side",
            winning_sides=["good", "wolf"],
            special_conditions=list(conditions),
        ),
    )


def _state(*, wolf_alive: bool = True) -> GameState:
    return GameState(
        game_id="cycle-snapshot-test",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.VICTORY_CHECK,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="ruleset-" + "a" * 64,
            manifest_sha256="b" * 64,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id="wolf",
                faction_id="wolf",
                alive=wolf_alive,
                runtime_ref="pi-1",
            ),
            2: PlayerState(
                seat=2,
                role_id="villager",
                faction_id="good",
                runtime_ref="pi-2",
            ),
        },
    )


async def _writer(store: GameSnapshotStore, state: GameState) -> dict[str, object]:
    snapshot = await store.create(state, created_at=NOW)
    return {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_revision": snapshot.manifest.snapshot_revision,
        "state_revision": snapshot.manifest.state_revision,
        "created_at": snapshot.manifest.created_at,
        "manifest_sha256": snapshot.manifest.manifest_sha256,
    }


@pytest.mark.asyncio
async def test_ongoing_victory_publishes_completed_night_boundary_and_syncs_reference(
    tmp_path: Path,
) -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    store = GameSnapshotStore(tmp_path / "active")

    committed = await manager.commit_victory_check(
        _board("good_wins_when_all_wolves_are_dead"),
        expected_revision=0,
        now=NOW,
        snapshot_writer=lambda candidate: _writer(store, candidate),
    )

    assert committed.phase is GamePhase.NIGHT_TEAM_CHAT
    assert committed.winner is None
    assert committed.last_snapshot is not None
    active = json.loads((tmp_path / "active" / "state.json").read_text(encoding="utf-8"))
    assert active["last_snapshot"] == committed.last_snapshot
    snapshot_id = committed.last_snapshot["snapshot_id"]
    snapshot_path = tmp_path / "active" / "snapshots"
    snapshot_dirs = list(snapshot_path.glob("*_round_001"))
    assert len(snapshot_dirs) == 1
    snapshot_state = json.loads((snapshot_dirs[0] / "state.json").read_text(encoding="utf-8"))
    assert snapshot_state["phase"] == GamePhase.NIGHT_TEAM_CHAT.value
    assert snapshot_state["moderator_audit"][-1]["operation"] == "VICTORY_CHECK"
    assert snapshot_state["last_snapshot"] is None
    assert (
        snapshot_id
        == json.loads((snapshot_dirs[0] / "snapshot_manifest.json").read_text(encoding="utf-8"))[
            "snapshot_id"
        ]
    )


@pytest.mark.asyncio
async def test_finished_victory_is_snapshotted_with_winner(tmp_path: Path) -> None:
    manager = GameManager(_state(wolf_alive=False), registry=load_action_registry())
    store = GameSnapshotStore(tmp_path / "active")

    committed = await manager.commit_victory_check(
        _board("good_wins_when_all_wolves_are_dead"),
        expected_revision=0,
        now=NOW,
        snapshot_writer=lambda candidate: _writer(store, candidate),
    )

    assert committed.phase is GamePhase.FINISHED
    assert committed.winner is not None
    assert committed.winner["side"] == "good"
    assert committed.last_snapshot is not None
    assert list((tmp_path / "active" / "snapshots").glob("*_round_001"))


@pytest.mark.asyncio
async def test_failed_victory_snapshot_keeps_boundary_retryable(tmp_path: Path) -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    attempts = 0

    async def fail_once(state: GameState) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated disk failure")
        return await _writer(GameSnapshotStore(tmp_path / "active"), state)

    with pytest.raises(ValueError, match="VICTORY_SNAPSHOT_FAILED"):
        await manager.commit_victory_check(
            _board("good_wins_when_all_wolves_are_dead"),
            expected_revision=0,
            now=NOW,
            snapshot_writer=fail_once,
        )
    assert manager.state.phase is GamePhase.VICTORY_CHECK
    assert manager.state.state_revision == 0
    assert manager.state.last_snapshot is None

    committed = await manager.commit_victory_check(
        _board("good_wins_when_all_wolves_are_dead"),
        expected_revision=0,
        now=NOW,
        snapshot_writer=fail_once,
    )
    assert committed.phase is GamePhase.NIGHT_TEAM_CHAT
    assert committed.last_snapshot is not None


@pytest.mark.asyncio
async def test_active_projection_failure_removes_published_attempt_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import werewolf.persistence.snapshot as snapshot_module

    state = _state()
    store = GameSnapshotStore(tmp_path / "active")
    original_write = snapshot_module.atomic_write_bytes
    active_public = (tmp_path / "active" / "public.md").resolve()
    failed = False

    async def fail_active_public(path: object, data: bytes) -> None:
        nonlocal failed
        if not failed and Path(path).resolve() == active_public:
            failed = True
            raise OSError("simulated active projection failure")
        await original_write(path, data)

    monkeypatch.setattr(snapshot_module, "atomic_write_bytes", fail_active_public)
    with pytest.raises(OSError, match="active projection"):
        await store.create(state, created_at=NOW)
    assert not list((tmp_path / "active" / "snapshots").glob("*_round_001"))
    assert not (tmp_path / "active" / "state.json").exists()
    assert not (tmp_path / "active" / "public.md").exists()

    monkeypatch.setattr(snapshot_module, "atomic_write_bytes", original_write)
    retried = await store.create(state, created_at=NOW)
    assert retried.snapshot_id


@pytest.mark.asyncio
async def test_arbitrary_night_team_chat_cannot_be_saved(tmp_path: Path) -> None:
    state = _state().model_copy(update={"phase": GamePhase.NIGHT_TEAM_CHAT})
    with pytest.raises(SnapshotBoundaryError, match="completed victory check"):
        await GameSnapshotStore(tmp_path / "active").create(state, created_at=NOW)


@pytest.mark.asyncio
async def test_public_projection_keeps_team_and_role_data_private(tmp_path: Path) -> None:
    state = _state().model_copy(
        update={
            "moderator_audit": (
                {
                    "operation": "VICTORY_CHECK",
                    "status": "ONGOING",
                    "winner": None,
                    "committed_revision": 0,
                },
            ),
            "phase": GamePhase.NIGHT_TEAM_CHAT,
        }
    )
    snapshot = await GameSnapshotStore(tmp_path / "active").create(state, created_at=NOW)
    public = snapshot.path.joinpath("public.md").read_text(encoding="utf-8")
    assert "role_id" not in public
    assert "faction_id" not in public
    assert "runtime_ref" not in public
