"""Trusted startup card and server-receipt readiness gate.

The startup card is a small navigation object.  It contains only the current
board summary and the authenticated seat's role summary; full rule sections
remain behind the knowledge tools.  Readiness is derived from receipts emitted
by the gateway, never from a model's text claiming that it is ready.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from werewolf.knowledge.gateway import KnowledgeReceipt
from werewolf.knowledge.service import (
    KnowledgeService,
    KnowledgeServiceError,
    QueryContext,
)

DEFAULT_KNOWLEDGE_POLICY = (
    "规则不确定、遗忘或遇到特殊交互时必须重新调用知识工具，不要仅凭常识猜测。"
)


class _StrictRuntimeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class KnowledgeRequiredRead(_StrictRuntimeModel):
    """One tool call required before a player may report ready."""

    tool: Literal["get_board", "get_role"]
    id: str = Field(min_length=1, max_length=128)


class KnowledgeBootstrapBoard(_StrictRuntimeModel):
    """The public board fields included in a startup card."""

    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=300)


class KnowledgeBootstrapRole(_StrictRuntimeModel):
    """The authenticated seat's public role fields included in a startup card."""

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=300)


class KnowledgeBootstrapCard(_StrictRuntimeModel):
    """Short, seat-specific knowledge navigation sent at session start."""

    board: KnowledgeBootstrapBoard
    your_role: KnowledgeBootstrapRole
    snapshot_id: str = Field(min_length=1, max_length=256)
    required_reads: tuple[KnowledgeRequiredRead, KnowledgeRequiredRead]
    knowledge_policy: str = Field(
        default=DEFAULT_KNOWLEDGE_POLICY,
        min_length=1,
        max_length=300,
    )


class KnowledgeBootstrapError(ValueError):
    """Raised when a trusted startup card cannot be created."""


class KnowledgeNotReadyError(KnowledgeBootstrapError):
    """Raised when the two required server-side receipts are absent or stale."""

    code = "KNOWLEDGE_NOT_READY"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


def build_knowledge_bootstrap_card(
    service: KnowledgeService,
    context: QueryContext,
    role_id: str,
) -> KnowledgeBootstrapCard:
    """Build a card from the current package and a trusted seat binding.

    ``role_id`` is deliberately required as the real identity resolved by the
    game manager.  The function accepts no public role list and never includes
    an identity mapping or private information for any other seat.  Public
    board composition may remain in the board summary.
    """

    _validate_inputs(service, context, role_id)
    board_result = service.get_board(context)
    role_result = service.get_role(context, role_id)
    role = role_result.document
    if role.kind != "role" or role.id != role_id:
        raise KnowledgeBootstrapError("role_id must be the seat's canonical role ID")

    board = board_result.document
    return KnowledgeBootstrapCard(
        board=KnowledgeBootstrapBoard(
            id=board.id,
            version=board.version,
            name=board.title,
            summary=_short_summary(board.summary),
        ),
        your_role=KnowledgeBootstrapRole(
            id=role.id,
            name=role.title,
            summary=_short_summary(role.summary),
        ),
        snapshot_id=context.snapshot_id,
        required_reads=(
            KnowledgeRequiredRead(tool="get_board", id=board.id),
            KnowledgeRequiredRead(tool="get_role", id=role.id),
        ),
    )


class KnowledgeReadyGate:
    """Validate the server receipts needed for one player session."""

    def __init__(self, service: KnowledgeService, context: QueryContext, role_id: str) -> None:
        _validate_inputs(service, context, role_id)
        self._service = service
        self._context = context
        self._role_id = role_id

    @property
    def context(self) -> QueryContext:
        return self._context

    @property
    def role_id(self) -> str:
        return self._role_id

    def is_ready(self, receipts: Iterable[KnowledgeReceipt]) -> bool:
        """Return whether both current-session required reads succeeded."""

        try:
            values = tuple(receipts)
        except TypeError:
            return False
        if any(not isinstance(receipt, KnowledgeReceipt) for receipt in values):
            return False

        try:
            board_result = self._service.get_board(self._context)
            role_result = self._service.get_role(self._context, self._role_id)
        except KnowledgeServiceError:
            # A stale snapshot binding is a failed readiness check.  The
            # caller can still report the service error through its normal
            # diagnostics without turning it into a ready transition.
            return False
        board = board_result.document
        role = role_result.document
        if board.kind != "board" or role.kind != "role" or role.id != self._role_id:
            return False

        expected = {
            (
                "get_board",
                f"board:{board.id}@{board.version}",
                board_result.result_id,
            ),
            (
                "get_role",
                f"role:{role.id}@{role.version}",
                role_result.result_id,
            ),
        }
        matched: set[tuple[str, str, str]] = set()
        receipt_ids: set[str] = set()
        for receipt in values:
            if not self._same_binding(receipt):
                continue
            if not receipt.receipt_id or receipt.receipt_id in receipt_ids:
                continue
            receipt_ids.add(receipt.receipt_id)
            candidate = (receipt.tool, receipt.canonical_ref, receipt.result_id)
            if candidate in expected:
                matched.add(candidate)
        return matched == expected

    def require_ready(self, receipts: Iterable[KnowledgeReceipt]) -> None:
        """Raise a stable error unless the current session is ready."""

        if not self.is_ready(receipts):
            raise KnowledgeNotReadyError(
                "current session requires successful get_board and get_role receipts"
            )

    # ``check`` is a concise adapter name for callers that prefer a gate API.
    def check(self, receipts: Iterable[KnowledgeReceipt]) -> bool:
        return self.is_ready(receipts)

    def _same_binding(self, receipt: KnowledgeReceipt) -> bool:
        return (
            receipt.game_id == self._context.game_id
            and receipt.snapshot_id == self._context.snapshot_id
            and receipt.seat == self._context.seat
            and receipt.session_epoch == self._context.session_epoch
        )


def check_knowledge_ready(
    service: KnowledgeService,
    context: QueryContext,
    role_id: str,
    receipts: Iterable[KnowledgeReceipt],
) -> bool:
    """Functional adapter for the readiness gate."""

    return KnowledgeReadyGate(service, context, role_id).is_ready(receipts)


def _validate_inputs(service: KnowledgeService, context: QueryContext, role_id: str) -> None:
    if not isinstance(service, KnowledgeService):
        raise TypeError("service must be a KnowledgeService")
    if not isinstance(context, QueryContext):
        raise TypeError("context must be a QueryContext")
    if not isinstance(role_id, str) or not role_id.strip():
        raise ValueError("role_id must be a non-empty canonical role ID")
    if role_id != role_id.strip():
        raise ValueError("role_id must not contain surrounding whitespace")


def _short_summary(value: str) -> str:
    """Keep card text compact even if a malformed package has a long summary."""

    compact = " ".join(value.replace("\r", "\n").split())
    encoded = compact.encode("utf-8")
    if len(encoded) <= 300:
        return compact
    return encoded[:300].decode("utf-8", errors="ignore")


__all__ = [
    "DEFAULT_KNOWLEDGE_POLICY",
    "KnowledgeBootstrapBoard",
    "KnowledgeBootstrapCard",
    "KnowledgeBootstrapError",
    "KnowledgeBootstrapRole",
    "KnowledgeNotReadyError",
    "KnowledgeReadyGate",
    "KnowledgeRequiredRead",
    "build_knowledge_bootstrap_card",
    "check_knowledge_ready",
]
