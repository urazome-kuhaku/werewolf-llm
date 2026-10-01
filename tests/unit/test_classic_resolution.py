"""Focused checks for the explicit classic night proposal layer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionRequest,
    ActionWindow,
    GameManager,
    GameState,
    GrantedAbility,
    PlayerState,
    RulesetRef,
    load_action_registry,
)
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.frontmatter import parse_markdown
from werewolf.knowledge.role import ResourceDefinition, TargetKind, TargetRule
from werewolf.moderator.classic_resolution import (
    CLASSIC_BOARD_ID,
    ClassicNightResolutionError,
    build_classic_night_resolutions,
)

NOW = datetime(2026, 9, 30, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


def _board() -> BoardDefinition:
    path = (
        ROOT
        / "vault"
        / "_workbench"
        / "official_12_20260928"
        / "draft"
        / "boards"
        / CLASSIC_BOARD_ID
        / "1.0.0"
        / "board.md"
    )
    data = parse_markdown(path.read_bytes()).frontmatter
    data = dict(data)
    data["roles"] = [
        {
            **binding,
            "role_ref": {
                "id": binding["role_ref"].split("@")[0],
                "version": binding["role_ref"].split("@")[1],
            },
        }
        for binding in data["roles"]
    ]
    data["reading_plan"] = {
        **data["reading_plan"],
        "board_ref": {
            "id": CLASSIC_BOARD_ID,
            "version": "1.0.0",
        },
    }
    return BoardDefinition.model_validate(data)


def _ability(
    ability_id: str,
    code: int,
    target_rule: TargetRule,
    *,
    resource: ResourceDefinition | None = None,
) -> GrantedAbility:
    return GrantedAbility(
        ability_id=ability_id,
        action_code=code,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=(GamePhase.NIGHT_ACTION,),
        target_rule=target_rule,
        resource=resource,
    )


def _state() -> GameState:
    registry = load_action_registry()
    del registry
    players = {
        seat: PlayerState(
            seat=seat,
            role_id="wolf"
            if seat == 1
            else "seer"
            if seat == 2
            else "witch"
            if seat == 3
            else "villager",
            faction_id="wolf" if seat == 1 else "good",
            session_epoch=1,
        )
        for seat in range(1, 13)
    }
    players[1] = players[1].model_copy(
        update={
            "granted_abilities": (
                _ability(
                    "kill", 101, TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1)
                ),
            )
        }
    )
    players[2] = players[2].model_copy(
        update={
            "granted_abilities": (
                _ability(
                    "inspect", 102, TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1)
                ),
            )
        }
    )
    players[3] = players[3].model_copy(
        update={
            "skill_resources": {"witch_heal": 1, "witch_poison": 1},
            "granted_abilities": (
                _ability(
                    "heal",
                    104,
                    TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
                    resource=ResourceDefinition(
                        resource_id="witch_heal", initial_amount=1, cost_per_use=1
                    ),
                ),
                _ability(
                    "poison",
                    103,
                    TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
                    resource=ResourceDefinition(
                        resource_id="witch_poison", initial_amount=1, cost_per_use=1
                    ),
                ),
            ),
        }
    )
    window = ActionWindow(
        window_id="night_actions",
        game_id="classic-test",
        session_epoch=1,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(1, 2, 3),
        allowed_action_codes=(101, 102, 103, 104, 299),
        allow_pass=True,
    )
    resolve_window = window.model_copy(
        update={"window_id": "night_resolve", "phase": GamePhase.NIGHT_RESOLVE}
    )
    raw_requests: dict[str, dict[str, object]] = {}
    for request_id, seat, code, target in (
        ("wolf-request", 1, 101, 4),
        ("seer-request", 2, 102, 1),
        ("witch-request", 3, 104, 4),
    ):
        request = ActionRequest(
            request_id=request_id,
            game_id="classic-test",
            window_id="night_actions",
            seat=seat,
            session_epoch=1,
            phase=GamePhase.NIGHT_ACTION,
            actions=(Action(action_code=code, targets=(target,)),),
        )
        raw = request.model_dump(mode="json")
        raw.update({"status": "PENDING", "request_fingerprint": "a" * 64})
        raw_requests[request_id] = raw

    return GameState(
        game_id="classic-test",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_RESOLVE,
        ruleset=RulesetRef(
            board_id=CLASSIC_BOARD_ID,
            version="1.0.0",
            snapshot_id="classic-test",
            manifest_sha256="a" * 64,
        ),
        players=players,
        action_windows={
            "night_actions": window.model_dump(mode="json"),
            "night_resolve": resolve_window.model_dump(mode="json"),
        },
        action_requests=raw_requests,
    )


@pytest.mark.asyncio
async def test_classic_proposal_applies_heal_and_builds_every_pending_request() -> None:
    state = _state()
    proposals = build_classic_night_resolutions(state, _board())

    assert {item.request_id for item in proposals} == {
        "wolf-request",
        "seer-request",
        "witch-request",
    }
    wolf = next(item for item in proposals if item.request_id == "wolf-request")
    assert wolf.actions[0].effects == ()
    assert (
        next(item for item in proposals if item.request_id == "witch-request")
        .actions[0]
        .resource_cost
        == 1
    )

    manager = GameManager(state, registry=load_action_registry())
    committed = await manager.commit_night_resolution(
        proposals,
        action_window_id="night_actions",
        resolve_window_id="night_resolve",
        board=_board(),
        now=NOW,
    )
    assert committed.players[4].alive is True
    assert committed.players[3].skill_resources["witch_heal"] == 0
    seer_events = [
        event
        for event in committed.events
        if getattr(event, "event_type", None).value == "seer_result"
    ]
    assert len(seer_events) == 1
    assert seer_events[0].audience == (2,)
    assert seer_events[0].payload.faction_id == "wolf"


def test_classic_resolver_rejects_another_board_before_mutation() -> None:
    state = _state()
    board = _board().model_copy(update={"board_id": "other-board"})
    with pytest.raises(ClassicNightResolutionError, match="CLASSIC_BOARD_UNSUPPORTED"):
        build_classic_night_resolutions(state, board)
