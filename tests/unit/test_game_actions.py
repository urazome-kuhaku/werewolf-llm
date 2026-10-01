from __future__ import annotations

from datetime import UTC, datetime

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game.actions import (
    Action,
    ActionDefinition,
    ActionRegistry,
    ActionRequest,
    ActionValidationContext,
    ActionValidationError,
    ActionWindow,
    load_action_registry,
    validate_action_request,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)
REGISTRY = load_action_registry()


def _window(*, codes: tuple[int, ...] = (104, 103, 299), allow_pass: bool = True) -> ActionWindow:
    return ActionWindow(
        window_id="witch-r1",
        game_id="game-1",
        session_epoch=2,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(4,),
        allowed_role_ids=("witch",),
        allowed_action_codes=codes,
        forbidden_action_combinations=((103, 104),),
        min_actions=1,
        max_actions=1,
        allow_pass=allow_pass,
        opened_at=NOW,
    )


def _context(
    *, request_id: str = "req-1", resources: dict[str, int] | None = None
) -> ActionValidationContext:
    return ActionValidationContext(
        game_id="game-1",
        session_epoch=2,
        active_request_id=request_id,
        role_id="witch",
        authorized_action_codes=(103, 104, 299),
        skill_resources=resources or {"witch_heal": 1, "witch_poison": 1},
        alive_seats=(1, 2, 3, 4),
        eligible_targets_by_action={103: (1, 2, 3), 104: (1,)},
        current_kill_target_seat=1,
    )


def _request(action: Action, *, request_id: str = "req-1") -> ActionRequest:
    return ActionRequest(
        request_id=request_id,
        game_id="game-1",
        window_id="witch-r1",
        seat=4,
        session_epoch=2,
        actions=(action,),
        phase=GamePhase.NIGHT_ACTION,
    )


def test_registry_contains_stable_codes() -> None:
    assert {entry.action_code for entry in REGISTRY.actions} == {
        101,
        102,
        103,
        104,
        105,
        106,
        107,
        201,
        202,
        299,
    }
    assert REGISTRY.get(299).action_name == "PASS"


def test_default_registry_path_does_not_depend_on_current_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_action_registry().get(101).action_name == "WOLF_KILL"


def test_validator_uses_explicitly_injected_registry() -> None:
    registry = ActionRegistry(
        actions=(
            ActionDefinition(
                action_code=901,
                action_name="CUSTOM_PASS",
                target_policy="none",
                target_count=0,
            ),
        )
    )
    window = _window(codes=(901,), allow_pass=False)
    context = _context().model_copy(update={"authorized_action_codes": (901,)})
    result = validate_action_request(
        _request(Action(action_code=901)), window, context, registry=registry, now=NOW
    )
    assert result.actions[0].action_code == 901


def test_witch_heal_validates_without_consuming_resource() -> None:
    request = _request(Action(action_code=104, targets=(1,)))
    context = _context()
    result = validate_action_request(
        request, _window(codes=(104, 299)), context, registry=REGISTRY, now=NOW
    )
    assert result.request_fingerprint
    assert context.skill_resources["witch_heal"] == 1
    assert result.actions[0].targets == (1,)


def test_pass_is_standalone_and_exactly_one_action() -> None:
    request = _request(Action(action_code=299))
    result = validate_action_request(
        request, _window(codes=(104, 299)), _context(), registry=REGISTRY, now=NOW
    )
    assert result.actions[0].action_code == 299
    with pytest.raises(ActionValidationError, match="PASS_MIXED"):
        validate_action_request(
            _request(Action(action_code=299)).model_copy(
                update={"actions": (Action(action_code=299), Action(action_code=104, targets=(1,)))}
            ),
            _window(codes=(104, 299)).model_copy(update={"max_actions": 2}),
            _context(),
            registry=REGISTRY,
            now=NOW,
        )


def test_witch_cannot_submit_both_potions_atomically() -> None:
    request = _request(Action(action_code=103, targets=(2,))).model_copy(
        update={
            "actions": (
                Action(action_code=103, targets=(2,)),
                Action(action_code=104, targets=(1,)),
            )
        }
    )
    window = _window(codes=(103, 104), allow_pass=False).model_copy(update={"max_actions": 2})
    with pytest.raises(ActionValidationError, match="ACTION_COMBINATION_FORBIDDEN"):
        validate_action_request(request, window, _context(), registry=REGISTRY, now=NOW)


def test_other_window_can_explicitly_allow_both_potions() -> None:
    request = _request(Action(action_code=103, targets=(2,))).model_copy(
        update={
            "actions": (
                Action(action_code=103, targets=(2,)),
                Action(action_code=104, targets=(1,)),
            )
        }
    )
    window = _window(codes=(103, 104), allow_pass=False).model_copy(
        update={"max_actions": 2, "forbidden_action_combinations": ()}
    )
    result = validate_action_request(request, window, _context(), registry=REGISTRY, now=NOW)
    assert result.idempotent_replay is False


def test_rejects_wrong_request_and_duplicate_submission() -> None:
    with pytest.raises(ActionValidationError, match="REQUEST_MISMATCH"):
        validate_action_request(
            _request(Action(action_code=299), request_id="other"),
            _window(),
            _context(),
            registry=REGISTRY,
            now=NOW,
        )
    context = _context()
    duplicate = context.model_copy(update={"submitted_request_ids": ("req-1",)})
    with pytest.raises(ActionValidationError, match="DUPLICATE_REQUEST"):
        validate_action_request(
            _request(Action(action_code=299)), _window(), duplicate, registry=REGISTRY, now=NOW
        )


def test_repeated_identical_request_is_an_idempotent_replay() -> None:
    request = _request(Action(action_code=299))
    accepted = validate_action_request(request, _window(), _context(), registry=REGISTRY, now=NOW)
    replay_context = _context().model_copy(
        update={
            "submitted_request_ids": ("req-1",),
            "submitted_request_fingerprints": {"req-1": accepted.request_fingerprint},
        }
    )
    replay = validate_action_request(request, _window(), replay_context, registry=REGISTRY, now=NOW)
    assert replay.idempotent_replay is True
    assert replay.request_fingerprint == accepted.request_fingerprint


def test_replay_with_changed_payload_is_rejected() -> None:
    request = _request(Action(action_code=299))
    accepted = validate_action_request(request, _window(), _context(), registry=REGISTRY, now=NOW)
    context = _context().model_copy(
        update={
            "submitted_request_ids": ("req-1",),
            "submitted_request_fingerprints": {"req-1": accepted.request_fingerprint},
        }
    )
    changed = _request(Action(action_code=104, targets=(1,)))
    with pytest.raises(ActionValidationError, match="IDEMPOTENCY_CONFLICT"):
        validate_action_request(changed, _window(codes=(104,)), context, registry=REGISTRY, now=NOW)


def test_submission_limit_is_enforced_per_seat() -> None:
    context = _context().model_copy(update={"submitted_counts_by_seat": {4: 1}})
    with pytest.raises(ActionValidationError, match="SUBMISSION_LIMIT"):
        validate_action_request(
            _request(Action(action_code=299)), _window(), context, registry=REGISTRY, now=NOW
        )


def test_invalid_target_rejected_without_resolution() -> None:
    with pytest.raises(ActionValidationError, match="TARGET_NOT_ALLOWED"):
        validate_action_request(
            _request(Action(action_code=104, targets=(3,))),
            _window(codes=(104,)),
            _context(),
            registry=REGISTRY,
            now=NOW,
        )


def _sacrifice_window(*, codes: tuple[int, ...] = (107,)) -> ActionWindow:
    return ActionWindow(
        window_id="sacrifice-r1",
        game_id="game-1",
        session_epoch=2,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(4,),
        allowed_role_ids=("white_wolf_king",),
        allowed_action_codes=codes,
        min_actions=1,
        max_actions=1,
        opened_at=NOW,
    )


def _sacrifice_context(
    *,
    eligible: tuple[int, ...] = (4, 2),
    authorized_action_codes: tuple[int, ...] = (107,),
) -> ActionValidationContext:
    return ActionValidationContext(
        game_id="game-1",
        session_epoch=2,
        active_request_id="req-1",
        role_id="white_wolf_king",
        authorized_action_codes=authorized_action_codes,
        alive_seats=(1, 2, 3, 4),
        eligible_targets_by_action={107: eligible},
    )


def _sacrifice_request(targets: tuple[int, ...]) -> ActionRequest:
    return ActionRequest(
        request_id="req-1",
        game_id="game-1",
        window_id="sacrifice-r1",
        seat=4,
        session_epoch=2,
        actions=(Action(action_code=107, targets=targets),),
        phase=GamePhase.NIGHT_ACTION,
    )


def test_guard_protect_uses_board_eligible_living_target() -> None:
    request = _request(Action(action_code=106, targets=(4,)))
    window = _window(codes=(106,), allow_pass=False).model_copy(
        update={"allowed_role_ids": ("guard",)}
    )
    context = _context().model_copy(
        update={
            "role_id": "guard",
            "authorized_action_codes": (106,),
            "eligible_targets_by_action": {106: (1, 2, 3, 4)},
        }
    )
    result = validate_action_request(request, window, context, registry=REGISTRY, now=NOW)
    assert result.actions[0].targets == (4,)


def test_self_sacrifice_policy_requires_two_targets() -> None:
    with pytest.raises(ValueError, match="self_and_other_alive requires target_count=2"):
        ActionDefinition(
            action_code=901,
            action_name="SELF_SACRIFICE_TEST",
            target_policy="self_and_other_alive",
            target_count=1,
        )


def test_self_sacrifice_requires_actor_first_and_other_living_second() -> None:
    result = validate_action_request(
        _sacrifice_request((4, 2)),
        _sacrifice_window(),
        _sacrifice_context(),
        registry=REGISTRY,
        now=NOW,
    )
    assert result.actions[0].targets == (4, 2)

    with pytest.raises(ActionValidationError, match="ACTOR_TARGET_REQUIRED"):
        validate_action_request(
            _sacrifice_request((2, 4)),
            _sacrifice_window(),
            _sacrifice_context(),
            registry=REGISTRY,
            now=NOW,
        )
    with pytest.raises(ActionValidationError, match="TARGET_NOT_ALIVE"):
        validate_action_request(
            _sacrifice_request((4, 5)),
            _sacrifice_window(),
            _sacrifice_context(eligible=(4, 5)),
            registry=REGISTRY,
            now=NOW,
        )


def test_self_sacrifice_requires_authorization_for_both_targets() -> None:
    with pytest.raises(ActionValidationError, match="TARGET_NOT_ALLOWED"):
        validate_action_request(
            _sacrifice_request((4, 3)),
            _sacrifice_window(),
            _sacrifice_context(eligible=(4, 2)),
            registry=REGISTRY,
            now=NOW,
        )

    with pytest.raises(ActionValidationError, match="ACTION_UNAUTHORIZED"):
        validate_action_request(
            _sacrifice_request((4, 2)),
            _sacrifice_window(),
            _sacrifice_context(authorized_action_codes=()),
            registry=REGISTRY,
            now=NOW,
        )
