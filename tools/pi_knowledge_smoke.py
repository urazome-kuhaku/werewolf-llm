"""Run a short, real Pi knowledge bootstrap smoke test.

The smoke test intentionally keeps all generated state in a temporary
directory.  It copies the selected workbench draft, compiles it, creates an
immutable game snapshot, removes the source and compiled trees, and restores
the runtime service from the snapshot alone.  A loopback-only KnowledgeGateway
then serves the Pi extension.  A trusted, seat-specific bootstrap card is
rendered into a temporary system prompt.  The model is asked to complete the
short bootstrap task; the result is accepted only when the gateway receipts
pass the server-side readiness gate.

Run with::

    uv run python tools/pi_knowledge_smoke.py

Pi's model response is diagnostic output only.  The authoritative success
criterion is the pair of server-side KnowledgeReceipt records, so a model
claim without receipts cannot make this check pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.gateway import (
    INTERACTION_QUERY_REQUEST_KEY,
    KnowledgeGateway,
    KnowledgeReceipt,
)
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.runtime_loader import load_service_from_snapshot
from werewolf.knowledge.service import KnowledgeService, QueryContext
from werewolf.knowledge.snapshot import KnowledgeSnapshot, KnowledgeSnapshotBuilder
from werewolf.runtime.knowledge_bootstrap import (
    KnowledgeNotReadyError,
    KnowledgeReadyGate,
    build_knowledge_bootstrap_card,
)
from werewolf.runtime.prompt_composer import compose_system_prompt, write_system_prompt

DEFAULT_BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
DEFAULT_WORKBENCH = (
    Path(__file__).resolve().parents[1] / "vault" / "_workbench" / "official_12_20260928"
)
KNOWLEDGE_TOOLS = (
    "get_board",
    "get_role",
    "get_mechanic",
    "get_interaction",
    "get_rule_topic",
    "search_rules",
)
ROLE_ID = "witch"
READING_SKILL_SHA256 = "40c53b649f8ec4c2ad4345ff074826425921abd728bd5833f7c25d3a89117d1f"
READING_SKILL_PATH = Path(__file__).resolve().parents[1] / "prompts" / "werewolf_reading.md"
PI_MODEL = "github-copilot/gpt-6-luna"
PI_USER_TASK = "请按启动卡完成必读，准备好后回复 ready"
PI_INTERACTION_TASK = (
    '请先调用 get_board 和 get_role(role_id="witch") 完成启动必读。然后验证女巫毒杀猎人的交互，'
    '先调用 get_interaction，使用 subjects=["witch","hunter"] '
    '和 situation="witch.poison"；如果返回 NOT_FOUND，按错误提示调用 search_rules '
    "并用搜索得到的精确 subjects 和 situation_key 重试，最后回复 ready。"
)
PI_TIMEOUT_SECONDS = 180


class SmokeTestError(RuntimeError):
    """Raised when the smoke test cannot prove the required tool calls."""


def _decode_pi_output(raw: bytes) -> str:
    """Decode Pi's Windows console output without leaking replacement noise."""

    for encoding in ("utf-8", "cp936"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workbench",
        type=Path,
        default=DEFAULT_WORKBENCH,
        help="workbench job containing draft/ and publish artifacts",
    )
    parser.add_argument("--board-ref", default=DEFAULT_BOARD_REF)
    parser.add_argument("--game-id", default="pi-knowledge-smoke")
    parser.add_argument("--seat", type=int, default=7)
    parser.add_argument("--session-epoch", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=PI_TIMEOUT_SECONDS)
    parser.add_argument(
        "--interaction-check",
        action="store_true",
        help="ask Pi to exercise exact interaction lookup and recover through search_rules",
    )
    return parser.parse_args(argv)


def _pi_command() -> list[str]:
    """Return a Windows-safe command for the installed Pi executable."""

    executable = shutil.which("pi")
    if executable is None:
        raise SmokeTestError("Pi executable was not found on PATH")
    suffix = Path(executable).suffix.lower()
    if suffix == ".ps1":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            raise SmokeTestError("PowerShell is required to invoke the Pi launcher")
        return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", executable]
    if suffix in {".cmd", ".bat"}:
        command_shell = shutil.which("cmd") or os.environ.get("COMSPEC")
        if command_shell is None:
            raise SmokeTestError("cmd.exe is required to invoke the Pi launcher")
        return [command_shell, "/d", "/c", executable]
    return [executable]


def _site_port(gateway: KnowledgeGateway) -> int:
    runner = gateway._runner  # noqa: SLF001 - no public site address accessor exists.
    if runner is None or not runner.addresses:
        raise SmokeTestError("KnowledgeGateway did not expose a listening address")
    address = runner.addresses[0]
    if not isinstance(address, tuple) or len(address) < 2:
        raise SmokeTestError("KnowledgeGateway returned an invalid listening address")
    port = address[1]
    if not isinstance(port, int) or port <= 0:
        raise SmokeTestError("KnowledgeGateway did not bind a TCP port")
    return port


async def _build_snapshot(
    workbench: Path,
    board_ref: str,
    game_id: str,
    temporary_root: Path,
) -> tuple[KnowledgeSnapshot, KnowledgeService]:
    """Compile a copied draft and restore a service from its snapshot only."""

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
    snapshot = await KnowledgeSnapshotBuilder(compiled_store, games_root).create(
        game_id,
        board_ref,
    )

    # This is the important isolation assertion: runtime loading must not
    # consult either build-time input after a snapshot has been created.
    shutil.rmtree(source_root)
    shutil.rmtree(compiled_root)
    service = await load_service_from_snapshot(snapshot)
    return snapshot, service


async def _run_pi(
    *,
    gateway: KnowledgeGateway,
    context: QueryContext,
    system_prompt_path: Path,
    temporary_root: Path,
    timeout: float,
    user_task: str,
) -> tuple[int, str, str]:
    token = gateway.issue_token(
        game_id=context.game_id,
        snapshot_id=context.snapshot_id,
        seat=context.seat,
        session_epoch=context.session_epoch,
    )
    try:
        base_url = f"http://127.0.0.1:{_site_port(gateway)}/v1"
        environment = os.environ.copy()
        environment.update(
            {
                "WEREWOLF_KNOWLEDGE_BASE_URL": base_url,
                "WEREWOLF_KNOWLEDGE_TOKEN": token,
            }
        )
        extension = (
            Path(__file__).resolve().parents[1] / "extensions" / "werewolf_knowledge.ts"
        ).resolve()
        session_dir = temporary_root / "pi-session"
        session_dir.mkdir()
        command = [
            *_pi_command(),
            "--model",
            PI_MODEL,
            "--append-system-prompt",
            str(system_prompt_path),
            "--no-skills",
            "--no-builtin-tools",
            "--no-context-files",
            "--session-dir",
            str(session_dir),
            "--session-id",
            str(uuid4()),
            "--thinking",
            "off",
            "--no-extensions",
            "--extension",
            str(extension),
            "--tools",
            ",".join(KNOWLEDGE_TOOLS),
            "--print",
            user_task,
        ]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=temporary_root,
                env=environment,
                capture_output=True,
                text=False,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SmokeTestError(f"Pi timed out after {timeout:g} seconds") from exc
        # Redact the bearer before diagnostics can reach the caller.  The
        # token itself is never printed or persisted, even if a launcher
        # unexpectedly echoes its environment.
        token_bytes = token.encode("ascii")
        stdout = _decode_pi_output(completed.stdout.replace(token_bytes, b"[redacted]"))
        stderr = _decode_pi_output(completed.stderr.replace(token_bytes, b"[redacted]"))
        return completed.returncode, stdout, stderr
    finally:
        # Revoke the token as soon as Pi exits (or subprocess.run times out).
        gateway.revoke_token(token)


def _receipt_summary(receipts: Sequence[KnowledgeReceipt]) -> list[dict[str, str]]:
    return [
        {
            "tool": receipt.tool,
            "canonical_ref": receipt.canonical_ref,
            "result_id": receipt.result_id,
        }
        for receipt in receipts
    ]


async def run_smoke(args: argparse.Namespace) -> int:
    if args.seat < 0:
        raise SmokeTestError("seat must be non-negative")
    if args.session_epoch < 0:
        raise SmokeTestError("session_epoch must be non-negative")
    if args.timeout <= 0:
        raise SmokeTestError("timeout must be positive")

    workbench = args.workbench.resolve()
    if not workbench.is_dir() or workbench.is_symlink():
        raise SmokeTestError(f"workbench directory is missing: {workbench}")

    with tempfile.TemporaryDirectory(prefix="werewolf-pi-smoke-") as raw_root:
        temporary_root = Path(raw_root)
        snapshot, service = await _build_snapshot(
            workbench,
            args.board_ref,
            args.game_id,
            temporary_root,
        )
        context = QueryContext(
            game_id=args.game_id,
            snapshot_id=snapshot.snapshot_id,
            seat=args.seat,
            session_epoch=args.session_epoch,
        )
        bootstrap_card = build_knowledge_bootstrap_card(service, context, ROLE_ID)
        system_prompt = compose_system_prompt(
            bootstrap_card,
            READING_SKILL_SHA256,
            reading_skill_path=READING_SKILL_PATH,
        )
        system_prompt_path = write_system_prompt(
            (temporary_root / "system-prompt.md").absolute(),
            system_prompt,
        )
        receipts: list[KnowledgeReceipt] = []

        async def receipt_sink(receipt: KnowledgeReceipt) -> None:
            receipts.append(receipt)

        request_log: list[dict[str, object]] = []

        async def record_response(request: object, response: object) -> None:
            entry: dict[str, object] = {
                "method": request.method,  # type: ignore[attr-defined]
                "path": request.path,  # type: ignore[attr-defined]
                "status": response.status,  # type: ignore[attr-defined]
            }
            # The interaction keys are public rule identifiers.  Record only
            # these keys for diagnosis; bearer tokens and game/session context
            # never enter the smoke output.
            diagnostic = request.get(INTERACTION_QUERY_REQUEST_KEY)  # type: ignore[attr-defined]
            if isinstance(diagnostic, dict):
                entry["interaction_query"] = {
                    "subjects": list(diagnostic.get("subjects", ())),
                    "situation": diagnostic.get("situation"),
                }
            request_log.append(entry)

        gateway = KnowledgeGateway(service, receipt_sink=receipt_sink)
        gateway.app.on_response_prepare.append(record_response)
        await gateway.start(port=0)
        try:
            returncode, stdout, stderr = await _run_pi(
                gateway=gateway,
                context=context,
                system_prompt_path=system_prompt_path,
                temporary_root=temporary_root,
                timeout=args.timeout,
                user_task=PI_INTERACTION_TASK if args.interaction_check else PI_USER_TASK,
            )
        finally:
            await gateway.close()

    observed = tuple(receipt.tool for receipt in receipts)
    if returncode != 0:
        raise SmokeTestError(
            f"Pi exited with code {returncode}: {stderr[-2000:] or stdout[-2000:]}"
        )
    ready_gate = KnowledgeReadyGate(service, context, ROLE_ID)
    try:
        ready_gate.require_ready(receipts)
    except KnowledgeNotReadyError as exc:
        diagnostics = " ".join((stdout + " " + stderr).split())[-2000:]
        raise SmokeTestError(
            f"server receipts did not prove bootstrap readiness: {exc}; "
            f"observed {observed}; HTTP requests: {request_log}; Pi output: {diagnostics}"
        ) from exc
    unknown_tools = tuple(tool for tool in observed if tool not in KNOWLEDGE_TOOLS)
    board_index = observed.index("get_board") if "get_board" in observed else None
    role_index = observed.index("get_role") if "get_role" in observed else None
    if unknown_tools or board_index is None or role_index is None or board_index >= role_index:
        diagnostics = " ".join((stdout + " " + stderr).split())[-2000:]
        raise SmokeTestError(
            "server receipts did not prove the required ordered calls: "
            f"allowed {KNOWLEDGE_TOOLS}, observed {observed}; "
            f"HTTP requests: {request_log}; Pi output: {diagnostics}"
        )
    if any(receipt.snapshot_id != snapshot.snapshot_id for receipt in receipts):
        raise SmokeTestError("a knowledge receipt was bound to the wrong snapshot")
    if any(receipt.game_id != args.game_id or receipt.seat != args.seat for receipt in receipts):
        raise SmokeTestError("a knowledge receipt was bound to the wrong game or seat")

    # Keep diagnostics deliberately short.  The receipt summary is the
    # authoritative evidence; model text is shown only to aid debugging.
    model_text = " ".join(stdout.split())[:500]
    print(
        json.dumps(
            {
                "status": "ok",
                "board_ref": args.board_ref,
                "snapshot_id": snapshot.snapshot_id,
                "pi_exit_code": returncode,
                "ready": True,
                "gateway_requests": request_log,
                "receipts": _receipt_summary(receipts),
                "model_output": model_text,
            },
            ensure_ascii=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run_smoke(_parse_args(argv)))
    except (SmokeTestError, OSError, ValueError) as exc:
        print(f"pi knowledge smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
