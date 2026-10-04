"""Safe lifecycle boundary for the player runtimes of one game.

The moderator owns this object.  It starts one loopback knowledge gateway,
issues one seat-bound bearer token, composes one seat-bound startup context,
and starts one independent ``PlayerRuntime`` per assigned seat.  The gateway
and every runtime are treated as one resource group: a partial start is never
left running and normal close always follows runtime -> token -> gateway.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias

from werewolf.game.setup import PlayerAssignmentPlan
from werewolf.knowledge.gateway import (
    KnowledgeGateway,
    KnowledgeReceipt,
    ReceiptSink,
    SkillStatusProvider,
)
from werewolf.knowledge.runtime_loader import RuntimeKnowledgeBundle
from werewolf.knowledge.service import QueryContext
from werewolf.moderator.config import PlayerConfiguration, PlayerSessionConfig
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.knowledge_bootstrap import (
    KnowledgeBootstrapCard,
    build_knowledge_bootstrap_card,
)
from werewolf.runtime.pi_runtime import PiRuntime
from werewolf.runtime.player_runtime import (
    InitialContext,
    PlayerRuntime,
    RuntimeRef,
)
from werewolf.runtime.prompt_composer import (
    DEFAULT_READING_SKILL_PATH,
    compose_system_prompt,
)

# This is a review pin for the checked-in frozen reading protocol.  A caller
# deploying a separately reviewed prompt must pass its own digest explicitly.
DEFAULT_READING_SKILL_SHA256 = "9c4e599881bdd694fa52e62b84f043c6f3ea0cab2b47c7a56a984eec60707a64"

RuntimeFactory: TypeAlias = Callable[
    [PlayerSessionConfig, str, str], PlayerRuntime | Awaitable[PlayerRuntime]
]


class PlayerSessionError(RuntimeError):
    """Raised when a player session group cannot be started or closed safely."""


@dataclass(slots=True)
class PlayerSessionRecord:
    """Private host-side record for one started seat.

    ``token`` is deliberately excluded from repr.  Callers should pass the
    record's runtime to the scheduler, while treating the token as a secret
    implementation detail of the Pi process environment.
    """

    seat: int
    runtime: PlayerRuntime
    runtime_ref: RuntimeRef
    context: InitialContext
    bootstrap_card: KnowledgeBootstrapCard
    gateway_url: str
    token: str = field(repr=False)


class PlayerSessionService:
    """Start and stop all seat-scoped player runtimes for one game."""

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactory | None = None,
        reading_skill_sha256: str = DEFAULT_READING_SKILL_SHA256,
        reading_skill_path: str | Path = DEFAULT_READING_SKILL_PATH,
        receipt_sink: ReceiptSink | None = None,
        token_ttl: timedelta | None = None,
        state_provider: SkillStatusProvider | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._reading_skill_sha256 = reading_skill_sha256
        self._reading_skill_path = reading_skill_path
        self._receipt_sink = receipt_sink
        self._token_ttl = token_ttl
        self._state_provider = state_provider
        self._gateway: KnowledgeGateway | None = None
        self._gateway_url: str | None = None
        self._records: dict[int, PlayerSessionRecord] = {}
        # Receipts are retained by the moderator for the lifetime of this
        # session group.  The gateway still owns receipt creation; keeping a
        # seat index here lets the prepare flow validate a model's claimed
        # IDs without trusting model text or an unbound external sink.
        self._receipts: dict[int, list[KnowledgeReceipt]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def gateway(self) -> KnowledgeGateway:
        gateway = self._gateway
        if gateway is None:
            raise PlayerSessionError("player sessions have not been started")
        return gateway

    @property
    def gateway_url(self) -> str:
        url = self._gateway_url
        if url is None:
            raise PlayerSessionError("player sessions have not been started")
        return url

    @property
    def records(self) -> Mapping[int, PlayerSessionRecord]:
        """Return seat records without exposing a mutable registry."""

        return MappingProxyType(dict(self._records))

    @property
    def runtimes(self) -> Mapping[int, PlayerRuntime]:
        """Return the seat-to-runtime mapping used by the game scheduler."""

        return MappingProxyType({seat: record.runtime for seat, record in self._records.items()})

    def receipts_for_seat(self, seat: int) -> tuple[KnowledgeReceipt, ...]:
        """Return successful gateway receipts captured for one current seat."""

        if seat not in self._records:
            raise PlayerSessionError(f"player seat {seat} has not been started")
        return tuple(self._receipts.get(seat, ()))

    @property
    def receipts(self) -> Mapping[int, tuple[KnowledgeReceipt, ...]]:
        """Return the captured gateway receipts without exposing mutable lists."""

        return MappingProxyType(
            {seat: tuple(receipts) for seat, receipts in self._receipts.items()}
        )

    async def start(
        self,
        bundle: RuntimeKnowledgeBundle,
        configuration: PlayerConfiguration,
        assignments: PlayerAssignmentPlan,
    ) -> Mapping[int, PlayerSessionRecord]:
        """Start one gateway and one runtime per assigned seat.

        The three inputs must already have passed their respective trusted
        validation boundaries.  This method still checks their cross-object
        identities before issuing any token, because a mismatch must never
        produce a partially bound runtime.
        """

        async with self._lock:
            if self._gateway is not None or self._records:
                raise PlayerSessionError("player sessions are already running")
            if self._closed:
                raise PlayerSessionError("player session service has been closed")
            self._validate_inputs(bundle, configuration, assignments)

            self._receipts.clear()

            async def capture_receipt(receipt: KnowledgeReceipt) -> None:
                self._receipts.setdefault(receipt.seat, []).append(receipt)
                if self._receipt_sink is not None:
                    await self._receipt_sink(receipt)

            gateway = KnowledgeGateway(
                bundle.service,
                receipt_sink=capture_receipt,
                token_ttl=self._token_ttl or timedelta(hours=1),
                state_provider=self._state_provider,
                execution_package=getattr(bundle.package, "execution", None),
                action_registry=getattr(bundle.package, "action_registry", None),
            )
            issued_tokens: dict[int, str] = {}
            started: list[PlayerSessionRecord] = []
            try:
                site = await gateway.start(port=0)
                gateway_url = _site_url(site)
                for player in configuration.players:
                    assignment = assignments.players[player.seat]
                    context = _context_for(
                        configuration,
                        bundle,
                        player,
                        assignment.role_id,
                        assignment.faction_id,
                        assignment.session_epoch,
                    )
                    token = gateway.issue_token(
                        game_id=context.game_id,
                        snapshot_id=context_snapshot_id(context, bundle),
                        seat=context.seat,
                        session_epoch=context.session_epoch,
                    )
                    issued_tokens[player.seat] = token
                    card = build_knowledge_bootstrap_card(
                        bundle.service, _query_context(context, bundle), assignment.role_id
                    )
                    prompt = compose_system_prompt(
                        card,
                        self._reading_skill_sha256,
                        faction_id=assignment.faction_id,
                        reading_skill_path=self._reading_skill_path,
                    )
                    runtime = await self._create_runtime(player, gateway_url, token)
                    if not isinstance(runtime, PlayerRuntime):
                        raise PlayerSessionError(
                            f"runtime factory returned an invalid runtime for seat {player.seat}"
                        )
                    startup_context = context.model_copy(update={"system_prompt": prompt})
                    try:
                        runtime_ref = await runtime.start(
                            player.to_runtime_config(),
                            startup_context,
                        )
                        _validate_runtime_ref(runtime_ref, context, player)
                    except BaseException:
                        # A runtime may have spawned its process before its
                        # handshake or reference validation failed.  It is
                        # not yet in ``started`` so close it immediately.
                        try:
                            await runtime.close("startup failed")
                        except BaseException:
                            pass
                        raise
                    started.append(
                        PlayerSessionRecord(
                            seat=player.seat,
                            runtime=runtime,
                            runtime_ref=runtime_ref,
                            context=startup_context,
                            bootstrap_card=card,
                            gateway_url=gateway_url,
                            token=token,
                        )
                    )
            except BaseException:
                await self._cleanup(gateway, started, issued_tokens)
                raise

            self._gateway = gateway
            self._gateway_url = gateway_url
            self._records = {record.seat: record for record in started}
            return self.records

    # ``launch`` is an explicit synonym for callers that use launcher wording.
    launch = start

    async def close(self, reason: str = "moderator shutdown") -> None:
        """Close all runtimes, revoke all tokens, and stop the gateway."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("close reason must not be blank")
        async with self._lock:
            gateway = self._gateway
            records = tuple(self._records.values())
            if gateway is None and not records:
                self._closed = True
                return
            tokens = {record.seat: record.token for record in records}
            try:
                await self._cleanup(gateway, records, tokens, reason=reason)
            finally:
                # The resource group is consumed even when one component's
                # close reports an error.  This prevents a second close from
                # releasing already-revoked tokens or reusing stale records.
                self._gateway = None
                self._gateway_url = None
                self._records.clear()
                self._receipts.clear()
                self._closed = True

    async def _create_runtime(
        self,
        player: PlayerSessionConfig,
        gateway_url: str,
        token: str,
    ) -> PlayerRuntime:
        if self._runtime_factory is None:
            if player.runtime == "scripted":
                return DemoRuntime(gateway_url, token)
            runtime_config = player.to_runtime_config()
            return PiRuntime(
                knowledge_base_url=f"{gateway_url}/v1",
                knowledge_token=token,
                executable=runtime_config.executable,
                compatible_version=runtime_config.compatible_version,
                auto_compaction=runtime_config.auto_compaction,
                auto_retry=runtime_config.auto_retry,
            )
        value = self._runtime_factory(player, gateway_url, token)
        return await value if inspect.isawaitable(value) else value

    async def _cleanup(
        self,
        gateway: KnowledgeGateway | None,
        records: tuple[PlayerSessionRecord, ...] | list[PlayerSessionRecord],
        tokens: Mapping[int, str],
        *,
        reason: str = "startup failed",
    ) -> None:
        first_error: BaseException | None = None
        for record in reversed(tuple(records)):
            try:
                await record.runtime.close(reason)
            except BaseException as exc:  # cleanup must continue for every seat
                if first_error is None:
                    first_error = exc
        if gateway is not None:
            for token in tokens.values():
                gateway.revoke_token(token)
            try:
                await gateway.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None and reason != "startup failed":
            raise first_error

    @staticmethod
    def _validate_inputs(
        bundle: RuntimeKnowledgeBundle,
        configuration: PlayerConfiguration,
        assignments: PlayerAssignmentPlan,
    ) -> None:
        if not isinstance(bundle, RuntimeKnowledgeBundle):
            raise TypeError("bundle must be a RuntimeKnowledgeBundle")
        if not isinstance(configuration, PlayerConfiguration):
            raise TypeError("configuration must be a PlayerConfiguration")
        if not isinstance(assignments, PlayerAssignmentPlan):
            raise TypeError("assignments must be a PlayerAssignmentPlan")
        if assignments.board_ref != bundle.board.board_ref:
            raise PlayerSessionError("assignment plan board does not match runtime knowledge board")
        seats = tuple(player.seat for player in configuration.players)
        if seats != assignments.seats:
            raise PlayerSessionError("player configuration seats do not match assignment plan")


def _context_for(
    configuration: PlayerConfiguration,
    bundle: RuntimeKnowledgeBundle,
    player: PlayerSessionConfig,
    role_id: str,
    faction_id: str,
    session_epoch: int,
) -> InitialContext:
    del bundle
    return InitialContext(
        game_id=configuration.game_id,
        seat=player.seat,
        session_epoch=session_epoch,
        role_id=role_id,
        faction_id=faction_id,
    )


def _query_context(context: InitialContext, bundle: RuntimeKnowledgeBundle) -> QueryContext:
    # Kept as a helper so the secret-bearing gateway token never enters this
    # object or the prompt composer.
    return QueryContext(
        game_id=context.game_id,
        snapshot_id=bundle.service.snapshot_id,
        seat=context.seat,
        session_epoch=context.session_epoch,
    )


def context_snapshot_id(context: InitialContext, bundle: RuntimeKnowledgeBundle) -> str:
    del context
    return bundle.service.snapshot_id


def _validate_runtime_ref(
    runtime_ref: RuntimeRef,
    context: InitialContext,
    player: PlayerSessionConfig,
) -> None:
    if not isinstance(runtime_ref, RuntimeRef):
        raise PlayerSessionError(f"runtime for seat {player.seat} returned an invalid reference")
    if (
        runtime_ref.session_id != player.session_id
        or runtime_ref.game_id != context.game_id
        or runtime_ref.seat != context.seat
        or runtime_ref.session_epoch != context.session_epoch
    ):
        raise PlayerSessionError(f"runtime reference does not match seat {player.seat}")


def _site_url(site: object) -> str:
    server = getattr(site, "_server", None)
    sockets = getattr(server, "sockets", None)
    if not sockets:
        raise PlayerSessionError("knowledge gateway did not expose a listening socket")
    address = sockets[0].getsockname()
    if not isinstance(address, tuple) or len(address) < 2:
        raise PlayerSessionError("knowledge gateway returned an invalid socket address")
    return f"http://127.0.0.1:{int(address[1])}"


# Compatibility aliases keep the service discoverable without duplicate logic.
ModeratorPlayerSessions = PlayerSessionService
PlayerSessionLauncher = PlayerSessionService


__all__ = [
    "DEFAULT_READING_SKILL_SHA256",
    "ModeratorPlayerSessions",
    "PlayerSessionError",
    "PlayerSessionLauncher",
    "PlayerSessionRecord",
    "PlayerSessionService",
    "RuntimeFactory",
]
