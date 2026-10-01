from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionRequest,
    ActionResolution,
    ActionResolutionEntry,
    ActionValidationContext,
    ActionWindow,
    GameManager,
    GameState,
    GrantedAbility,
    PlayerState,
    ResolutionEffect,
    ResolutionError,
    ResolutionStatus,
    RulesetRef,
    load_action_registry,
)
from werewolf.knowledge.role import ResourceDefinition, TargetKind, TargetRule, UsageLimit

NOW = datetime(2026, 9, 28, tzinfo=UTC)
REGISTRY = load_action_registry()


def _manager(
    *, allowed_seats: tuple[int, ...] = (4,), max_uses: int | None = None
) -> tuple[GameManager, ActionRequest]:
    state = GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_ACTION,
        ruleset=RulesetRef(
            board_id="board-1",
            version="1.0.0",
            snapshot_id="snapshot-1",
            manifest_sha256="0" * 64,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id="villager",
                faction_id="village",
                session_epoch=2,
            ),
            4: PlayerState(
                seat=4,
                role_id="witch",
                faction_id="village",
                session_epoch=2,
                current_request_id="req-1",
                skill_resources={"witch_poison": 1},
                granted_abilities=(
                    GrantedAbility(
                        ability_id="poison",
                        action_code=103,
                        timing=GamePhase.NIGHT_ACTION,
                        allowed_phases=(GamePhase.NIGHT_ACTION,),
                        target_rule=TargetRule(
                            kind=TargetKind.PLAYER,
                            min_targets=1,
                            max_targets=1,
                        ),
                        usage_limit=(
                            UsageLimit(max_uses=max_uses) if max_uses is not None else None
                        ),
                        resource=ResourceDefinition(
                            resource_id="witch_poison",
                            initial_amount=1,
                        ),
                    ),
                ),
            ),
        },
        action_windows={
            "window-1": ActionWindow(
                window_id="window-1",
                game_id="game-1",
                session_epoch=2,
                phase=GamePhase.NIGHT_ACTION,
                allowed_seats=allowed_seats,
                allowed_role_ids=("witch",),
                allowed_action_codes=(103,),
                opened_at=NOW,
            ).model_dump(mode="json")
        },
    )
    request = ActionRequest(
        request_id="req-1",
        game_id="game-1",
        window_id="window-1",
        seat=4,
        session_epoch=2,
        actions=(Action(action_code=103, targets=(1,)),),
        phase=GamePhase.NIGHT_ACTION,
    )
    return GameManager(state, registry=REGISTRY), request


def _context() -> ActionValidationContext:
    return ActionValidationContext(
        game_id="game-1",
        session_epoch=2,
        active_request_id="req-1",
        role_id="witch",
        authorized_action_codes=(103,),
        skill_resources={"witch_poison": 1},
        alive_seats=(1, 4),
        eligible_targets_by_action={103: (1,)},
    )


def _resolution(*, base_revision: int, effect_target: int = 1) -> ActionResolution:
    requested = Action(action_code=103, targets=(1,))
    return ActionResolution(
        resolution_id="resolution-1",
        bundle_id="bundle-1",
        game_id="game-1",
        window_id="window-1",
        request_id="req-1",
        session_epoch=2,
        base_revision=base_revision,
        status=ResolutionStatus.CONFIRMED,
        actions=(
            ActionResolutionEntry(
                action_index=0,
                requested_action=requested,
                resource_cost=1,
                effects=(
                    ResolutionEffect(
                        effect_id="effect-1",
                        action_index=0,
                        effect_type="SET_ALIVE",
                        target_seat=effect_target,
                        value=False,
                    ),
                ),
            ),
        ),
        moderator_id="gm",
        created_at=NOW,
    )


async def test_resolution_consumes_resource_and_applies_effect_atomically() -> None:
    manager, request = _manager()
    await manager.commit_action_request(request, _context(), now=NOW)

    committed = await manager.commit_action_resolution(_resolution(base_revision=1), now=NOW)

    assert committed.state_revision == 2
    assert committed.players[4].skill_resources["witch_poison"] == 0
    assert committed.players[1].alive is False
    assert committed.action_requests["req-1"]["status"] == "CONFIRMED"
    assert committed.action_windows["window-1"]["closed_at"]
    assert committed.moderator_audit[-1]["operation"] == "ACTION_RESOLUTION"


async def test_resolution_consumes_one_authoritative_active_ability_use() -> None:
    manager, request = _manager(max_uses=1)
    await manager.commit_action_request(request, _context(), now=NOW)

    committed = await manager.commit_action_resolution(_resolution(base_revision=1), now=NOW)

    grant = committed.players[4].granted_abilities[0]
    assert grant.uses_consumed == 1


async def test_resource_effect_for_actor_is_applied_once() -> None:
    manager, request = _manager()
    await manager.commit_action_request(request, _context(), now=NOW)
    resolution = _resolution(base_revision=1)
    entry = resolution.actions[0].model_copy(
        update={
            "effects": (
                *resolution.actions[0].effects,
                ResolutionEffect(
                    effect_id="effect-resource",
                    action_index=0,
                    effect_type="ADJUST_RESOURCE",
                    target_seat=4,
                    resource_id="witch_reagent",
                    value=1,
                ),
            )
        }
    )
    resolution = resolution.model_copy(update={"actions": (entry,)})

    committed = await manager.commit_action_resolution(resolution, now=NOW)

    assert committed.players[4].skill_resources == {
        "witch_poison": 0,
        "witch_reagent": 1,
    }


async def test_resolution_window_from_an_old_phase_is_rejected() -> None:
    manager, request = _manager()
    await manager.commit_action_request(request, _context(), now=NOW)
    state = await manager.snapshot()
    manager._state = state.model_copy(update={"phase": GamePhase.DAY_RESOLVE})

    with pytest.raises(ResolutionError, match="PHASE_MISMATCH"):
        await manager.commit_action_resolution(_resolution(base_revision=1), now=NOW)

    assert (await manager.snapshot()).state_revision == 1


async def test_nonfinite_vote_weight_effect_is_rejected_atomically() -> None:
    manager, request = _manager()
    await manager.commit_action_request(request, _context(), now=NOW)
    before = await manager.snapshot()
    resolution = _resolution(base_revision=1)
    entry = resolution.actions[0].model_copy(
        update={
            "effects": (
                ResolutionEffect(
                    effect_id="effect-weight",
                    action_index=0,
                    effect_type="SET_VOTE_WEIGHT",
                    target_seat=1,
                    value=float("nan"),
                ),
            )
        }
    )
    resolution = resolution.model_copy(update={"actions": (entry,)})

    with pytest.raises(ResolutionError, match="EFFECT_VALUE"):
        await manager.commit_action_resolution(resolution, now=NOW)

    assert await manager.snapshot() == before


async def test_multi_seat_resolution_does_not_close_window_early() -> None:
    manager, request = _manager(allowed_seats=(4, 1))
    await manager.commit_action_request(request, _context(), now=NOW)

    committed = await manager.commit_action_resolution(_resolution(base_revision=1), now=NOW)

    assert committed.action_windows["window-1"]["closed_at"] is None


async def test_invalid_resolution_leaves_state_unchanged() -> None:
    manager, request = _manager()
    await manager.commit_action_request(request, _context(), now=NOW)
    before = await manager.snapshot()

    with pytest.raises(ResolutionError, match="TARGET_NOT_ASSIGNED"):
        await manager.commit_action_resolution(
            _resolution(base_revision=1, effect_target=2), now=NOW
        )

    after = await manager.snapshot()
    assert after == before
    assert after.state_revision == 1
    assert after.players[4].skill_resources["witch_poison"] == 1


async def test_resolution_replay_is_idempotent_but_conflict_is_rejected() -> None:
    manager, request = _manager()
    await manager.commit_action_request(request, _context(), now=NOW)
    resolution = _resolution(base_revision=1)
    committed = await manager.commit_action_resolution(resolution, now=NOW)

    replay = await manager.commit_action_resolution(resolution, now=NOW)
    assert replay is committed
    assert replay.state_revision == 2

    conflicting = resolution.model_copy(update={"reason": "different"})
    with pytest.raises(ResolutionError, match="IDEMPOTENCY_CONFLICT"):
        await manager.commit_action_resolution(conflicting, now=NOW)


async def test_stale_resolution_is_rejected_before_player_mutation() -> None:
    manager, request = _manager()
    await manager.commit_action_request(request, _context(), now=NOW)
    stale = _resolution(base_revision=0)

    with pytest.raises(ResolutionError, match="REVISION_MISMATCH"):
        await manager.commit_action_resolution(stale, now=NOW)
    assert (await manager.snapshot()).state_revision == 1
