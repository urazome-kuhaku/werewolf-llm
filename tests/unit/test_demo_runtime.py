"""Focused contract tests for the playable deterministic harness runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from werewolf.domain.enums import GamePhase
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.player_runtime import (
    ActionWindowView,
    Deadline,
    InitialContext,
    Observation,
    ObservationEvent,
    ResponseKind,
    RuntimeConfig,
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
    return {
        "abilities": [
            {
                "action_code": code,
                "target_rule": {"kind": "PLAYER", "allow_self": False},
            }
            for code in codes
        ],
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
                        event_type="witch_target",
                        payload={
                            "kind": "witch_target",
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
                payload={"seat": 1, "night_round": 1},
                events=[
                    ObservationEvent(
                        event_id=3,
                        event_type="witch_target",
                        payload={
                            "kind": "witch_target",
                            "target_seat": 3,
                            "window_id": "night_actions",
                            "night_round": 0,
                        },
                    ),
                    ObservationEvent(
                        event_id=4,
                        event_type="witch_target",
                        payload={
                            "kind": "witch_target",
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
        "witch-3",
        ResponseKind.ACTION,
        window=ActionWindowView(
            window_id="night_actions",
            allowed_action_codes=[103],
            candidate_seats=[1],
        ),
    )

    ability = {103: _witch_status(103)["abilities"][0]}
    assert DemoRuntime._targets_for(103, [1], ability, request) == []
