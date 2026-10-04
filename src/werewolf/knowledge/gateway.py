"""Local HTTP transport for the snapshot-bound knowledge service.

The gateway is the trust boundary between a Pi process and
``KnowledgeService``.  A client receives an opaque bearer token when a
session is created; all query context is reconstructed from that token.  The
HTTP layer deliberately has no filesystem access and never accepts a game,
snapshot, or path from a query body.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from aiohttp import web
from pydantic import ValidationError

from .service import (
    KnowledgeInteractionNotFoundError,
    KnowledgeResult,
    KnowledgeSearchResult,
    KnowledgeService,
    KnowledgeServiceError,
    QueryContext,
    SearchQuery,
)
from .skill_status import SkillStatusSessionMismatch, project_skill_status

GATEWAY_SCHEMA_VERSION = 1
DEFAULT_TOKEN_TTL = timedelta(hours=1)
MAX_REQUEST_BYTES = 64 * 1024
MAX_TOKEN_LENGTH = 512
INTERACTION_QUERY_REQUEST_KEY: web.RequestKey[dict[str, object]] = web.RequestKey(
    "werewolf.interaction_query"
)

ReceiptSink = Callable[["KnowledgeReceipt"], Awaitable[None]]
SkillStatusProvider = Callable[[], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class KnowledgeTokenBinding:
    """Server-side context associated with one opaque bearer token."""

    game_id: str
    snapshot_id: str
    seat: int
    session_epoch: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class KnowledgeReceipt:
    """Audit record emitted only after a query has succeeded."""

    receipt_id: str
    game_id: str
    snapshot_id: str
    seat: int
    session_epoch: int
    tool: str
    canonical_ref: str
    result_id: str
    created_at: datetime


class KnowledgeGateway:
    """Serve one immutable ``KnowledgeService`` over loopback HTTP.

    ``KnowledgeGateway`` does not load a Vault or package itself.  The caller
    must provide a service that has already been validated against the active
    snapshot.  ``create_app`` is suitable for aiohttp's ``TestServer`` and
    ``start`` binds only to ``127.0.0.1`` for local runtime use.
    """

    def __init__(
        self,
        service: KnowledgeService,
        *,
        receipt_sink: ReceiptSink | None = None,
        token_ttl: timedelta = DEFAULT_TOKEN_TTL,
        clock: Callable[[], datetime] | None = None,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        state_provider: SkillStatusProvider | None = None,
        execution_package: object | None = None,
        action_registry: object | None = None,
    ) -> None:
        if not isinstance(service, KnowledgeService):
            raise TypeError("service must be a KnowledgeService")
        if token_ttl <= timedelta(0):
            raise ValueError("token_ttl must be positive")
        if max_request_bytes < 1024 or max_request_bytes > MAX_REQUEST_BYTES:
            raise ValueError(f"max_request_bytes must be between 1024 and {MAX_REQUEST_BYTES}")
        self._service = service
        self._receipt_sink = receipt_sink or _discard_receipt
        self._token_ttl = token_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_request_bytes = max_request_bytes
        self._state_provider = state_provider
        self._execution_package = execution_package
        self._action_registry = action_registry
        self._tokens: dict[str, KnowledgeTokenBinding] = {}
        self._runner: web.AppRunner | None = None
        self.app = self.create_app()

    @property
    def service(self) -> KnowledgeService:
        return self._service

    def issue_token(
        self,
        *,
        game_id: str,
        snapshot_id: str,
        seat: int,
        session_epoch: int,
        expires_at: datetime | None = None,
    ) -> str:
        """Issue a high-entropy opaque token for one Pi session."""

        _validate_binding_values(game_id, snapshot_id, seat, session_epoch)
        now = _utc(self._clock())
        expiry = _utc(expires_at) if expires_at is not None else now + self._token_ttl
        if expiry <= now:
            raise ValueError("expires_at must be in the future")
        token = secrets.token_urlsafe(32)
        self._tokens[token] = KnowledgeTokenBinding(
            game_id=game_id,
            snapshot_id=snapshot_id,
            seat=seat,
            session_epoch=session_epoch,
            expires_at=expiry,
        )
        return token

    def rotate_token(self, token: str, *, expires_at: datetime | None = None) -> str:
        """Revoke ``token`` and issue a fresh token with the same binding."""

        binding = self._tokens.get(token)
        if binding is None or not self._is_live(binding):
            self.revoke_token(token)
            raise ValueError("token is invalid or expired")
        self.revoke_token(token)
        return self.issue_token(
            game_id=binding.game_id,
            snapshot_id=binding.snapshot_id,
            seat=binding.seat,
            session_epoch=binding.session_epoch,
            expires_at=expires_at,
        )

    def revoke_token(self, token: str) -> bool:
        """Invalidate a token immediately; return whether it was registered."""

        return self._tokens.pop(token, None) is not None

    def create_app(self) -> web.Application:
        """Build the aiohttp application used by this gateway."""

        app = web.Application(
            client_max_size=self._max_request_bytes,
            middlewares=[self._error_middleware],
        )
        app.router.add_get("/v1/health", self._health)
        app.router.add_get("/v1/game/skills/me", self._get_skill_status)
        app.router.add_get("/v1/board/{id}", self._get_board)
        app.router.add_get("/v1/role/{id}", self._get_role)
        app.router.add_get("/v1/mechanic/{id}", self._get_mechanic)
        app.router.add_get("/v1/topic/{id}", self._get_topic)
        app.router.add_post("/v1/interactions/query", self._query_interaction)
        app.router.add_post("/v1/search", self._search)
        return app

    async def start(self, *, port: int = 0) -> web.TCPSite:
        """Start a loopback-only server, primarily for local runtime use."""

        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if self._runner is not None:
            raise RuntimeError("gateway is already running")
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        self._runner = runner
        return site

    async def close(self) -> None:
        """Stop a server started with ``start``."""

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _error_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except _HTTPGatewayError as exc:
            return _error_response(exc.code, exc.message, exc.status)
        except KnowledgeServiceError as exc:
            return _service_error_response(exc)
        except ValidationError:
            return _error_response("INVALID_QUERY", "invalid knowledge query", 400)
        except web.HTTPException as exc:
            # aiohttp can reject an oversized body before the route handler
            # runs.  Keep that parser-level failure in the same structured
            # JSON envelope as all application errors.
            too_large = exc.status == 413 or (
                request.content_length is not None
                and request.content_length > self._max_request_bytes
            )
            return _error_response(
                "REQUEST_TOO_LARGE" if too_large else "HTTP_ERROR",
                "request body is too large" if too_large else "knowledge gateway request failed",
                413 if too_large else exc.status,
            )
        except Exception:
            # Do not expose service, token, filesystem, or exception details
            # through a transport error.
            return _error_response("INTERNAL_ERROR", "knowledge gateway request failed", 500)

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"schema_version": GATEWAY_SCHEMA_VERSION, "status": "ok"})

    async def _get_skill_status(self, request: web.Request) -> web.Response:
        context = self._context(request)
        provider = self._state_provider
        if provider is None:
            raise _HTTPGatewayError(
                "SKILL_STATUS_UNAVAILABLE",
                "skill status is unavailable before player sessions start",
                503,
            )
        try:
            state = await provider()
        except Exception as exc:
            raise _HTTPGatewayError(
                "SKILL_STATUS_UNAVAILABLE",
                "skill status is temporarily unavailable",
                503,
            ) from exc
        try:
            projection = project_skill_status(
                state,
                game_id=context.game_id,
                snapshot_id=context.snapshot_id,
                seat=context.seat,
                session_epoch=context.session_epoch,
                execution_package=self._execution_package,
                action_registry=self._action_registry,
            )
        except SkillStatusSessionMismatch as exc:
            raise _HTTPGatewayError(
                "SESSION_MISMATCH",
                "player session is no longer current",
                409,
            ) from exc
        return web.json_response(
            {
                "schema_version": GATEWAY_SCHEMA_VERSION,
                "status": "ok",
                "skill_status": projection,
            }
        )

    async def _get_board(self, request: web.Request) -> web.Response:
        context = self._context(request)
        identifier = _path_id(request, "id")
        # ``current`` is a safe alias resolved only through the bearer-bound
        # context.  Explicit IDs remain restricted to this package's current
        # board reference; a mismatching ID is a normal not-found response.
        board_id = self._service.board_ref.split("@", 1)[0]
        result = (
            self._service.get_board(context)
            if identifier in {"current", board_id, self._service.board_ref}
            else None
        )
        if result is None:
            return _error_response("NOT_FOUND", "knowledge document not found", 404)
        return await self._success(request, "get_board", self._ref(result), result)

    async def _get_role(self, request: web.Request) -> web.Response:
        context = self._context(request)
        identifier = _path_id(request, "id")
        result = self._service.get_role(context, identifier)
        return await self._success(request, "get_role", self._ref(result), result)

    async def _get_mechanic(self, request: web.Request) -> web.Response:
        context = self._context(request)
        result = self._service.get_mechanic(context, _path_id(request, "id"))
        return await self._success(request, "get_mechanic", self._ref(result), result)

    async def _get_topic(self, request: web.Request) -> web.Response:
        context = self._context(request)
        result = self._service.get_rule_topic(context, _path_id(request, "id"))
        return await self._success(request, "get_rule_topic", self._ref(result), result)

    async def _query_interaction(self, request: web.Request) -> web.Response:
        context = self._context(request)
        body = await self._json_body(request)
        if set(body) - {"subjects", "situation"}:
            return _error_response("INVALID_QUERY", "invalid interaction query", 400)
        subjects = body.get("subjects")
        situation = body.get("situation")
        if not isinstance(subjects, list) or any(not isinstance(item, str) for item in subjects):
            return _error_response("INVALID_QUERY", "invalid interaction query", 400)
        if situation is not None and not isinstance(situation, str):
            return _error_response("INVALID_QUERY", "invalid interaction query", 400)
        # Keep a non-secret, in-process diagnostic for the smoke harness.  It
        # is never serialized into the response and contains only the public
        # logical keys already supplied by the caller.
        request[INTERACTION_QUERY_REQUEST_KEY] = {  # type: ignore[misc]
            "subjects": tuple(subjects),
            "situation": situation,
        }
        result = self._service.get_interaction(context, tuple(subjects), situation)
        return await self._success(request, "get_interaction", self._ref(result), result)

    async def _search(self, request: web.Request) -> web.Response:
        context = self._context(request)
        body = await self._json_body(request)
        # JSON has arrays while the strict service model uses an immutable
        # tuple.  Convert only this transport representation and preserve
        # strict validation for every other field.
        if isinstance(body.get("kinds"), list):
            body = {**body, "kinds": tuple(body["kinds"])}
        try:
            query = SearchQuery.model_validate(body)
        except ValidationError:
            return _error_response("INVALID_QUERY", "invalid search query", 400)
        result = self._service.search_rules(context, query)
        return await self._success(request, "search_rules", "search", result)

    def _context(self, request: web.Request) -> QueryContext:
        authorization = request.headers.get("Authorization", "")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > MAX_TOKEN_LENGTH:
            raise _HTTPGatewayError("UNAUTHORIZED", "authentication required", 401)
        token = parts[1]
        binding = self._tokens.get(token)
        if binding is None or not self._is_live(binding):
            self.revoke_token(token)
            raise _HTTPGatewayError("UNAUTHORIZED", "authentication required", 401)
        return QueryContext(
            game_id=binding.game_id,
            snapshot_id=binding.snapshot_id,
            seat=binding.seat,
            session_epoch=binding.session_epoch,
        )

    def _is_live(self, binding: KnowledgeTokenBinding) -> bool:
        return _utc(self._clock()) < binding.expires_at

    async def _json_body(self, request: web.Request) -> dict[str, Any]:
        try:
            raw = await request.content.read(self._max_request_bytes + 1)
        except Exception as exc:
            raise _HTTPGatewayError("INVALID_QUERY", "invalid JSON request", 400) from exc
        if len(raw) > self._max_request_bytes:
            raise _HTTPGatewayError("REQUEST_TOO_LARGE", "request body is too large", 413)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise _HTTPGatewayError("INVALID_QUERY", "invalid JSON request", 400) from exc
        if not isinstance(value, dict):
            raise _HTTPGatewayError("INVALID_QUERY", "JSON body must be an object", 400)
        return cast(dict[str, Any], value)

    async def _success(
        self,
        request: web.Request,
        tool: str,
        canonical_ref: str,
        result: KnowledgeResult | KnowledgeSearchResult,
    ) -> web.Response:
        binding = self._binding_for_request(request)
        receipt = KnowledgeReceipt(
            receipt_id=f"receipt_{secrets.token_urlsafe(24)}",
            game_id=binding.game_id,
            snapshot_id=binding.snapshot_id,
            seat=binding.seat,
            session_epoch=binding.session_epoch,
            tool=tool,
            canonical_ref=canonical_ref,
            result_id=result.result_id,
            created_at=_utc(self._clock()),
        )
        await self._receipt_sink(receipt)
        response = result.model_copy(update={"receipt_id": receipt.receipt_id})
        return web.json_response(response.model_dump(mode="json"))

    def _binding_for_request(self, request: web.Request) -> KnowledgeTokenBinding:
        authorization = request.headers.get("Authorization", "")
        token = authorization.split()[1] if len(authorization.split()) == 2 else ""
        binding = self._tokens.get(token)
        if binding is None or not self._is_live(binding):
            raise _HTTPGatewayError("UNAUTHORIZED", "authentication required", 401)
        return binding

    @staticmethod
    def _ref(result: KnowledgeResult) -> str:
        document = result.document
        return f"{document.kind}:{document.id}@{document.version}"


class _HTTPGatewayError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def _error_response(
    code: str,
    message: str,
    status: int,
    *,
    details: Mapping[str, object] | None = None,
) -> web.Response:
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = dict(details)
    return web.json_response(
        {
            "schema_version": GATEWAY_SCHEMA_VERSION,
            "status": "error",
            "error": error,
        },
        status=status,
    )


def _service_error_response(error: KnowledgeServiceError) -> web.Response:
    status = {
        "NOT_FOUND": 404,
        "AMBIGUOUS": 409,
        "INVALID_QUERY": 400,
        "FORBIDDEN": 403,
        "KNOWLEDGE_INTEGRITY_ERROR": 500,
    }.get(error.code, 500)
    messages = {
        "NOT_FOUND": "knowledge document not found",
        "AMBIGUOUS": "knowledge reference is ambiguous",
        "INVALID_QUERY": "invalid knowledge query",
        "FORBIDDEN": "knowledge query is not allowed",
        "KNOWLEDGE_INTEGRITY_ERROR": "knowledge package integrity check failed",
    }
    details: dict[str, object] | None = None
    message = messages.get(error.code, "knowledge query failed")
    if isinstance(error, KnowledgeInteractionNotFoundError):
        message = error.message
        details = {
            "next_tool": "search_rules",
            "next_tool_args": {"kinds": ["interaction"], "limit": 4},
            "message": (
                "interaction lookup is exact; search current interaction rules "
                "to discover the exact subjects and situation_key before retrying"
            ),
            "candidates": [
                {
                    "ref": suggestion.canonical_ref,
                    "subjects": list(suggestion.subjects),
                    "situation_key": suggestion.situation_key,
                }
                for suggestion in error.suggestions
            ],
        }
    return _error_response(error.code, message, status, details=details)


def _path_id(request: web.Request, name: str) -> str:
    value = request.match_info.get(name, "")
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise _HTTPGatewayError("INVALID_QUERY", "invalid knowledge reference", 400)
    return value


def _validate_binding_values(game_id: str, snapshot_id: str, seat: int, session_epoch: int) -> None:
    if not isinstance(game_id, str) or not game_id or len(game_id) > 256:
        raise ValueError("game_id must be a non-empty string")
    if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 256:
        raise ValueError("snapshot_id must be a non-empty string")
    if isinstance(seat, bool) or not isinstance(seat, int) or seat < 0:
        raise ValueError("seat must be a non-negative integer")
    if isinstance(session_epoch, bool) or not isinstance(session_epoch, int) or session_epoch < 0:
        raise ValueError("session_epoch must be a non-negative integer")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock and expires_at must return datetime values")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _discard_receipt(_receipt: KnowledgeReceipt) -> None:
    return None


__all__ = [
    "DEFAULT_TOKEN_TTL",
    "GATEWAY_SCHEMA_VERSION",
    "KnowledgeGateway",
    "KnowledgeReceipt",
    "KnowledgeTokenBinding",
    "MAX_REQUEST_BYTES",
    "INTERACTION_QUERY_REQUEST_KEY",
    "SkillStatusProvider",
]
