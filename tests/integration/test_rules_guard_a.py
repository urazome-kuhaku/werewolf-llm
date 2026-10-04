"""Stage A guard acceptance through the serialized game manager path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TypeAlias

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionDefinition,
    ActionDisposition,
    ActionRegistry,
    ActionRequest,
    ActionResolution,
    ActionResolutionEntry,
    ActionValidationContext,
    ActionValidationError,
    ActionWindow,
    GameManager,
    GameState,
    PlayerState,
    ResolutionError,
    ResolutionStatus,
    RulesetRef,
    load_action_registry,
)
from werewolf.rules.compiler import validate_execution_package
from werewolf.rules.models import ExecutionPackage

NOW = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)
BOARD_ID = "rules_guard_acceptance_fixture"
BOARD_VERSION = "1.0.0"
GAME_ID = "rules-guard-acceptance"
WINDOW_ID = "night-actions"
RESOLVE_WINDOW_ID = "night-resolve"

WOLF_ACTION = 7311
GUARD_ACTION = 7312
HEAL_ACTION = 7313
POISON_ACTION = 7314

ActionPlan: TypeAlias = Mapping[int, tuple[tuple[int, tuple[int, ...]], ...]]


def _ref(source: str, name: str) -> dict[str, object]:
    return {"op": "ref", "source": source, "name": name}


def _literal(value: object) -> dict[str, object]:
    return {"op": "literal", "value": value}


def _compare(op: str, left: object, right: object) -> dict[str, object]:
    return {"op": op, "left": left, "right": right}


def _select_players(*, where: object | None = None, map_seat: bool = False) -> dict[str, object]:
    selector: dict[str, object] = {"op": "select", "source": "players"}
    if where is not None:
        selector["where"] = where
    if map_seat:
        selector["map"] = _ref("item", "seat")
    return selector


def _and(*values: object) -> dict[str, object]:
    return {"op": "and", "values": list(values)}


def _role_grant(role_id: str, grant_id: str) -> dict[str, object]:
    return {
        "grant_id": grant_id,
        "actor_selector": _select_players(
            where=_and(
                _compare("eq", _ref("item", "alive"), _literal(True)),
                _compare("eq", _ref("item", "role_id"), _literal(role_id)),
            ),
            map_seat=True,
        ),
    }


def _alive_other_targets(*, excludes_last_target: bool = False) -> dict[str, object]:
    constraints: list[object] = [
        _compare("eq", _ref("item", "alive"), _literal(True)),
        _compare("ne", _ref("item", "seat"), _ref("actor", "seat")),
    ]
    if excludes_last_target:
        constraints.append(_compare("ne", _ref("item", "seat"), _ref("skill_state", "last_target")))
    return _select_players(where=_and(*constraints), map_seat=True)


def _interactions(*, duplicate_poison_cause: bool = False) -> list[dict[str, object]]:
    rules: list[dict[str, object]] = [
        {
            "interaction_id": "guard-blocks-wolf-attack",
            "rule_type": "BLOCK_DAMAGE",
            "damage_tags": ["wolf_attack"],
            "counter_tags": ["guard"],
        },
        {
            "interaction_id": "antidote-cancels-wolf-attack",
            "rule_type": "CANCEL_DAMAGE_HEAL",
            "damage_tags": ["wolf_attack"],
            "counter_tags": ["antidote"],
        },
        {
            "interaction_id": "confirm-wolf-attack",
            "rule_type": "CONFIRM_DEATH",
            "priority": 10,
            "damage_tags": ["wolf_attack"],
            "death_cause": "wolf_attack",
        },
        {
            "interaction_id": "confirm-poison",
            "rule_type": "CONFIRM_DEATH",
            "priority": 20,
            "damage_tags": ["poison"],
            "death_cause": "poison",
        },
    ]
    if duplicate_poison_cause:
        rules.append(
            {
                "interaction_id": "confirm-poison-again-differently",
                "rule_type": "CONFIRM_DEATH",
                "priority": 20,
                "damage_tags": ["poison"],
                "death_cause": "second_poison_cause",
            }
        )
    return rules


def _execution(
    *,
    interactions: Sequence[Mapping[str, object]] | None = None,
    reverse_load_order: bool = False,
    duplicate_poison_cause: bool = False,
) -> ExecutionPackage:
    skills: list[dict[str, object]] = [
        {
            "skill_id": "night_raider_unfamiliar_skill",
            "action_code": WOLF_ACTION,
            "grants": [_role_grant("raider", "raider-grant")],
            "timing": ["NIGHT_ACTION"],
            "targets": {
                "min_targets": 1,
                "max_targets": 1,
                "selector": _alive_other_targets(),
                "allow_self": False,
            },
            "usage": {"max_uses": 1, "scope": "ROUND"},
            "effects": [
                {
                    "effect_id": "raider-damage",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["wolf_attack"],
                }
            ],
        },
        {
            "skill_id": "moon_ward_unfamiliar_skill",
            "action_code": GUARD_ACTION,
            "grants": [_role_grant("warden", "warden-grant")],
            "timing": ["NIGHT_ACTION"],
            "targets": {
                "min_targets": 1,
                "max_targets": 1,
                "selector": _alive_other_targets(excludes_last_target=True),
                "allow_self": False,
            },
            "usage": {
                "max_uses": 1,
                "scope": "ROUND",
                "pass_records": True,
                "pass_updates_history": True,
            },
            "effects": [
                {
                    "effect_id": "warden-protection",
                    "effect_type": "PROTECTION",
                    "target": _ref("target", "seat"),
                    "tags": ["guard"],
                },
                {
                    "effect_id": "warden-record-target",
                    "effect_type": "STATE_SET",
                    "state_key": "last_target",
                    "value": _ref("target", "seat"),
                },
            ],
            "pass_effects": [
                {
                    "effect_id": "warden-clear-target-on-pass",
                    "effect_type": "STATE_SET",
                    "state_key": "last_target",
                    "value": _literal(None),
                }
            ],
        },
        {
            "skill_id": "healer_unfamiliar_skill",
            "action_code": HEAL_ACTION,
            "grants": [_role_grant("alchemist", "alchemist-heal-grant")],
            "timing": ["NIGHT_ACTION"],
            "targets": {
                "min_targets": 1,
                "max_targets": 1,
                "selector": _alive_other_targets(),
                "allow_self": False,
            },
            "usage": {
                "max_uses": 1,
                "scope": "ROUND",
                "costs": [{"resource_id": "antidote", "amount": 1}],
            },
            "effects": [
                {
                    "effect_id": "alchemist-heal",
                    "effect_type": "HEAL",
                    "target": _ref("target", "seat"),
                    "tags": ["antidote"],
                }
            ],
        },
        {
            "skill_id": "venom_unfamiliar_skill",
            "action_code": POISON_ACTION,
            "grants": [_role_grant("alchemist", "alchemist-poison-grant")],
            "timing": ["NIGHT_ACTION"],
            "targets": {
                "min_targets": 1,
                "max_targets": 1,
                "selector": _alive_other_targets(),
                "allow_self": False,
            },
            "usage": {
                "max_uses": 1,
                "scope": "ROUND",
                "costs": [{"resource_id": "venom", "amount": 1}],
            },
            "effects": [
                {
                    "effect_id": "alchemist-poison",
                    "effect_type": "DAMAGE",
                    "target": _ref("target", "seat"),
                    "tags": ["poison"],
                }
            ],
        },
    ]
    if reverse_load_order:
        skills.reverse()
    interaction_values = (
        list(interactions)
        if interactions is not None
        else _interactions(duplicate_poison_cause=duplicate_poison_cause)
    )
    if reverse_load_order:
        interaction_values.reverse()
    action_specs = [
        {"action_code": code, "action_id": name, "allow_pass": True}
        for code, name in (
            (WOLF_ACTION, "NIGHT_RAIDER"),
            (GUARD_ACTION, "MOON_WARD"),
            (HEAL_ACTION, "ALCHEMIST_HEAL"),
            (POISON_ACTION, "ALCHEMIST_VENOM"),
            (299, "PASS"),
        )
    ]
    if reverse_load_order:
        action_specs.reverse()
    state_declarations = [
        {
            "skill_id": "moon_ward_unfamiliar_skill",
            "key": "last_target",
            "value_type": "nullable_seat",
            "initial": None,
        }
    ]
    if reverse_load_order:
        state_declarations.reverse()
    package = ExecutionPackage.model_validate(
        {
            "board_id": BOARD_ID,
            "board_version": BOARD_VERSION,
            "actions": action_specs,
            "skills": skills,
            "state_declarations": state_declarations,
            "interactions": interaction_values,
        }
    )
    return package


def _registry() -> ActionRegistry:
    base = load_action_registry()
    additions = (
        ActionDefinition(
            action_code=WOLF_ACTION,
            action_name="NIGHT_RAIDER",
            target_policy="board_eligible",
            target_count=1,
        ),
        ActionDefinition(
            action_code=GUARD_ACTION,
            action_name="MOON_WARD",
            target_policy="board_eligible",
            target_count=1,
        ),
        ActionDefinition(
            action_code=HEAL_ACTION,
            action_name="ALCHEMIST_HEAL",
            target_policy="board_eligible",
            target_count=1,
        ),
        ActionDefinition(
            action_code=POISON_ACTION,
            action_name="ALCHEMIST_VENOM",
            target_policy="board_eligible",
            target_count=1,
        ),
    )
    registry = ActionRegistry(actions=(*base.actions, *additions))
    return registry


def _window(window_id: str, phase: GamePhase) -> ActionWindow:
    return ActionWindow(
        window_id=window_id,
        game_id=GAME_ID,
        session_epoch=0,
        phase=phase,
        allowed_seats=(1, 2, 3),
        allowed_action_codes=(WOLF_ACTION, GUARD_ACTION, HEAL_ACTION, POISON_ACTION, 299),
        min_actions=1,
        max_actions=4,
        allow_pass=True,
        allow_duplicate_action_codes=True,
        allow_concurrent=True,
        max_submissions_per_seat=1,
        opened_at=NOW,
        visible_context={"candidate_seats": [1, 2, 3, 4, 5, 6]},
    )


def _initial_state(*, round_no: int = 1) -> GameState:
    return GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_ACTION,
        round_no=round_no,
        ruleset=RulesetRef(
            board_id=BOARD_ID,
            version=BOARD_VERSION,
            snapshot_id="rules-guard-fixture-snapshot",
            manifest_sha256="b" * 64,
        ),
        players={
            1: PlayerState(seat=1, role_id="raider", faction_id="wolf"),
            2: PlayerState(seat=2, role_id="warden", faction_id="village"),
            3: PlayerState(
                seat=3,
                role_id="alchemist",
                faction_id="village",
                skill_resources={"antidote": 1, "venom": 1},
            ),
            4: PlayerState(seat=4, role_id="villager", faction_id="village"),
            5: PlayerState(seat=5, role_id="villager", faction_id="village"),
            6: PlayerState(seat=6, role_id="villager", faction_id="village"),
        },
        action_windows={
            WINDOW_ID: _window(WINDOW_ID, GamePhase.NIGHT_ACTION).model_dump(mode="json"),
            RESOLVE_WINDOW_ID: _window(RESOLVE_WINDOW_ID, GamePhase.NIGHT_RESOLVE).model_dump(
                mode="json"
            ),
        },
    )


def _manager(
    package: ExecutionPackage,
    *,
    state: GameState | None = None,
) -> GameManager:
    registry = _registry()
    validate_execution_package(package, registry)
    return GameManager(state or _initial_state(), registry=registry, execution_package=package)


def _ability_state_value(state: GameState, *, seat: int, key: str) -> object:
    instance = next(
        item
        for item in state.ability_instances
        if item.actor_seat == seat and item.skill_id == "moon_ward_unfamiliar_skill"
    )
    value = next(
        item
        for item in state.rule_state
        if item.scope == "ABILITY"
        and item.scope_id == instance.ability_instance_id
        and item.key == key
    )
    return value.value


async def _submit_action(
    manager: GameManager,
    *,
    seat: int,
    request_id: str,
    actions: tuple[Action, ...],
    previous_request_id: str | None = None,
    retry: bool = False,
) -> None:
    action_windows = [
        (window_id, raw)
        for window_id, raw in manager.state.action_windows.items()
        if raw.get("phase") == GamePhase.NIGHT_ACTION.value and raw.get("closed_at") is None
    ]
    assert len(action_windows) == 1
    window_id = action_windows[0][0]
    await manager.begin_action_turn(
        seat,
        0,
        window_id=window_id,
        request_id=request_id,
        previous_request_id=previous_request_id,
        retry=retry,
        now=NOW,
    )
    request = ActionRequest(
        request_id=request_id,
        game_id=GAME_ID,
        window_id=window_id,
        seat=seat,
        session_epoch=0,
        phase=GamePhase.NIGHT_ACTION,
        actions=actions,
    )
    await manager.commit_action_request(
        request,
        ActionValidationContext(
            game_id=GAME_ID,
            session_epoch=0,
            active_request_id=request_id,
            authorized_action_codes=tuple(dict.fromkeys(action.action_code for action in actions)),
            alive_seats=(1, 2, 3, 4, 5, 6),
            eligible_targets_by_action={
                WOLF_ACTION: (1, 2, 3, 4, 5, 6),
                GUARD_ACTION: (1, 2, 3, 4, 5, 6),
                HEAL_ACTION: (1, 2, 3, 4, 5, 6),
                POISON_ACTION: (1, 2, 3, 4, 5, 6),
            },
        ),
        now=NOW,
    )


async def _resolve_neutrally(
    manager: GameManager,
    *,
    resolution_order: Sequence[str] | None = None,
    already_resolving: bool = False,
) -> GameState:
    if not already_resolving:
        await manager.commit_phase_transition(GamePhase.NIGHT_RESOLVE, now=NOW)
    state = manager.state
    action_windows = [
        window_id
        for window_id, raw in state.action_windows.items()
        if raw.get("phase") == GamePhase.NIGHT_ACTION.value and raw.get("closed_at") is None
    ]
    resolve_windows = [
        window_id
        for window_id, raw in state.action_windows.items()
        if raw.get("phase") == GamePhase.NIGHT_RESOLVE.value and raw.get("closed_at") is None
    ]
    assert len(action_windows) == len(resolve_windows) == 1
    action_window_id = action_windows[0]
    resolve_window_id = resolve_windows[0]
    requests = {
        request_id: ActionRequest.model_validate(
            {
                key: payload[key]
                for key in (
                    "request_id",
                    "game_id",
                    "window_id",
                    "seat",
                    "session_epoch",
                    "actions",
                    "idempotency_key",
                )
                if key in payload
            }
            | {"phase": GamePhase(payload["phase"])}
        )
        for request_id, payload in state.action_requests.items()
        if payload.get("window_id") == action_window_id and payload.get("status") == "PENDING"
    }
    order = tuple(resolution_order or sorted(requests))
    assert set(order) == set(requests)
    acknowledgements = tuple(
        ActionResolution(
            resolution_id=f"resolution-{request_id}",
            bundle_id="night-guard-fixture-bundle",
            game_id=GAME_ID,
            window_id=action_window_id,
            request_id=request_id,
            session_epoch=0,
            base_revision=state.state_revision,
            status=ResolutionStatus.CONFIRMED,
            actions=tuple(
                ActionResolutionEntry(
                    action_index=index,
                    requested_action=action,
                    disposition=ActionDisposition.CONFIRMED,
                    resource_cost=0,
                    effects=(),
                )
                for index, action in enumerate(request.actions)
            ),
            moderator_id="fixture-moderator",
            created_at=NOW,
        )
        for request_id in order
        for request in (requests[request_id],)
    )
    return await manager.commit_night_resolution(
        acknowledgements,
        action_window_id=action_window_id,
        resolve_window_id=resolve_window_id,
        use_rules_engine=True,
        expected_revision=state.state_revision,
        now=NOW,
    )


async def _play_night(
    package: ExecutionPackage,
    action_plan: ActionPlan,
    *,
    submit_order: Sequence[int] | None = None,
    resolution_order: Sequence[int] | None = None,
) -> GameState:
    manager = _manager(package)
    order = tuple(submit_order or sorted(action_plan))
    assert set(order) == set(action_plan)
    for seat in order:
        await _submit_action(
            manager,
            seat=seat,
            request_id=f"night-1-seat-{seat}",
            actions=tuple(
                Action(action_code=code, targets=targets) for code, targets in action_plan[seat]
            ),
        )
    resolution_ids = (
        tuple(f"night-1-seat-{seat}" for seat in resolution_order)
        if resolution_order is not None
        else None
    )
    return await _resolve_neutrally(manager, resolution_order=resolution_ids)


def _next_night_manager(manager: GameManager, *, next_round: int) -> GameManager:
    """Restore a serialized state snapshot and open its next test night."""

    restored = GameState.model_validate_json(manager.state.model_dump_json())
    action_window_id = f"night-actions-{next_round}"
    resolve_window_id = f"night-resolve-{next_round}"
    action_windows = {
        **restored.action_windows,
        action_window_id: _window(action_window_id, GamePhase.NIGHT_ACTION).model_dump(mode="json"),
        resolve_window_id: _window(resolve_window_id, GamePhase.NIGHT_RESOLVE).model_dump(
            mode="json"
        ),
    }
    next_state = restored.model_copy(
        update={
            "phase": GamePhase.NIGHT_ACTION,
            "round_no": next_round,
            "updated_at": NOW + timedelta(days=next_round),
            "action_windows": action_windows,
        }
    )
    package = manager.execution_package
    assert package is not None
    next_manager = GameManager(next_state, registry=manager.registry, execution_package=package)
    assert next_manager.state.execution_identity == manager.state.execution_identity
    return next_manager


async def _run_next_guard_request(
    manager: GameManager,
    *,
    request_id: str,
    target: int | None,
    retry_previous_request_id: str | None = None,
) -> GameState:
    actions = (
        (Action(action_code=299),)
        if target is None
        else (Action(action_code=GUARD_ACTION, targets=(target,)),)
    )
    await _submit_action(
        manager,
        seat=2,
        request_id=request_id,
        actions=actions,
        previous_request_id=retry_previous_request_id,
        retry=retry_previous_request_id is not None,
    )
    return await _resolve_neutrally(manager)


@pytest.mark.asyncio
async def test_unknown_guard_history_first_null_rejection_pass_reset_and_snapshot_restore() -> None:
    package = _execution()
    manager = _manager(package)
    assert _ability_state_value(manager.state, seat=2, key="last_target") is None
    instance_id = next(
        item.ability_instance_id
        for item in manager.state.ability_instances
        if item.actor_seat == 2 and item.skill_id == "moon_ward_unfamiliar_skill"
    )
    assert instance_id == "seat-2-warden-grant"

    first = await _run_next_guard_request(
        manager,
        request_id="night-1-warden",
        target=4,
    )
    assert _ability_state_value(first, seat=2, key="last_target") == 4
    assert first.players[4].alive is True
    assert first.rule_ledger[0].history_updates[0].targets == (4,)
    assert first.rule_ledger[0].history_updates[0].round_number == 1

    manager = _next_night_manager(manager, next_round=2)
    snapshot = manager.state
    assert _ability_state_value(snapshot, seat=2, key="last_target") == 4
    with pytest.raises(ActionValidationError, match="TARGET_NOT_ALLOWED"):
        await _submit_action(
            manager,
            seat=2,
            request_id="night-2-repeat-target",
            actions=(Action(action_code=GUARD_ACTION, targets=(4,)),),
        )
    assert _ability_state_value(manager.state, seat=2, key="last_target") == 4
    assert len(manager.state.rule_ledger) == 1
    assert len(manager.state.rule_receipts) == 1

    second = await _run_next_guard_request(
        manager,
        request_id="night-2-warden-retry",
        target=5,
        retry_previous_request_id="night-2-repeat-target",
    )
    assert _ability_state_value(second, seat=2, key="last_target") == 5
    assert len(second.rule_ledger[1].history_updates) == 1
    assert second.rule_ledger[1].history_updates[0].round_number == 2

    manager = _next_night_manager(manager, next_round=3)
    passed = await _run_next_guard_request(manager, request_id="night-3-warden-pass", target=None)
    assert _ability_state_value(passed, seat=2, key="last_target") is None
    pass_record = passed.rule_ledger[2].history_updates[0]
    assert pass_record.passed is True
    assert pass_record.targets == ()
    assert pass_record.round_number == 3

    manager = _next_night_manager(manager, next_round=4)
    fourth = await _run_next_guard_request(
        manager,
        request_id="night-4-warden",
        target=4,
    )
    assert _ability_state_value(fourth, seat=2, key="last_target") == 4
    assert fourth.rule_ledger[3].history_updates[0].targets == (4,)
    assert fourth.rule_ledger[3].history_updates[0].round_number == 4


@pytest.mark.asyncio
async def test_guard_protection_and_interaction_outcomes() -> None:
    package = _execution()
    protected = await _play_night(
        package,
        {
            1: ((WOLF_ACTION, (4,)),),
            2: ((GUARD_ACTION, (4,)),),
        },
    )
    assert protected.players[4].alive is True
    assert protected.rule_ledger[0].history_updates
    assert {item.skill_id for item in protected.rule_ledger[0].history_updates} == {
        "night_raider_unfamiliar_skill",
        "moon_ward_unfamiliar_skill",
    }

    poisoned = await _play_night(
        package,
        {
            2: ((GUARD_ACTION, (4,)),),
            3: ((POISON_ACTION, (4,)),),
        },
        submit_order=(3, 2),
    )
    assert poisoned.players[4].alive is False
    assert poisoned.players[4].death_cause == "poison"

    same_guard_and_heal = {
        1: ((WOLF_ACTION, (4,)),),
        2: ((GUARD_ACTION, (4,)),),
        3: ((HEAL_ACTION, (4,)),),
    }
    default_outcome = await _play_night(package, same_guard_and_heal)
    assert default_outcome.players[4].alive is True
    assert default_outcome.players[3].skill_resources == {"antidote": 0, "venom": 1}

    changed_interactions = tuple(
        rule
        for rule in package.interactions
        if rule.rule_type not in {"BLOCK_DAMAGE", "CANCEL_DAMAGE_HEAL"}
    )
    changed_package = package.model_copy(update={"interactions": changed_interactions})
    assert package.model_dump(exclude={"interactions"}) == changed_package.model_dump(
        exclude={"interactions"}
    )
    changed_outcome = await _play_night(changed_package, same_guard_and_heal)
    assert changed_outcome.players[4].alive is False
    assert changed_outcome.players[4].death_cause == "wolf_attack"


@pytest.mark.asyncio
async def test_rule_results_ignore_package_and_submission_order_and_failed_batch_is_atomic() -> (
    None
):
    package = _execution()
    reversed_package = _execution(reverse_load_order=True)
    assert reversed_package.package_id == package.package_id
    actions = {
        1: ((WOLF_ACTION, (4,)),),
        2: ((GUARD_ACTION, (4,)),),
        3: ((HEAL_ACTION, (4,)),),
    }
    first = await _play_night(
        package,
        actions,
        submit_order=(1, 2, 3),
        resolution_order=(1, 2, 3),
    )
    second = await _play_night(
        reversed_package,
        actions,
        submit_order=(3, 1, 2),
        resolution_order=(3, 1, 2),
    )
    assert first.rule_receipts[0].outcome_digest == second.rule_receipts[0].outcome_digest
    assert tuple(
        (seat, item.alive, item.death_cause) for seat, item in first.players.items()
    ) == tuple((seat, item.alive, item.death_cause) for seat, item in second.players.items())
    assert tuple(first.rule_state) == tuple(second.rule_state)

    ambiguous = _execution(duplicate_poison_cause=True)
    manager = _manager(ambiguous)
    for seat, actions_for_seat in (
        (1, (Action(action_code=WOLF_ACTION, targets=(4,)),)),
        (2, (Action(action_code=GUARD_ACTION, targets=(4,)),)),
        (
            3,
            (
                Action(action_code=HEAL_ACTION, targets=(4,)),
                Action(action_code=POISON_ACTION, targets=(4,)),
            ),
        ),
    ):
        await _submit_action(
            manager,
            seat=seat,
            request_id=f"atomic-seat-{seat}",
            actions=actions_for_seat,
        )
    await manager.commit_phase_transition(GamePhase.NIGHT_RESOLVE, now=NOW)
    before_resolution = manager.state
    with pytest.raises(ResolutionError, match="RULE_PLAN_INVALID"):
        await _resolve_neutrally(manager, already_resolving=True)
    assert manager.state == before_resolution
    assert all(player.alive for player in manager.state.players.values())
    assert manager.state.players[3].skill_resources == {"antidote": 1, "venom": 1}
    assert _ability_state_value(manager.state, seat=2, key="last_target") is None
    assert manager.state.rule_ledger == ()
    assert manager.state.rule_receipts == ()
    assert manager.state.resolutions == ()

    over_limit_manager = _manager(package)
    await _submit_action(
        over_limit_manager,
        seat=2,
        request_id="max-uses-seat-2",
        actions=(
            Action(action_code=GUARD_ACTION, targets=(4,)),
            Action(action_code=GUARD_ACTION, targets=(5,)),
        ),
    )
    await over_limit_manager.commit_phase_transition(GamePhase.NIGHT_RESOLVE, now=NOW)
    before_over_limit_resolution = over_limit_manager.state
    with pytest.raises(ResolutionError, match="RULE_REQUEST_REJECTED"):
        await _resolve_neutrally(over_limit_manager, already_resolving=True)
    assert over_limit_manager.state == before_over_limit_resolution
    assert over_limit_manager.state.rule_ledger == ()
    assert over_limit_manager.state.rule_receipts == ()
    assert _ability_state_value(over_limit_manager.state, seat=2, key="last_target") is None
