"""Offline mixed Pi/Demo harness acceptance for the classic candidate board.

The fake process below is deliberately a Pi RPC process, rather than a
second deterministic ``PlayerRuntime``.  This keeps the protocol boundary in
the test: handshake records, prompt records, assistant events, final-text
lookup, strict JSON parsing, and the authoritative action scheduler all remain
in the path.  No provider or model is contacted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
import yaml  # type: ignore[import-untyped]

from werewolf.cli_support.play_setup import build_play_setup
from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionTurnScheduler,
    ActionValidationContext,
    ActionWindow,
    GameManager,
    GameState,
    RulesetRef,
    load_action_registry,
)
from werewolf.game.setup import build_role_assignment_plan
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.preview import experimental_preview
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.runtime_loader import (
    RuntimeKnowledgeBundle,
    load_runtime_knowledge_bundle_from_snapshot,
)
from werewolf.knowledge.snapshot import KnowledgeSnapshotBuilder
from werewolf.moderator.config import PlayerSessionConfig, parse_player_configuration
from werewolf.moderator.sessions import PlayerSessionService
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import (
    ActionWindowView,
    Deadline,
    Observation,
    ResponseKind,
    TurnRequest,
)

BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
GAME_ID_PREFIX = "mixed-harness"
SEED = 20261001
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


class _FakePiProcess:
    """A deterministic process on the real Pi RPC wire boundary."""

    started = False
    closed = False

    def __init__(self, gateway_url: str, token: str, *, seat: int, role_id: str) -> None:
        self._gateway_url = f"{gateway_url.rstrip('/')}/v1"
        self._token = token
        self.seat = seat
        self.role_id = role_id
        self.records: asyncio.Queue[object | None] = asyncio.Queue()
        self.writes: list[dict[str, Any]] = []
        self.knowledge_reads: dict[str, Mapping[str, Any]] = {}
        self._http: aiohttp.ClientSession | None = None
        self._last_response = ""

    async def start(self) -> _FakePiProcess:
        self._http = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self._token}"})
        for path in (
            "/board/current",
            f"/role/{self.role_id}",
            "/game/skills/me",
        ):
            async with self._http.get(f"{self._gateway_url}{path}") as response:
                payload = await response.json(content_type=None)
            if response.status != 200 or not isinstance(payload, Mapping):
                raise AssertionError(f"fake Pi knowledge read failed: {path}")
            self.knowledge_reads[path] = payload
        self.started = True
        return self

    async def send_record(self, record: dict[str, Any]) -> None:
        self.writes.append(record)
        command = record["type"]
        if command in {
            "get_state",
            "set_steering_mode",
            "set_follow_up_mode",
            "set_auto_compaction",
            "set_auto_retry",
            "clear_queue",
            "abort",
        }:
            await self.records.put(
                {"type": "response", "id": record["id"], "command": command, "success": True}
            )
            if command == "abort":
                await self.records.put({"type": "agent_settled"})
            return

        if command == "prompt":
            payload = json.loads(record["message"])
            self._last_response = self._response_text(payload)
            await self.records.put(
                {"type": "response", "id": record["id"], "command": "prompt", "success": True}
            )
            await self.records.put({"type": "agent_start"})
            await self.records.put(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": self._last_response}],
                    },
                }
            )
            await self.records.put({"type": "agent_settled"})
            return

        if command == "get_last_assistant_text":
            await self.records.put(
                {
                    "type": "response",
                    "id": record["id"],
                    "command": command,
                    "success": True,
                    "data": {"text": self._last_response},
                }
            )
            return

        if command == "steer":
            await self.records.put(
                {"type": "response", "id": record["id"], "command": command, "success": True}
            )
            return

        raise AssertionError(f"unexpected Pi RPC command: {command}")

    async def read_record(self) -> object | None:
        return await self.records.get()

    async def close(self, reason: str = "normal shutdown") -> None:
        del reason
        http, self._http = self._http, None
        if http is not None and not http.closed:
            await http.close()
        self.closed = True

    def _response_text(self, payload: Mapping[str, Any]) -> str:
        request_id = cast(str, payload["request_id"])
        kind = payload["expected_kind"]
        if kind == ResponseKind.READY.value:
            receipts = []
            for path in ("/board/current", f"/role/{self.role_id}"):
                receipt = self.knowledge_reads[path].get("receipt_id")
                if isinstance(receipt, str):
                    receipts.append(receipt)
            response: dict[str, Any] = {
                "schema_version": 1,
                "request_id": request_id,
                "kind": "ready",
                "ready": {"knowledge_receipts": receipts},
            }
        elif kind == ResponseKind.SPEECH.value:
            response = {
                "schema_version": 1,
                "request_id": request_id,
                "kind": "speech",
                "speech": {"text": f"Pi seat {self.seat} public observation"},
            }
        elif kind == ResponseKind.ACTION.value:
            window = payload.get("action_window") or {}
            allowed = list(window.get("allowed_action_codes", []))
            candidates = list(window.get("candidate_seats", []))
            action_code = (
                201 if 201 in allowed else next((code for code in allowed if code != 299), 299)
            )
            action: dict[str, Any] = {"action_code": action_code, "targets": []}
            if action_code != 299 and candidates:
                action["targets"] = [candidates[0]]
            response = {
                "schema_version": 1,
                "request_id": request_id,
                "kind": "action",
                "actions": [action],
            }
        else:
            raise AssertionError(f"unsupported fake Pi response kind: {kind}")
        return json.dumps(response, ensure_ascii=False, separators=(",", ":"))


def _turn_request(
    request_id: str,
    *,
    game_id: str,
    seat: int,
    phase: GamePhase,
    expected_kind: ResponseKind,
    action_window: ActionWindowView | None = None,
) -> TurnRequest:
    return TurnRequest(
        request_id=request_id,
        logical_request_id=request_id,
        attempt_no=1,
        game_id=game_id,
        session_epoch=0,
        phase=phase,
        expected_kind=expected_kind,
        action_window=action_window,
        observation=Observation(
            summary="公开测试回合",
            payload={"seat": seat, "phase": phase.value},
        ),
        output_schema={"type": "object"},
        deadline=Deadline(
            soft_deadline=datetime.now(UTC),
            hard_deadline=datetime.now(UTC) + timedelta(seconds=10),
        ),
    )


async def _setup_bundle(output: Path, game_id: str) -> RuntimeKnowledgeBundle:
    snapshot = await KnowledgeSnapshotBuilder(
        CompiledKnowledgeStore(output / "preview" / "compiled"),
        output / "games",
    ).create(game_id, VersionedRef.parse(BOARD_REF))
    return await load_runtime_knowledge_bundle_from_snapshot(snapshot)


def _game_state(
    bundle: RuntimeKnowledgeBundle,
    assignments: object,
    *,
    game_id: str,
    phase: GamePhase,
    action_windows: Mapping[str, object] | None = None,
) -> GameState:
    plan = assignments
    players = cast(Any, plan).players
    return GameState(
        game_id=game_id,
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        ruleset=RulesetRef(
            board_id=bundle.board.board_id,
            version=bundle.board.version,
            snapshot_id=bundle.service.snapshot_id,
            manifest_sha256=bundle.package.manifest_sha256,
        ),
        players=dict(players),
        action_windows=dict(action_windows or {}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pi_seats", [(1, 2, 3), (1, 2, 3, 4)])
async def test_classic_mixed_pi_protocol_identity_and_authoritative_actions(
    tmp_path: Path,
    pi_seats: tuple[int, ...],
) -> None:
    """Exercise both mixed setup sizes without contacting an external model."""

    game_id = f"{GAME_ID_PREFIX}-{len(pi_seats)}"
    output = tmp_path / f"play-{len(pi_seats)}"
    report = await asyncio.to_thread(
        build_play_setup,
        output,
        pi_seats=pi_seats,
        provider="github-copilot",
        model="gpt-6-luna",
        reasoning="medium",
        game_id=game_id,
        seed=SEED,
    )
    assert report["pi_seats"] == list(pi_seats)
    raw_config = yaml.safe_load((output / "game.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw_config, Mapping)
    assert sum(item["runtime"] == "pi" for item in raw_config["players"]) == len(pi_seats)

    with experimental_preview():
        bundle = await _setup_bundle(output, game_id)
        assignments = build_role_assignment_plan(
            bundle.board,
            bundle.package,
            seed=SEED,
            seats=tuple(range(1, 13)),
        )

        skill_seat = next(
            seat
            for seat in pi_seats
            if assignments.players[seat].granted_abilities
            and assignments.players[seat].granted_abilities[0].action_code != 104
        )
        skill_code = assignments.players[skill_seat].granted_abilities[0].action_code
        skill_targets = tuple(seat for seat in range(1, 13) if seat != skill_seat)
        skill_window = ActionWindow(
            window_id=f"mixed-skill-{len(pi_seats)}",
            game_id=game_id,
            session_epoch=0,
            phase=GamePhase.NIGHT_ACTION,
            allowed_seats=(skill_seat,),
            allowed_action_codes=(skill_code,),
            opened_at=NOW,
            visible_context={"candidate_seats": list(skill_targets)},
        )
        skill_state = _game_state(
            bundle,
            assignments,
            game_id=game_id,
            phase=GamePhase.NIGHT_ACTION,
        )
        skill_manager = GameManager(skill_state, registry=load_action_registry())
        await skill_manager.commit_action_window(skill_window, now=NOW)

        fake_processes: dict[int, _FakePiProcess] = {}

        def runtime_factory(
            player: PlayerSessionConfig, gateway_url: str, token: str
        ) -> PiRuntime | DemoRuntime:
            assignment = assignments.players[player.seat]
            if player.runtime == "pi":
                process = _FakePiProcess(
                    gateway_url,
                    token,
                    seat=player.seat,
                    role_id=assignment.role_id,
                )
                fake_processes[player.seat] = process
                return PiRuntime(process=process, command_timeout_seconds=1)
            return DemoRuntime(gateway_url, token)

        service = PlayerSessionService(
            runtime_factory=runtime_factory,
            state_provider=skill_manager.snapshot,
        )
        try:
            configuration = parse_player_configuration(
                raw_config,
                board=bundle.board,
                game_root=output,
                allow_scripted=True,
            )
            records = await service.start(bundle, configuration, assignments)
            assert set(records) == set(range(1, 13))
            assert len(service.gateway_url.split(":")) == 3
            assert len({record.token for record in records.values()}) == 12
            assert len({record.runtime_ref.session_id for record in records.values()}) == 12
            role_ids = [record.context.role_id for record in records.values()]
            assert role_ids.count("wolf") == 4
            assert set(role_ids) == {"wolf", "seer", "witch", "hunter", "idiot", "villager"}
            assert all(
                record.context.role_id == assignments.players[record.seat].role_id
                for record in records.values()
            )

            for seat, process in fake_processes.items():
                assert process.started
                assert set(process.knowledge_reads) == {
                    "/board/current",
                    f"/role/{assignments.players[seat].role_id}",
                    "/game/skills/me",
                }
                skill = process.knowledge_reads["/game/skills/me"]["skill_status"]
                assert skill["seat"] == seat
                assert skill["role_id"] == assignments.players[seat].role_id

            # The session records expose the seat-bound identity and prompt,
            # while the bearer token never enters that prompt.
            for seat, record in records.items():
                assert record.context.system_prompt is not None
                assert record.token not in record.context.system_prompt
                assert record.bootstrap_card.your_role.id == assignments.players[seat].role_id

            pi_record = records[pi_seats[0]]
            other_record = records[pi_seats[1]]
            async with aiohttp.ClientSession() as client:
                async with client.get(
                    f"{service.gateway_url}/v1/game/skills/me",
                    headers={"Authorization": f"Bearer {pi_record.token}"},
                ) as response:
                    own_status = await response.json()
                async with client.get(
                    f"{service.gateway_url}/v1/game/skills/me",
                    headers={"Authorization": f"Bearer {other_record.token}"},
                ) as response:
                    other_status = await response.json()
            assert own_status["skill_status"]["seat"] == pi_record.seat
            assert (
                own_status["skill_status"]["role_id"] == assignments.players[pi_record.seat].role_id
            )
            assert other_status["skill_status"]["seat"] == other_record.seat
            assert (
                other_status["skill_status"]["role_id"]
                == assignments.players[other_record.seat].role_id
            )

            # A real PiRuntime action response is submitted through the
            # manager, so the action code and target are revalidated here.
            skill_context = ActionValidationContext(
                game_id=game_id,
                session_epoch=0,
                active_request_id="coordinator-placeholder",
                authorized_action_codes=(skill_code,),
                alive_seats=tuple(range(1, 13)),
                eligible_targets_by_action={skill_code: skill_targets},
            )
            skill_result = await ActionTurnScheduler(skill_manager, service.runtimes).run_turn(
                skill_window, skill_seat, skill_context
            )
            assert skill_result.runtime_result.response.kind == "action"
            assert skill_result.state.action_requests
            assert fake_processes[skill_seat].writes[-1]["type"] == "get_last_assistant_text"

            # Public speech crosses the same runtime protocol for a Pi seat
            # and the production deterministic runtime for one scripted seat.
            speech_pi = await service.runtimes[pi_seats[0]].run_turn(
                _turn_request(
                    f"speech-pi-{len(pi_seats)}",
                    game_id=game_id,
                    seat=pi_seats[0],
                    phase=GamePhase.DAY_SPEECH,
                    expected_kind=ResponseKind.SPEECH,
                )
            )
            scripted_seat = next(seat for seat in range(1, 13) if seat not in pi_seats)
            speech_scripted = await service.runtimes[scripted_seat].run_turn(
                _turn_request(
                    f"speech-scripted-{len(pi_seats)}",
                    game_id=game_id,
                    seat=scripted_seat,
                    phase=GamePhase.DAY_SPEECH,
                    expected_kind=ResponseKind.SPEECH,
                )
            )
            assert speech_pi.response.kind == "speech"
            assert speech_scripted.response.kind == "speech"

            vote_seats = (pi_seats[0], scripted_seat)
            vote_window = ActionWindow(
                window_id=f"mixed-vote-{len(pi_seats)}",
                game_id=game_id,
                session_epoch=0,
                phase=GamePhase.VOTE,
                allowed_seats=vote_seats,
                allowed_action_codes=(201, 202, 299),
                allow_pass=True,
                opened_at=NOW,
                visible_context={
                    "candidate_seats": [seat for seat in range(1, 13) if seat != pi_seats[0]]
                },
            )
            vote_manager = GameManager(
                _game_state(
                    bundle,
                    assignments,
                    game_id=game_id,
                    phase=GamePhase.VOTE,
                ),
                registry=load_action_registry(),
            )
            await vote_manager.commit_action_window(vote_window, now=NOW)
            vote_context = ActionValidationContext(
                game_id=game_id,
                session_epoch=0,
                active_request_id="coordinator-placeholder",
                authorized_action_codes=(201, 299),
                alive_seats=tuple(range(1, 13)),
                eligible_targets_by_action={
                    201: tuple(seat for seat in range(1, 13) if seat != pi_seats[0])
                },
            )
            vote_scheduler = ActionTurnScheduler(vote_manager, service.runtimes)
            pi_vote = await vote_scheduler.run_turn(vote_window, pi_seats[0], vote_context)
            # A successful submission freezes the updated window snapshot;
            # the next moderator turn must use that authoritative copy.
            vote_window = await vote_manager.get_action_window(vote_window.window_id)
            scripted_vote = await vote_scheduler.run_turn(vote_window, scripted_seat, vote_context)
            assert pi_vote.runtime_result.response.kind == "action"
            assert scripted_vote.runtime_result.response.kind == "action"
            assert len((await vote_manager.snapshot()).action_requests) == 2
        finally:
            await service.close("offline mixed harness complete")

        assert service.records == {}
        assert all(process.closed for process in fake_processes.values())
