"""Integration coverage for complete-cycle game snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionRequest,
    ActionWindow,
    DeliveryCursor,
    EventType,
    GameEvent,
    GameState,
    GmAuditPayload,
    PlayerState,
    PrivateRolePayload,
    PublicAnnouncementPayload,
    RulesetRef,
    TeamSpeechPayload,
)
from werewolf.persistence import (
    CorruptSnapshotError,
    FrozenRulesetSnapshot,
    GameSnapshotStore,
    SnapshotBoundaryError,
    SnapshotSecurityError,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _ruleset() -> RulesetRef:
    return RulesetRef(
        board_id="classic-12",
        version="1.0.0",
        snapshot_id="snapshot-rules",
        manifest_sha256="a" * 64,
    )


def _state(*, events: tuple[GameEvent, ...] = ()) -> GameState:
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.VICTORY_CHECK,
        round_no=1,
        ruleset=_ruleset(),
        players={
            1: PlayerState(seat=1, role_id="wolf", faction_id="wolves", runtime_ref="pi-1"),
            2: PlayerState(seat=2, role_id="seer", faction_id="village", runtime_ref="pi-2"),
        },
        events=events,
    )


def _events() -> tuple[GameEvent, ...]:
    return (
        GameEvent.public(
            event_id=1,
            game_id="game-1",
            state_revision=3,
            round_no=1,
            phase=GamePhase.DAY_ANNOUNCE,
            created_at=NOW,
            event_type=EventType.ANNOUNCEMENT,
            eligible_seats=(1, 2),
            payload=PublicAnnouncementPayload(content="白天开始"),
        ),
        GameEvent.team(
            event_id=2,
            game_id="game-1",
            state_revision=3,
            round_no=1,
            phase=GamePhase.NIGHT_TEAM_CHAT,
            created_at=NOW,
            event_type=EventType.TEAM_SPEECH,
            authorized_seats=(1,),
            payload=TeamSpeechPayload(speaker_seat=1, content="wolf-secret"),
        ),
        GameEvent.private(
            event_id=3,
            game_id="game-1",
            state_revision=3,
            round_no=1,
            phase=GamePhase.NIGHT_ACTION,
            created_at=NOW,
            event_type=EventType.ROLE_ASSIGNMENT,
            seat=2,
            payload=PrivateRolePayload(role_id="seer", faction_id="village"),
        ),
        GameEvent.gm_only(
            event_id=4,
            game_id="game-1",
            state_revision=3,
            round_no=1,
            phase=GamePhase.NIGHT_RESOLVE,
            created_at=NOW,
            event_type=EventType.GM_AUDIT,
            payload=GmAuditPayload(code="resolution", details={"target": 2}),
        ),
    )


def _action_window(*, closed_at: datetime | None) -> dict[str, object]:
    return ActionWindow(
        window_id="night-window",
        game_id="game-1",
        session_epoch=0,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1,),
        allowed_action_codes=(102,),
        opened_at=NOW,
        closed_at=closed_at,
    ).model_dump(mode="json")


def _action_request(*, status: str) -> dict[str, object]:
    request = ActionRequest(
        request_id="request-1",
        game_id="game-1",
        window_id="night-window",
        seat=1,
        session_epoch=0,
        actions=(Action(action_code=102, targets=(2,)),),
        phase=GamePhase.NIGHT_ACTION,
    ).model_dump(mode="json")
    request["status"] = status
    request["resolution_id"] = "resolution-1"
    return request


@pytest.mark.asyncio
async def test_snapshot_projects_channels_and_verifies_manifest(tmp_path: Path) -> None:
    state = _state(events=_events())
    store = GameSnapshotStore(tmp_path / "active")

    result = await store.create(state, created_at=NOW)

    assert result.path.name == "20260928T120000Z_round_001"
    assert result.path.joinpath("state.json").is_file()
    public = result.path.joinpath("public.md").read_text(encoding="utf-8")
    team = result.path.joinpath("private", "channels", "wolves.md").read_text(encoding="utf-8")
    gm = result.path.joinpath("private", "gm.md").read_text(encoding="utf-8")
    assert "白天开始" in public
    assert "wolf-secret" not in public
    assert "team_speech" not in public
    assert "role_assignment" not in public
    assert "wolf-secret" in team
    assert "role=wolf" in gm
    assert "role_assignment" in gm

    manifest = await store.verify(result.path.name)
    raw = json.loads(result.path.joinpath("snapshot_manifest.json").read_text(encoding="utf-8"))
    assert manifest.manifest_sha256 == result.manifest_sha256
    assert raw["snapshot_id"] == result.snapshot_id
    assert all(item["sha256"] for item in raw["files"])
    assert state.last_snapshot is None
    assert (
        json.loads((tmp_path / "active" / "state.json").read_text(encoding="utf-8"))[
            "last_snapshot"
        ]["snapshot_id"]
        == result.snapshot_id
    )


@pytest.mark.asyncio
async def test_snapshot_copies_and_verifies_real_ruleset_files(tmp_path: Path) -> None:
    state = _state()
    ruleset = FrozenRulesetSnapshot.from_ref(
        _ruleset(),
        files={
            "board.md": b"# Classic 12\n",
            "roles/seer.md": b"# Seer\n",
        },
    )
    store = GameSnapshotStore(tmp_path / "active")

    result = await store.create(state, ruleset=ruleset, created_at=NOW)

    assert result.path.joinpath("private", "ruleset", "files", "board.md").read_bytes() == (
        b"# Classic 12\n"
    )
    manifest = await store.verify(result.path.name)
    assert dict(manifest.ruleset.file_digests or {}) == dict(ruleset.file_digests or {})

    result.path.joinpath("private", "ruleset", "files", "board.md").write_bytes(b"# Tampered\n")
    with pytest.raises(CorruptSnapshotError, match="hash mismatch|digest mismatch"):
        await store.verify(result.path.name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [GamePhase.CREATED, GamePhase.RULESET_READY, GamePhase.NIGHT_TEAM_CHAT, GamePhase.DAY_RESOLVE],
)
async def test_snapshot_rejects_non_cycle_boundary_phases(tmp_path: Path, phase: GamePhase) -> None:
    state = _state().model_copy(update={"phase": phase})
    store = GameSnapshotStore(tmp_path / "active")

    with pytest.raises(SnapshotBoundaryError, match="VICTORY_CHECK or FINISHED"):
        await store.create(state, created_at=NOW)


@pytest.mark.asyncio
async def test_snapshot_allows_finished_boundary(tmp_path: Path) -> None:
    state = _state().model_copy(update={"phase": GamePhase.FINISHED})
    store = GameSnapshotStore(tmp_path / "active")

    result = await store.create(state, created_at=NOW)

    assert result.manifest.round_no == state.round_no


@pytest.mark.asyncio
async def test_snapshot_failure_leaves_no_staging_directory_or_active_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    store = GameSnapshotStore(tmp_path / "active")
    original = __import__("werewolf.persistence.snapshot", fromlist=["atomic_write_bytes"])
    original_write = original.atomic_write_bytes

    async def fail_write(path: object, data: bytes) -> None:
        if str(path).endswith("snapshot_manifest.json"):
            raise OSError("simulated disk failure")
        await original_write(path, data)

    monkeypatch.setattr(original, "atomic_write_bytes", fail_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        await store.create(state, created_at=NOW)

    snapshots = tmp_path / "active" / "snapshots"
    assert list(snapshots.iterdir()) == []
    assert not (tmp_path / "active" / "state.json").exists()
    assert not (tmp_path / "active" / "public.md").exists()


@pytest.mark.asyncio
async def test_snapshot_rejects_open_boundary_and_secret_fields(tmp_path: Path) -> None:
    store = GameSnapshotStore(tmp_path / "active")
    open_state = _state().model_copy(update={"pending_resolution": {"token": "secret"}})
    with pytest.raises(SnapshotBoundaryError):
        await store.create(open_state, created_at=NOW)

    secret_state = _state().model_copy(update={"moderator_audit": ({"api_key": "secret"},)})
    with pytest.raises(SnapshotSecurityError):
        await store.create(secret_state, created_at=NOW)


@pytest.mark.asyncio
async def test_snapshot_rejects_a_real_open_action_window(tmp_path: Path) -> None:
    state = _state().model_copy(
        update={"action_windows": {"night-window": _action_window(closed_at=None)}}
    )
    store = GameSnapshotStore(tmp_path / "active")

    with pytest.raises(SnapshotBoundaryError, match="action window .* still active"):
        await store.create(state, created_at=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PENDING", "OPEN"])
async def test_snapshot_rejects_unresolved_real_action_requests(
    tmp_path: Path,
    status: str,
) -> None:
    state = _state().model_copy(
        update={
            "action_windows": {
                "night-window": _action_window(closed_at=NOW),
            },
            "action_requests": {
                "request-1": _action_request(status=status),
            },
        }
    )
    store = GameSnapshotStore(tmp_path / "active")

    with pytest.raises(SnapshotBoundaryError, match="action request .* still active"):
        await store.create(state, created_at=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["CONFIRMED", "OVERRIDDEN", "CANCELLED"])
async def test_snapshot_allows_closed_window_with_resolved_historical_request(
    tmp_path: Path,
    status: str,
) -> None:
    state = _state().model_copy(
        update={
            "action_windows": {
                "night-window": _action_window(closed_at=NOW),
            },
            "action_requests": {
                "request-1": _action_request(status=status),
            },
        }
    )
    store = GameSnapshotStore(tmp_path / "active")

    result = await store.create(state, created_at=NOW)

    assert result.path.joinpath("state.json").is_file()


@pytest.mark.asyncio
async def test_snapshot_allows_completed_sheriff_badge_request_with_common_terminal_status(
    tmp_path: Path,
) -> None:
    badge_window = ActionWindow(
        window_id="sheriff-badge-r1-d1-s1",
        game_id="game-1",
        session_epoch=0,
        phase=GamePhase.DAY_ANNOUNCE,
        allowed_seats=(1,),
        allowed_action_codes=(201, 202),
        opened_at=NOW,
        closed_at=NOW,
        visible_context={
            "kind": "sheriff_badge",
            "source_seat": 1,
            "candidate_seats": [2],
        },
    )
    request = ActionRequest(
        request_id="badge-request-1",
        game_id="game-1",
        window_id=badge_window.window_id,
        seat=1,
        session_epoch=0,
        actions=(Action(action_code=201, targets=(2,)),),
        phase=GamePhase.DAY_ANNOUNCE,
    ).model_dump(mode="json")
    request.update(
        {
            "status": "CONFIRMED",
            "resolution_kind": "SHERIFF_BADGE",
            "resolved_action_code": 201,
            "resolved_target_seat": 2,
        }
    )
    state = _state().model_copy(
        update={
            "action_windows": {badge_window.window_id: badge_window.model_dump(mode="json")},
            "action_requests": {request["request_id"]: request},
            "sheriff_seat": 2,
            "sheriff_badge": {
                "status": "COMPLETE",
                "source_seat": 1,
                "target_seat": 2,
                "window_id": badge_window.window_id,
                "request_id": request["request_id"],
                "action_code": 201,
            },
        }
    )
    store = GameSnapshotStore(tmp_path / "active")

    result = await store.create(state, created_at=NOW)

    persisted = json.loads(result.path.joinpath("state.json").read_text(encoding="utf-8"))
    assert persisted["action_requests"]["badge-request-1"]["status"] == "CONFIRMED"
    assert persisted["action_requests"]["badge-request-1"]["resolution_kind"] == "SHERIFF_BADGE"


@pytest.mark.asyncio
async def test_snapshot_thaws_frozen_nested_action_window_context(tmp_path: Path) -> None:
    raw_window = _action_window(closed_at=NOW)
    raw_window["visible_context"] = {
        "depends_on": (),
        "candidate_seats": (1, 2),
    }
    state = _state().model_copy(update={"action_windows": {"night-window": raw_window}})
    store = GameSnapshotStore(tmp_path / "active")

    result = await store.create(state, created_at=NOW)

    persisted = json.loads(result.path.joinpath("state.json").read_text(encoding="utf-8"))
    assert persisted["action_windows"]["night-window"]["visible_context"] == {
        "depends_on": [],
        "candidate_seats": [1, 2],
    }
    await store.verify(result.path.name)


@pytest.mark.asyncio
async def test_snapshot_rejects_corrupt_nested_action_window_context(tmp_path: Path) -> None:
    raw_window = _action_window(closed_at=NOW)
    raw_window["visible_context"] = {"candidate_seats": object()}
    state = _state().model_copy(update={"action_windows": {"night-window": raw_window}})
    store = GameSnapshotStore(tmp_path / "active")

    with pytest.raises(SnapshotBoundaryError, match="action window .* malformed"):
        await store.create(state, created_at=NOW)


@pytest.mark.asyncio
async def test_snapshot_rejects_active_player_request_binding(tmp_path: Path) -> None:
    state = _state().model_copy(
        update={
            "action_windows": {"night-window": _action_window(closed_at=NOW)},
            "action_requests": {"request-1": _action_request(status="CONFIRMED")},
            "players": {
                **_state().players,
                1: _state().players[1].model_copy(update={"current_request_id": "request-1"}),
            },
        }
    )
    store = GameSnapshotStore(tmp_path / "active")

    with pytest.raises(SnapshotBoundaryError, match="seat 1 .* active action request"):
        await store.create(state, created_at=NOW)


@pytest.mark.asyncio
async def test_snapshot_rejects_in_flight_delivery_cursor(tmp_path: Path) -> None:
    cursor = DeliveryCursor(session_epoch=0).with_in_flight("delivery-1", (1,))
    state = _state().model_copy(
        update={
            "action_windows": {"night-window": _action_window(closed_at=NOW)},
            "action_requests": {"request-1": _action_request(status="CONFIRMED")},
            "delivery_cursors": {1: cursor},
        }
    )
    store = GameSnapshotStore(tmp_path / "active")

    with pytest.raises(SnapshotBoundaryError, match="seat 1 .* in-flight delivery cursor"):
        await store.create(state, created_at=NOW)
