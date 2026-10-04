"""Public custom rule disclosures must remain typed and visibility-safe in snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_persistence_snapshot import NOW, _events, _state

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    EventType,
    GameEvent,
    GameState,
    PrivateSeerResultPayload,
    PublicAnnouncementPayload,
)
from werewolf.persistence import (
    CorruptArchiveError,
    GameArchiveStore,
    GameSnapshotStore,
    SnapshotSecurityError,
)


def _identity_disclosure(*, audience: tuple[int, ...] = (1, 2)) -> GameEvent:
    return GameEvent.public(
        event_id=5,
        game_id="game-1",
        state_revision=3,
        round_no=1,
        phase=GamePhase.DAY_ANNOUNCE,
        created_at=NOW,
        event_type=EventType("idiot_revealed"),
        eligible_seats=audience,
        payload=PublicAnnouncementPayload(content="玩家2的白痴身份已经公开。"),
    )


@pytest.mark.asyncio
async def test_snapshot_preserves_typed_custom_public_disclosure_and_archive_projection(
    tmp_path: Path,
) -> None:
    active = tmp_path / "games" / "active" / "game-1"
    state = _state(events=(*_events(), _identity_disclosure()))
    snapshots = GameSnapshotStore(active)

    result = await snapshots.create(state, created_at=NOW)

    public = result.path.joinpath("public.md").read_text(encoding="utf-8")
    assert "idiot_revealed" in public
    assert "玩家2的白痴身份已经公开。" in public
    assert "wolf-secret" not in public
    assert "role_assignment" not in public
    assert (await snapshots.verify(result.path.name)).snapshot_id == result.snapshot_id
    assert (await snapshots.load(result.path.name)).snapshot_id == result.snapshot_id

    restored = GameState.model_validate_json(result.path.joinpath("state.json").read_bytes())
    restored_event = GameEvent.model_validate_json(json.dumps(restored.events[-1]))
    assert restored_event.event_type.value == "idiot_revealed"
    assert isinstance(restored_event.payload, PublicAnnouncementPayload)

    archives = GameArchiveStore(tmp_path / "games", clock=lambda: NOW)
    archive = await archives.create(active)
    archived_public = archive.path.joinpath("public.md").read_text(encoding="utf-8")
    assert "玩家2的白痴身份已经公开。" in archived_public
    assert "wolf-secret" not in archived_public
    assert "role_assignment" not in archived_public
    await archives.verify(archive.path)


@pytest.mark.asyncio
async def test_snapshot_rejects_private_payload_forged_as_public_and_blocks_archive(
    tmp_path: Path,
) -> None:
    active = tmp_path / "games" / "active" / "game-1"
    forged = _identity_disclosure().model_copy(
        update={"payload": PrivateSeerResultPayload(target_seat=2, faction_id="wolves")}
    )
    store = GameSnapshotStore(active)

    with pytest.raises(SnapshotSecurityError, match="authorization validation"):
        await store.create(_state().model_copy(update={"events": (forged,)}), created_at=NOW)

    assert not (active / "public.md").exists()
    with pytest.raises(CorruptArchiveError, match="state.json"):
        await GameArchiveStore(tmp_path / "games", clock=lambda: NOW).create(active)


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", [(2, 1), (1, 64)])
async def test_snapshot_rejects_public_events_with_invalid_or_unassigned_audience(
    tmp_path: Path,
    audience: tuple[int, ...],
) -> None:
    forged = _identity_disclosure().model_copy(update={"audience": audience})
    store = GameSnapshotStore(tmp_path / "active")

    with pytest.raises(SnapshotSecurityError, match="authorization validation|audience"):
        await store.create(_state().model_copy(update={"events": (forged,)}), created_at=NOW)
