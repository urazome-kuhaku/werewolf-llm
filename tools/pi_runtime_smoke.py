"""Run a short real two-turn PiRuntime session.

This is an intentionally small integration probe for the persistent player
runtime.  It uses the local Pi authentication that is already configured on
the host, but gives the child process only a loopback knowledge URL and a
synthetic token.  The model is asked not to call tools and receives no game
state or provider credentials.

The probe proves all of the following in one process:

* ``PiRuntime`` can complete two strict ``TurnResponse`` turns through a real
  ``PiProcess``;
* the same runtime/session continues the model context between turns, proved
  with a fresh random nonce sent only in turn one;
* request IDs and the session epoch remain associated with the host session;
* closing the runtime closes the Pi process tree.

Run with::

    uv run python tools/pi_runtime_smoke.py

The output is a compact JSON diagnostic.  It reports only whether the second
turn matched the host nonce; errors include only a stage, type, and bounded,
redacted text so a nonce, provider, or bearer value cannot be copied to the
terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from werewolf.domain.enums import GamePhase
from werewolf.runtime.pi_process import (
    DEFAULT_PI_EXECUTABLE,
    PiProcess,
    PiProcessConfig,
)
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import (
    Deadline,
    InitialContext,
    Observation,
    ResponseKind,
    RuntimeConfig,
    TurnRequest,
)

FAKE_KNOWLEDGE_URL = "http://127.0.0.1:9/v1"
FAKE_KNOWLEDGE_TOKEN = "runtime-smoke-loopback-token"
PROVIDER = "github-copilot"
MODEL = "gpt-6-luna"
SESSION_EPOCH = 17
SEAT = 3
GAME_ID = "runtime-smoke-game"
DEFAULT_TIMEOUT_SECONDS = 180.0


class SmokeTestError(RuntimeError):
    """Raised when the real two-turn contract cannot be proven."""


class _RecordingProcess:
    """PiProcess adapter that retains only safe event-shape diagnostics."""

    def __init__(self, process: PiProcess) -> None:
        self.process = process
        self.records: list[object] = []

    @property
    def started(self) -> bool:
        return self.process.started

    @property
    def closed(self) -> bool:
        return self.process.closed

    async def start(self) -> _RecordingProcess:
        await self.process.start()
        return self

    async def send_record(self, record: Mapping[str, object]) -> None:
        await self.process.send_record(record)

    async def read_record(self) -> object | None:
        record = await self.process.read_record()
        self.records.append(record)
        return record

    async def close(self, reason: str = "normal shutdown") -> None:
        await self.process.close(reason)


def _event_shapes(records: Sequence[object]) -> list[str]:
    """Describe protocol shapes without printing model text or credentials."""

    shapes: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            shapes.append(type(record).__name__)
            continue
        raw_type = record.get("type")
        if raw_type != "message_end":
            if isinstance(raw_type, str):
                shapes.append(raw_type)
            continue
        message = record.get("message")
        if not isinstance(message, Mapping):
            shapes.append("message_end:message=" + type(message).__name__)
            continue
        role = message.get("role")
        content = message.get("content")
        content_shape = type(content).__name__
        if isinstance(content, list):
            content_shape += (
                "["
                + ",".join(
                    str(item.get("type"))
                    for item in content
                    if isinstance(item, Mapping) and isinstance(item.get("type"), str)
                )
                + "]"
            )
        shapes.append(f"message_end:role={role!r},content={content_shape}")
    return shapes[-24:]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="hard timeout for each model turn (seconds)",
    )
    return parser.parse_args(argv)


def _output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["schema_version", "request_id", "kind", "speech"],
        "properties": {
            "schema_version": {"const": 1},
            "request_id": {"type": "string"},
            "kind": {"const": "speech"},
            "speech": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
        },
        "additionalProperties": False,
    }


def _request(request_id: str, summary: str, timeout: float) -> TurnRequest:
    now = datetime.now(UTC)
    return TurnRequest(
        request_id=request_id,
        logical_request_id=request_id,
        attempt_no=1,
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        phase=GamePhase.DAY_SPEECH,
        expected_kind=ResponseKind.SPEECH,
        observation=Observation(summary=summary),
        output_schema=_output_schema(),
        deadline=Deadline(
            soft_deadline=now + timedelta(seconds=max(1.0, timeout * 0.75)),
            hard_deadline=now + timedelta(seconds=timeout),
        ),
    )


def _safe_error(error: BaseException, *, redactions: Sequence[str] = ()) -> str:
    """Return bounded diagnostics with synthetic secrets and URLs removed."""

    text = str(error)
    for value in (FAKE_KNOWLEDGE_TOKEN, FAKE_KNOWLEDGE_URL, *redactions):
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|bearer|password)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2[redacted]",
        text,
    )
    return " ".join(text.split())[:800]


def _assert_nonce_absent(
    nonce: str,
    *,
    second_request: TurnRequest,
    second_prompt: str,
    logical_session_id: str,
    physical_session_id: str | None,
    session_dir: Path,
    append_system_prompt: Path | None,
) -> None:
    """Prove the second turn cannot answer from a host-side request leak.

    The nonce is intentionally present only in the first turn's prompt.  Keep
    this check close to the real request construction so a future edit cannot
    silently add it to an observation, schema, system-prompt path, or session
    identity while the smoke still appears to prove persistence.
    """

    values = (
        json.dumps(second_request.model_dump(mode="json"), ensure_ascii=False),
        second_prompt,
        logical_session_id,
        physical_session_id or "",
        str(session_dir),
        str(append_system_prompt) if append_system_prompt is not None else "",
    )
    if any(nonce in value for value in values):
        raise SmokeTestError("second-turn request or session metadata contains the nonce")


async def run_smoke(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise SmokeTestError("timeout must be positive")

    public_nonce = f"RUNTIME_PUBLIC_NONCE_{secrets.token_hex(12).upper()}"
    session_id = f"runtime-smoke-{uuid4().hex[:12]}"
    process: PiProcess | None = None
    recording: _RecordingProcess | None = None
    runtime: PiRuntime | None = None
    stage = "setup"
    physical_session_id: str | None = None
    process_pid: int | None = None
    closed_process_returncode: int | None = None

    # Windows can retain a short lived handle to a batch-launcher working
    # directory after the child has reported its return code.  The runtime
    # process cleanup is asserted explicitly below; temporary-tree cleanup is
    # best effort so that it cannot hide the actual integration stage/error.
    with tempfile.TemporaryDirectory(
        prefix="werewolf-pi-runtime-", ignore_cleanup_errors=True
    ) as raw_root:
        temporary_root = Path(raw_root)
        process_config = PiProcessConfig(
            session_root=temporary_root / "sessions",
            provider=PROVIDER,
            model=MODEL,
            knowledge_base_url=FAKE_KNOWLEDGE_URL,
            knowledge_token=FAKE_KNOWLEDGE_TOKEN,
            seat=SEAT,
            executable=DEFAULT_PI_EXECUTABLE,
            # Keep this probe inexpensive while still exercising the real
            # provider/session path.
            thinking="off",
        )
        process = PiProcess(process_config)
        recording = _RecordingProcess(process)
        runtime = PiRuntime(process=recording, command_timeout_seconds=20.0)
        context = InitialContext(
            game_id=GAME_ID,
            seat=SEAT,
            session_epoch=SESSION_EPOCH,
        )
        config = RuntimeConfig(
            session_id=session_id,
            session_dir=temporary_root / "runtime",
            provider=PROVIDER,
            model=MODEL,
        )

        try:
            stage = "start"
            reference = await runtime.start(config, context)
            physical_session_id = process.session_id
            process_pid = process.process.pid if process.process is not None else None
            if reference.session_id != session_id:
                raise SmokeTestError("runtime returned the wrong logical session_id")
            if reference.session_epoch != SESSION_EPOCH:
                raise SmokeTestError("runtime returned the wrong session epoch")

            stage = "turn_1"
            first_request = _request(
                "runtime-smoke-turn-1",
                (
                    "Do not call any tool. Return exactly one strict JSON TurnResponse "
                    f"with request_id runtime-smoke-turn-1, kind speech, and speech.text "
                    f"exactly {public_nonce}. Remember the public nonce for the next turn."
                ),
                args.timeout,
            )
            first = await runtime.run_turn(first_request)
            if first.request_id != first_request.request_id:
                raise SmokeTestError("first result request_id was not preserved")
            if first.response.request_id != first_request.request_id:
                raise SmokeTestError("first response request_id was not preserved")
            first_ack_matches = (
                first.response.speech is not None and first.response.speech.text == public_nonce
            )
            if not first_ack_matches:
                raise SmokeTestError("first turn did not return the nonce acknowledgement")

            stage = "turn_2"
            second_request = _request(
                "runtime-smoke-turn-2",
                "What is the nonce you were asked to remember in the previous turn? "
                "Return only that nonce as speech.text, with no prefix or suffix.",
                args.timeout,
            )
            second_prompt = runtime._build_prompt_message(second_request)  # noqa: SLF001
            _assert_nonce_absent(
                public_nonce,
                second_request=second_request,
                second_prompt=second_prompt,
                logical_session_id=session_id,
                physical_session_id=physical_session_id,
                session_dir=process.session_dir,
                append_system_prompt=process.config.append_system_prompt,
            )
            second = await runtime.run_turn(second_request)
            if second.request_id != second_request.request_id:
                raise SmokeTestError("second result request_id was not preserved")
            if second.response.request_id != second_request.request_id:
                raise SmokeTestError("second response request_id was not preserved")
            nonce_match = (
                second.response.speech is not None and second.response.speech.text == public_nonce
            )
            if not nonce_match:
                raise SmokeTestError("second turn did not recover the first-turn nonce")
            if process.session_id != physical_session_id:
                raise SmokeTestError("Pi physical session_id changed between turns")

            stage = "close"
            await runtime.close("real two-turn runtime smoke complete")
            closed_process_returncode = process.returncode
            if not process.closed or closed_process_returncode is None:
                raise SmokeTestError("Pi process did not report closed with a final return code")
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "provider": PROVIDER,
                        "model": MODEL,
                        "logical_session_id": session_id,
                        "physical_session_id": physical_session_id,
                        "session_epoch": SESSION_EPOCH,
                        "request_ids": [first.request_id, second.request_id],
                        "nonce_match": nonce_match,
                        "process_pid": process_pid,
                        "process_returncode": closed_process_returncode,
                        "process_tree_closed": True,
                    },
                    ensure_ascii=True,
                )
            )
            return 0
        except Exception as exc:
            # Close is attempted exactly once below; no retry loop can spend
            # additional provider calls after a failed stage.
            shapes = _event_shapes(recording.records) if recording is not None else []
            suffix = f"; event_shapes={shapes}" if shapes else ""
            raise SmokeTestError(
                f"{stage}: {_safe_error(exc, redactions=(public_nonce,))}{suffix}"
            ) from exc
        finally:
            if runtime is not None and not runtime._closed:  # noqa: SLF001 - smoke cleanup guard
                try:
                    await runtime.close("real two-turn runtime smoke cleanup")
                except Exception:
                    pass
            if process is not None:
                closed_process_returncode = process.returncode


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run_smoke(_parse_args(argv)))
    except (SmokeTestError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": _safe_error(exc),
                },
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
