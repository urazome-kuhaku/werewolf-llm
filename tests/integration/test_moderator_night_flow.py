"""Integration coverage for the moderator's board-driven night adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_serial_speech import _BlockingRuntime

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    GameManager,
    GameState,
    GrantedAbility,
    PlayerState,
    RulesetRef,
    load_action_registry,
)
from werewolf.game.events import EventType
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.role import ResourceDefinition, TargetKind, TargetRule, UsageLimit
from werewolf.moderator import ModeratorError, ModeratorShell
from werewolf.moderator.night_flow import ModeratorNightError, ModeratorNightFlow
from werewolf.runtime.player_runtime import (
    ActionResponse,
    InitialContext,
    RuntimeConfig,
    Speech,
    SpeechResponse,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _board() -> BoardDefinition:
    return BoardDefinition.model_validate(
        {
            "schema_version": 1,
            "kind": "board",
            "id": "fictional-board",
            "version": "1.0.0",
            "name": "夜间主持器测试板",
            "aliases": [],
            "locale": "zh-CN",
            "status": "published",
            "reviewed_by": "GM",
            "reviewed_at": "2026-09-27",
            "summary": "用于验证主持器夜间命令。",
            "seat_count": 4,
            "factions": {"town": 2, "wolf": 2},
            "roles": [
                {
                    "role_ref": {"id": "wolf", "version": "1.0.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
                {
                    "role_ref": {"id": "villager", "version": "1.0.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
            ],
            "victory": {
                "mode": "eliminate_side",
                "winning_sides": ["town", "wolf"],
                "check_phases": ["VICTORY_CHECK"],
            },
            "wolf_team_visibility": {
                "members_know_each_other": True,
                "discussion_enabled": True,
                "identity_visibility": "members",
            },
            "knife_rule": {"selection_mode": "consensus", "target_visibility": "wolf_team"},
            "night_windows": [
                {"window_id": "wolf_team_chat", "visible_to": ["wolf"]},
                {"window_id": "wolf_kill", "depends_on": ["wolf_team_chat"]},
                {
                    "window_id": "night_resolve",
                    "phase": "NIGHT_RESOLVE",
                    "depends_on": ["wolf_kill"],
                },
            ],
            "day_flow": {
                "announce_deaths": True,
                "vote": {
                    "visibility_during_collection": "secret",
                    "reveal_after_close": "totals_only",
                    "tie_policy": "no_exile_on_tie",
                },
            },
            "mechanics": ["night-resolution@1.0.0"],
            "interactions": ["night-edge@1.0.0"],
            "reading_plan": {
                "board_ref": {"id": "fictional-board", "version": "1.0.0"},
                "bootstrap_topics": ["board:overview"],
                "role_required_topics": {"wolf": ["role:wolf"]},
                "phase_topics": {"NIGHT_ACTION": ["mechanic:night-resolution"]},
                "high_risk_topics": ["mechanic:night-resolution"],
                "suggested_queries": [],
            },
            "claim_refs": ["claim-board"],
            "source_refs": ["source-board"],
        }
    )


def _state() -> GameState:
    ability = GrantedAbility(
        ability_id="kill",
        action_code=101,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=(GamePhase.NIGHT_ACTION,),
        target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
    )
    return GameState(
        game_id="game-1",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        ruleset=RulesetRef(
            board_id="fictional-board",
            version="1.0.0",
            snapshot_id="ruleset-test",
            manifest_sha256="a" * 64,
        ),
        players={
            1: PlayerState(
                seat=1,
                role_id="wolf",
                faction_id="wolf",
                granted_abilities=(ability,),
                session_epoch=1,
            ),
            2: PlayerState(
                seat=2,
                role_id="wolf",
                faction_id="wolf",
                granted_abilities=(ability,),
                session_epoch=1,
            ),
            3: PlayerState(seat=3, role_id="villager", faction_id="town", session_epoch=1),
            4: PlayerState(seat=4, role_id="villager", faction_id="town", session_epoch=1),
        },
    )


async def _runtime(script=()):
    runtime = ScriptedRuntime(script)
    await runtime.start(
        RuntimeConfig(session_id="session-1"),
        InitialContext(game_id="game-1", seat=1, session_epoch=1),
    )
    return runtime


async def _action_runtime(seat: int, script: list[object]) -> ScriptedRuntime:
    runtime = ScriptedRuntime(script)
    await runtime.start(
        RuntimeConfig(session_id=f"action-session-{seat}"),
        InitialContext(game_id="game-1", seat=seat, session_epoch=1),
    )
    return runtime


async def _team_runtime(seat: int, script: list[object]) -> ScriptedRuntime:
    runtime = ScriptedRuntime(script)
    await runtime.start(
        RuntimeConfig(session_id=f"team-session-{seat}"),
        InitialContext(game_id="game-1", seat=seat, session_epoch=1),
    )
    return runtime


async def _open_action(flow: ModeratorNightFlow) -> None:
    await flow.open()
    # Existing action-window tests focus on the action/resolution boundary.
    # Mark the private team round complete explicitly so the moderator guard
    # still enforces that team speech cannot be skipped in production.
    flow._manager._state = flow.state.model_copy(update={"current_queue": ()})  # type: ignore[attr-defined]
    await flow.advance()
    await flow.open()


def _witch_board() -> BoardDefinition:
    data = _board().model_dump(mode="json")
    role_bindings = [dict(item) for item in data["role_bindings"]]
    role_bindings[1]["count"] = 1
    data["role_bindings"] = [
        *role_bindings,
        {
            "role_ref": {"id": "witch", "version": "1.0.0"},
            "count": 1,
            "effective_rules": {"knows_wolf_target": True},
            "override_claim_refs": ["claim-witch-target"],
        },
    ]
    return BoardDefinition.model_validate(data)


def _witch_state(*, heal_amount: int = 1) -> GameState:
    base = _state()
    heal = GrantedAbility(
        ability_id="heal",
        action_code=104,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=(GamePhase.NIGHT_ACTION,),
        target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
        usage_limit=UsageLimit(max_uses=1),
        resource=ResourceDefinition(
            resource_id="witch_heal",
            initial_amount=1,
            cost_per_use=1,
        ),
    )
    poison = GrantedAbility(
        ability_id="poison",
        action_code=103,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=(GamePhase.NIGHT_ACTION,),
        target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
        usage_limit=UsageLimit(max_uses=1),
        resource=ResourceDefinition(
            resource_id="witch_poison",
            initial_amount=1,
            cost_per_use=1,
        ),
    )
    players = dict(base.players)
    players[2] = PlayerState(
        seat=2,
        role_id="witch",
        faction_id="town",
        granted_abilities=(heal, poison),
        skill_resources={"witch_heal": heal_amount, "witch_poison": 1},
        session_epoch=1,
    )
    return base.model_copy(update={"players": players})


@pytest.mark.asyncio
async def test_night_team_next_runs_frozen_wolf_queue_and_hides_non_wolf() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    wolf_one = await _team_runtime(
        1,
        [
            lambda request: SpeechResponse(
                request_id=request.request_id,
                speech=Speech(text="一号狼发言"),
            )
        ],
    )
    wolf_two = await _team_runtime(
        2,
        [
            lambda request: SpeechResponse(
                request_id=request.request_id,
                speech=Speech(text="二号狼发言"),
            )
        ],
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: wolf_one, 2: wolf_two},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )

    opened = await flow.open()
    assert opened.action_window.allowed_seats == (1, 2)
    assert 3 not in opened.action_window.allowed_seats

    first = await flow.team_next()
    second = await flow.team_next()
    assert first["seat"] == 1
    assert second["seat"] == 2
    assert manager.state.current_queue == ()
    event = manager.state.events[-1]
    assert getattr(event, "audience", ()) == (1, 2)
    assert all(
        getattr(item, "audience", ()) == (1, 2)
        for item in manager.state.events
        if getattr(item, "event_type", None).value == "TEAM_SPEECH"
    )


@pytest.mark.asyncio
async def test_night_team_failed_turn_retries_same_queue_head() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    wolf_one = _BlockingRuntime()
    await wolf_one.start(
        RuntimeConfig(session_id="team-session-1"),
        InitialContext(game_id="game-1", seat=1, session_epoch=1),
    )
    wolf_two = await _team_runtime(
        2,
        [
            lambda request: SpeechResponse(
                request_id=request.request_id,
                speech=Speech(text="二号狼发言"),
            )
        ],
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: wolf_one, 2: wolf_two},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
        timeout_seconds=0.01,
    )
    await flow.open()

    with pytest.raises(ModeratorNightError, match="runtime timed out"):
        await flow.team_next()
    assert manager.state.current_queue == (1, 2)
    assert manager.state.serial_turn is not None

    retried = await flow.team_retry()
    assert retried["seat"] == 1
    assert retried["attempt_no"] == 2
    assert manager.state.current_queue == (2,)


@pytest.mark.asyncio
async def test_night_team_advance_requires_complete_round_and_again_is_explicit() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    wolf_one = await _team_runtime(
        1,
        [
            lambda request: SpeechResponse(
                request_id=request.request_id,
                speech=Speech(text="一号狼发言"),
            ),
            lambda request: SpeechResponse(
                request_id=request.request_id,
                speech=Speech(text="追加讨论"),
            ),
        ],
    )
    wolf_two = await _team_runtime(
        2,
        [
            lambda request: SpeechResponse(
                request_id=request.request_id,
                speech=Speech(text="二号狼发言"),
            ),
            lambda request: SpeechResponse(
                request_id=request.request_id,
                speech=Speech(text="追加讨论二号"),
            ),
        ],
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: wolf_one, 2: wolf_two},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )
    await flow.open()

    with pytest.raises(ModeratorNightError, match="TEAM_SPEECH_NOT_STARTED"):
        await flow.advance()
    await flow.team_next()
    with pytest.raises(ModeratorNightError, match="TEAM_SPEECH_INCOMPLETE"):
        await flow.advance()
    await flow.team_next()
    assert manager.state.current_queue == ()

    again = await flow.team_again()
    assert again["queue"] == [1, 2]
    assert manager.state.current_queue == (1, 2)
    with pytest.raises(ModeratorNightError, match="TEAM_SPEECH_INCOMPLETE"):
        await flow.advance()


@pytest.mark.asyncio
async def test_night_flow_opens_windows_and_authorizes_one_wolf_submitter() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    runtime = await _runtime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [3]}],
            )
        ]
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: runtime},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )

    team = await flow.open()
    assert team.action_window.allowed_seats == (1, 2)
    manager._state = manager.state.model_copy(update={"current_queue": ()})  # type: ignore[attr-defined]
    await flow.advance()
    action = await flow.open()
    assert action.action_window.allowed_seats == (1,)
    result = await flow.action_next()
    assert result["seat"] == 1
    assert manager.state.action_requests
    assert manager.state.moderator_audit[-1]["operation"] == "NIGHT_COORDINATOR_SELECTED"


@pytest.mark.asyncio
async def test_witch_peeks_committed_wolf_target_before_runtime_turn() -> None:
    manager = GameManager(_witch_state(), registry=load_action_registry())
    observed: list[object] = []

    wolf = await _action_runtime(
        1,
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [3]}],
            )
        ],
    )

    def witch_turn(request: object) -> ActionResponse:
        observed.extend(request.observation.events)  # type: ignore[attr-defined]
        return ActionResponse(
            request_id=request.request_id,  # type: ignore[attr-defined]
            actions=[{"action_code": 299, "targets": []}],
        )

    witch = await _action_runtime(2, [witch_turn])
    flow = ModeratorNightFlow(
        manager,
        _witch_board(),
        {1: wolf, 2: witch},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )

    await _open_action(flow)
    await flow.action_next(1)
    await flow.action_next(2)

    target_events = [
        event
        for event in observed
        if getattr(event, "event_type", None) == EventType.WITCH_TARGET.value
    ]
    assert len(target_events) == 1
    assert target_events[0].payload["target_seat"] == 3
    assert (
        len([event for event in manager.state.events if event.event_type is EventType.WITCH_TARGET])
        == 1
    )

    action_window = flow._current_action_window()
    event_count = len(manager.state.events)
    await flow._publish_witch_target_notice(action_window)
    assert len(manager.state.events) == event_count


@pytest.mark.asyncio
async def test_witch_gets_no_knife_notice_when_wolves_pass() -> None:
    manager = GameManager(_witch_state(), registry=load_action_registry())
    observed: list[object] = []
    wolf = await _action_runtime(
        1,
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 299, "targets": []}],
            )
        ],
    )

    def witch_turn(request: object) -> ActionResponse:
        observed.extend(request.observation.events)  # type: ignore[attr-defined]
        return ActionResponse(
            request_id=request.request_id,  # type: ignore[attr-defined]
            actions=[{"action_code": 299, "targets": []}],
        )

    witch = await _action_runtime(2, [witch_turn])
    flow = ModeratorNightFlow(
        manager,
        _witch_board(),
        {1: wolf, 2: witch},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )

    await _open_action(flow)
    await flow.action_next(1)
    await flow.action_next(2)

    notice_events = [
        event
        for event in observed
        if getattr(event, "event_type", None) == EventType.PRIVATE_NOTICE.value
    ]
    assert len(notice_events) == 1
    assert notice_events[0].payload["content"] == "本夜没有狼人刀口。"


@pytest.mark.asyncio
async def test_witch_with_exhausted_heal_does_not_receive_knife_notice() -> None:
    manager = GameManager(_witch_state(heal_amount=0), registry=load_action_registry())
    observed: list[object] = []
    wolf = await _action_runtime(
        1,
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [3]}],
            )
        ],
    )

    def witch_turn(request: object) -> ActionResponse:
        observed.extend(request.observation.events)  # type: ignore[attr-defined]
        return ActionResponse(
            request_id=request.request_id,  # type: ignore[attr-defined]
            actions=[{"action_code": 299, "targets": []}],
        )

    witch = await _action_runtime(2, [witch_turn])
    flow = ModeratorNightFlow(
        manager,
        _witch_board(),
        {1: wolf, 2: witch},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )

    await _open_action(flow)
    await flow.action_next(1)
    await flow.action_next(2)

    assert not [
        event
        for event in observed
        if getattr(event, "event_type", None)
        in {EventType.WITCH_TARGET.value, EventType.PRIVATE_NOTICE.value}
    ]


@pytest.mark.asyncio
async def test_night_flow_failed_turn_can_retry_and_resolve_requires_explicit_ruling() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    runtime = await _runtime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [1]}],
            ),
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [3]}],
            ),
        ]
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: runtime},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )
    await _open_action(flow)

    with pytest.raises(ModeratorNightError, match="TARGET_NOT_ALLOWED"):
        await flow.action_next()
    retried = await flow.action_retry()
    assert retried["attempt_no"] == 2

    await flow.advance()
    await flow.open()
    with pytest.raises(ModeratorNightError, match="explicit moderator resolutions"):
        await flow.resolve()


@pytest.mark.asyncio
async def test_night_flow_resolve_reads_a_complete_action_resolution_file(
    tmp_path: Path,
) -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    runtime = await _runtime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [3]}],
            )
        ]
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: runtime},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )
    await _open_action(flow)
    await flow.action_next()
    await flow.advance()
    await flow.open()

    request_id, raw_request = next(iter(manager.state.action_requests.items()))
    assert isinstance(raw_request, dict)
    resolution_file = tmp_path / "night-resolution.json"
    resolution_file.write_text(
        json.dumps(
            [
                {
                    "schema_version": 1,
                    "resolution_id": "resolution-1",
                    "bundle_id": "bundle-1",
                    "game_id": manager.state.game_id,
                    "window_id": raw_request["window_id"],
                    "request_id": request_id,
                    "session_epoch": raw_request["session_epoch"],
                    "base_revision": manager.state.state_revision,
                    "status": "CONFIRMED",
                    "actions": [
                        {
                            "action_index": 0,
                            "requested_action": raw_request["actions"][0],
                            "resource_cost": 0,
                            "effects": [],
                        }
                    ],
                    "moderator_id": "moderator-test",
                    "created_at": NOW.isoformat(),
                }
            ]
        ),
        encoding="utf-8",
    )

    committed = await flow.resolve((str(resolution_file),))

    assert committed.phase is GamePhase.DAY_ANNOUNCE
    assert committed.action_requests[request_id]["status"] == "CONFIRMED"
    assert committed.action_windows["wolf_kill"]["closed_at"] is not None


@pytest.mark.asyncio
async def test_night_flow_pending_is_private_and_filters_to_current_night_window() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    runtime = await _runtime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [3]}],
            )
        ]
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: runtime},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )

    with pytest.raises(ModeratorNightError, match="requires NIGHT_RESOLVE"):
        flow.pending()

    await _open_action(flow)
    await flow.action_next()
    await flow.advance()
    await flow.open()

    request_id, raw_request = next(iter(manager.state.action_requests.items()))
    assert isinstance(raw_request, dict)
    requests = dict(manager.state.action_requests)
    requests["historical-request"] = {
        **raw_request,
        "request_id": "historical-request",
        "window_id": "wolf_kill-r99",
        "status": "PENDING",
    }
    requests["resolved-request"] = {
        **raw_request,
        "request_id": "resolved-request",
        "status": "CONFIRMED",
    }
    manager._state = manager.state.model_copy(update={"action_requests": requests})  # type: ignore[attr-defined]

    pending = flow.pending()

    assert pending["private"] is True
    assert pending["sensitive"] is True
    assert pending["action_window_id"] == "wolf_kill"
    assert pending["resolve_window_id"] == "night_resolve"
    assert pending["base_revision"] == manager.state.state_revision
    records = pending["pending_requests"]
    assert isinstance(records, list)
    assert len(records) == 1
    record = records[0]
    assert record["request_id"] == request_id
    assert record["bundle_id"] == request_id
    assert record["window_id"] == "wolf_kill"
    assert record["session_epoch"] == raw_request["session_epoch"]
    assert record["base_revision"] == manager.state.state_revision
    assert record["actions"] == [
        {
            "action_code": 101,
            "targets": [3],
            "parameters": {},
            "reason_public": None,
        }
    ]


@pytest.mark.asyncio
async def test_night_flow_resolve_rejects_extra_and_stale_records_without_mutating_state(
    tmp_path: Path,
) -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    runtime = await _runtime(
        [
            lambda request: ActionResponse(
                request_id=request.request_id,
                actions=[{"action_code": 101, "targets": [3]}],
            )
        ]
    )
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {1: runtime},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )
    await _open_action(flow)
    await flow.action_next()
    await flow.advance()
    await flow.open()
    request_id, raw_request = next(iter(manager.state.action_requests.items()))
    assert isinstance(raw_request, dict)

    def record(*, request: str, base_revision: int, resolution_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "resolution_id": resolution_id,
            "bundle_id": f"bundle-{resolution_id}",
            "game_id": manager.state.game_id,
            "window_id": raw_request["window_id"],
            "request_id": request,
            "session_epoch": raw_request["session_epoch"],
            "base_revision": base_revision,
            "status": "CONFIRMED",
            "actions": [
                {
                    "action_index": 0,
                    "requested_action": raw_request["actions"][0],
                    "resource_cost": 0,
                    "effects": [],
                }
            ],
            "moderator_id": "moderator-test",
            "created_at": NOW.isoformat(),
        }

    before = manager.state
    extra_file = tmp_path / "extra.json"
    extra_file.write_text(
        json.dumps(
            [
                record(
                    request=request_id,
                    base_revision=before.state_revision,
                    resolution_id="resolution-1",
                ),
                record(
                    request="stale-request",
                    base_revision=before.state_revision,
                    resolution_id="resolution-2",
                ),
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModeratorNightError, match="RESOLUTION_INCOMPLETE"):
        await flow.resolve((str(extra_file),))
    assert manager.state == before

    stale_file = tmp_path / "stale.json"
    stale_file.write_text(
        json.dumps(
            [
                record(
                    request=request_id,
                    base_revision=before.state_revision - 1,
                    resolution_id="resolution-stale",
                )
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModeratorNightError, match="REVISION_MISMATCH"):
        await flow.resolve((str(stale_file),))
    assert manager.state == before


@pytest.mark.asyncio
async def test_shell_night_commands_expose_status_and_reject_ambiguous_commands() -> None:
    manager = GameManager(_state(), registry=load_action_registry())
    runtime = await _runtime()
    board = _board()
    flow = ModeratorNightFlow(
        manager,
        board,
        {1: runtime},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )
    shell = ModeratorShell("unused.yaml")
    shell.manager = manager
    shell.runtime_bundle = SimpleNamespace(board=board)
    shell.session_service = SimpleNamespace(runtimes={1: runtime})
    shell._night_flow = flow

    status = await shell.execute("night status")
    assert status is not None
    assert status["night"]["wolf_coordinator_seat"] == 1  # type: ignore[index]
    with pytest.raises(ModeratorError, match="night action requires"):
        await shell.execute("night action unknown")
    with pytest.raises(ModeratorError, match="explicit moderator resolutions"):
        await shell.execute("night resolve")


@pytest.mark.asyncio
async def test_night_flow_rebuilds_wolf_coordinator_after_round_state_changes() -> None:
    initial = _state()
    manager = GameManager(initial, registry=load_action_registry())
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )

    # Simulate the authoritative snapshot at the next night boundary: the
    # first wolf died and the second wolf still has an available kill grant.
    next_round = initial.model_copy(
        update={
            "round_no": 1,
            "phase": GamePhase.NIGHT_TEAM_CHAT,
            "players": {
                1: initial.players[1].model_copy(update={"alive": False}),
                2: initial.players[2],
                3: initial.players[3],
                4: initial.players[4],
            },
        }
    )
    manager._state = next_round  # type: ignore[attr-defined]

    assert flow.wolf_coordinator_seat == 2
    team = await flow.open()
    assert team.action_window.window_id == "wolf_team_chat-r1"
    assert team.action_window.allowed_seats == (2,)

    manager._state = manager.state.model_copy(update={"current_queue": ()})  # type: ignore[attr-defined]
    await flow.advance()
    action = await flow.open()
    assert action.action_window.window_id == "wolf_kill-r1"
    assert action.action_window.allowed_seats == (2,)


@pytest.mark.asyncio
async def test_night_flow_fail_closes_when_no_authorized_wolf_is_alive() -> None:
    initial = _state()
    manager = GameManager(initial, registry=load_action_registry())
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )
    manager._state = initial.model_copy(
        update={
            "round_no": 1,
            "phase": GamePhase.NIGHT_TEAM_CHAT,
            "players": {
                1: initial.players[1].model_copy(update={"alive": False}),
                2: initial.players[2].model_copy(update={"alive": False}),
                3: initial.players[3],
                4: initial.players[4],
            },
        }
    )  # type: ignore[attr-defined]

    assert flow.wolf_coordinator_seat is None
    assert flow._window_configs["wolf_team_chat"].allowed_seats == ()  # type: ignore[attr-defined]
    with pytest.raises(ModeratorNightError, match="NO_ELIGIBLE_SEATS"):
        await flow.open()


@pytest.mark.asyncio
async def test_exhausted_first_wolf_grant_selects_next_available_submitter() -> None:
    initial = _state()
    exhausted = (
        initial.players[1]
        .granted_abilities[0]
        .model_copy(update={"usage_limit": UsageLimit(max_uses=1), "uses_consumed": 1})
    )
    manager = GameManager(initial, registry=load_action_registry())
    flow = ModeratorNightFlow(
        manager,
        _board(),
        {},
        snapshot_id="ruleset-test",
        clock=lambda: NOW,
    )
    manager._state = initial.model_copy(
        update={
            "round_no": 1,
            "phase": GamePhase.NIGHT_ACTION,
            "players": {
                1: initial.players[1].model_copy(update={"granted_abilities": (exhausted,)}),
                2: initial.players[2],
                3: initial.players[3],
                4: initial.players[4],
            },
        }
    )  # type: ignore[attr-defined]

    assert flow.wolf_coordinator_seat == 2
    assert flow._window_configs["wolf_kill"].allowed_seats == (2,)  # type: ignore[attr-defined]
