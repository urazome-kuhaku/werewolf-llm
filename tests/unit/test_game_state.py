import json
import warnings
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    GameState,
    GrantedAbility,
    GrantedTriggerAbility,
    PlayerState,
    PrivateRolePayload,
    RandomStateRef,
    RulesetRef,
)
from werewolf.knowledge.role import (
    ResourceDefinition,
    TargetKind,
    TargetRule,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
    UsageLimit,
)
from werewolf.knowledge.snapshot import _snapshot_id


def _ruleset() -> RulesetRef:
    return RulesetRef(
        board_id="classic-12",
        version="1.0.0",
        snapshot_id="snapshot-20260928",
        manifest_sha256="a" * 64,
    )


def _state() -> GameState:
    timestamp = datetime(2026, 9, 28, tzinfo=UTC)
    player = PlayerState(seat=1, role_id="seer", faction_id="town")
    return GameState(
        game_id="game-1",
        created_at=timestamp,
        updated_at=timestamp,
        ruleset=_ruleset(),
        rng=RandomStateRef(seed=7),
        players={1: player},
    )


def _granted_trigger() -> GrantedTriggerAbility:
    return GrantedTriggerAbility(
        ability_id="last_shot",
        action_code=105,
        trigger=TriggerRule(
            event=TriggerEvent.DEATH_CONFIRMED,
            allowed_death_causes=["wolf_kill"],
            mode=TriggerMode.PLAYER_CHOICE,
            effects=[TriggerEffect.OPEN_PLAYER_ACTION],
            allow_pass=True,
            once=True,
        ),
        target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
    )


def _granted_active() -> GrantedAbility:
    return GrantedAbility(
        ability_id="inspect",
        action_code=102,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=(GamePhase.NIGHT_ACTION,),
        target_rule=TargetRule(kind="PLAYER", min_targets=1, max_targets=1),
        usage_limit=UsageLimit(max_uses=2, uses_per_round=1),
        resource=ResourceDefinition(
            resource_id="inspect_charge",
            initial_amount=2,
            cost_per_use=1,
        ),
    )


def test_state_uses_utc_and_schema_version() -> None:
    state = _state()
    assert state.schema_version == 1
    assert state.created_at.tzinfo is UTC
    assert state.updated_at.tzinfo is UTC


def test_offset_timestamp_is_normalized_to_utc() -> None:
    values = _state().model_dump()
    values["created_at"] = "2026-09-28T08:00:00+08:00"
    state = GameState.model_validate(values)
    assert state.created_at == datetime(2026, 9, 28, tzinfo=UTC)


def test_naive_timestamp_is_rejected() -> None:
    values = _state().model_dump()
    values["created_at"] = datetime(2026, 9, 28)
    with pytest.raises(ValidationError):
        GameState.model_validate(values)


def test_players_are_private_and_indexed_by_matching_seat() -> None:
    state = _state()
    assert state.players[1].role_id == "seer"
    values = state.model_dump()
    values["players"] = {2: values["players"][1]}
    with pytest.raises(ValidationError):
        GameState.model_validate(values)


def test_trigger_ability_state_round_trips_without_public_role_payload() -> None:
    granted = _granted_trigger()
    state = GameState.model_validate(
        _state()
        .model_copy(
            update={
                "players": {
                    1: PlayerState(
                        seat=1,
                        role_id="seer",
                        faction_id="town",
                        granted_trigger_abilities=(granted,),
                    )
                }
            }
        )
        .model_dump()
    )

    restored = GameState.model_validate(state.model_dump())
    restored_granted = restored.players[1].granted_trigger_abilities
    assert restored_granted == (granted,)
    assert restored_granted[0].trigger.allow_pass is True
    assert restored_granted[0].consumed is False
    role_payload = PrivateRolePayload(role_id="seer", faction_id="town")
    assert "granted_trigger_abilities" not in role_payload.model_dump()


def test_active_ability_state_round_trips_and_stays_out_of_role_payload() -> None:
    granted = _granted_active()
    state = GameState.model_validate(
        _state()
        .model_copy(
            update={
                "players": {
                    1: PlayerState(
                        seat=1,
                        role_id="seer",
                        faction_id="town",
                        granted_abilities=(granted,),
                    )
                }
            }
        )
        .model_dump()
    )

    restored = GameState.model_validate(state.model_dump())
    restored_granted = restored.players[1].granted_abilities
    assert restored_granted == (granted,)
    assert restored_granted[0].allowed_phases == (GamePhase.NIGHT_ACTION,)
    assert restored_granted[0].resource is not None
    assert restored_granted[0].resource.resource_id == "inspect_charge"
    role_payload = PrivateRolePayload(role_id="seer", faction_id="town")
    assert "granted_abilities" not in role_payload.model_dump()


def test_player_rejects_duplicate_ability_ids_across_active_and_trigger_grants() -> None:
    granted = _granted_active().model_copy(update={"ability_id": "shared"})
    trigger = _granted_trigger().model_copy(update={"ability_id": "shared"})

    with pytest.raises(ValidationError, match="across all ability grants"):
        PlayerState(
            seat=1,
            role_id="seer",
            faction_id="town",
            granted_abilities=(granted,),
            granted_trigger_abilities=(trigger,),
        )


def test_ruleset_rejects_non_sha256_manifest() -> None:
    with pytest.raises(ValidationError):
        RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="not-a-hash",
        )


def test_ruleset_ref_accepts_snapshot_id_from_knowledge_snapshot_builder() -> None:
    snapshot_id = _snapshot_id(
        {
            "board_ref": "classic-12@1.0.0",
            "compiler_version": "knowledge-compiler/1",
            "files": [{"path": "board.md", "sha256": "a" * 64}],
            "game_id": "game-1",
            "package_id": "classic-12@1.0.0",
            "package_identity": "b" * 64,
            "schema_version": 1,
        }
    )
    ref = RulesetRef(
        board_id="classic-12",
        version="1.0.0",
        snapshot_id=snapshot_id,
        manifest_sha256="a" * 64,
    )
    assert ref.snapshot_id == snapshot_id
    assert len(ref.snapshot_id) == len("ruleset-") + 64


def test_ruleset_rejects_other_ids_longer_than_64_characters() -> None:
    with pytest.raises(ValidationError):
        RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="snapshot-" + "a" * 64,
            manifest_sha256="a" * 64,
        )


def test_nested_state_containers_are_immutable() -> None:
    values = _state().model_dump()
    values.update(
        {
            "players": {
                1: PlayerState(
                    seat=1,
                    role_id="seer",
                    faction_id="town",
                    skill_resources={"vision": 1},
                )
            },
            "events": ({"payload": {"kind": "night"}},),
            "action_windows": {"seer": {"open": True}},
        }
    )
    state = GameState.model_validate(values)

    with pytest.raises(TypeError):
        state.players[1].skill_resources["vision"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        state.events[0]["payload"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        state.action_windows["seer"]["open"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        state.players[1] = state.players[1]  # type: ignore[index]

    # Frozen containers remain ordinary JSON objects/arrays in the wire format.
    assert '"open":true' in state.model_dump_json()


def test_json_serialization_thaws_frozen_extension_containers() -> None:
    values = _state().model_dump()
    nested = {"items": [{"flags": ["a", {"enabled": True}]}]}
    values.update(
        {
            "events": ({"payload": nested},),
            "action_windows": {"seer": nested},
            "action_requests": {"request-1": nested},
            "resolutions": (nested,),
            "pending_resolution": nested,
            "knowledge_receipts": (nested,),
            "vote_state": nested,
            "sheriff_election": nested,
            "sheriff_badge": nested,
            "moderator_audit": (nested,),
            "winner": nested,
            "last_snapshot": nested,
        }
    )
    state = GameState.model_validate(values)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        dumped = state.model_dump(mode="json")
        encoded = state.model_dump_json()

    assert dumped["action_windows"]["seer"]["items"][0]["flags"] == [
        "a",
        {"enabled": True},
    ]
    assert isinstance(dumped["resolutions"], list)
    assert isinstance(dumped["events"], list)
    assert json.loads(encoded) == dumped

    restored = GameState.model_validate_json(encoded)
    assert restored.model_dump(mode="json") == dumped
    with pytest.raises(TypeError):
        restored.action_windows["seer"]["items"][0]["flags"][0] = "changed"  # type: ignore[index]


def test_python_dump_keeps_frozen_extension_shape_for_reducers() -> None:
    state = GameState.model_validate(
        _state()
        .model_copy(update={"action_windows": {"seer": {"targets": [1, {"nested": [2]}]}}})
        .model_dump()
    )

    dumped = state.model_dump(mode="python", warnings=False)
    assert isinstance(dumped["action_windows"]["seer"]["targets"], tuple)
    assert isinstance(dumped["action_windows"]["seer"]["targets"][1]["nested"], tuple)
