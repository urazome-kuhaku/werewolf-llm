"""Strict framing and turn-completion contracts for the Pi RPC protocol.

This module does not create or own a subprocess.  It handles the untrusted
bytes and JSON records produced by Pi's RPC mode and keeps the semantic
boundary needed by a future PiRuntime.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, TypeAlias

DEFAULT_MAX_JSONL_LINE_BYTES: Final = 1 * 1024 * 1024
DEFAULT_MAX_JSONL_BUFFER_BYTES: Final = 4 * 1024 * 1024
DEFAULT_MAX_JSONL_TOTAL_BYTES: Final = 64 * 1024 * 1024
JsonRecord: TypeAlias = dict[str, Any]


class PiProtocolError(RuntimeError):
    """Base error for malformed or semantically unsafe Pi protocol input."""


class JsonlProtocolError(PiProtocolError):
    """The stdout JSONL stream could not be decoded safely."""


class JsonlLimitError(JsonlProtocolError):
    """A JSONL line, pending buffer, or stream exceeded its configured limit."""


class JsonlEncodingError(JsonlProtocolError):
    """A JSONL line was not valid strict UTF-8."""


class JsonlParseError(JsonlProtocolError):
    """A JSONL line was not valid strict JSON."""


class PiRpcCorrelationError(PiProtocolError):
    """A response/event could not be associated with the active turn."""


class PiRpcStateError(PiRpcCorrelationError):
    """The requested tracker transition is invalid for the active turn."""


class PiRpcFinalTextError(PiProtocolError):
    """The authoritative final-text contract was violated."""


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


class JsonlDecoder:
    """Incrementally decode strict LF-framed JSON records from bytes.

    Framing is performed on byte LF only.  U+2028 and U+2029 therefore remain
    ordinary characters inside JSON strings.  A final unterminated record is
    accepted by finish, matching Pi's own RPC reader.
    """

    def __init__(
        self,
        *,
        max_line_bytes: int = DEFAULT_MAX_JSONL_LINE_BYTES,
        max_buffer_bytes: int = DEFAULT_MAX_JSONL_BUFFER_BYTES,
        max_total_bytes: int = DEFAULT_MAX_JSONL_TOTAL_BYTES,
    ) -> None:
        for name, value in (
            ("max_line_bytes", max_line_bytes),
            ("max_buffer_bytes", max_buffer_bytes),
            ("max_total_bytes", max_total_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_line_bytes > max_buffer_bytes:
            raise ValueError("max_line_bytes must not exceed max_buffer_bytes")
        if max_buffer_bytes > max_total_bytes:
            raise ValueError("max_buffer_bytes must not exceed max_total_bytes")
        self.max_line_bytes = max_line_bytes
        self.max_buffer_bytes = max_buffer_bytes
        self.max_total_bytes = max_total_bytes
        self._buffer = bytearray()
        self._total_bytes = 0
        self._finished = False
        self._failed = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[object]:
        """Consume bytes and return every complete JSON record."""

        self._ensure_usable()
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("JSONL input must be bytes-like")
        raw = bytes(chunk)
        next_total = self._total_bytes + len(raw)
        if next_total > self.max_total_bytes:
            self._fail(JsonlLimitError("JSONL stream exceeded max_total_bytes"))
        self._total_bytes = next_total
        if raw:
            self._buffer.extend(raw)
        records: list[object] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            records.append(self._decode_line(line))
        if len(self._buffer) > self.max_line_bytes:
            self._fail(JsonlLimitError("JSONL line exceeded max_line_bytes"))
        return records

    def finish(self) -> list[object]:
        """Finish the stream, decoding one optional final partial line."""

        self._ensure_usable()
        self._finished = True
        if not self._buffer:
            return []
        line = bytes(self._buffer)
        self._buffer.clear()
        return [self._decode_line(line)]

    def feed_eof(self) -> list[object]:
        return self.finish()

    def reset(self) -> None:
        self._buffer.clear()
        self._total_bytes = 0
        self._finished = False
        self._failed = False

    def _decode_line(self, line: bytes) -> object:
        if not line:
            self._fail(JsonlParseError("empty JSONL record"))
        if len(line) > self.max_line_bytes:
            self._fail(JsonlLimitError("JSONL line exceeded max_line_bytes"))
        try:
            text = line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            self._fail(JsonlEncodingError("JSONL record is not valid UTF-8"), cause=exc)
            raise AssertionError("unreachable")
        try:
            return json.loads(text, parse_constant=_reject_non_finite)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._fail(JsonlParseError("JSONL record is not valid JSON"), cause=exc)
            raise AssertionError("unreachable")
        raise AssertionError("unreachable")

    def _ensure_usable(self) -> None:
        if self._failed:
            raise JsonlProtocolError("JSONL decoder is failed closed")
        if self._finished:
            raise JsonlProtocolError("JSONL decoder has already finished")

    def _fail(self, error: JsonlProtocolError, *, cause: BaseException | None = None) -> Any:
        self._failed = True
        if cause is None:
            raise error
        raise error from cause


def decode_jsonl(
    data: bytes | bytearray | memoryview,
    *,
    max_line_bytes: int = DEFAULT_MAX_JSONL_LINE_BYTES,
    max_buffer_bytes: int = DEFAULT_MAX_JSONL_BUFFER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_JSONL_TOTAL_BYTES,
) -> list[object]:
    decoder = JsonlDecoder(
        max_line_bytes=max_line_bytes,
        max_buffer_bytes=max_buffer_bytes,
        max_total_bytes=max_total_bytes,
    )
    return [*decoder.feed(data), *decoder.finish()]


def serialize_jsonl(value: Mapping[str, Any]) -> bytes:
    """Serialize one command/record with Pi-compatible LF framing."""

    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JsonlParseError("record cannot be serialized as strict JSON") from exc
    return encoded + b"\n"


class PiEventKind(StrEnum):
    """Normalized Pi session event categories."""

    COMMAND_ACCEPTED = "COMMAND_ACCEPTED"
    TURN_STARTED = "TURN_STARTED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_FINISHED = "TOOL_FINISHED"
    OUTPUT_DELTA = "OUTPUT_DELTA"
    ASSISTANT_MESSAGE_END = "ASSISTANT_MESSAGE_END"
    AGENT_SETTLED = "AGENT_SETTLED"
    TURN_FAILED = "TURN_FAILED"
    TURN_ABORTED = "TURN_ABORTED"
    PROCESS_EXITED = "PROCESS_EXITED"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class PiRpcResponse:
    """Validated shape of one Pi RPC response record."""

    rpc_id: str | None
    command: str
    success: bool
    data: Any = None
    error: str | None = None
    raw: Mapping[str, Any] | None = None

    @property
    def id(self) -> str | None:
        return self.rpc_id


@dataclass(frozen=True, slots=True)
class PiRpcEvent:
    """Normalized session event; raw remains available for diagnostics."""

    kind: PiEventKind
    raw_type: str
    text_delta: str | None = None
    assistant_text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    error: str | None = None
    raw: Mapping[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.kind in {
            PiEventKind.TURN_FAILED,
            PiEventKind.TURN_ABORTED,
            PiEventKind.PROCESS_EXITED,
        }


@dataclass(frozen=True, slots=True)
class RpcCorrelationKey:
    """Local identity used to associate one active turn and its terminal."""

    rpc_id: str
    session_epoch: int
    terminal_sequence: int


@dataclass(frozen=True, slots=True)
class ActivePiTurn:
    """Read-only view of tracker state for diagnostics and tests."""

    key: RpcCorrelationKey
    settled: bool = False
    final_text_rpc_id: str | None = None
    assistant_message_text: str | None = None


@dataclass(frozen=True, slots=True)
class PiTurnCompletion:
    """The only result eligible to cross into runtime validation."""

    key: RpcCorrelationKey
    final_text: str
    message_end_text: str
    usage: Mapping[str, Any] | None = None


def _as_record(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PiProtocolError("Pi RPC record must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise PiProtocolError("Pi RPC record keys must be strings")
    return value


def normalize_pi_response(value: object) -> PiRpcResponse:
    """Validate and normalize one Pi type=response record."""

    record = _as_record(value)
    if record.get("type") != "response":
        raise PiProtocolError("record is not a Pi RPC response")
    command = record.get("command")
    if not isinstance(command, str) or not command:
        raise PiProtocolError("RPC response command must be a non-empty string")
    success = record.get("success")
    if not isinstance(success, bool):
        raise PiProtocolError("RPC response success must be a boolean")
    rpc_id = record.get("id")
    if rpc_id is not None and (not isinstance(rpc_id, str) or not rpc_id):
        raise PiProtocolError("RPC response id must be a non-empty string when present")
    error = record.get("error")
    if error is not None and not isinstance(error, str):
        raise PiProtocolError("RPC response error must be a string when present")
    if not success and not error:
        raise PiProtocolError("failed RPC response must include an error")
    return PiRpcResponse(
        rpc_id=rpc_id,
        command=command,
        success=success,
        data=record.get("data"),
        error=error,
        raw=record,
    )


def _message_text(message: object) -> str:
    if not isinstance(message, Mapping):
        raise PiProtocolError("assistant message must be an object")
    if message.get("role") != "assistant":
        raise PiProtocolError("message_end did not contain an assistant message")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise PiProtocolError("assistant message content must be a string or list")
    parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def normalize_pi_event(value: object) -> PiRpcEvent:
    """Normalize one Pi session event without assigning it to a turn."""

    record = _as_record(value)
    raw_type = record.get("type")
    if not isinstance(raw_type, str) or not raw_type:
        raise PiProtocolError("Pi event type must be a non-empty string")
    if raw_type in {"agent_start", "turn_start"}:
        return PiRpcEvent(PiEventKind.TURN_STARTED, raw_type, raw=record)
    if raw_type == "tool_execution_start":
        tool_call_id = record.get("toolCallId")
        tool_name = record.get("toolName")
        if tool_call_id is not None and not isinstance(tool_call_id, str):
            raise PiProtocolError("tool_execution_start toolCallId must be a string")
        if tool_name is not None and not isinstance(tool_name, str):
            raise PiProtocolError("tool_execution_start toolName must be a string")
        return PiRpcEvent(
            PiEventKind.TOOL_STARTED,
            raw_type,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            raw=record,
        )
    if raw_type == "tool_execution_end":
        tool_call_id = record.get("toolCallId")
        tool_name = record.get("toolName")
        is_error = record.get("isError", False)
        if tool_call_id is not None and not isinstance(tool_call_id, str):
            raise PiProtocolError("tool_execution_end toolCallId must be a string")
        if tool_name is not None and not isinstance(tool_name, str):
            raise PiProtocolError("tool_execution_end toolName must be a string")
        if not isinstance(is_error, bool):
            raise PiProtocolError("tool_execution_end isError must be a boolean")
        return PiRpcEvent(
            PiEventKind.TOOL_FINISHED,
            raw_type,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            error="tool execution failed" if is_error else None,
            raw=record,
        )
    if raw_type == "message_update":
        update = record.get("assistantMessageEvent")
        if not isinstance(update, Mapping):
            raise PiProtocolError("message_update assistantMessageEvent must be an object")
        update_type = update.get("type")
        if update_type == "text_delta":
            delta = update.get("delta")
            if not isinstance(delta, str):
                raise PiProtocolError("text_delta delta must be a string")
            return PiRpcEvent(PiEventKind.OUTPUT_DELTA, raw_type, text_delta=delta, raw=record)
        if update_type == "error":
            error = update.get("error")
            if not isinstance(error, str):
                error = "assistant stream failed"
            return PiRpcEvent(PiEventKind.TURN_FAILED, raw_type, error=error, raw=record)
        return PiRpcEvent(PiEventKind.OTHER, raw_type, raw=record)
    if raw_type == "message_end":
        message = record.get("message")
        # Pi emits lifecycle messages for system and user context as well as
        # the assistant's answer. Only the assistant message can settle the
        # runtime turn; other roles are informative stream events.
        if isinstance(message, Mapping) and message.get("role") != "assistant":
            return PiRpcEvent(PiEventKind.OTHER, raw_type, raw=record)
        text = _message_text(message)
        return PiRpcEvent(
            PiEventKind.ASSISTANT_MESSAGE_END,
            raw_type,
            assistant_text=text,
            raw=record,
        )
    if raw_type == "agent_settled":
        return PiRpcEvent(PiEventKind.AGENT_SETTLED, raw_type, raw=record)
    if raw_type in {"agent_error", "error"}:
        error = record.get("error")
        if not isinstance(error, str):
            error = "Pi agent failed"
        return PiRpcEvent(PiEventKind.TURN_FAILED, raw_type, error=error, raw=record)
    if raw_type in {"agent_abort", "aborted"}:
        return PiRpcEvent(PiEventKind.TURN_ABORTED, raw_type, raw=record)
    return PiRpcEvent(PiEventKind.OTHER, raw_type, raw=record)


class PiRpcTurnTracker:
    """Associate one active Pi turn and enforce its terminal text contract."""

    def __init__(self) -> None:
        self._active: ActivePiTurn | None = None
        self._last_terminal_sequence = 0

    @property
    def active_turn(self) -> ActivePiTurn | None:
        return self._active

    @property
    def last_terminal_sequence(self) -> int:
        return self._last_terminal_sequence

    def start_turn(self, rpc_id: str, session_epoch: int) -> ActivePiTurn:
        if not isinstance(rpc_id, str) or not rpc_id:
            raise ValueError("rpc_id must be a non-empty string")
        if (
            isinstance(session_epoch, bool)
            or not isinstance(session_epoch, int)
            or session_epoch < 0
        ):
            raise ValueError("session_epoch must be a non-negative integer")
        if self._active is not None:
            raise PiRpcStateError("a Pi session already has an active turn")
        self._active = ActivePiTurn(
            key=RpcCorrelationKey(rpc_id, session_epoch, self._last_terminal_sequence + 1)
        )
        return self._active

    def accept_event(
        self,
        event: PiRpcEvent,
        *,
        session_epoch: int | None = None,
        key: RpcCorrelationKey | None = None,
    ) -> PiRpcEvent:
        """Associate an event with the active turn or reject it."""

        active = self._require_active()
        self._check_key(active, session_epoch=session_epoch, key=key)
        if event.kind == PiEventKind.ASSISTANT_MESSAGE_END:
            if active.settled:
                raise PiRpcStateError("assistant message_end must precede agent_settled")
            if event.assistant_text is None:
                raise PiProtocolError("assistant message_end has no text")
            self._active = ActivePiTurn(
                key=active.key,
                settled=active.settled,
                final_text_rpc_id=active.final_text_rpc_id,
                assistant_message_text=event.assistant_text,
            )
        elif event.kind == PiEventKind.AGENT_SETTLED:
            self._active = ActivePiTurn(
                key=active.key,
                settled=True,
                final_text_rpc_id=active.final_text_rpc_id,
                assistant_message_text=active.assistant_message_text,
            )
        elif event.is_terminal:
            self._last_terminal_sequence = active.key.terminal_sequence
            self._active = None
        return event

    def accept_response(
        self,
        response: PiRpcResponse,
        *,
        session_epoch: int | None = None,
        key: RpcCorrelationKey | None = None,
    ) -> PiRpcEvent | PiTurnCompletion:
        """Associate a response and, when appropriate, complete the turn."""

        active = self._require_active()
        self._check_key(active, session_epoch=session_epoch, key=key)
        if response.rpc_id is None:
            raise PiRpcCorrelationError("RPC response without id cannot be correlated")
        if response.rpc_id == active.key.rpc_id:
            if response.command != "prompt":
                raise PiRpcCorrelationError("active turn RPC id belongs to prompt only")
            if not response.success:
                self._last_terminal_sequence = active.key.terminal_sequence
                self._active = None
                return PiRpcEvent(
                    PiEventKind.TURN_FAILED,
                    "response",
                    error=response.error or "prompt was rejected",
                    raw=response.raw,
                )
            return PiRpcEvent(PiEventKind.COMMAND_ACCEPTED, "response", raw=response.raw)
        if response.rpc_id != active.final_text_rpc_id:
            raise PiRpcCorrelationError("RPC response id does not match the active turn")
        if response.command != "get_last_assistant_text":
            raise PiRpcCorrelationError("final-text RPC must be get_last_assistant_text")
        return self.finalize(response)

    def mark_final_text_request(self, rpc_id: str) -> ActivePiTurn:
        """Bind the RPC id used after settle to get_last_assistant_text."""

        active = self._require_active()
        if not active.settled:
            raise PiRpcStateError("agent_settled is required before requesting final text")
        if active.assistant_message_text is None:
            raise PiRpcStateError("assistant message_end is required before requesting final text")
        if not isinstance(rpc_id, str) or not rpc_id:
            raise ValueError("rpc_id must be a non-empty string")
        if rpc_id == active.key.rpc_id:
            raise PiRpcCorrelationError("final-text RPC id must differ from prompt RPC id")
        self._active = ActivePiTurn(
            key=active.key,
            settled=True,
            final_text_rpc_id=rpc_id,
            assistant_message_text=active.assistant_message_text,
        )
        return self._active

    def finalize(self, response: PiRpcResponse) -> PiTurnCompletion:
        """Validate authoritative text and close the active turn."""

        active = self._require_active()
        if not active.settled:
            raise PiRpcFinalTextError("final text is not accepted before agent_settled")
        if response.rpc_id != active.final_text_rpc_id:
            raise PiRpcCorrelationError("final-text response id does not match active request")
        if response.command != "get_last_assistant_text":
            raise PiRpcFinalTextError("final text must come from get_last_assistant_text")
        if not response.success:
            raise PiRpcFinalTextError(response.error or "get_last_assistant_text failed")
        if not isinstance(response.data, Mapping):
            raise PiRpcFinalTextError("get_last_assistant_text response data is not an object")
        final_text = response.data.get("text")
        if not isinstance(final_text, str) or not final_text:
            raise PiRpcFinalTextError("authoritative final text is empty or missing")
        message_end_text = active.assistant_message_text
        if message_end_text is None:
            raise PiRpcFinalTextError("assistant message_end was not observed")
        if final_text != message_end_text:
            raise PiRpcFinalTextError(
                "get_last_assistant_text disagrees with assistant message_end"
            )
        usage = response.data.get("usage")
        usage_mapping = usage if isinstance(usage, Mapping) else None
        completion = PiTurnCompletion(
            key=active.key,
            final_text=final_text,
            message_end_text=message_end_text,
            usage=usage_mapping,
        )
        self._last_terminal_sequence = active.key.terminal_sequence
        self._active = None
        return completion

    def process_record(
        self,
        value: object,
        *,
        session_epoch: int | None = None,
        key: RpcCorrelationKey | None = None,
    ) -> PiRpcEvent | PiRpcResponse | PiTurnCompletion:
        """Normalize and associate one decoded JSON record."""

        record = _as_record(value)
        if record.get("type") == "response":
            return self.accept_response(
                normalize_pi_response(record), session_epoch=session_epoch, key=key
            )
        return self.accept_event(normalize_pi_event(record), session_epoch=session_epoch, key=key)

    def process_process_exit(
        self,
        *,
        session_epoch: int | None = None,
        key: RpcCorrelationKey | None = None,
    ) -> PiRpcEvent:
        event = PiRpcEvent(PiEventKind.PROCESS_EXITED, "process_exit")
        return self.accept_event(event, session_epoch=session_epoch, key=key)

    def _require_active(self) -> ActivePiTurn:
        if self._active is None:
            raise PiRpcCorrelationError("no active Pi turn")
        return self._active

    @staticmethod
    def _check_key(
        active: ActivePiTurn,
        *,
        session_epoch: int | None,
        key: RpcCorrelationKey | None,
    ) -> None:
        if session_epoch is not None and session_epoch != active.key.session_epoch:
            raise PiRpcCorrelationError("session_epoch does not match active turn")
        if key is not None and key != active.key:
            raise PiRpcCorrelationError("correlation key does not match active turn")


PiJsonlDecoder = JsonlDecoder
PiRpcProtocol = PiRpcTurnTracker
normalize_event = normalize_pi_event
normalize_response = normalize_pi_response


__all__ = [
    "ActivePiTurn",
    "DEFAULT_MAX_JSONL_BUFFER_BYTES",
    "DEFAULT_MAX_JSONL_LINE_BYTES",
    "DEFAULT_MAX_JSONL_TOTAL_BYTES",
    "JsonlDecoder",
    "JsonlEncodingError",
    "JsonlLimitError",
    "JsonlParseError",
    "JsonlProtocolError",
    "PiEventKind",
    "PiJsonlDecoder",
    "PiProtocolError",
    "PiRpcCorrelationError",
    "PiRpcEvent",
    "PiRpcFinalTextError",
    "PiRpcProtocol",
    "PiRpcResponse",
    "PiRpcStateError",
    "PiRpcTurnTracker",
    "PiTurnCompletion",
    "RpcCorrelationKey",
    "decode_jsonl",
    "normalize_event",
    "normalize_pi_event",
    "normalize_pi_response",
    "normalize_response",
    "serialize_jsonl",
]
