"""PlayerRuntime adapter for one persistent Pi RPC session.

The process and wire framing boundaries live in :mod:`pi_process`; this
module owns the higher level session contract used by the game scheduler.  A
session has one reader task and at most one active game turn.  Pi events are
correlated by :class:`PiRpcTurnTracker`, and only the authoritative final
assistant text is allowed to cross the runtime boundary.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import ValidationError

from .pi_process import (
    DEFAULT_PI_EXECUTABLE,
    PiProcess,
    PiProcessConfig,
    PiProcessError,
)
from .pi_rpc_protocol import (
    PiEventKind,
    PiRpcCorrelationError,
    PiRpcEvent,
    PiRpcResponse,
    PiRpcTurnTracker,
    PiTurnCompletion,
    normalize_pi_response,
)
from .player_runtime import (
    ActionResponse,
    InitialContext,
    RuntimeConfig,
    RuntimeLifecycleError,
    RuntimeProtocolError,
    RuntimeRef,
    RuntimeRequestMismatchError,
    RuntimeResponseValidationError,
    RuntimeTurnResult,
    TurnRequest,
    TurnResponse,
    validate_turn_response,
)
from .prompt_composer import write_system_prompt


class PiProcessLike(Protocol):
    started: bool
    closed: bool

    async def start(self) -> Any: ...

    async def send_record(self, record: Mapping[str, Any]) -> None: ...

    async def read_record(self) -> object | None: ...

    async def close(self, reason: str = "normal shutdown") -> None: ...


ProcessFactory = Callable[[RuntimeConfig, InitialContext], PiProcessLike | Awaitable[PiProcessLike]]


class PiRuntimeError(RuntimeProtocolError):
    """Base error raised by the Pi player runtime."""


class PiRuntimeHandshakeError(PiRuntimeError):
    """The Pi session did not complete the required startup handshake."""


class PiRuntimeTurnFailed(PiRuntimeError):
    """Pi ended the active turn without a usable response."""


class PiRuntimeTurnAborted(PiRuntimeTurnFailed):
    """The active turn was explicitly aborted by the host."""


class PiRuntimeHardTimeout(PiRuntimeTurnFailed):
    """The hard deadline elapsed; the game must wait for host handling."""


class _RecordEnd:
    pass


class _RecordFailure:
    def __init__(self, error: BaseException) -> None:
        self.error = error


class PiRuntime:
    """One seat-scoped, persistent implementation of ``PlayerRuntime``.

    Tests and embedders may inject ``process`` or ``process_factory``.  The
    default factory constructs the hardened :class:`PiProcess` from the
    session configuration.  ``knowledge_base_url`` and ``knowledge_token``
    are only needed by that default factory and are never placed in a prompt.
    """

    def __init__(
        self,
        *,
        process: PiProcessLike | None = None,
        process_factory: ProcessFactory | None = None,
        knowledge_base_url: str | None = None,
        knowledge_token: str | None = None,
        executable: str | Path | None = None,
        compatible_version: str | None = None,
        auto_compaction: bool = True,
        auto_retry: bool = False,
        soft_timeout_seconds: float = 120.0,
        command_timeout_seconds: float = 10.0,
    ) -> None:
        if process is not None and process_factory is not None:
            raise ValueError("process and process_factory are mutually exclusive")
        for name, value in (
            ("soft_timeout_seconds", soft_timeout_seconds),
            ("command_timeout_seconds", command_timeout_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        self._injected_process = process
        self._process_factory = process_factory
        self._knowledge_base_url = knowledge_base_url
        self._knowledge_token = knowledge_token
        self._executable = executable
        self._compatible_version = compatible_version
        self._auto_compaction = auto_compaction
        self._auto_retry = auto_retry
        self._soft_timeout_seconds = float(soft_timeout_seconds)
        self._command_timeout_seconds = float(command_timeout_seconds)
        self._process: PiProcessLike | None = None
        self._session_ref: RuntimeRef | None = None
        self._context: InitialContext | None = None
        self._runtime_config: RuntimeConfig | None = None
        self._closed = False
        self._tracker = PiRpcTurnTracker()
        self._reader_task: asyncio.Task[None] | None = None
        self._turn_records: asyncio.Queue[object] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[PiRpcResponse]] = {}
        self._active_request: TurnRequest | None = None
        self._active_key: object | None = None
        self._abort_requested = False
        self._abort_done = asyncio.Event()
        self._reader_failure: BaseException | None = None
        self._rpc_counter = 0
        self._system_prompt_path: Path | None = None

    async def start(self, config: RuntimeConfig, context: InitialContext) -> RuntimeRef:
        if self._session_ref is not None:
            raise RuntimeLifecycleError("runtime has already been started")
        if config.session_id.strip() == "":
            raise RuntimeLifecycleError("runtime session_id must not be blank")
        if (
            config.session_dir is None
            and self._process_factory is None
            and self._injected_process is None
        ):
            # The default process uses this directory as its bounded root.  A
            # caller can still provide a different root through RuntimeConfig.
            session_root = Path(".runtime")
        else:
            session_root = config.session_dir or Path(".runtime")
        self._context = context
        self._runtime_config = config
        process = self._injected_process
        try:
            if process is None:
                if self._process_factory is not None:
                    created = self._process_factory(config, context)
                    process = await created if inspect.isawaitable(created) else created
                else:
                    process = cast(
                        PiProcessLike, self._build_default_process(config, context, session_root)
                    )
            if process is None:
                raise RuntimeLifecycleError("runtime process factory returned None")
            self._process = process
            try:
                started = process.started
            except AttributeError:
                started = False
            if not started:
                await process.start()
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name=f"pi-runtime-reader-{config.session_id}"
            )
            await self._handshake()
        except BaseException:
            reader_task = self._reader_task
            self._reader_task = None
            if reader_task is not None:
                reader_task.cancel()
                try:
                    await reader_task
                except asyncio.CancelledError:
                    pass
            if process is not None:
                try:
                    await process.close("startup failed")
                except BaseException:
                    pass
            self._remove_system_prompt()
            raise
        self._session_ref = RuntimeRef(
            session_id=config.session_id,
            game_id=context.game_id,
            seat=context.seat,
            session_epoch=context.session_epoch,
        )
        return self._session_ref

    async def run_turn(self, request: TurnRequest) -> RuntimeTurnResult:
        self._require_open()
        context = self._require_context()
        if request.game_id != context.game_id or request.session_epoch != context.session_epoch:
            raise RuntimeProtocolError("turn request does not match runtime game or session epoch")
        if self._active_request is not None:
            raise RuntimeLifecycleError("runtime already has an active turn")
        if self._reader_failure is not None:
            raise PiRuntimeError("Pi reader is unavailable") from self._reader_failure
        self._drain_turn_records()
        prompt_id = self._new_rpc_id("prompt")
        active = self._tracker.start_turn(prompt_id, request.session_epoch)
        self._active_key = active.key
        self._active_request = request
        self._abort_requested = False
        self._abort_done.clear()
        started_at = time.monotonic()
        try:
            await self._send(
                {
                    "id": prompt_id,
                    "type": "prompt",
                    "message": self._build_prompt_message(request),
                }
            )
            soft_sent = False
            soft_at = min(
                self._soft_timeout_seconds, self._deadline_delta(request.deadline.soft_deadline)
            )
            hard_at = self._deadline_delta(request.deadline.hard_deadline)
            soft_timer = started_at + soft_at
            hard_timer = started_at + hard_at
            while True:
                now = time.monotonic()
                remaining_hard = hard_timer - now
                if remaining_hard <= 0:
                    raise PiRuntimeHardTimeout(
                        f"hard timeout reached for request {request.request_id}; "
                        "host action required"
                    )
                timeout = remaining_hard
                if not soft_sent:
                    timeout = min(timeout, max(0.0, soft_timer - now))
                try:
                    record = await asyncio.wait_for(self._turn_records.get(), timeout=timeout)
                except TimeoutError:
                    if not soft_sent and time.monotonic() < hard_timer:
                        soft_sent = True
                        await self._send_steer_internal(
                            request.request_id,
                            "The turn is approaching its deadline. "
                            "Return one strict JSON response now.",
                        )
                        continue
                    raise PiRuntimeHardTimeout(
                        f"hard timeout reached for request {request.request_id}; "
                        "host action required"
                    )
                if isinstance(record, _RecordEnd):
                    raise PiRuntimeTurnFailed("Pi process exited before the turn settled")
                if isinstance(record, _RecordFailure):
                    raise PiRuntimeTurnFailed(
                        "Pi reader failed before the turn settled"
                    ) from record.error
                result = self._process_turn_record(record, request)
                if result is None:
                    continue
                if isinstance(result, PiTurnCompletion):
                    return self._build_result(request, result, started_at)
                if isinstance(result, PiRpcEvent):
                    if result.kind is PiEventKind.AGENT_SETTLED:
                        if self._abort_requested:
                            raise PiRuntimeTurnAborted("turn was aborted")
                        final_id = self._new_rpc_id("final")
                        self._tracker.mark_final_text_request(final_id)
                        await self._send({"id": final_id, "type": "get_last_assistant_text"})
                    elif result.kind in {
                        PiEventKind.TURN_FAILED,
                        PiEventKind.TURN_ABORTED,
                        PiEventKind.PROCESS_EXITED,
                    }:
                        if result.kind is PiEventKind.TURN_ABORTED or self._abort_requested:
                            raise PiRuntimeTurnAborted(result.error or "turn was aborted")
                        raise PiRuntimeTurnFailed(result.error or "Pi turn failed")
        except PiRuntimeHardTimeout:
            # Keep the session marked active.  The host must abort or extend
            # it; late records cannot be submitted as a new game turn.
            raise
        finally:
            if self._active_request is not None and self._tracker.active_turn is None:
                self._active_request = None
                self._active_key = None
                self._abort_requested = False

    async def steer(self, request_id: str, message: str) -> None:
        self._require_open()
        self._require_active(request_id)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("steer message must not be blank")
        await self._send_steer_internal(request_id, message)

    async def abort(self, request_id: str) -> None:
        self._require_open()
        self._require_active(request_id)
        self._abort_requested = True
        self._abort_done.clear()
        await self._request_command("clear_queue")
        await self._request_command("abort")
        try:
            await asyncio.wait_for(self._abort_done.wait(), timeout=self._command_timeout_seconds)
        except TimeoutError as exc:
            raise PiRuntimeTurnFailed("abort was accepted but Pi did not settle in time") from exc
        self._tracker = PiRpcTurnTracker()
        self._active_request = None
        self._active_key = None
        self._abort_requested = False

    async def close(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("close reason must not be blank")
        if self._active_request is not None:
            raise RuntimeLifecycleError("cannot close while a turn is active")
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        process = self._process
        try:
            if process is not None:
                await process.close(reason)
        finally:
            self._remove_system_prompt()

    def get_session_ref(self) -> RuntimeRef:
        if self._session_ref is None:
            raise RuntimeLifecycleError("runtime has not been started")
        return self._session_ref

    async def _handshake(self) -> None:
        config = self._runtime_config
        commands: tuple[tuple[str, dict[str, Any]], ...] = (
            ("get_state", {}),
            ("set_steering_mode", {"mode": "one-at-a-time"}),
            ("set_follow_up_mode", {"mode": "one-at-a-time"}),
            (
                "set_auto_compaction",
                {
                    "enabled": config.auto_compaction
                    if config is not None
                    else self._auto_compaction
                },
            ),
            (
                "set_auto_retry",
                {"enabled": config.auto_retry if config is not None else self._auto_retry},
            ),
        )
        for command, fields in commands:
            try:
                await self._request_command(command, fields)
            except (PiProcessError, PiRuntimeError, TimeoutError) as exc:
                raise PiRuntimeHandshakeError(f"Pi handshake failed at {command}") from exc

    async def _request_command(
        self, command: str, fields: Mapping[str, Any] | None = None
    ) -> PiRpcResponse:
        process = self._require_process()
        rpc_id = self._new_rpc_id(command)
        future: asyncio.Future[PiRpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = future
        record: dict[str, Any] = {"id": rpc_id, "type": command}
        if fields:
            record.update(fields)
        try:
            await process.send_record(record)
            response = await asyncio.wait_for(future, timeout=self._command_timeout_seconds)
        finally:
            self._pending.pop(rpc_id, None)
        if response.command != command:
            raise PiRuntimeHandshakeError(
                f"Pi command {command} received response for {response.command}"
            )
        if not response.success:
            raise PiRuntimeHandshakeError(response.error or f"Pi command {command} failed")
        return response

    async def _send_steer_internal(self, request_id: str, message: str) -> None:
        process = self._require_process()
        rpc_id = self._new_rpc_id("steer")
        await process.send_record(
            {"id": rpc_id, "type": "steer", "message": message, "request_id": request_id}
        )

    async def _send(self, record: Mapping[str, Any]) -> None:
        await self._require_process().send_record(record)

    async def _reader_loop(self) -> None:
        process = self._require_process()
        try:
            while True:
                record = await process.read_record()
                if record is None:
                    self._abort_done.set()
                    await self._turn_records.put(_RecordEnd())
                    return
                if isinstance(record, Mapping) and record.get("type") == "response":
                    try:
                        response = normalize_pi_response(record)
                    except Exception as exc:
                        await self._turn_records.put(_RecordFailure(exc))
                        continue
                    future = self._pending.get(response.rpc_id or "")
                    if future is not None and not future.done():
                        future.set_result(response)
                        continue
                if isinstance(record, Mapping) and record.get("type") in {
                    "agent_settled",
                    "agent_abort",
                    "aborted",
                    "agent_error",
                }:
                    self._abort_done.set()
                await self._turn_records.put(record)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._reader_failure = exc
            await self._turn_records.put(_RecordFailure(exc))

    def _process_turn_record(
        self, record: object, request: TurnRequest
    ) -> PiTurnCompletion | PiRpcEvent | None:
        try:
            result = self._tracker.process_record(
                record,
                session_epoch=request.session_epoch,
                key=cast(Any, self._active_key),
            )
            if isinstance(result, PiRpcResponse):
                return None
            return result
        except PiRpcCorrelationError:
            # Records from an invalidated/older physical request are observed
            # and discarded.  They are never converted into a game result.
            return None

    def _build_result(
        self, request: TurnRequest, completion: PiTurnCompletion, started_at: float
    ) -> RuntimeTurnResult:
        try:
            raw = json.loads(completion.final_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeResponseValidationError(
                f"Pi final text for request {request.request_id} is not strict JSON"
            ) from exc
        try:
            response = validate_turn_response(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            raise RuntimeResponseValidationError(
                f"Pi final response for request {request.request_id} violates TurnResponse"
            ) from exc
        if response.request_id != request.request_id:
            raise RuntimeRequestMismatchError("response request_id does not match active request")
        if response.kind != request.expected_kind.value:
            raise RuntimeProtocolError(
                f"response kind {response.kind!r} does not match expected kind "
                f"{request.expected_kind.value!r}"
            )
        self._validate_action_window(request, response)
        self._active_request = None
        self._active_key = None
        self._tracker = PiRpcTurnTracker()
        self._abort_requested = False
        return RuntimeTurnResult(
            request_id=request.request_id,
            logical_request_id=request.logical_request_id,
            attempt_no=request.attempt_no,
            response=response,
            elapsed_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        )

    def _build_prompt_message(self, request: TurnRequest) -> str:
        payload = {
            "request_id": request.request_id,
            "logical_request_id": request.logical_request_id,
            "attempt_no": request.attempt_no,
            "phase": request.phase.value,
            "expected_kind": request.expected_kind.value,
            "action_window": request.action_window.model_dump(mode="json")
            if request.action_window is not None
            else None,
            "observation": request.observation.model_dump(mode="json"),
            "output_schema": request.output_schema,
            "instruction": (
                "Return exactly one complete JSON response object matching output_schema. "
                "Include schema_version, request_id, kind, and the required kind-specific "
                "payload. Set request_id exactly to the current request_id. For a ready "
                "response, knowledge_receipts must contain only receipt IDs returned by "
                "successful get_board and get_role tool queries; never guess or invent "
                "receipt IDs. Do not use markdown fences."
            ),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _validate_action_window(request: TurnRequest, response: TurnResponse) -> None:
        if request.action_window is None or not isinstance(response, ActionResponse):
            return
        window = request.action_window
        count = len(response.actions)
        if not window.min_actions <= count <= window.max_actions:
            raise RuntimeProtocolError("action count is outside the action window")
        allowed = set(window.allowed_action_codes)
        for action in response.actions:
            if action.action_code not in allowed:
                raise RuntimeProtocolError("response contains an action code outside the window")
            if action.action_code == 299 and not window.allow_pass:
                raise RuntimeProtocolError("PASS is not allowed by the action window")

    def _build_default_process(
        self, config: RuntimeConfig, context: InitialContext, session_root: Path
    ) -> PiProcess:
        if not self._knowledge_base_url or not self._knowledge_token:
            raise RuntimeLifecycleError(
                "default PiRuntime requires knowledge_base_url and knowledge_token"
            )
        system_prompt: Path | None = None
        if context.system_prompt is not None:
            if self._knowledge_token and self._knowledge_token in context.system_prompt:
                raise RuntimeLifecycleError("system prompt must not contain the knowledge token")
            prompt_root = Path(session_root)
            if config.session_dir is None:
                prompt_root = prompt_root / f"seat_{context.seat}"
            prompt_root = prompt_root.expanduser().resolve()
            prompt_root.mkdir(parents=True, exist_ok=True)
            system_prompt = write_system_prompt(
                prompt_root / f".system_prompt_{uuid.uuid4().hex}.md",
                context.system_prompt,
            )
            self._system_prompt_path = system_prompt
        process_config = PiProcessConfig(
            session_root=session_root,
            provider=config.provider or "github-copilot",
            model=config.model or "gpt-6-luna",
            knowledge_base_url=self._knowledge_base_url,
            knowledge_token=self._knowledge_token,
            seat=context.seat,
            executable=config.executable or self._executable or DEFAULT_PI_EXECUTABLE,
            compatible_version=config.compatible_version or self._compatible_version,
            thinking=config.reasoning,
            system_prompt=system_prompt,
            extension=_default_knowledge_extension(),
        )
        return PiProcess(process_config)

    def _remove_system_prompt(self) -> None:
        path, self._system_prompt_path = self._system_prompt_path, None
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _require_process(self) -> PiProcessLike:
        if self._process is None:
            raise RuntimeLifecycleError("runtime has not been started")
        return self._process

    def _require_open(self) -> None:
        if self._session_ref is None:
            raise RuntimeLifecycleError("runtime has not been started")
        if self._closed:
            raise RuntimeLifecycleError("runtime has been closed")

    def _require_context(self) -> InitialContext:
        if self._context is None:
            raise RuntimeLifecycleError("runtime has not been started")
        return self._context

    def _require_active(self, request_id: str) -> None:
        if self._active_request is None or self._active_request.request_id != request_id:
            raise RuntimeRequestMismatchError("request_id is not the active turn")

    def _drain_turn_records(self) -> None:
        while True:
            try:
                self._turn_records.get_nowait()
            except asyncio.QueueEmpty:
                return

    @staticmethod
    def _deadline_delta(deadline: datetime) -> float:
        value = deadline
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return max(0.0, value.timestamp() - datetime.now(UTC).timestamp())

    def _new_rpc_id(self, prefix: str) -> str:
        self._rpc_counter += 1
        return f"{prefix}-{self._rpc_counter}-{uuid.uuid4().hex[:8]}"


def _default_knowledge_extension() -> Path | None:
    """Return the checked-in gateway extension for source-tree launches."""

    candidate = Path(__file__).resolve().parents[3] / "extensions" / "werewolf_knowledge.ts"
    return candidate if candidate.is_file() else None


__all__ = [
    "PiProcessLike",
    "PiRuntime",
    "PiRuntimeError",
    "PiRuntimeHandshakeError",
    "PiRuntimeHardTimeout",
    "PiRuntimeTurnAborted",
    "PiRuntimeTurnFailed",
]
