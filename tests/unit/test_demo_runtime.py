"""Focused contract tests for the playable deterministic harness runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.skill_status import project_skill_status
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.player_runtime import (
    ActionWindowView,
    Deadline,
    InitialContext,
    Observation,
    ObservationEvent,
    ResponseKind,
    RuntimeConfig,
    RuntimeProtocolError,
    RuntimeRequestMismatchError,
    TurnRequest,
)


def _request(
    request_id: str,
    kind: ResponseKind,
    *,
    window: ActionWindowView | None = None,
) -> TurnRequest:
    start = datetime.now(UTC)
    return TurnRequest(
        request_id=request_id,
        logical_request_id=request_id,
        attempt_no=1,
        game_id="game-1",
        session_epoch=0,
        phase=GamePhase.VOTE if kind is ResponseKind.ACTION else GamePhase.PLAYER_PREPARE,
        expected_kind=kind,
        action_window=window,
        observation=Observation(payload={"seat": 1}),
        output_schema={"type": "object"},
        deadline=Deadline(soft_deadline=start, hard_deadline=start + timedelta(seconds=30)),
    )


@pytest.mark.asyncio
async def test_demo_runtime_reads_private_documents_and_claims_receipts() -> None:
    app = web.Application()

    async def board(_request: web.Request) -> web.Response:
        return web.json_response({"receipt_id": "receipt-board", "document": {}})

    async def role(_request: web.Request) -> web.Response:
        return web.json_response({"receipt_id": "receipt-role", "document": {}})

    async def skills(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "skill_status": {
                    "abilities": [],
                    "resources": {},
                    "windows": [],
                }
            }
        )

    app.router.add_get("/v1/board/current", board)
    app.router.add_get("/v1/role/wolf", role)
    app.router.add_get("/v1/game/skills/me", skills)
    server = TestServer(app)
    await server.start_server()
    runtime = DemoRuntime(str(server.make_url("")), "seat-token")
    try:
        await runtime.start(
            RuntimeConfig(session_id="session-1"),
            InitialContext(game_id="game-1", seat=1, session_epoch=0, role_id="wolf"),
        )
        ready = await runtime.run_turn(_request("prepare-1", ResponseKind.READY))
        assert ready.response.ready is not None
        assert ready.response.ready.knowledge_receipts == ["receipt-board", "receipt-role"]
        action = await runtime.run_turn(
            _request(
                "vote-1",
                ResponseKind.ACTION,
                window=ActionWindowView(
                    window_id="vote",
                    allowed_action_codes=[201],
                    candidate_seats=[1, 2],
                ),
            )
        )
        assert action.response.actions[0].action_code == 201
        assert action.response.actions[0].targets == [2]
    finally:
        await runtime.close("test complete")
        await server.close()


@pytest.mark.asyncio
async def test_demo_runtime_accepts_abort_for_recent_request_but_rejects_unknown() -> None:
    app = web.Application()

    async def board(_request: web.Request) -> web.Response:
        return web.json_response({"receipt_id": "board", "document": {}})

    async def role(_request: web.Request) -> web.Response:
        return web.json_response({"receipt_id": "role", "document": {}})

    async def skills(_request: web.Request) -> web.Response:
        return web.json_response(
            {"skill_status": {"abilities": [], "resources": {}, "windows": []}}
        )

    app.router.add_get(
        "/v1/board/current",
        board,
    )
    app.router.add_get("/v1/role/wolf", role)
    app.router.add_get("/v1/game/skills/me", skills)
    server = TestServer(app)
    await server.start_server()
    runtime = DemoRuntime(str(server.make_url("")), "seat-token")
    try:
        await runtime.start(
            RuntimeConfig(session_id="session-abort"),
            InitialContext(game_id="game-1", seat=1, session_epoch=0, role_id="wolf"),
        )
        await runtime.run_turn(_request("recent-request", ResponseKind.READY))
        await runtime.abort("recent-request")
        assert runtime.aborts == ("recent-request",)
        with pytest.raises(RuntimeRequestMismatchError):
            await runtime.abort("foreign-request")
    finally:
        await runtime.close("test complete")
        await server.close()


def _witch_status(*codes: int) -> dict[str, object]:
    target_selector = {
        "op": "select",
        "source": "facts",
        "where": {
            "op": "and",
            "values": [
                {
                    "op": "eq",
                    "left": {"op": "ref", "source": "item", "name": "fact_type"},
                    "right": {"op": "literal", "value": "wolf_attack_proposed"},
                },
                {
                    "op": "ne",
                    "left": {"op": "ref", "source": "item", "name": "target_seat"},
                    "right": {"op": "ref", "source": "actor", "name": "seat"},
                },
            ],
        },
        "map": {"op": "ref", "source": "item", "name": "target_seat"},
    }
    return {
        "abilities": [
            {
                "action_code": code,
                "target_rule": {
                    "min_targets": 1,
                    "max_targets": 1,
                    "allow_self": False,
                    "selector": target_selector,
                },
            }
            for code in codes
        ],
        "actions": [{"action_code": 299, "action_id": "pass", "is_pass": True}],
        "resources": {"witch_heal": 1, "witch_poison": 1},
        "windows": ["NIGHT_ACTION"],
    }


def test_demo_runtime_heal_uses_only_the_private_knife_notice() -> None:
    request = _request(
        "witch-1",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions",
            allowed_action_codes=[104, 103, 299],
            allow_pass=True,
            candidate_seats=[2, 3, 4],
        ),
    ).model_copy(
        update={
            "observation": Observation(
                payload={"seat": 1},
                events=[
                    ObservationEvent(
                        event_id=8,
                        event_type="wolf_attack_proposed",
                        payload={
                            "kind": "wolf_attack_proposed",
                            "target_seat": 4,
                            "window_id": "night_actions",
                            "night_round": 0,
                        },
                    )
                ],
            )
        }
    )

    response = DemoRuntime("http://127.0.0.1", "token")._action_for(
        request, _witch_status(104, 103)
    )

    assert response.actions[0].action_code == 104
    assert response.actions[0].targets == [4]


def test_demo_runtime_does_not_heal_without_a_private_knife_notice() -> None:
    request = _request(
        "witch-2",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions",
            allowed_action_codes=[104, 299],
            allow_pass=True,
            candidate_seats=[2, 3],
        ),
    )

    response = DemoRuntime("http://127.0.0.1", "token")._action_for(request, _witch_status(104))

    assert response.actions[0].action_code == 299
    assert response.actions[0].targets == []


def test_demo_runtime_rejects_a_knife_notice_from_another_window_or_round() -> None:
    request = _request(
        "witch-old-notice",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions-r1",
            allowed_action_codes=[104, 299],
            allow_pass=True,
            candidate_seats=[2, 3, 4],
        ),
    ).model_copy(
        update={
            "observation": Observation(
                payload={"seat": 1, "night_round": 1, "round_no": 1},
                events=[
                    ObservationEvent(
                        event_id=3,
                        event_type="wolf_attack_proposed",
                        payload={
                            "kind": "wolf_attack_proposed",
                            "target_seat": 3,
                            "window_id": "night_actions",
                            "night_round": 0,
                        },
                    ),
                    ObservationEvent(
                        event_id=4,
                        event_type="wolf_attack_proposed",
                        payload={
                            "kind": "wolf_attack_proposed",
                            "target_seat": 4,
                            "window_id": "other-night-actions-r1",
                            "night_round": 1,
                        },
                    ),
                ],
            )
        }
    )

    response = DemoRuntime("http://127.0.0.1", "token")._action_for(request, _witch_status(104))

    assert response.actions[0].action_code == 299
    assert response.actions[0].targets == []


def _guard_status(previous_target: int | None) -> dict[str, object]:
    not_actor = {
        "op": "ne",
        "left": {"op": "ref", "source": "item", "name": "seat"},
        "right": {"op": "ref", "source": "actor", "name": "seat"},
    }
    not_previous_target = {
        "op": "ne",
        "left": {"op": "ref", "source": "item", "name": "seat"},
        "right": {"op": "ref", "source": "skill_state", "name": "previous_target"},
    }
    return {
        "abilities": [
            {
                "skill_id": "unfamiliar_guard",
                "action_code": 987,
                "kind": "ACTIVE",
                "state": {"previous_target": previous_target},
                "history": [],
                "usage_limit": {"max_uses": 1, "scope": "ROUND"},
                "uses_consumed": 0,
                "target_rule": {
                    "min_targets": 1,
                    "max_targets": 1,
                    "allow_self": False,
                    "selector": {
                        "op": "select",
                        "source": "players",
                        "where": {"op": "and", "values": [not_actor, not_previous_target]},
                        "map": {"op": "ref", "source": "item", "name": "seat"},
                    },
                },
            }
        ],
        "actions": [{"action_code": 299, "action_id": "pass", "is_pass": True}],
        "resources": {},
    }


def test_demo_runtime_uses_own_skill_state_to_block_the_previous_guard_target() -> None:
    first_night = _request(
        "guard-night-one",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions-r1",
            allowed_action_codes=[987, 299],
            allow_pass=True,
            candidate_seats=[2, 3, 4],
        ),
    ).model_copy(update={"observation": Observation(payload={"seat": 1, "round_no": 1})})
    first = DemoRuntime("http://127.0.0.1", "token")._action_for(first_night, _guard_status(None))
    assert first.actions[0].action_code == 987
    previous_target = first.actions[0].targets[0]

    second_night = first_night.model_copy(
        update={
            "request_id": "guard-night-two",
            "logical_request_id": "guard-night-two",
            "action_window": first_night.action_window.model_copy(
                update={"window_id": "night_actions-r2"}
            ),
            "observation": Observation(payload={"seat": 1, "round_no": 2}),
        }
    )
    second = DemoRuntime("http://127.0.0.1", "token")._action_for(
        second_night, _guard_status(previous_target)
    )
    assert second.actions[0].action_code == 987
    assert second.actions[0].targets[0] != previous_target


def test_demo_runtime_keeps_public_candidate_union_when_target_identity_is_hidden() -> None:
    request = _request(
        "unknown-identity",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions",
            allowed_action_codes=[987, 299],
            allow_pass=True,
            candidate_seats=[2, 3],
        ),
    )
    status = _guard_status(None)
    ability = status["abilities"][0]
    assert isinstance(ability, dict)
    target_rule = ability["target_rule"]
    assert isinstance(target_rule, dict)
    selector = target_rule["selector"]
    assert isinstance(selector, dict)
    selector["where"] = {
        "op": "eq",
        "left": {"op": "ref", "source": "item", "name": "faction_id"},
        "right": {"op": "literal", "value": "wolves"},
    }

    response = DemoRuntime("http://127.0.0.1", "token")._action_for(request, status)

    assert response.actions[0].action_code == 987
    assert response.actions[0].targets in ([2], [3])


def test_demo_runtime_applies_role_facts_explicitly_visible_to_the_seat() -> None:
    request = _request(
        "visible-team-roster",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions",
            allowed_action_codes=[987, 299],
            allow_pass=True,
            candidate_seats=[2, 3],
            visible_context={
                "player_facts": [
                    {"seat": 2, "alive": True, "faction_id": "village"},
                    {"seat": 3, "alive": True, "faction_id": "wolves"},
                ]
            },
        ),
    )
    status = _guard_status(None)
    ability = status["abilities"][0]
    assert isinstance(ability, dict)
    target_rule = ability["target_rule"]
    assert isinstance(target_rule, dict)
    selector = target_rule["selector"]
    assert isinstance(selector, dict)
    selector["where"] = {
        "op": "eq",
        "left": {"op": "ref", "source": "item", "name": "faction_id"},
        "right": {"op": "literal", "value": "wolves"},
    }

    response = DemoRuntime("http://127.0.0.1", "token")._action_for(request, status)

    assert response.actions[0].action_code == 987
    assert response.actions[0].targets == [3]


def test_demo_runtime_uses_frozen_pass_metadata_when_skill_status_contains_pass_instance() -> None:
    from types import SimpleNamespace

    from werewolf.game.actions import load_action_registry

    state = SimpleNamespace(
        game_id="classic-pass-fallback",
        ruleset=SimpleNamespace(snapshot_id="snapshot-classic"),
        phase=GamePhase.NIGHT_ACTION,
        round_no=1,
        day_no=1,
        players={
            1: SimpleNamespace(
                seat=1,
                session_epoch=0,
                role_id="classic_role",
                alive=True,
                death_cause=None,
                skill_resources={"witch_poison": 0},
            )
        },
        ability_instances=(
            {
                "ability_instance_id": "pass-instance",
                "skill_id": "classic_pass",
                "actor_seat": 1,
                "action_code": 299,
                "uses_consumed": 0,
                "consumed": False,
                "enabled": True,
            },
            {
                "ability_instance_id": "ordinary-instance",
                "skill_id": "witch_poison",
                "actor_seat": 1,
                "action_code": 103,
                "uses_consumed": 0,
                "consumed": False,
                "enabled": True,
            },
        ),
        rule_state=(),
        rule_ledger=(),
        action_windows={},
        pending_resolution=None,
        sheriff_seat=None,
        sheriff_badge=None,
        sheriff_election=None,
        events=(),
        resolutions=(),
    )
    execution_package = {
        "skills": [
            {
                "skill_id": "classic_pass",
                "action_code": 299,
                "timing": ["NIGHT_ACTION"],
                "targets": {"min_targets": 0, "max_targets": 0},
                "usage": {"scope": "GAME", "costs": []},
            },
            {
                "skill_id": "witch_poison",
                "action_code": 103,
                "timing": ["NIGHT_ACTION"],
                "targets": {"min_targets": 1, "max_targets": 1},
                "usage": {
                    "scope": "GAME",
                    "costs": [{"resource_id": "witch_poison", "amount": 1}],
                },
            },
        ]
    }
    status = project_skill_status(
        state,
        game_id="classic-pass-fallback",
        snapshot_id="snapshot-classic",
        seat=1,
        session_epoch=0,
        execution_package=execution_package,
        action_registry=load_action_registry(),
    )
    pass_action = next(action for action in status["actions"] if action["is_pass"])
    pass_code = pass_action["action_code"]
    assert any(ability["action_code"] == pass_code for ability in status["abilities"])

    request = _request(
        "classic-pass-fallback",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions-r1",
            allowed_action_codes=[103, pass_code],
            allow_pass=True,
            candidate_seats=[],
        ),
    )
    response = DemoRuntime("http://127.0.0.1", "token")._action_for(request, status)

    assert [(action.action_code, action.targets) for action in response.actions] == [
        (pass_code, [])
    ]


def test_skill_status_recomputes_round_scoped_use_count_and_projects_only_own_history() -> None:
    from types import SimpleNamespace

    old_use = {
        "record_id": "use-r1",
        "request_id": "request-r1",
        "ability_instance_id": "own-instance",
        "skill_id": "unfamiliar_guard",
        "action_code": 987,
        "actor_seat": 1,
        "round_number": 1,
        "targets": [4],
        "passed": False,
        "successful": True,
        "disposition": "ACCEPTED",
    }
    passed_use = {
        **old_use,
        "record_id": "pass-r2",
        "request_id": "request-pass-r2",
        "round_number": 2,
        "targets": [],
        "passed": True,
        "disposition": "PASSED",
    }
    other_use = {
        **old_use,
        "record_id": "other-use",
        "ability_instance_id": "other-instance",
        "actor_seat": 2,
    }
    state = SimpleNamespace(
        game_id="game-1",
        ruleset=SimpleNamespace(snapshot_id="snapshot-1"),
        phase=GamePhase.NIGHT_ACTION,
        round_no=2,
        day_no=1,
        players={
            1: SimpleNamespace(
                seat=1,
                session_epoch=0,
                role_id="role-one",
                alive=True,
                death_cause=None,
                skill_resources={},
            )
        },
        ability_instances=(
            {
                "ability_instance_id": "own-instance",
                "skill_id": "unfamiliar_guard",
                "actor_seat": 1,
                "action_code": 987,
                "uses_consumed": 1,
                "consumed": False,
                "enabled": True,
            },
        ),
        rule_state=(),
        rule_ledger=({"history_updates": [old_use, passed_use, other_use]},),
        action_windows={},
        pending_resolution=None,
        sheriff_seat=None,
        sheriff_badge=None,
        sheriff_election=None,
        events=(),
        resolutions=(),
    )
    execution_package = {
        "skills": [
            {
                "skill_id": "unfamiliar_guard",
                "action_code": 987,
                "targets": {
                    "min_targets": 1,
                    "max_targets": 1,
                    "selector": {"op": "select", "source": "players"},
                },
                "usage": {
                    "max_uses": 1,
                    "scope": "ROUND",
                    "pass_records": True,
                    "pass_updates_history": False,
                    "charge_on_pass": True,
                    "costs": [],
                },
                "timing": ["NIGHT_ACTION"],
            }
        ]
    }

    status = project_skill_status(
        state,
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=1,
        session_epoch=0,
        execution_package=execution_package,
    )

    ability = status["abilities"][0]
    assert ability["uses_consumed"] == 0
    assert ability["consumed"] is False
    history = ability["history"]
    assert isinstance(history, list)
    assert [item["record_id"] for item in history] == ["use-r1", "pass-r2"]
    assert history[0]["target_seat"] == 4
    assert history[1]["passed"] is True
    assert ability["pass_updates_history"] is False
    assert ability["charge_on_pass"] is True

    execution_package["skills"][0]["usage"]["pass_updates_history"] = True
    charged_status = project_skill_status(
        state,
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=1,
        session_epoch=0,
        execution_package=execution_package,
    )
    charged_ability = charged_status["abilities"][0]
    assert charged_ability["uses_consumed"] == 1
    assert charged_ability["consumed"] is True


def test_demo_runtime_vote_respects_a_self_candidate_from_the_window() -> None:
    request = _request(
        "vote-self",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="vote",
            allowed_action_codes=[201],
            candidate_seats=[1],
        ),
    )

    response = DemoRuntime("http://127.0.0.1", "token")._action_for(request, {201: {}})

    assert response.actions[0].action_code == 201
    assert response.actions[0].targets == [1]


def test_demo_runtime_does_not_reintroduce_self_when_all_targets_are_filtered() -> None:
    request = _request(
        "guard-self",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions",
            allowed_action_codes=[103],
            candidate_seats=[1],
        ),
    )

    with pytest.raises(RuntimeProtocolError, match="authorized action"):
        DemoRuntime("http://127.0.0.1", "token")._action_for(request, _witch_status(103))
