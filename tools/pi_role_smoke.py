"""Run a short real-Pi player role smoke test.

This probe exercises one seat without starting a complete game.  It creates a
temporary knowledge snapshot from the local workbench, starts one seat-scoped
``PiRuntime`` with the loopback knowledge extension, and performs three small
turns:

* read the current board and the authenticated seat's role, then return READY;
* receive two short public speeches and return one structured speech;
* receive a legal role skill window and return the role action or PASS.

The default process is intentionally pinned to ``github-copilot`` and
``gpt-6-luna``.  The script never falls back to another provider or model.
Only receipt/tool names, response shapes, and bounded action metadata are
printed.  Prompt text, bearer tokens, receipt IDs, and model speech are not
printed.  All state is created below a temporary directory and both the Pi
process and loopback gateway are closed in ``finally`` blocks.

Run manually after the host Pi authentication and moderator flow are ready::

    uv run python tools/pi_role_smoke.py

No real model call is made by the repository's offline checks; this command is
the intentionally explicit live probe for the main agent to run later.  Pass
``--role guard`` or ``--role white_wolf_king`` together with the matching
candidate ``--workbench`` and ``--board-ref`` to exercise those fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from werewolf.domain.enums import GamePhase
from werewolf.game.state import (
    GameState,
    GrantedAbility,
    PlayerState,
    RulesetRef,
)
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.gateway import KnowledgeGateway, KnowledgeReceipt
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.role import ResourceDefinition, TargetKind, TargetRule, UsageLimit
from werewolf.knowledge.service import KnowledgeService, QueryContext
from werewolf.knowledge.snapshot import KnowledgeSnapshot, KnowledgeSnapshotBuilder
from werewolf.runtime.knowledge_bootstrap import (
    KnowledgeReadyGate,
    build_knowledge_bootstrap_card,
)
from werewolf.runtime.pi_process import DEFAULT_PI_EXECUTABLE, PiProcess, PiProcessConfig
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import (
    ActionWindowView,
    Deadline,
    InitialContext,
    Observation,
    ObservationEvent,
    ResponseKind,
    RuntimeConfig,
    TurnRequest,
)
from werewolf.runtime.prompt_composer import compose_system_prompt, write_system_prompt

DEFAULT_BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
DEFAULT_WORKBENCH = (
    Path(__file__).resolve().parents[1] / "vault" / "_workbench" / "official_12_20260928"
)
READING_SKILL_PATH = Path(__file__).resolve().parents[1] / "prompts" / "werewolf_reading.md"
# This is the reviewed digest used by the existing knowledge bootstrap smoke.
READING_SKILL_SHA256 = "9c4e599881bdd694fa52e62b84f043c6f3ea0cab2b47c7a56a984eec60707a64"
EXTENSION_PATH = Path(__file__).resolve().parents[1] / "extensions" / "werewolf_knowledge.ts"

PROVIDER = "github-copilot"
MODEL = "gpt-6-luna"
ROLE_ID = "witch"
SEAT = 7
TARGET_SEATS = (2, 3)
POISON_ACTION_CODE = 103
GUARD_ACTION_CODE = 106
WHITE_WOLF_KING_ACTION_CODE = 107
PASS_ACTION_CODE = 299
KNOWLEDGE_TOOLS = (
    "get_board",
    "get_role",
    "get_mechanic",
    "get_interaction",
    "get_rule_topic",
    "search_rules",
    "get_skill_status",
)
DEFAULT_TIMEOUT_SECONDS = 180.0
KNOWLEDGE_STATUS = "CANDIDATE_PENDING_HUMAN_REVIEW"


@dataclass(frozen=True)
class RoleSmokeSpec:
    """Bounded role fixture used by the live Pi probe.

    This is intentionally local to the probe.  The authoritative role and
    board definitions still come from the candidate knowledge snapshot; these
    fields only describe the small private state and response contract needed
    to exercise one seat without starting a complete game.
    """

    role_id: str
    phase: GamePhase
    action_code: int
    ability_id: str
    faction_id: str
    target_rule: TargetRule
    skill_summary: str
    resource: ResourceDefinition | None = None
    resource_amounts: tuple[tuple[str, int], ...] = ()


ROLE_SPECS: dict[str, RoleSmokeSpec] = {
    "witch": RoleSmokeSpec(
        role_id="witch",
        phase=GamePhase.NIGHT_ACTION,
        action_code=POISON_ACTION_CODE,
        ability_id="poison",
        faction_id="good",
        target_rule=TargetRule(
            kind=TargetKind.PLAYER,
            min_targets=1,
            max_targets=1,
            allow_self=False,
            allow_dead=False,
        ),
        skill_summary=(
            "先调用 get_skill_status 查看当前女巫技能和剩余资源，再选择一次毒药行动或 PASS。"
        ),
        resource=ResourceDefinition(
            resource_id="witch_poison",
            initial_amount=1,
            cost_per_use=1,
        ),
        resource_amounts=(("witch_heal", 1), ("witch_poison", 1)),
    ),
    "guard": RoleSmokeSpec(
        role_id="guard",
        phase=GamePhase.NIGHT_ACTION,
        action_code=GUARD_ACTION_CODE,
        ability_id="protect",
        faction_id="good",
        target_rule=TargetRule(
            kind=TargetKind.PLAYER,
            min_targets=1,
            max_targets=1,
            allow_self=False,
            allow_dead=False,
        ),
        skill_summary=(
            "先调用 get_skill_status 查看守卫技能状态，再选择一名其他仍存活的玩家"
            "守护或 PASS。当前候选不允许自守。"
        ),
    ),
    "white_wolf_king": RoleSmokeSpec(
        role_id="white_wolf_king",
        phase=GamePhase.DAY_SPEECH,
        action_code=WHITE_WOLF_KING_ACTION_CODE,
        ability_id="sacrifice",
        faction_id="werewolf",
        target_rule=TargetRule(
            kind=TargetKind.PLAYERS,
            min_targets=2,
            max_targets=2,
            allow_self=True,
            allow_dead=False,
        ),
        skill_summary=(
            "先调用 get_skill_status 查看白狼王技能状态，再按 [自己, 一名其他存活玩家] 的顺序"
            "发动或 PASS。"
        ),
    ),
}


class SmokeTestError(RuntimeError):
    """Raised when the bounded player smoke contract cannot be proven."""


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        choices=tuple(ROLE_SPECS),
        default=ROLE_ID,
        help="role fixture to probe; the default preserves the original witch smoke",
    )
    parser.add_argument("--workbench", type=Path, default=DEFAULT_WORKBENCH)
    parser.add_argument("--board-ref", default=DEFAULT_BOARD_REF)
    parser.add_argument("--game-id", default="pi-role-smoke")
    parser.add_argument("--seat", type=int, default=SEAT)
    parser.add_argument("--session-epoch", type=int, default=0)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="hard timeout for each real-Pi turn (seconds)",
    )
    return parser.parse_args(argv)


def _role_spec(role_id: str) -> RoleSmokeSpec:
    try:
        return ROLE_SPECS[role_id]
    except KeyError as exc:
        raise SmokeTestError(f"unsupported role fixture: {role_id}") from exc


def _candidate_seats(spec: RoleSmokeSpec, seat: int) -> tuple[int, ...]:
    if spec.role_id == ROLE_ID:
        return TARGET_SEATS
    if spec.role_id == "guard":
        return (seat + 1,)
    return (seat, seat + 1)


async def _build_snapshot(
    workbench: Path,
    board_ref: str,
    game_id: str,
    temporary_root: Path,
) -> tuple[KnowledgeSnapshot, KnowledgeService]:
    """Compile a copied candidate draft and serve it from a verified snapshot."""

    draft = workbench / "draft"
    if not draft.is_dir() or draft.is_symlink():
        raise SmokeTestError(f"workbench draft directory is missing: {draft}")
    source_root = temporary_root / "source"
    compiled_root = temporary_root / "compiled"
    games_root = temporary_root / "games"
    shutil.copytree(draft, source_root)
    package = await KnowledgePackageLoader(source_root).load(board_ref)
    compiled = KnowledgePackageCompiler().compile(package)
    compiled_store = CompiledKnowledgeStore(compiled_root)
    await compiled_store.publish(compiled)
    snapshot = await KnowledgeSnapshotBuilder(compiled_store, games_root).create(game_id, board_ref)
    shutil.rmtree(source_root)
    shutil.rmtree(compiled_root)
    # The checked-in board remains a candidate awaiting human review.  The
    # formal runtime loader rejects that status by design, so this probe serves
    # the verified temporary candidate in memory without promoting or editing
    # the workbench package.
    return snapshot, KnowledgeService(compiled, snapshot_id=snapshot.snapshot_id)


def _build_skill_state(
    *,
    game_id: str,
    board_ref: str,
    snapshot: KnowledgeSnapshot,
    seat: int,
    session_epoch: int,
    role_id: str = ROLE_ID,
) -> GameState:
    """Build the smallest private state needed by the live smoke probe.

    This is deliberately a probe fixture rather than game logic.  The real
    moderator owns this state in production; the smoke process only needs a
    truthful seat-bound projection so a Pi can inspect its currently usable
    role resources before choosing an action or PASS.
    """

    spec = _role_spec(role_id)
    board_id, version = board_ref.split("@", 1)
    now = datetime.now(UTC)
    abilities: tuple[GrantedAbility, ...]
    if spec.role_id == ROLE_ID:
        abilities = (
            GrantedAbility(
                ability_id="heal",
                action_code=104,
                timing=GamePhase.NIGHT_ACTION,
                allowed_phases=(GamePhase.NIGHT_ACTION,),
                target_rule=TargetRule(
                    kind=TargetKind.PLAYER,
                    min_targets=1,
                    max_targets=1,
                    allow_self=False,
                    allow_dead=False,
                ),
                usage_limit=UsageLimit(max_uses=1),
                resource=ResourceDefinition(
                    resource_id="witch_heal",
                    initial_amount=1,
                    cost_per_use=1,
                ),
            ),
            GrantedAbility(
                ability_id="poison",
                action_code=POISON_ACTION_CODE,
                timing=GamePhase.NIGHT_ACTION,
                allowed_phases=(GamePhase.NIGHT_ACTION,),
                target_rule=spec.target_rule,
                usage_limit=UsageLimit(max_uses=1),
                resource=spec.resource,
            ),
        )
    else:
        abilities = (
            GrantedAbility(
                ability_id=spec.ability_id,
                action_code=spec.action_code,
                timing=spec.phase,
                allowed_phases=(spec.phase,),
                target_rule=spec.target_rule,
                usage_limit=UsageLimit(max_uses=1),
                resource=spec.resource,
            ),
        )
    actor = PlayerState(
        seat=seat,
        role_id=spec.role_id,
        faction_id=spec.faction_id,
        session_epoch=session_epoch,
        skill_resources=dict(spec.resource_amounts),
        granted_abilities=abilities,
    )
    # The alternate seat is present solely to make the isolation contract
    # explicit.  Its private marker must never appear in the actor response.
    other = PlayerState(
        seat=seat + 1,
        role_id="villager",
        faction_id="good",
        session_epoch=session_epoch,
        skill_resources={"private_marker": 1},
    )
    return GameState(
        game_id=game_id,
        created_at=now,
        updated_at=now,
        phase=spec.phase,
        ruleset=RulesetRef(
            board_id=board_id,
            version=version,
            snapshot_id=snapshot.snapshot_id,
            manifest_sha256=snapshot.manifest_sha256,
        ),
        players={seat: actor, seat + 1: other},
        action_windows={
            f"pi-role-smoke-{spec.role_id}-window": {
                "window_id": f"pi-role-smoke-{spec.role_id}-window",
                "phase": spec.phase.value,
                "session_epoch": session_epoch,
                "allowed_seats": [seat],
                "allowed_action_codes": [spec.action_code, PASS_ACTION_CODE],
                "allow_pass": True,
                "dependencies_satisfied": True,
                "closed_at": None,
            }
        },
    )


def _site_port(gateway: KnowledgeGateway) -> int:
    runner = gateway._runner  # noqa: SLF001 - no public bound-port accessor exists.
    if runner is None or not runner.addresses:
        raise SmokeTestError("knowledge gateway did not expose a listening address")
    address = runner.addresses[0]
    if not isinstance(address, tuple) or len(address) < 2 or not isinstance(address[1], int):
        raise SmokeTestError("knowledge gateway returned an invalid listening address")
    if address[1] <= 0:
        raise SmokeTestError("knowledge gateway did not bind an ephemeral port")
    return address[1]


def _request(
    *,
    request_id: str,
    game_id: str,
    session_epoch: int,
    phase: GamePhase,
    expected_kind: ResponseKind,
    observation: Observation,
    output_schema: dict[str, object],
    timeout: float,
    action_window: ActionWindowView | None = None,
) -> TurnRequest:
    now = datetime.now(UTC)
    return TurnRequest(
        request_id=request_id,
        logical_request_id=request_id,
        attempt_no=1,
        game_id=game_id,
        session_epoch=session_epoch,
        phase=phase,
        expected_kind=expected_kind,
        action_window=action_window,
        observation=observation,
        output_schema=output_schema,
        deadline=Deadline(
            soft_deadline=now + timedelta(seconds=max(1.0, timeout * 0.7)),
            hard_deadline=now + timedelta(seconds=timeout),
        ),
    )


def _ready_request(
    game_id: str,
    session_epoch: int,
    timeout: float,
    role_id: str = ROLE_ID,
) -> TurnRequest:
    return _request(
        request_id="pi-role-smoke-ready",
        game_id=game_id,
        session_epoch=session_epoch,
        phase=GamePhase.PLAYER_PREPARE,
        expected_kind=ResponseKind.READY,
        observation=Observation(
            summary="完成启动必读后提交 READY。",
            payload={
                "required_tools": ["get_board", "get_role"],
                "role_id": role_id,
            },
        ),
        output_schema={
            "type": "object",
            "required": ["schema_version", "request_id", "kind", "ready"],
            "properties": {
                "schema_version": {"const": 1},
                "request_id": {"const": "pi-role-smoke-ready"},
                "kind": {"const": "ready"},
                "ready": {
                    "type": "object",
                    "required": ["knowledge_receipts"],
                    "properties": {"knowledge_receipts": {"type": "array"}},
                },
            },
            "additionalProperties": False,
        },
        timeout=timeout,
    )


def _speech_request(game_id: str, session_epoch: int, timeout: float) -> TurnRequest:
    return _request(
        request_id="pi-role-smoke-speech",
        game_id=game_id,
        session_epoch=session_epoch,
        phase=GamePhase.DAY_SPEECH,
        expected_kind=ResponseKind.SPEECH,
        observation=Observation(
            summary="阅读这段短公屏后，发表一段简短的公共发言。",
            events=[
                ObservationEvent(
                    event_id=1,
                    event_type="public_speech",
                    payload={"seat": 2, "text": "我先报平民，暂时关注 5 号和 9 号。"},
                ),
                ObservationEvent(
                    event_id=2,
                    event_type="public_speech",
                    payload={"seat": 9, "text": "2 号的视角有点跳，我想听更多信息。"},
                ),
            ],
            payload={"channel": "PUBLIC"},
        ),
        output_schema={
            "type": "object",
            "required": ["schema_version", "request_id", "kind", "speech"],
            "properties": {
                "schema_version": {"const": 1},
                "request_id": {"const": "pi-role-smoke-speech"},
                "kind": {"const": "speech"},
                "speech": {"type": "object", "required": ["text"]},
            },
            "additionalProperties": False,
        },
        timeout=timeout,
    )


def _skill_request(
    game_id: str,
    session_epoch: int,
    timeout: float,
    role_id: str = ROLE_ID,
    seat: int = SEAT,
) -> TurnRequest:
    spec = _role_spec(role_id)
    candidate_seats = _candidate_seats(spec, seat)
    return _request(
        request_id="pi-role-smoke-skill",
        game_id=game_id,
        session_epoch=session_epoch,
        phase=spec.phase,
        expected_kind=ResponseKind.ACTION,
        action_window=ActionWindowView(
            window_id=f"pi-role-smoke-{spec.role_id}-window",
            allowed_action_codes=[spec.action_code, PASS_ACTION_CODE],
            min_actions=1,
            max_actions=1,
            allow_pass=True,
            candidate_seats=list(candidate_seats),
        ),
        observation=Observation(
            summary=spec.skill_summary,
            payload={
                "required_tool_first": "get_skill_status",
                "skill_window": f"{spec.role_id}_action_or_pass",
                "legal_action_codes": [spec.action_code, PASS_ACTION_CODE],
                "candidate_seats": list(candidate_seats),
            },
        ),
        output_schema={
            "type": "object",
            "required": ["schema_version", "request_id", "kind", "actions"],
            "properties": {
                "schema_version": {"const": 1},
                "request_id": {"const": "pi-role-smoke-skill"},
                "kind": {"const": "action"},
                "actions": {"type": "array", "minItems": 1, "maxItems": 1},
            },
            "additionalProperties": False,
        },
        timeout=timeout,
    )


def _assert_skill_response(
    response: object,
    role_id: str = ROLE_ID,
    seat: int = SEAT,
) -> tuple[int, int, bool]:
    spec = _role_spec(role_id)
    candidate_seats = _candidate_seats(spec, seat)
    if getattr(response, "kind", None) != ResponseKind.ACTION.value:
        raise SmokeTestError("skill turn returned a non-action response")
    actions = getattr(response, "actions", None)
    if not isinstance(actions, list) or len(actions) != 1:
        raise SmokeTestError("skill turn did not return exactly one action")
    action = actions[0]
    code = getattr(action, "action_code", None)
    targets = getattr(action, "targets", None)
    if not isinstance(code, int) or not isinstance(targets, list):
        raise SmokeTestError("skill turn returned an invalid action shape")
    if code == PASS_ACTION_CODE:
        if targets:
            raise SmokeTestError("PASS action must not contain targets")
        return code, 0, True
    if code != spec.action_code:
        raise SmokeTestError(
            f"skill turn returned an action outside the legal {spec.role_id} window"
        )
    if spec.role_id == "white_wolf_king":
        if (
            len(targets) != 2
            or targets[0] != seat
            or targets[1] not in candidate_seats
            or targets[1] == seat
        ):
            raise SmokeTestError("white wolf king action must target [actor, another live seat]")
        return code, 2, False
    if len(targets) != 1 or targets[0] not in candidate_seats:
        raise SmokeTestError(
            f"skill turn returned an action outside the legal {spec.role_id} window"
        )
    return code, 1, False


def _require_skill_status_call(*, baseline: int, current: int) -> int:
    """Require the skill turn to have read its authoritative private state.

    ``get_skill_status`` is a read-only endpoint and therefore does not emit a
    ``KnowledgeReceipt``.  The live probe records successful
    ``state_provider`` calls instead; that provider is only reached by this
    endpoint.  Keep this check separate so the offline test can prove that an
    action response alone never counts as a completed skill turn.
    """

    calls = current - baseline
    if calls < 1:
        raise SmokeTestError("skill turn did not successfully call get_skill_status")
    return calls


async def run_smoke(args: argparse.Namespace) -> int:
    if args.seat < 1 or args.session_epoch < 0 or args.timeout <= 0:
        raise SmokeTestError("seat, session_epoch, and timeout must be valid positive values")
    spec = _role_spec(getattr(args, "role", ROLE_ID))
    if not EXTENSION_PATH.is_file():
        raise SmokeTestError(f"knowledge extension is missing: {EXTENSION_PATH}")
    workbench = args.workbench.resolve()
    if not workbench.is_dir() or workbench.is_symlink():
        raise SmokeTestError(f"workbench directory is missing: {workbench}")

    gateway: KnowledgeGateway | None = None
    process: PiProcess | None = None
    runtime: PiRuntime | None = None
    token: str | None = None
    receipts: list[KnowledgeReceipt] = []
    stage = "setup"
    try:
        with tempfile.TemporaryDirectory(prefix="werewolf-pi-role-") as raw_root:
            root = Path(raw_root)
            snapshot, service = await _build_snapshot(
                workbench,
                args.board_ref,
                args.game_id,
                root,
            )
            context = QueryContext(
                game_id=args.game_id,
                snapshot_id=snapshot.snapshot_id,
                seat=args.seat,
                session_epoch=args.session_epoch,
            )
            card = build_knowledge_bootstrap_card(service, context, spec.role_id)
            prompt_path = write_system_prompt(
                (root / "system-prompt.md").absolute(),
                compose_system_prompt(
                    card,
                    READING_SKILL_SHA256,
                    reading_skill_path=READING_SKILL_PATH,
                ),
            )

            async def receipt_sink(receipt: KnowledgeReceipt) -> None:
                receipts.append(receipt)

            skill_state = _build_skill_state(
                game_id=args.game_id,
                board_ref=args.board_ref,
                snapshot=snapshot,
                seat=args.seat,
                session_epoch=args.session_epoch,
                role_id=spec.role_id,
            )

            skill_status_call_count = 0

            async def state_provider() -> GameState:
                nonlocal skill_status_call_count
                # This provider is reached by the read-only
                # ``get_skill_status`` endpoint.  Count only successful state
                # retrievals so an action response cannot bypass the probe's
                # private-state-read requirement.
                state = skill_state
                skill_status_call_count += 1
                return state

            gateway = KnowledgeGateway(
                service,
                receipt_sink=receipt_sink,
                state_provider=state_provider,
            )
            await gateway.start(port=0)
            token = gateway.issue_token(
                game_id=args.game_id,
                snapshot_id=snapshot.snapshot_id,
                seat=args.seat,
                session_epoch=args.session_epoch,
            )
            process = PiProcess(
                PiProcessConfig(
                    session_root=root / "pi-sessions",
                    provider=PROVIDER,
                    model=MODEL,
                    knowledge_base_url=f"http://127.0.0.1:{_site_port(gateway)}/v1",
                    knowledge_token=token,
                    seat=args.seat,
                    executable=DEFAULT_PI_EXECUTABLE,
                    append_system_prompt=prompt_path,
                    extension=EXTENSION_PATH,
                    tools=KNOWLEDGE_TOOLS,
                    thinking="off",
                )
            )
            runtime = PiRuntime(process=process, command_timeout_seconds=20.0)
            config = RuntimeConfig(
                session_id=f"pi-role-smoke-{uuid4().hex[:12]}",
                session_dir=root / "runtime",
                provider=PROVIDER,
                model=MODEL,
                reasoning="minimal",
            )
            initial_context = InitialContext(
                game_id=args.game_id,
                seat=args.seat,
                session_epoch=args.session_epoch,
                role_id=spec.role_id,
                system_prompt=None,
            )

            stage = "start"
            await runtime.start(config, initial_context)
            stage = "ready"
            ready_result = await asyncio.wait_for(
                runtime.run_turn(
                    _ready_request(args.game_id, args.session_epoch, args.timeout, spec.role_id)
                ),
                timeout=args.timeout + 20.0,
            )
            if ready_result.response.kind != ResponseKind.READY.value:
                raise SmokeTestError("READY turn returned the wrong response kind")
            if ready_result.response.ready is None:
                raise SmokeTestError("READY turn did not include a readiness payload")
            KnowledgeReadyGate(service, context, spec.role_id).require_ready(receipts)
            claimed_receipt_ids = set(ready_result.response.ready.knowledge_receipts)
            if any(
                not any(
                    receipt.tool == tool and receipt.receipt_id in claimed_receipt_ids
                    for receipt in receipts
                )
                for tool in ("get_board", "get_role")
            ):
                raise SmokeTestError("READY did not claim both successful knowledge receipts")

            stage = "speech"
            speech_result = await asyncio.wait_for(
                runtime.run_turn(_speech_request(args.game_id, args.session_epoch, args.timeout)),
                timeout=args.timeout + 20.0,
            )
            if speech_result.response.kind != ResponseKind.SPEECH.value:
                raise SmokeTestError("public speech turn returned the wrong response kind")
            if speech_result.response.speech is None:
                raise SmokeTestError("public speech turn did not include speech text")

            stage = "skill"
            # The skill observation tells the controller to inspect the
            # authoritative private state before selecting poison or PASS.
            skill_status_calls_before = skill_status_call_count
            skill_result = await asyncio.wait_for(
                runtime.run_turn(
                    _skill_request(
                        args.game_id,
                        args.session_epoch,
                        args.timeout,
                        spec.role_id,
                        args.seat,
                    )
                ),
                timeout=args.timeout + 20.0,
            )
            skill_status_calls = _require_skill_status_call(
                baseline=skill_status_calls_before,
                current=skill_status_call_count,
            )
            action_code, target_count, passed = _assert_skill_response(
                skill_result.response,
                spec.role_id,
                args.seat,
            )

            stage = "close"
            await runtime.close("Pi role smoke complete")
            if not process.closed or process.returncode is None:
                raise SmokeTestError("Pi process did not close with a final return code")
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "provider": PROVIDER,
                        "model": MODEL,
                        "board_ref": args.board_ref,
                        "role": spec.role_id,
                        "knowledge_status": KNOWLEDGE_STATUS,
                        "formal_publish": False,
                        "snapshot_id": snapshot.snapshot_id,
                        "seat": args.seat,
                        "ready": True,
                        "knowledge_tools": sorted({receipt.tool for receipt in receipts}),
                        "public_speech": True,
                        "skill": {
                            "action_code": action_code,
                            "target_count": target_count,
                            "pass": passed,
                            "skill_status_calls": skill_status_calls,
                        },
                        "process_returncode": process.returncode,
                        "process_tree_closed": True,
                    },
                    ensure_ascii=True,
                )
            )
            return 0
    except Exception as exc:
        raise SmokeTestError(f"{stage}:{type(exc).__name__}") from exc
    finally:
        if runtime is not None and not runtime._closed:  # noqa: SLF001 - cleanup guard
            try:
                await runtime.close("Pi role smoke cleanup")
            except Exception:
                pass
        elif process is not None and process.started and not process.closed:
            try:
                await process.close("Pi role smoke cleanup")
            except Exception:
                pass
        if gateway is not None:
            if token is not None:
                gateway.revoke_token(token)
            try:
                await gateway.close()
            except Exception:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run_smoke(_parse_args(argv)))
    except (SmokeTestError, OSError, ValueError) as exc:
        # Deliberately omit exception text: Pi/provider diagnostics can contain
        # prompt fragments.  The stage and exception class are sufficient for
        # the main agent to decide which live probe phase needs inspection.
        text = str(exc).split(":", 1)
        stage = text[0] if text else "unknown"
        error_type = text[1] if len(text) > 1 else type(exc).__name__
        print(
            json.dumps(
                {"status": "failed", "stage": stage, "error_type": error_type},
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
