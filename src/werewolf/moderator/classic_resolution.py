"""Small, explicit proposal layer for the published 12-seat classic board.

The game manager remains board agnostic: it validates and commits a supplied
``ActionResolution``.  This module is the host-side adjudicator for the first
board only.  It reads the frozen board binding and the private grants copied
onto players at setup, then turns every pending intent into a complete,
auditable resolution batch.  Adding another board requires another proposal
layer until a ruleset interpreter is intentionally introduced.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue

from werewolf.domain.enums import GamePhase
from werewolf.game.actions import (
    Action,
    ActionDefinition,
    ActionRegistry,
    ActionRequest,
    ActionWindow,
)
from werewolf.game.resolution import (
    ActionResolution,
    ActionResolutionEntry,
    ResolutionEffect,
    ResolutionStatus,
)
from werewolf.game.state import (
    GameState,
    GrantedAbility,
    GrantedTriggerAbility,
    PlayerState,
    utc_now,
)
from werewolf.knowledge.board import BoardDefinition

CLASSIC_BOARD_ID = "classic_12_seer_witch_hunter_idiot"
_SUPPORTED_ACTIONS = frozenset(
    {"WOLF_KILL", "SEER_INSPECT", "WITCH_HEAL", "WITCH_POISON", "HUNTER_SHOOT", "PASS"}
)


class ClassicNightResolutionError(ValueError):
    """Raised when a classic proposal cannot be constructed safely."""


def _physical_window_id(window_id: str, round_no: int) -> str:
    return window_id if round_no == 0 else f"{window_id}-r{round_no}"


def _action_definition(registry: ActionRegistry, name: str) -> ActionDefinition:
    for definition in registry.actions:
        if definition.action_name == name:
            return definition
    raise ClassicNightResolutionError(f"CLASSIC_ACTION_MISSING: {name}")


def _classic_definitions(registry: ActionRegistry) -> dict[str, ActionDefinition]:
    definitions: dict[str, ActionDefinition] = {}
    for name in _SUPPORTED_ACTIONS:
        definitions[name] = _action_definition(registry, name)
    return definitions


def _board_rule(board: BoardDefinition, role_id: str, key: str, default: Any = None) -> Any:
    binding = next((item for item in board.role_bindings if item.role_ref.id == role_id), None)
    if binding is None:
        return default
    return binding.effective_rules.get(key, default)


def _request_from_payload(payload: object) -> ActionRequest:
    if not isinstance(payload, Mapping):
        raise ClassicNightResolutionError("CLASSIC_REQUEST_INVALID: stored request is malformed")
    data = dict(payload)
    for key in ("status", "validated_at", "request_fingerprint", "idempotent_replay", "attempt_no"):
        data.pop(key, None)
    phase = data.get("phase")
    if isinstance(phase, str):
        try:
            data["phase"] = GamePhase(phase)
        except ValueError as exc:
            raise ClassicNightResolutionError(
                "CLASSIC_REQUEST_INVALID: invalid request phase"
            ) from exc
    data = json.loads(json.dumps(data))
    if isinstance(data.get("phase"), str):
        data["phase"] = GamePhase(data["phase"])
    try:
        return ActionRequest.model_validate(data)
    except (TypeError, ValueError) as exc:
        raise ClassicNightResolutionError(
            "CLASSIC_REQUEST_INVALID: stored request is malformed"
        ) from exc


def _action_window(state: GameState, board: BoardDefinition) -> ActionWindow:
    windows = [item for item in board.night_windows if item.phase is GamePhase.NIGHT_ACTION]
    if len(windows) != 1:
        raise ClassicNightResolutionError(
            "CLASSIC_WINDOW_UNSUPPORTED: classic resolver requires one NIGHT_ACTION window"
        )
    window_id = _physical_window_id(windows[0].window_id, state.round_no)
    raw = state.action_windows.get(window_id)
    if not isinstance(raw, Mapping):
        raise ClassicNightResolutionError("CLASSIC_WINDOW_MISSING: night action window is absent")
    try:
        data = json.loads(json.dumps(raw))
        phase = data.get("phase")
        if isinstance(phase, str):
            data["phase"] = GamePhase(phase)
        window = ActionWindow.model_validate(data)
    except (TypeError, ValueError) as exc:
        raise ClassicNightResolutionError(
            "CLASSIC_WINDOW_INVALID: night action window is malformed"
        ) from exc
    if window.phase is not GamePhase.NIGHT_ACTION or window.closed_at is not None:
        raise ClassicNightResolutionError(
            "CLASSIC_WINDOW_INVALID: night action window is not pending"
        )
    return window


def _pending_requests(state: GameState, window_id: str) -> tuple[ActionRequest, ...]:
    requests: list[ActionRequest] = []
    for request_id, payload in sorted(state.action_requests.items()):
        if not isinstance(payload, Mapping):
            continue
        if payload.get("window_id") != window_id or payload.get("status") != "PENDING":
            continue
        if payload.get("request_id") != request_id:
            raise ClassicNightResolutionError(
                "CLASSIC_REQUEST_INVALID: request key does not match payload"
            )
        requests.append(_request_from_payload(payload))
    if not requests:
        raise ClassicNightResolutionError(
            "CLASSIC_REQUEST_EMPTY: no pending classic night requests"
        )
    return tuple(requests)


def _active_grant(player: PlayerState, action_code: int, phase: GamePhase) -> GrantedAbility | None:
    for ability in player.granted_abilities:
        if ability.action_code != action_code or ability.timing is not phase:
            continue
        if phase not in ability.allowed_phases:
            continue
        if (
            ability.usage_limit is not None
            and ability.usage_limit.max_uses is not None
            and ability.uses_consumed >= ability.usage_limit.max_uses
        ):
            continue
        if (
            ability.resource is not None
            and player.skill_resources.get(ability.resource.resource_id, 0)
            < ability.resource.cost_per_use
        ):
            continue
        return ability
    return None


def _active_trigger_grant(player: PlayerState, action_code: int) -> GrantedTriggerAbility | None:
    return next(
        (
            item
            for item in player.granted_trigger_abilities
            if item.action_code == action_code and not item.consumed
        ),
        None,
    )


def _target(action: Action) -> int:
    if len(action.targets) != 1:
        raise ClassicNightResolutionError("CLASSIC_TARGET_INVALID: classic action needs one target")
    return action.targets[0]


def _effect_id(state: GameState, request_id: str, action_index: int, suffix: str) -> str:
    digest = hashlib.sha256(
        f"{state.game_id}:{state.round_no}:{request_id}:{action_index}:{suffix}".encode()
    ).hexdigest()[:20]
    return f"classic-{digest}-{suffix}"


def _effect(
    state: GameState,
    request_id: str,
    action_index: int,
    suffix: str,
    effect_type: str,
    target_seat: int,
    value: JsonValue | None,
) -> ResolutionEffect:
    return ResolutionEffect(
        effect_id=_effect_id(state, request_id, action_index, suffix),
        action_index=action_index,
        effect_type=effect_type,  # type: ignore[arg-type]
        target_seat=target_seat,
        value=value,
    )


def build_classic_night_resolutions(
    state: GameState,
    board: BoardDefinition,
    *,
    registry: ActionRegistry | None = None,
    moderator_id: str = "classic-resolver",
    now: datetime | None = None,
) -> tuple[ActionResolution, ...]:
    """Build one confirmed resolution for every pending classic night request.

    This function is pure.  It performs all board and grant checks before it
    returns any record, so a caller can pass the tuple to the manager's
    serialized night commit without a partially adjudicated batch.
    """

    if board.board_id != CLASSIC_BOARD_ID:
        raise ClassicNightResolutionError(
            f"CLASSIC_BOARD_UNSUPPORTED: expected {CLASSIC_BOARD_ID}, got {board.board_id}"
        )
    if state.phase is not GamePhase.NIGHT_RESOLVE:
        raise ClassicNightResolutionError("CLASSIC_PHASE_INVALID: proposals require NIGHT_RESOLVE")
    if (
        state.ruleset is None
        or state.ruleset.board_id != board.board_id
        or state.ruleset.version != board.version
    ):
        raise ClassicNightResolutionError(
            "CLASSIC_RULESET_MISMATCH: board does not match game state"
        )
    if registry is None:
        from werewolf.game.actions import load_action_registry

        registry = load_action_registry()
    definitions = _classic_definitions(registry)
    window = _action_window(state, board)
    requests = _pending_requests(state, window.window_id)

    names_by_code = {definition.action_code: name for name, definition in definitions.items()}
    wolf_targets: list[int] = []
    poison_targets: list[int] = []
    heal_targets: list[int] = []
    for request in requests:
        player = state.players.get(request.seat)
        if player is None or not player.alive:
            raise ClassicNightResolutionError("CLASSIC_ACTOR_INVALID: pending actor is not alive")
        if request.window_id != window.window_id:
            raise ClassicNightResolutionError(
                "CLASSIC_WINDOW_MISMATCH: request is outside this night"
            )
        potion_names: set[str] = set()
        for action in request.actions:
            name = names_by_code.get(action.action_code)
            if name is None:
                raise ClassicNightResolutionError(
                    f"CLASSIC_ACTION_UNSUPPORTED: action code {action.action_code}"
                )
            if name in {"WITCH_HEAL", "WITCH_POISON"}:
                potion_names.add(name)
            if name == "WOLF_KILL":
                wolf_targets.append(_target(action))
            elif name == "WITCH_HEAL":
                heal_targets.append(_target(action))
            elif name == "WITCH_POISON":
                poison_targets.append(_target(action))
        if len(potion_names) > 1 or len(request.actions) > 1:
            raise ClassicNightResolutionError(
                "CLASSIC_ACTION_UNSUPPORTED: classic night accepts one action per request"
            )
    if len(wolf_targets) > 1:
        raise ClassicNightResolutionError("CLASSIC_WOLF_KILL_AMBIGUOUS: multiple wolf targets")
    wolf_target = wolf_targets[0] if wolf_targets else None
    heal_target = heal_targets[0] if heal_targets else None
    poisoned = set(poison_targets)
    if heal_target is not None and heal_target != wolf_target:
        raise ClassicNightResolutionError(
            "CLASSIC_HEAL_TARGET: heal must target the current wolf kill"
        )

    created_at = (now or utc_now()).astimezone(UTC)
    result: list[ActionResolution] = []
    for request in requests:
        player = state.players[request.seat]
        entries: list[ActionResolutionEntry] = []
        for action_index, action in enumerate(request.actions):
            name = names_by_code[action.action_code]
            definition = definitions[name]
            grant = _active_grant(player, action.action_code, GamePhase.NIGHT_ACTION)
            if name == "PASS":
                resource_cost = 0
            elif name == "HUNTER_SHOOT":
                raise ClassicNightResolutionError(
                    "CLASSIC_TRIGGER_ACTION: hunter shot belongs to TRIGGER_ACTION"
                )
            else:
                if grant is None:
                    raise ClassicNightResolutionError(
                        f"CLASSIC_GRANT_MISSING: seat {request.seat} lacks {name}"
                    )
                if definition.resource_id is not None and (
                    grant.resource is None or grant.resource.resource_id != definition.resource_id
                ):
                    raise ClassicNightResolutionError(
                        f"CLASSIC_RESOURCE_CONTRACT: seat {request.seat} grant does not "
                        f"match {name}"
                    )
                resource_cost = grant.resource.cost_per_use if grant.resource is not None else 0

            effects: list[ResolutionEffect] = []
            if name == "WOLF_KILL":
                target = _target(action)
                if (
                    target not in state.players
                    or not state.players[target].alive
                    or target
                    in {
                        seat
                        for seat, item in state.players.items()
                        if item.faction_id == player.faction_id
                    }
                ):
                    raise ClassicNightResolutionError("CLASSIC_KILL_TARGET: target is not eligible")
                # Healing suppresses the kill; poison supplies the winning
                # death cause when both actions select the same victim.
                if target != heal_target and target not in poisoned:
                    effects = [
                        _effect(
                            state,
                            request.request_id,
                            action_index,
                            "alive",
                            "SET_ALIVE",
                            target,
                            False,
                        ),
                        _effect(
                            state,
                            request.request_id,
                            action_index,
                            "cause",
                            "SET_DEATH_CAUSE",
                            target,
                            "wolf_kill",
                        ),
                    ]
                elif target in poisoned:
                    effects = []
            elif name == "SEER_INSPECT":
                target = _target(action)
                if (
                    target not in state.players
                    or not state.players[target].alive
                    or target == request.seat
                ):
                    raise ClassicNightResolutionError(
                        "CLASSIC_INSPECT_TARGET: target is not eligible"
                    )
            elif name in {"WITCH_HEAL", "WITCH_POISON"}:
                target = _target(action)
                if (
                    target not in state.players
                    or not state.players[target].alive
                    or target == request.seat
                ):
                    raise ClassicNightResolutionError(
                        "CLASSIC_POTION_TARGET: target is not eligible"
                    )
                if name == "WITCH_HEAL" and target != wolf_target:
                    raise ClassicNightResolutionError("CLASSIC_HEAL_TARGET: no matching wolf kill")
                if name == "WITCH_POISON":
                    effects = [
                        _effect(
                            state,
                            request.request_id,
                            action_index,
                            "alive",
                            "SET_ALIVE",
                            target,
                            False,
                        ),
                        _effect(
                            state,
                            request.request_id,
                            action_index,
                            "cause",
                            "SET_DEATH_CAUSE",
                            target,
                            "witch_poison",
                        ),
                    ]
            entries.append(
                ActionResolutionEntry(
                    action_index=action_index,
                    requested_action=action,
                    resource_cost=resource_cost,
                    effects=tuple(effects),
                )
            )
        digest = hashlib.sha256(
            f"{state.game_id}:{state.round_no}:{request.request_id}".encode()
        ).hexdigest()[:24]
        result.append(
            ActionResolution(
                resolution_id=f"classic-resolution-{digest}",
                bundle_id=f"classic-bundle-{digest}",
                game_id=state.game_id,
                window_id=window.window_id,
                request_id=request.request_id,
                session_epoch=request.session_epoch,
                base_revision=state.state_revision,
                status=ResolutionStatus.CONFIRMED,
                actions=tuple(entries),
                moderator_id=moderator_id,
                reason="classic board proposal",
                created_at=created_at,
            )
        )
    return tuple(result)


def build_classic_trigger_resolution(
    state: GameState,
    request: ActionRequest,
    *,
    registry: ActionRegistry | None = None,
    moderator_id: str = "classic-resolver",
    now: datetime | None = None,
) -> ActionResolution:
    """Build a confirmed classic HUNTER_SHOOT or PASS trigger ruling."""

    if state.phase is not GamePhase.TRIGGER_ACTION:
        raise ClassicNightResolutionError(
            "CLASSIC_PHASE_INVALID: hunter shot requires TRIGGER_ACTION"
        )
    if len(request.actions) != 1:
        raise ClassicNightResolutionError("CLASSIC_ACTION_UNSUPPORTED: trigger accepts one action")
    registry = (
        registry
        or __import__(
            "werewolf.game.actions", fromlist=["load_action_registry"]
        ).load_action_registry()
    )
    definitions = _classic_definitions(registry)
    player = state.players.get(request.seat)
    if player is None:
        raise ClassicNightResolutionError("CLASSIC_ACTOR_INVALID: trigger actor is unassigned")
    action = request.actions[0]
    name = next(
        (key for key, value in definitions.items() if value.action_code == action.action_code), None
    )
    if name not in {"HUNTER_SHOOT", "PASS"}:
        raise ClassicNightResolutionError(
            "CLASSIC_ACTION_UNSUPPORTED: trigger action is not hunter shoot/pass"
        )
    if name == "HUNTER_SHOOT":
        if _active_trigger_grant(player, action.action_code) is None:
            raise ClassicNightResolutionError(
                "CLASSIC_GRANT_MISSING: hunter trigger is unavailable"
            )
        target = _target(action)
        if target not in state.players or not state.players[target].alive or target == request.seat:
            raise ClassicNightResolutionError("CLASSIC_SHOOT_TARGET: target is not eligible")
        effects: tuple[ResolutionEffect, ...] = (
            _effect(state, request.request_id, 0, "alive", "SET_ALIVE", target, False),
            _effect(
                state, request.request_id, 0, "cause", "SET_DEATH_CAUSE", target, "hunter_shot"
            ),
        )
    else:
        effects = ()
    digest = hashlib.sha256(
        f"{state.game_id}:{state.round_no}:{request.request_id}".encode()
    ).hexdigest()[:24]
    raw_window = state.action_windows.get(request.window_id)
    if not isinstance(raw_window, Mapping):
        raise ClassicNightResolutionError("CLASSIC_WINDOW_MISSING: trigger window is absent")
    window_data = json.loads(json.dumps(raw_window))
    if isinstance(window_data.get("phase"), str):
        window_data["phase"] = GamePhase(window_data["phase"])
    try:
        ActionWindow.model_validate(window_data)
    except (TypeError, ValueError) as exc:
        raise ClassicNightResolutionError(
            "CLASSIC_WINDOW_INVALID: trigger window is malformed"
        ) from exc
    return ActionResolution(
        resolution_id=f"classic-trigger-resolution-{digest}",
        bundle_id=f"classic-trigger-bundle-{digest}",
        game_id=state.game_id,
        window_id=request.window_id,
        request_id=request.request_id,
        session_epoch=request.session_epoch,
        base_revision=state.state_revision,
        status=ResolutionStatus.CONFIRMED,
        actions=(
            ActionResolutionEntry(
                action_index=0,
                requested_action=action,
                effects=effects,
            ),
        ),
        moderator_id=moderator_id,
        reason="classic trigger proposal",
        created_at=(now or utc_now()).astimezone(UTC),
    )


__all__ = [
    "CLASSIC_BOARD_ID",
    "ClassicNightResolutionError",
    "build_classic_night_resolutions",
    "build_classic_trigger_resolution",
]
