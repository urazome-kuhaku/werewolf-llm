"""Deterministic in-process PlayerRuntime used by game-flow tests.

``ScriptedRuntime`` follows the same request/response boundary as the future
Pi adapter.  A caller may provide a sequence of JSON-like responses (or a
response factory), allowing tests to exercise several prepare, speech, and
vote turns without starting a subprocess.  Invalid scripted data remains
invalid: it is reported at the runtime boundary instead of being silently
repaired.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable

from pydantic import ValidationError

from .player_runtime import (
    Action,
    ActionResponse,
    InitialContext,
    Ready,
    ReadyResponse,
    ResponseError,
    ResponseKind,
    RuntimeConfig,
    RuntimeLifecycleError,
    RuntimeProtocolError,
    RuntimeRef,
    RuntimeRequestMismatchError,
    RuntimeResponseValidationError,
    RuntimeTurnResult,
    Speech,
    SpeechResponse,
    TurnRequest,
    TurnResponse,
    validate_turn_response,
)

ScriptedValue = object
ResponseFactory = Callable[[TurnRequest], object]


class ScriptedRuntime:
    """A small deterministic runtime with one independent session.

    ``script`` is consumed in order.  Each item may be a validated response,
    a JSON-like mapping, or a callable receiving the current request.  When
    the script is exhausted a deterministic response is generated from
    ``expected_kind``; this makes a multi-round smoke game concise while still
    allowing malformed-response and stale-request tests to provide explicit
    script items.
    """

    def __init__(self, script: Iterable[ScriptedValue] = ()) -> None:
        self._script = deque(script)
        self._session_ref: RuntimeRef | None = None
        self._context: InitialContext | None = None
        self._closed = False
        self._active_request_id: str | None = None
        self._requests: list[TurnRequest] = []
        self._steers: list[tuple[str, str]] = []
        self._aborts: list[str] = []

    async def start(self, config: RuntimeConfig, context: InitialContext) -> RuntimeRef:
        """Bind the runtime to one game, seat, and session epoch."""

        if self._session_ref is not None:
            raise RuntimeLifecycleError("runtime has already been started")
        self._context = context
        self._session_ref = RuntimeRef(
            session_id=config.session_id,
            game_id=context.game_id,
            seat=context.seat,
            session_epoch=context.session_epoch,
        )
        return self._session_ref

    async def run_turn(self, request: TurnRequest) -> RuntimeTurnResult:
        """Return the next scripted response after enforcing request binding."""

        self._require_open()
        context = self._require_context()
        if request.game_id != context.game_id or request.session_epoch != context.session_epoch:
            raise RuntimeProtocolError(
                "turn request does not match the runtime's game or session epoch"
            )
        if self._active_request_id is not None:
            raise RuntimeLifecycleError("runtime already has an active turn")

        self._active_request_id = request.request_id
        self._requests.append(request)
        try:
            raw = self._script.popleft() if self._script else self._default_response(request)
            if callable(raw):
                raw = raw(request)
            try:
                response = self._parse_response(raw)
            except (TypeError, ValidationError, ValueError) as exc:
                raise RuntimeResponseValidationError(
                    f"invalid scripted response for request {request.request_id}"
                ) from exc
            if response.request_id != request.request_id:
                raise RuntimeRequestMismatchError(
                    "response request_id does not match the active turn"
                )
            if response.kind != request.expected_kind.value:
                raise RuntimeProtocolError(
                    f"response kind {response.kind!r} does not match expected "
                    f"kind {request.expected_kind.value!r}"
                )
            self._validate_action_window(request, response)
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
        """Record a steer only while the supplied request is active."""

        self._require_open()
        self._require_active(request_id)
        if not isinstance(message, str) or not message.strip():
            raise ValueError("steer message must not be blank")
        self._steers.append((request_id, message))

    async def abort(self, request_id: str) -> None:
        """Record an abort only for the active request."""

        self._require_open()
        self._require_active(request_id)
        self._aborts.append(request_id)

    async def close(self, reason: str) -> None:
        """Close the session; repeated close calls are harmless."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("close reason must not be blank")
        if self._active_request_id is not None:
            raise RuntimeLifecycleError("cannot close while a turn is active")
        self._closed = True

    def get_session_ref(self) -> RuntimeRef:
        """Return the immutable session identity after ``start``."""

        if self._session_ref is None:
            raise RuntimeLifecycleError("runtime has not been started")
        return self._session_ref

    @property
    def requests(self) -> tuple[TurnRequest, ...]:
        """Requests accepted by this runtime, in deterministic order."""

        return tuple(self._requests)

    @property
    def steers(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._steers)

    @property
    def aborts(self) -> tuple[str, ...]:
        return tuple(self._aborts)

    @property
    def remaining_script_items(self) -> int:
        return len(self._script)

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

    @staticmethod
    def _parse_response(raw: object) -> TurnResponse:
        if isinstance(raw, (SpeechResponse, ActionResponse, ReadyResponse)):
            return raw
        # ErrorResponse is deliberately included through the adapter below;
        # keeping the branch narrow avoids importing a second union alias.
        return validate_turn_response(raw)

    @staticmethod
    def _validate_action_window(request: TurnRequest, response: TurnResponse) -> None:
        if request.action_window is None or not isinstance(response, ActionResponse):
            return
        count = len(response.actions)
        window = request.action_window
        if not window.min_actions <= count <= window.max_actions:
            raise RuntimeProtocolError(
                f"action count {count} is outside window bounds "
                f"[{window.min_actions}, {window.max_actions}]"
            )
        allowed = set(window.allowed_action_codes)
        for action in response.actions:
            if action.action_code not in allowed:
                raise RuntimeProtocolError(
                    f"action code {action.action_code} is not allowed by the window"
                )
            if action.action_code == 299 and not window.allow_pass:
                raise RuntimeProtocolError("PASS is not allowed by the action window")

    @staticmethod
    def _default_response(request: TurnRequest) -> TurnResponse:
        """Build a stable response for a script-free smoke game."""

        request_id = request.request_id
        if request.expected_kind is ResponseKind.READY:
            return ReadyResponse(
                request_id=request_id,
                ready=Ready(knowledge_receipts=["board", "role"]),
            )
        if request.expected_kind is ResponseKind.SPEECH:
            return SpeechResponse(
                request_id=request_id,
                speech=Speech(text=f"scripted speech for {request.logical_request_id}"),
            )
        if request.expected_kind is ResponseKind.ACTION:
            action_code = 299
            targets: list[int] = []
            if request.action_window is not None:
                window = request.action_window
                if action_code not in window.allowed_action_codes or not window.allow_pass:
                    action_code = window.allowed_action_codes[0]
                    if window.candidate_seats:
                        targets = [window.candidate_seats[0]]
            return ActionResponse(
                request_id=request_id,
                actions=[Action(action_code=action_code, targets=targets)],
            )
        return validate_turn_response(
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": "error",
                "error": ResponseError(
                    code="SCRIPTED_ERROR",
                    message="scripted runtime emitted an error response",
                    retryable=False,
                ).model_dump(mode="python"),
            }
        )


__all__ = ["ScriptedRuntime"]
