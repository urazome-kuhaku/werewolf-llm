"""Run a bounded real-Pi action turn through the authoritative scheduler.

The probe installs one synthetic seer action window, starts one seat-scoped
Pi session with the host's existing Pi authentication, and lets Pi propose a
single ``SEER_INSPECT`` action.  The game manager records the proposal and
acknowledges the visible event batch; it does not resolve the skill.  Only
safe protocol metadata is printed.

Run with::

    uv run python tools/pi_action_smoke.py

The knowledge URL and token are deliberately loopback values.  The prompt
contains only a synthetic role and two synthetic seats.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionTurnScheduler,
    ActionValidationContext,
    ActionWindow,
    GameManager,
    GameState,
    PlayerState,
    RulesetRef,
    load_action_registry,
)
from werewolf.game.events import EventType, GameEvent, PublicAnnouncementPayload
from werewolf.runtime.pi_process import (
    DEFAULT_PI_EXECUTABLE,
    PiProcess,
    PiProcessConfig,
)
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import InitialContext, RuntimeConfig

FAKE_KNOWLEDGE_URL = "http://127.0.0.1:9/v1"
FAKE_KNOWLEDGE_TOKEN = "action-smoke-loopback-token"
PROVIDER = "github-copilot"
MODEL = "gpt-6-luna"
GAME_ID = "pi-action-smoke-game"
SESSION_EPOCH = 7
SEAT = 1
TARGET = 2
ACTION_CODE = 102
DEFAULT_TIMEOUT_SECONDS = 180.0


class SmokeTestError(RuntimeError):
    """Raised when the end-to-end action contract cannot be proven."""


def _safe_error(error: BaseException) -> str:
    text = str(error)
    for value in (FAKE_KNOWLEDGE_TOKEN, FAKE_KNOWLEDGE_URL):
        text = text.replace(value, "[redacted]")
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|bearer|password)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2[redacted]",
        text,
    )
    return " ".join(text.split())[:600]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="hard timeout for the real Pi action turn (seconds)",
    )
    return parser.parse_args(argv)


def _action_window(now: datetime) -> ActionWindow:
    return ActionWindow(
        window_id="pi-action-smoke-window",
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(SEAT,),
        allowed_role_ids=("seer",),
        allowed_action_codes=(ACTION_CODE,),
        min_actions=1,
        max_actions=1,
        opened_at=now,
        visible_context={"candidate_seats": [TARGET]},
    )


def _state(now: datetime, window: ActionWindow) -> GameState:
    return GameState(
        game_id=GAME_ID,
        created_at=now,
        updated_at=now,
        phase=GamePhase.NIGHT_ACTION,
        ruleset=RulesetRef(
            board_id="classic-12",
            version="1.0.0",
            snapshot_id="pi-action-smoke",
            manifest_sha256="a" * 64,
        ),
        players={
            SEAT: PlayerState(
                seat=SEAT,
                role_id="seer",
                faction_id="town",
                session_epoch=SESSION_EPOCH,
            ),
            TARGET: PlayerState(
                seat=TARGET,
                role_id="villager",
                faction_id="town",
                session_epoch=SESSION_EPOCH,
            ),
        },
        action_windows={window.window_id: window.model_dump(mode="json")},
    )


def _context() -> ActionValidationContext:
    return ActionValidationContext(
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        active_request_id="scheduler-bound-request",
        role_id="seer",
        authorized_action_codes=(ACTION_CODE,),
        alive_seats=(SEAT, TARGET),
        eligible_targets_by_action={ACTION_CODE: (TARGET,)},
    )


def _system_prompt(path: Path) -> None:
    path.write_text(
        "You are a synthetic werewolf game player for a contract smoke test.\n"
        "Your private role is seer. There are exactly two synthetic seats: 1 and 2.\n"
        "For the action request, output ONLY one compact JSON object, with no prose, "
        "markdown fence, or extra keys. Use the request_id from the request. The exact "
        'shape is: {"schema_version":1,"request_id":"<request_id>",'
        '"kind":"action","actions":[{"action_code":102,'
        '"targets":[2],"parameters":{},"reason_public":null}]} .\n'
        "Do not call tools, mention credentials, or include hidden state.\n",
        encoding="utf-8",
    )


def _announcement(now: datetime) -> GameEvent:
    return GameEvent.public(
        event_id=1,
        game_id=GAME_ID,
        state_revision=1,
        round_no=0,
        phase=GamePhase.NIGHT_ACTION,
        created_at=now,
        event_type=EventType.ANNOUNCEMENT,
        eligible_seats=(SEAT, TARGET),
        payload=PublicAnnouncementPayload(content="synthetic night begins"),
    )


async def run_smoke(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise SmokeTestError("timeout must be positive")
    now = datetime.now(UTC)
    window = _action_window(now)
    manager = GameManager(_state(now, window), registry=load_action_registry())
    process: PiProcess | None = None
    runtime: PiRuntime | None = None
    stage = "setup"
    process_pid: int | None = None

    with tempfile.TemporaryDirectory(
        prefix="werewolf-pi-action-", ignore_cleanup_errors=True
    ) as raw:
        root = Path(raw)
        prompt_path = root / "synthetic-role.txt"
        _system_prompt(prompt_path)
        process = PiProcess(
            PiProcessConfig(
                session_root=root / "sessions",
                provider=PROVIDER,
                model=MODEL,
                knowledge_base_url=FAKE_KNOWLEDGE_URL,
                knowledge_token=FAKE_KNOWLEDGE_TOKEN,
                seat=SEAT,
                executable=DEFAULT_PI_EXECUTABLE,
                append_system_prompt=prompt_path,
                thinking="off",
            )
        )
        runtime = PiRuntime(process=process, command_timeout_seconds=20.0)
        config = RuntimeConfig(
            session_id=f"pi-action-smoke-{uuid4().hex[:12]}",
            session_dir=root / "runtime",
            provider=PROVIDER,
            model=MODEL,
        )
        context = InitialContext(
            game_id=GAME_ID,
            seat=SEAT,
            session_epoch=SESSION_EPOCH,
            role_id="seer",
        )
        try:
            stage = "start"
            await runtime.start(config, context)
            process_pid = process.process.pid if process.process is not None else None
            stage = "event"
            await manager.commit_events((_announcement(now),), now=now)
            before = await manager.snapshot()
            stage = "action_turn"
            result = await asyncio.wait_for(
                ActionTurnScheduler(
                    manager,
                    {SEAT: runtime},
                    timeout_seconds=args.timeout,
                ).run_turn(window, SEAT, _context()),
                timeout=args.timeout + 20.0,
            )
            response = result.runtime_result.response
            if response.kind != "action" or not response.actions:
                raise SmokeTestError("Pi did not return an action response")
            action = response.actions[0]
            if action.action_code != ACTION_CODE or action.targets != [TARGET]:
                raise SmokeTestError("Pi returned an unexpected action proposal")
            after = result.state
            if not after.action_requests:
                raise SmokeTestError("manager did not record the action request")
            if (
                after.phase != before.phase
                or after.players[SEAT].alive is not before.players[SEAT].alive
            ):
                raise SmokeTestError("action submission changed lifecycle or alive state")
            cursor = after.delivery_cursors.get(SEAT)
            if cursor is None or cursor.in_flight_request_id is not None:
                raise SmokeTestError("successful action did not confirm its event delivery")
            stage = "close"
            await runtime.close("real Pi action smoke complete")
            if not process.closed or process.returncode is None:
                raise SmokeTestError("Pi process did not close with a return code")
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "provider": PROVIDER,
                        "model": MODEL,
                        "game_id": GAME_ID,
                        "seat": SEAT,
                        "window_id": window.window_id,
                        "action_code": ACTION_CODE,
                        "target_count": len(action.targets),
                        "request_count": len(after.action_requests),
                        "process_pid": process_pid,
                        "process_returncode": process.returncode,
                        "process_tree_closed": True,
                        "alive_state_unchanged": True,
                    },
                    ensure_ascii=True,
                )
            )
            return 0
        except Exception as exc:
            raise SmokeTestError(f"{stage}: {_safe_error(exc)}") from exc
        finally:
            if runtime is not None and not runtime._closed:  # noqa: SLF001
                try:
                    await runtime.close("real Pi action smoke cleanup")
                except Exception:
                    pass


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run_smoke(_parse_args(argv)))
    except (SmokeTestError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__, "error": _safe_error(exc)},
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
