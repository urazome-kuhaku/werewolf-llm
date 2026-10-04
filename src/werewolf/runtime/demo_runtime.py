"""Deterministic, gateway-backed runtime for playable harness seats.

``DemoRuntime`` is the production harness runtime used when a configured seat
is intentionally scripted.  It follows the same runtime contract as Pi and
performs the same private reads over the seat bearer token.  It never receives
or inspects ``GameState``: the only inputs available to its decisions are the
startup receipts, its private skill projection, and the current
``TurnRequest``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp

from .deterministic_actions import build_deterministic_action_response
from .player_runtime import (
    ActionResponse,
    InitialContext,
    PlayerRuntime,
    Ready,
    ReadyResponse,
    RuntimeConfig,
    RuntimeLifecycleError,
    RuntimeProtocolError,
    RuntimeRef,
    RuntimeRequestMismatchError,
    RuntimeTurnResult,
    Speech,
    SpeechResponse,
    TurnRequest,
    TurnResponse,
    validate_turn_response,
)


class DemoRuntime(PlayerRuntime):
    """A deterministic harness player bound to one knowledge gateway token.

    The runtime is deliberately small and boring.  It is useful for running a
    complete game with a mixture of real Pi seats and local scripted seats,
    while still exercising the gateway, readiness receipts, action windows,
    and session lifecycle used by a real player.
    """

    def __init__(self, knowledge_base_url: str, knowledge_token: str) -> None:
        if not isinstance(knowledge_base_url, str) or not knowledge_base_url.strip():
            raise ValueError("knowledge_base_url must be a non-empty string")
        if not knowledge_base_url.startswith(("http://", "https://")):
            raise ValueError("knowledge_base_url must use HTTP or HTTPS")
        if not isinstance(knowledge_token, str) or not knowledge_token.strip():
            raise ValueError("knowledge_token must be a non-empty string")
        self._base_url = knowledge_base_url.rstrip("/")
        self._token = knowledge_token
        self._session: aiohttp.ClientSession | None = None
        self._session_ref: RuntimeRef | None = None
        self._context: InitialContext | None = None
        self._closed = False
        self._active_request_id: str | None = None
        # ``run_turn`` can be cancelled by the scheduler's hard timeout.  In
        # that case its finally block must clear the active marker, while the
        # immediately following retry still needs a safe, one-shot abort
        # handle for the request that was just cancelled.
        self._last_request_id: str | None = None
        self._receipts: list[str] = []
        self._requests: list[TurnRequest] = []
        self._steers: list[tuple[str, str]] = []
        self._aborts: list[str] = []

    async def start(self, config: RuntimeConfig, context: InitialContext) -> RuntimeRef:
        if self._session_ref is not None:
            raise RuntimeLifecycleError("runtime has already been started")
        if context.role_id is None:
            raise RuntimeProtocolError("demo runtime requires a seat role")
        self._context = context
        self._session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self._token}"})
        try:
            await self._read_document("/v1/board/current")
            await self._read_document(f"/v1/role/{context.role_id}")
        except BaseException:
            await self._close_http_session()
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
            raise RuntimeProtocolError(
                "turn request does not match the runtime's game or session epoch"
            )
        if self._active_request_id is not None:
            raise RuntimeLifecycleError("runtime already has an active turn")
        self._active_request_id = request.request_id
        self._last_request_id = request.request_id
        self._requests.append(request)
        try:
            if request.expected_kind.value == "ready":
                response: TurnResponse = ReadyResponse(
                    request_id=request.request_id,
                    ready=Ready(knowledge_receipts=list(self._receipts)),
                )
            elif request.expected_kind.value == "speech":
                response = SpeechResponse(
                    request_id=request.request_id,
                    speech=Speech(text=self._speech_for(request)),
                )
            elif request.expected_kind.value == "action":
                # This private query is intentionally performed for every
                # action turn.  It lets a harness seat observe only its own
                # current grants and resource counters before choosing.
                skill_status = await self._read_skill_status()
                response = self._action_for(request, skill_status)
            else:
                response = validate_turn_response(
                    {
                        "schema_version": 1,
                        "request_id": request.request_id,
                        "kind": "error",
                        "error": {
                            "code": "DEMO_UNSUPPORTED_KIND",
                            "message": "demo runtime does not support this turn kind",
                            "retryable": False,
                        },
                    }
                )
            self._validate_response(request, response)
            return RuntimeTurnResult(
                request_id=request.request_id,
                logical_request_id=request.logical_request_id,
                attempt_no=request.attempt_no,
                response=response,
                elapsed_ms=0,
            )
        finally:
            self._active_request_id = None

    async def steer(self, request_id: str, message: str) -> None:
        self._require_open()
        self._require_active(request_id)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("steer message must not be blank")
        self._steers.append((request_id, message))

    async def abort(self, request_id: str) -> None:
        self._require_open()
        if not isinstance(request_id, str) or request_id != self._last_request_id:
            raise RuntimeRequestMismatchError("request_id is not the active or recent turn")
        self._aborts.append(request_id)
        self._last_request_id = None

    async def close(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("close reason must not be blank")
        if self._active_request_id is not None:
            raise RuntimeLifecycleError("cannot close while a turn is active")
        self._closed = True
        await self._close_http_session()

    def get_session_ref(self) -> RuntimeRef:
        if self._session_ref is None:
            raise RuntimeLifecycleError("runtime has not been started")
        return self._session_ref

    @property
    def requests(self) -> tuple[TurnRequest, ...]:
        return tuple(self._requests)

    @property
    def receipts(self) -> tuple[str, ...]:
        return tuple(self._receipts)

    @property
    def steers(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._steers)

    @property
    def aborts(self) -> tuple[str, ...]:
        return tuple(self._aborts)

    async def _read_document(self, path: str) -> Mapping[str, Any]:
        payload = await self._get_json(path)
        receipt = payload.get("receipt_id")
        if not isinstance(receipt, str) or not receipt.strip():
            raise RuntimeProtocolError("knowledge response did not contain a receipt")
        self._receipts.append(receipt)
        return payload

    async def _read_skill_status(self) -> Mapping[str, Any]:
        payload = await self._get_json("/v1/game/skills/me")
        status = payload.get("skill_status")
        if not isinstance(status, Mapping):
            raise RuntimeProtocolError("skill status response was malformed")
        return status

    async def _get_json(self, path: str) -> Mapping[str, Any]:
        session = self._session
        if session is None or session.closed:
            raise RuntimeLifecycleError("demo runtime HTTP session is not open")
        try:
            async with session.get(f"{self._base_url}{path}") as response:
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as exc:
            raise RuntimeProtocolError("knowledge gateway request failed") from exc
        if response.status != 200 or not isinstance(payload, Mapping):
            raise RuntimeProtocolError("knowledge gateway rejected the demo runtime request")
        return payload

    async def _close_http_session(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    @staticmethod
    def _speech_for(request: TurnRequest) -> str:
        phase = request.phase.value
        return f"Demo seat observes the public {phase} information and states a deterministic view."

    def _action_for(self, request: TurnRequest, status: Mapping[str, Any]) -> ActionResponse:
        try:
            return build_deterministic_action_response(request, status)
        except ValueError as exc:
            raise RuntimeProtocolError(str(exc)) from exc

    @staticmethod
    def _validate_response(request: TurnRequest, response: TurnResponse) -> None:
        if response.request_id != request.request_id:
            raise RuntimeRequestMismatchError("response request_id does not match the active turn")
        if response.kind != request.expected_kind.value:
            raise RuntimeProtocolError(
                f"response kind {response.kind!r} does not match expected kind "
                f"{request.expected_kind.value!r}"
            )
        if request.action_window is None or not isinstance(response, ActionResponse):
            return
        window = request.action_window
        if not window.min_actions <= len(response.actions) <= window.max_actions:
            raise RuntimeProtocolError("action count is outside the action window")
        allowed = set(window.allowed_action_codes)
        for action in response.actions:
            if action.action_code not in allowed:
                raise RuntimeProtocolError("action code is outside the action window")
            if action.action_code == 299 and not window.allow_pass:
                raise RuntimeProtocolError("PASS is not allowed by the action window")

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
        if self._active_request_id != request_id:
            raise RuntimeRequestMismatchError("request_id is not the active turn")


__all__ = ["DemoRuntime"]
