"""Contract tests for Pi 0.87.1 JSONL framing and turn semantics."""

from __future__ import annotations

import json

import pytest

from werewolf.runtime.pi_rpc_protocol import (
    JsonlDecoder,
    JsonlEncodingError,
    JsonlLimitError,
    JsonlParseError,
    PiEventKind,
    PiRpcCorrelationError,
    PiRpcFinalTextError,
    PiRpcTurnTracker,
    decode_jsonl,
    normalize_pi_event,
    normalize_pi_response,
    serialize_jsonl,
)


def _assistant_message(text: str) -> dict[str, object]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _message_end(text: str) -> dict[str, object]:
    return {"type": "message_end", "message": _assistant_message(text)}


def _final_response(rpc_id: str, text: str) -> dict[str, object]:
    return {
        "id": rpc_id,
        "type": "response",
        "command": "get_last_assistant_text",
        "success": True,
        "data": {"text": text},
    }


def test_decoder_splits_only_byte_lf_and_accepts_crlf_and_unicode_separators() -> None:
    first = json.dumps({"text": "甲\u2028乙\u2029丙"}, ensure_ascii=False).encode("utf-8") + b"\r\n"
    second = b'{"type":"agent_settled"}'
    decoder = JsonlDecoder()

    assert decoder.feed(first[:4]) == []
    assert decoder.feed(first[4:] + second) == [{"text": "甲\u2028乙\u2029丙"}]
    assert decoder.finish() == [{"type": "agent_settled"}]


@pytest.mark.parametrize("payload", [b'{"x":\xff}\n', b"\xff\n"])
def test_decoder_rejects_invalid_utf8(payload: bytes) -> None:
    with pytest.raises(JsonlEncodingError):
        decode_jsonl(payload)


@pytest.mark.parametrize("payload", [b"{broken}\n", b"NaN\n", b"\n"])
def test_decoder_rejects_invalid_json(payload: bytes) -> None:
    with pytest.raises(JsonlParseError):
        decode_jsonl(payload)


def test_decoder_bounds_line_and_cumulative_stream_size() -> None:
    with pytest.raises(JsonlLimitError):
        decode_jsonl(b'{"long":"12345"}\n', max_line_bytes=8)

    decoder = JsonlDecoder(max_line_bytes=8, max_buffer_bytes=12, max_total_bytes=12)
    assert decoder.feed(b'{"a":1}\n') == [{"a": 1}]
    with pytest.raises(JsonlLimitError):
        decoder.feed(b'{"b":2}\n')


def test_serialize_jsonl_uses_utf8_and_lf() -> None:
    encoded = serialize_jsonl({"text": "a\u2028b"})
    assert encoded.endswith(b"\n")
    assert b"\r\n" not in encoded
    assert decode_jsonl(encoded) == [{"text": "a\u2028b"}]


def test_normalizers_cover_pi_events_and_rpc_response() -> None:
    assert normalize_pi_event({"type": "agent_start"}).kind is PiEventKind.TURN_STARTED
    delta = normalize_pi_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "增量"},
        }
    )
    assert delta.kind is PiEventKind.OUTPUT_DELTA
    assert delta.text_delta == "增量"
    ended = normalize_pi_event(_message_end("最终"))
    assert ended.kind is PiEventKind.ASSISTANT_MESSAGE_END
    assert ended.assistant_text == "最终"
    response = normalize_pi_response(
        {"id": "rpc-1", "type": "response", "command": "prompt", "success": True}
    )
    assert response.rpc_id == "rpc-1"
    assert response.command == "prompt"


def test_non_assistant_message_end_is_an_informative_event() -> None:
    event = normalize_pi_event(
        {
            "type": "message_end",
            "message": {"role": "system", "content": "runtime context"},
        }
    )

    assert event.kind is PiEventKind.OTHER
    assert event.assistant_text is None


def test_tracker_requires_settled_final_text_and_ignores_deltas_as_results() -> None:
    tracker = PiRpcTurnTracker()
    active = tracker.start_turn("prompt-1", session_epoch=4)
    assert active.key.terminal_sequence == 1
    assert (
        tracker.accept_response(
            normalize_pi_response(
                {"id": "prompt-1", "type": "response", "command": "prompt", "success": True}
            )
        ).kind
        is PiEventKind.COMMAND_ACCEPTED
    )
    delta = normalize_pi_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": '{"kind":"action"}'},
        }
    )
    tracker.accept_event(delta)
    assert tracker.active_turn is not None
    assert tracker.active_turn.assistant_message_text is None

    tracker.accept_event(normalize_pi_event(_message_end("最终文本")))
    tracker.accept_event(normalize_pi_event({"type": "agent_settled"}))
    tracker.mark_final_text_request("last-1")
    completion = tracker.accept_response(
        normalize_pi_response(_final_response("last-1", "最终文本"))
    )
    assert completion.final_text == "最终文本"
    assert tracker.active_turn is None
    assert tracker.last_terminal_sequence == 1


def test_tracker_rejects_early_final_text_and_mismatch() -> None:
    tracker = PiRpcTurnTracker()
    tracker.start_turn("prompt-1", session_epoch=0)
    tracker.accept_event(normalize_pi_event(_message_end("message-end")))
    with pytest.raises(PiRpcFinalTextError):
        tracker.finalize(normalize_pi_response(_final_response("last-1", "message-end")))

    tracker.accept_event(normalize_pi_event({"type": "agent_settled"}))
    tracker.mark_final_text_request("last-1")
    with pytest.raises(PiRpcFinalTextError, match="disagrees"):
        tracker.finalize(normalize_pi_response(_final_response("last-1", "different")))
    assert tracker.active_turn is not None


def test_tracker_rejects_stale_epoch_and_wrong_rpc_and_terminalizes_failures() -> None:
    tracker = PiRpcTurnTracker()
    key = tracker.start_turn("prompt-1", session_epoch=2).key
    with pytest.raises(PiRpcCorrelationError):
        tracker.accept_event(normalize_pi_event({"type": "turn_start"}), session_epoch=3)
    with pytest.raises(PiRpcCorrelationError):
        tracker.accept_response(
            normalize_pi_response(
                {"id": "other", "type": "response", "command": "prompt", "success": True}
            )
        )
    with pytest.raises(PiRpcCorrelationError):
        tracker.accept_event(
            normalize_pi_event({"type": "agent_settled"}),
            key=key.__class__(
                rpc_id=key.rpc_id, session_epoch=key.session_epoch, terminal_sequence=99
            ),
        )
    tracker.accept_event(normalize_pi_event({"type": "agent_error", "error": "provider"}))
    assert tracker.active_turn is None
    assert tracker.last_terminal_sequence == 1


def test_tracker_processes_decoded_records_end_to_end() -> None:
    wire = b"".join(
        [
            serialize_jsonl(
                {"id": "prompt", "type": "response", "command": "prompt", "success": True}
            ),
            serialize_jsonl(_message_end("ok")),
            serialize_jsonl({"type": "agent_settled"}),
            serialize_jsonl(_final_response("last", "ok")),
        ]
    )
    tracker = PiRpcTurnTracker()
    tracker.start_turn("prompt", 7)
    decoder = JsonlDecoder()
    records = decoder.feed(wire)
    assert decoder.finish() == []
    assert tracker.process_record(records[0], session_epoch=7).kind is PiEventKind.COMMAND_ACCEPTED
    tracker.process_record(records[1], session_epoch=7)
    tracker.process_record(records[2], session_epoch=7)
    tracker.mark_final_text_request("last")
    result = tracker.process_record(records[3], session_epoch=7)
    assert result.final_text == "ok"
