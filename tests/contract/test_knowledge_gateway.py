"""Contract coverage for the loopback Knowledge Gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import BaseModel, ConfigDict

from werewolf.domain.enums import GamePhase
from werewolf.game.state import (
    GameState,
    GrantedAbility,
    GrantedTriggerAbility,
    PlayerState,
    RulesetRef,
)
from werewolf.knowledge.compiler import CompiledKnowledgePackage, EffectiveRoleProfile
from werewolf.knowledge.gateway import KnowledgeGateway, KnowledgeReceipt
from werewolf.knowledge.indexes import KnowledgeIndex, KnowledgeIndexDocument
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.role import (
    ResourceDefinition,
    TargetRule,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
    UsageLimit,
)
from werewolf.knowledge.sections import MarkdownDocument, MarkdownSection
from werewolf.knowledge.service import KnowledgeService
from werewolf.knowledge.skill_status import project_skill_status


class _Role(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    name: str
    public_summary: str
    source_refs: list[str]


def _compiled() -> CompiledKnowledgePackage:
    board = VersionedRef(id="classic", version="1.0.0")
    records = (
        KnowledgeIndexDocument(
            kind="board",
            id="classic",
            version="1.0.0",
            title="经典板子",
            body="十二人板子。",
        ),
        KnowledgeIndexDocument(
            kind="role",
            id="witch",
            version="1.0.0",
            title="女巫",
            body="拥有解药和毒药。",
            related_ids=(board.format(),),
        ),
        KnowledgeIndexDocument(
            kind="mechanic",
            id="voting",
            version="1.0.0",
            title="投票",
            body="白天投票。",
            related_ids=(board.format(),),
        ),
        KnowledgeIndexDocument(
            kind="interaction",
            id="witch-hunter",
            version="1.0.0",
            title="女巫与猎人",
            body="猎人被毒不能开枪。",
            related_ids=("witch", "hunter", "witch.poison"),
        ),
        KnowledgeIndexDocument(
            kind="topic",
            id="overview",
            version="1.0.0",
            title="概览",
            body="板子概览。",
            topics=("overview",),
            related_ids=(f"board:{board.format()}",),
        ),
    )
    sections = {
        "board:classic@1.0.0": MarkdownDocument(
            title="概览",
            sections=(MarkdownSection(id="overview", title="概览", body="板子概览。"),),
        ),
        "role:witch@1.0.0": MarkdownDocument(
            title="技能",
            sections=(MarkdownSection(id="abilities", title="技能", body="解药和毒药。"),),
        ),
        "mechanic:voting@1.0.0": MarkdownDocument(
            title="投票",
            sections=(MarkdownSection(id="vote", title="投票", body="投票规则。"),),
        ),
        "interaction:witch-hunter@1.0.0": MarkdownDocument(
            title="交互",
            sections=(MarkdownSection(id="resolution", title="结算", body="不能开枪。"),),
        ),
    }
    profile = EffectiveRoleProfile(
        board_ref=board,
        role_ref=VersionedRef(id="witch", version="1.0.0"),
        count=1,
        base_role=_Role(
            role_id="witch",
            name="女巫",
            public_summary="板子上的女巫。",
            source_refs=["official"],
        ),
        effective_rules={"can_self_heal": False},
        override_claim_refs=(),
        sections=sections["role:witch@1.0.0"],
    )
    return CompiledKnowledgePackage(
        board_ref=board,
        documents=records,
        index=KnowledgeIndex.build(records),
        sections=MappingProxyType(sections),
        effective_roles={"witch": profile},
        document_digests={},
        package_payload={"reading_plan": {"bootstrap_topics": ["board:overview"]}},
        manifest_payload={},
        canonical_package_json="{}",
        canonical_manifest_json="{}",
        package_identity="package-1",
        manifest_sha256="manifest-1",
    )


@pytest.fixture()
async def gateway_client() -> AsyncIterator[
    tuple[TestClient, KnowledgeGateway, list[KnowledgeReceipt]]
]:
    receipts: list[KnowledgeReceipt] = []

    async def sink(receipt: KnowledgeReceipt) -> None:
        receipts.append(receipt)

    gateway = KnowledgeGateway(
        KnowledgeService(_compiled(), snapshot_id="snapshot-1"),
        receipt_sink=sink,
    )
    async with TestClient(TestServer(gateway.app)) as client:
        yield client, gateway, receipts


def _token(gateway: KnowledgeGateway) -> str:
    return gateway.issue_token(
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=2,
        session_epoch=4,
    )


def _skill_state(*, session_epoch: int = 4, snapshot_id: str = "snapshot-1") -> GameState:
    timestamp = datetime.now(UTC)
    player = PlayerState(
        seat=2,
        role_id="witch",
        faction_id="good",
        session_epoch=session_epoch,
        skill_resources={"potion": 1},
        granted_abilities=(
            GrantedAbility(
                ability_id="inspect",
                action_code=101,
                timing=GamePhase.NIGHT_ACTION,
                allowed_phases=(GamePhase.NIGHT_ACTION,),
                target_rule=TargetRule(
                    kind="PLAYER", min_targets=1, max_targets=1, allow_self=False
                ),
                usage_limit=UsageLimit(max_uses=2),
                resource=ResourceDefinition(resource_id="potion", initial_amount=2, cost_per_use=1),
                uses_consumed=1,
            ),
        ),
        granted_trigger_abilities=(
            GrantedTriggerAbility(
                ability_id="death-shot",
                action_code=201,
                trigger=TriggerRule(
                    event=TriggerEvent.DEATH_CONFIRMED,
                    allowed_death_causes=["wolf_kill", "exiled"],
                    mode=TriggerMode.PLAYER_CHOICE,
                    effects=[TriggerEffect.OPEN_PLAYER_ACTION],
                    allow_pass=True,
                ),
                target_rule=TargetRule(kind="PLAYER", min_targets=1, max_targets=1),
            ),
        ),
    )
    return GameState(
        game_id="game-1",
        created_at=timestamp,
        updated_at=timestamp,
        phase=GamePhase.NIGHT_ACTION,
        ruleset=RulesetRef(
            board_id="classic",
            version="1.0.0",
            snapshot_id=snapshot_id,
            manifest_sha256="a" * 64,
        ),
        players={2: player},
        action_windows={
            "night-2": {
                "window_id": "night-2",
                "game_id": "game-1",
                "session_epoch": 4,
                "phase": "NIGHT_ACTION",
                "allowed_seats": [2],
                "allowed_action_codes": [101, 201, 299],
                "allow_pass": True,
                "dependencies_satisfied": True,
                "closed_at": None,
                "visible_context": {"target_seats": [1, 3]},
            }
        },
    )


@pytest.mark.parametrize(
    ("path", "method", "payload"),
    [
        ("/v1/board/classic", "get", None),
        ("/v1/role/witch", "get", None),
        ("/v1/mechanic/voting", "get", None),
        ("/v1/topic/overview", "get", None),
        (
            "/v1/interactions/query",
            "post",
            {"subjects": ["witch", "hunter"], "situation": "witch.poison"},
        ),
        ("/v1/search", "post", {"query": "witch", "kinds": ["role"]}),
    ],
)
async def test_six_query_routes_require_token_and_write_receipt(
    gateway_client: tuple[TestClient, KnowledgeGateway, list[KnowledgeReceipt]],
    path: str,
    method: str,
    payload: dict[str, object] | None,
) -> None:
    client, gateway, receipts = gateway_client
    token = _token(gateway)
    response = await getattr(client, method)(
        path,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status == 200
    body = await response.json()
    assert body["status"] == "ok"
    assert body["receipt_id"].startswith("receipt_")
    assert len(receipts) == 1
    assert receipts[0].receipt_id == body["receipt_id"]
    assert receipts[0].result_id == body["result_id"]
    assert receipts[0].game_id == "game-1"
    assert receipts[0].snapshot_id == "snapshot-1"
    assert receipts[0].session_epoch == 4


async def test_health_does_not_require_auth_or_expose_game_context(
    gateway_client: tuple[TestClient, KnowledgeGateway, list[KnowledgeReceipt]],
) -> None:
    client, _, _ = gateway_client
    response = await client.get("/v1/health")
    assert response.status == 200
    body = await response.json()
    assert body == {"schema_version": 1, "status": "ok"}
    assert "game" not in body and "snapshot" not in body


async def test_skill_status_is_seat_scoped_dynamic_and_does_not_write_receipt() -> None:
    state = [_skill_state()]

    async def provider() -> GameState:
        return state[0]

    receipts: list[KnowledgeReceipt] = []
    gateway = KnowledgeGateway(
        KnowledgeService(_compiled(), snapshot_id="snapshot-1"),
        receipt_sink=receipts.append,
        state_provider=provider,
    )
    token = _token(gateway)
    async with TestClient(TestServer(gateway.app)) as client:
        response = await client.get(
            "/v1/game/skills/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status == 200
        payload = await response.json()
        status = payload["skill_status"]
        assert status["role_id"] == "witch"
        assert status["resources"] == {"potion": 1}
        assert status["abilities"][0]["ability_id"] == "inspect"
        assert status["abilities"][0]["uses_consumed"] == 1
        assert status["abilities"][1]["trigger"]["allowed_death_causes"] == [
            "wolf_kill",
            "exiled",
        ]
        assert status["windows"] == [
            {
                "window_id": "night-2",
                "phase": "NIGHT_ACTION",
                "allowed_action_codes": [101, 299],
                "allow_pass": True,
                "dependencies_satisfied": True,
            }
        ]
        assert "faction_id" not in status
        assert "players" not in status
        assert "target_seats" not in payload
        assert "visible_context" not in payload
        assert receipts == []

        state[0] = _skill_state(session_epoch=5)
        stale = await client.get("/v1/game/skills/me", headers={"Authorization": f"Bearer {token}"})
        assert stale.status == 409
        assert (await stale.json())["error"]["code"] == "SESSION_MISMATCH"

        state[0] = _skill_state(snapshot_id="snapshot-2")
        cross_snapshot = await client.get(
            "/v1/game/skills/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert cross_snapshot.status == 409
        assert (await cross_snapshot.json())["error"]["code"] == "SESSION_MISMATCH"


async def test_skill_status_requires_started_state_provider(
    gateway_client: tuple[TestClient, KnowledgeGateway, list[KnowledgeReceipt]],
) -> None:
    client, gateway, _ = gateway_client
    response = await client.get(
        "/v1/game/skills/me", headers={"Authorization": f"Bearer {_token(gateway)}"}
    )
    assert response.status == 503
    assert (await response.json())["error"]["code"] == "SKILL_STATUS_UNAVAILABLE"


def test_skill_status_ignores_stale_team_chat_and_blocked_windows() -> None:
    state = _skill_state().model_copy(
        update={
            "action_windows": {
                "current": {
                    "window_id": "current",
                    "game_id": "game-1",
                    "session_epoch": 4,
                    "phase": "NIGHT_ACTION",
                    "allowed_seats": [2],
                    "allowed_action_codes": [101, 299],
                    "allow_pass": True,
                    "dependencies_satisfied": True,
                    "closed_at": None,
                },
                "old-phase": {
                    "window_id": "old-phase",
                    "game_id": "game-1",
                    "session_epoch": 4,
                    "phase": "NIGHT_RESOLVE",
                    "allowed_seats": [2],
                    "allowed_action_codes": [101, 299],
                    "allow_pass": True,
                    "dependencies_satisfied": True,
                    "closed_at": None,
                },
                "team-chat": {
                    "window_id": "team-chat",
                    "game_id": "game-1",
                    "session_epoch": 4,
                    "phase": "NIGHT_TEAM_CHAT",
                    "allowed_seats": [2],
                    "allowed_action_codes": [299],
                    "allow_pass": True,
                    "dependencies_satisfied": True,
                    "closed_at": None,
                },
                "blocked": {
                    "window_id": "blocked",
                    "game_id": "game-1",
                    "session_epoch": 4,
                    "phase": "NIGHT_ACTION",
                    "allowed_seats": [2],
                    "allowed_action_codes": [101, 299],
                    "allow_pass": True,
                    "dependencies_satisfied": False,
                    "closed_at": None,
                },
            }
        }
    )

    status = project_skill_status(
        state,
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=2,
        session_epoch=4,
    )

    assert status["windows"] == [
        {
            "window_id": "current",
            "phase": "NIGHT_ACTION",
            "allowed_action_codes": [101, 299],
            "allow_pass": True,
            "dependencies_satisfied": True,
        }
    ]


def test_skill_status_trigger_window_requires_pending_bound_ability() -> None:
    player = (
        _skill_state().players[2].model_copy(update={"alive": False, "death_cause": "wolf_kill"})
    )
    trigger_window = {
        "window_id": "resolution-1-trigger-death-shot",
        "game_id": "game-1",
        "session_epoch": 4,
        "phase": "TRIGGER_ACTION",
        "allowed_seats": [2],
        "allowed_action_codes": [201, 299],
        "allow_pass": True,
        "dependencies_satisfied": True,
        "closed_at": None,
        "visible_context": {
            "resolution_id": "resolution-1",
            "ability_id": "death-shot",
            "action_code": 201,
            "trigger_event": "DEATH_CONFIRMED",
        },
    }
    state = _skill_state().model_copy(
        update={
            "phase": GamePhase.TRIGGER_ACTION,
            "players": {2: player},
            "pending_resolution": {
                "status": "TRIGGER_ACTION_REQUIRED",
                "resolution_id": "resolution-1",
                "window_id": "resolution-1-trigger-death-shot",
                "seat": 2,
                "ability_id": "death-shot",
                "action_code": 201,
                "trigger_event": "DEATH_CONFIRMED",
                "death_cause": "wolf_kill",
            },
            "action_windows": {"trigger": trigger_window},
        }
    )

    status = project_skill_status(
        state,
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=2,
        session_epoch=4,
    )
    assert status["windows"] == [
        {
            "window_id": "resolution-1-trigger-death-shot",
            "phase": "TRIGGER_ACTION",
            "allowed_action_codes": [201, 299],
            "allow_pass": True,
            "dependencies_satisfied": True,
        }
    ]

    wrong_binding = state.model_copy(
        update={
            "pending_resolution": {
                **state.pending_resolution,
                "ability_id": "other-ability",
            }
        }
    )
    status = project_skill_status(
        wrong_binding,
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=2,
        session_epoch=4,
    )
    assert status["windows"] == []


def _badge_state(
    *,
    phase: GamePhase = GamePhase.DAY_RESOLVE,
    sheriff_election: dict[str, object] | None = None,
    source_session_epoch: int = 4,
    source_alive: bool = False,
    source_can_vote: bool = True,
    candidate_seats: tuple[int, ...] = (3, 4),
    window_game_id: str = "game-1",
    window_session_epoch: int = 4,
    allowed_seats: tuple[int, ...] = (2,),
    marker_source_seat: int = 2,
    marker_epoch: int = 4,
    marker_office: int = 2,
    marker_window_id: str = "badge-window",
    visible_source_seat: int = 2,
    closed_at: str | None = None,
) -> GameState:
    source = (
        _skill_state(session_epoch=source_session_epoch)
        .players[2]
        .model_copy(update={"alive": source_alive, "can_vote": source_can_vote})
    )
    candidates = {
        seat: PlayerState(
            seat=seat,
            role_id="villager",
            faction_id="good",
            session_epoch=0,
        )
        for seat in (3, 4)
    }
    window = {
        "window_id": "badge-window",
        "game_id": window_game_id,
        "session_epoch": window_session_epoch,
        "phase": phase.value,
        "allowed_seats": list(allowed_seats),
        "allowed_action_codes": [201, 202],
        "min_actions": 1,
        "max_actions": 1,
        "allow_pass": False,
        "dependencies_satisfied": True,
        "closed_at": closed_at,
        "visible_context": {
            "kind": "sheriff_badge",
            "source_seat": visible_source_seat,
            "candidate_seats": list(candidate_seats),
        },
    }
    marker = {
        "status": "OPEN",
        "source_seat": marker_source_seat,
        "source_session_epoch": marker_epoch,
        "office_seat": marker_office,
        "window_id": marker_window_id,
        "candidate_seats": list(candidate_seats),
    }
    return _skill_state().model_copy(
        update={
            "phase": phase,
            "players": {2: source, **candidates},
            "sheriff_seat": 2,
            "action_windows": {"badge-window": window},
            "sheriff_badge": marker,
            "sheriff_election": sheriff_election,
        }
    )


@pytest.mark.parametrize(
    ("source_alive", "source_can_vote", "candidate_seats"),
    [
        (False, True, (3, 4)),
        (True, False, (3, 4)),
        (False, True, ()),
    ],
)
def test_skill_status_projects_bound_sheriff_badge_window(
    source_alive: bool,
    source_can_vote: bool,
    candidate_seats: tuple[int, ...],
) -> None:
    status = project_skill_status(
        _badge_state(
            source_alive=source_alive,
            source_can_vote=source_can_vote,
            candidate_seats=candidate_seats,
        ),
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=2,
        session_epoch=4,
    )

    assert {ability["ability_id"] for ability in status["abilities"]} == {
        "inspect",
        "death-shot",
    }
    assert status["windows"] == [
        {
            "window_id": "badge-window",
            "phase": "DAY_RESOLVE",
            "kind": "SHERIFF_BADGE",
            "allowed_action_codes": [201, 202],
            "allow_pass": False,
            "candidate_seats": list(candidate_seats),
            "dependencies_satisfied": True,
        }
    ]


@pytest.mark.parametrize(
    "updates",
    [
        {"window_game_id": "game-2"},
        {"window_session_epoch": 3},
        {"allowed_seats": (3,)},
        {"marker_office": 3},
        {"marker_window_id": "other-window"},
        {"source_alive": True, "source_can_vote": True},
        {"closed_at": "2026-01-01T00:00:00+00:00"},
    ],
)
def test_skill_status_rejects_unbound_sheriff_badge_window(
    updates: dict[str, object],
) -> None:
    status = project_skill_status(
        _badge_state(**updates),  # type: ignore[arg-type]
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=2,
        session_epoch=4,
    )

    assert status["windows"] == []


async def test_badge_status_is_projected_over_http_during_sheriff_election() -> None:
    state = [_badge_state(phase=GamePhase.DAY_SPEECH, sheriff_election={"status": "VOTING"})]

    async def provider() -> GameState:
        return state[0]

    gateway = KnowledgeGateway(
        KnowledgeService(_compiled(), snapshot_id="snapshot-1"),
        state_provider=provider,
    )
    token = _token(gateway)
    async with TestClient(TestServer(gateway.app)) as client:
        response = await client.get(
            "/v1/game/skills/me", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status == 200
        status = (await response.json())["skill_status"]
        assert status["seat"] == 2
        assert status["windows"] == [
            {
                "window_id": "badge-window",
                "phase": "DAY_SPEECH",
                "kind": "SHERIFF_BADGE",
                "allowed_action_codes": [201, 202],
                "allow_pass": False,
                "candidate_seats": [3, 4],
                "dependencies_satisfied": True,
            }
        ]


async def test_badge_status_rejects_day_speech_without_sheriff_election() -> None:
    state = [_badge_state(phase=GamePhase.DAY_SPEECH)]

    async def provider() -> GameState:
        return state[0]

    gateway = KnowledgeGateway(
        KnowledgeService(_compiled(), snapshot_id="snapshot-1"),
        state_provider=provider,
    )
    token = _token(gateway)
    async with TestClient(TestServer(gateway.app)) as client:
        response = await client.get(
            "/v1/game/skills/me", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status == 200
        status = (await response.json())["skill_status"]
        assert status["seat"] == 2
        assert status["windows"] == []


async def test_badge_status_does_not_expose_the_window_to_another_seat() -> None:
    state = [_badge_state()]

    async def provider() -> GameState:
        return state[0]

    gateway = KnowledgeGateway(
        KnowledgeService(_compiled(), snapshot_id="snapshot-1"),
        state_provider=provider,
    )
    other_token = gateway.issue_token(
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=3,
        session_epoch=0,
    )
    async with TestClient(TestServer(gateway.app)) as client:
        response = await client.get(
            "/v1/game/skills/me",
            headers={"Authorization": f"Bearer {other_token}"},
        )

        assert response.status == 200
        status = (await response.json())["skill_status"]
        assert status["seat"] == 3
        assert status["role_id"] == "villager"
        assert status["windows"] == []


async def test_badge_status_rejects_an_expired_session_epoch_over_http() -> None:
    state = [_badge_state()]

    async def provider() -> GameState:
        return state[0]

    gateway = KnowledgeGateway(
        KnowledgeService(_compiled(), snapshot_id="snapshot-1"),
        state_provider=provider,
    )
    token = _token(gateway)
    async with TestClient(TestServer(gateway.app)) as client:
        state[0] = _badge_state(
            source_session_epoch=5,
            window_session_epoch=5,
            marker_epoch=5,
        )
        response = await client.get(
            "/v1/game/skills/me", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status == 409
        assert (await response.json())["error"]["code"] == "SESSION_MISMATCH"


async def test_authentication_is_required_and_tokens_are_isolated(
    gateway_client: tuple[TestClient, KnowledgeGateway, list[KnowledgeReceipt]],
) -> None:
    client, gateway, _ = gateway_client
    missing = await client.get("/v1/role/witch")
    assert missing.status == 401
    assert (await missing.json())["error"]["code"] == "UNAUTHORIZED"

    other = gateway.issue_token(
        game_id="game-2",
        snapshot_id="snapshot-2",
        seat=9,
        session_epoch=1,
    )
    response = await client.get(
        "/v1/role/witch",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert response.status == 403
    assert "game-2" not in await response.text()
    assert "snapshot-2" not in await response.text()


async def test_board_current_alias_requires_token_and_rejects_other_board_ids(
    gateway_client: tuple[TestClient, KnowledgeGateway, list[KnowledgeReceipt]],
) -> None:
    client, gateway, receipts = gateway_client

    missing = await client.get("/v1/board/current")
    assert missing.status == 401
    assert (await missing.json())["error"]["code"] == "UNAUTHORIZED"

    token = _token(gateway)
    current = await client.get(
        "/v1/board/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert current.status == 200
    current_body = await current.json()
    assert current_body["status"] == "ok"
    assert current_body["snapshot"] == {"id": "snapshot-1", "board": "classic@1.0.0"}
    assert current_body["document"]["id"] == "classic"
    assert len(receipts) == 1

    other = await client.get(
        "/v1/board/other-board",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert other.status == 404
    assert (await other.json())["error"]["code"] == "NOT_FOUND"
    assert len(receipts) == 1


async def test_interaction_not_found_guides_exact_search_without_receipt(
    gateway_client: tuple[TestClient, KnowledgeGateway, list[KnowledgeReceipt]],
) -> None:
    client, gateway, receipts = gateway_client
    token = _token(gateway)
    response = await client.post(
        "/v1/interactions/query",
        json={"subjects": ["witch", "hunter"], "situation": "witch.poison_hunter"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status == 404
    body = await response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "NOT_FOUND"
    details = body["error"]["details"]
    assert details["next_tool"] == "search_rules"
    assert details["next_tool_args"] == {"kinds": ["interaction"], "limit": 4}
    assert details["candidates"] == [
        {
            "ref": "interaction:witch-hunter@1.0.0",
            "subjects": ["witch", "hunter"],
            "situation_key": "witch.poison",
        }
    ]
    assert len(receipts) == 0


async def test_revoke_and_expiry_fail_closed(
    gateway_client: tuple[TestClient, KnowledgeGateway, list[KnowledgeReceipt]],
) -> None:
    client, gateway, _ = gateway_client
    token = _token(gateway)
    assert gateway.revoke_token(token)
    revoked = await client.get(
        "/v1/board/classic",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert revoked.status == 401

    now = [datetime.now(UTC)]
    expiring_gateway = KnowledgeGateway(
        KnowledgeService(_compiled(), snapshot_id="snapshot-1"),
        clock=lambda: now[0],
    )
    expiring = expiring_gateway.issue_token(
        game_id="game-1",
        snapshot_id="snapshot-1",
        seat=2,
        session_epoch=4,
        expires_at=now[0] + timedelta(seconds=1),
    )
    async with TestClient(TestServer(expiring_gateway.app)) as expiring_client:
        now[0] += timedelta(seconds=2)
        response = await expiring_client.get(
            "/v1/board/classic",
            headers={"Authorization": f"Bearer {expiring}"},
        )
        assert response.status == 401
