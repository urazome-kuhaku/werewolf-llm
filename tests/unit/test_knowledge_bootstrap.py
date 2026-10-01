"""Unit tests for the trusted player knowledge bootstrap boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from pydantic import BaseModel, ConfigDict

from werewolf.knowledge.compiler import CompiledKnowledgePackage, EffectiveRoleProfile
from werewolf.knowledge.gateway import KnowledgeReceipt
from werewolf.knowledge.indexes import KnowledgeIndex, KnowledgeIndexDocument
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.sections import MarkdownDocument, MarkdownSection
from werewolf.knowledge.service import KnowledgeService, QueryContext
from werewolf.runtime.knowledge_bootstrap import (
    DEFAULT_KNOWLEDGE_POLICY,
    KnowledgeBootstrapCard,
    KnowledgeNotReadyError,
    KnowledgeReadyGate,
    build_knowledge_bootstrap_card,
)


class _Role(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    name: str
    public_summary: str
    source_refs: list[str]


BOARD_REF = VersionedRef(id="classic", version="1.0.0")


def _compiled() -> CompiledKnowledgePackage:
    records = (
        KnowledgeIndexDocument(
            kind="board",
            id="classic",
            version="1.0.0",
            title="经典预女猎白",
            body="十二人暗牌板子，包含预言家、女巫、警长与首夜遗言。完整规则不应进入启动卡。",
        ),
        KnowledgeIndexDocument(
            kind="role",
            id="witch",
            version="1.0.0",
            title="女巫",
            body="女巫拥有解药和毒药。",
            related_ids=(BOARD_REF.format(),),
        ),
        KnowledgeIndexDocument(
            kind="role",
            id="seer",
            version="1.0.0",
            title="预言家",
            body="预言家每晚查验一名玩家。",
            related_ids=(BOARD_REF.format(),),
        ),
        KnowledgeIndexDocument(
            kind="topic",
            id="overview",
            version="1.0.0",
            title="板子概览",
            body="板子概览和阅读入口。",
            related_ids=(f"board:{BOARD_REF.format()}",),
        ),
    )
    sections = {
        "board:classic@1.0.0": MarkdownDocument(
            title="概览",
            sections=(MarkdownSection(id="overview", title="概览", body="板子概览。"),),
        ),
        "role:witch@1.0.0": MarkdownDocument(
            title="女巫",
            sections=(MarkdownSection(id="ability", title="技能", body="解药与毒药。"),),
        ),
        "role:seer@1.0.0": MarkdownDocument(
            title="预言家",
            sections=(MarkdownSection(id="ability", title="技能", body="查验。"),),
        ),
    }
    witch = EffectiveRoleProfile(
        board_ref=BOARD_REF,
        role_ref=VersionedRef(id="witch", version="1.0.0"),
        count=1,
        base_role=_Role(
            role_id="witch",
            name="女巫",
            public_summary="你属于好人阵营，拥有一瓶解药和一瓶毒药。",
            source_refs=["official"],
        ),
        effective_rules={"can_self_heal": False},
        override_claim_refs=("claim-witch",),
        sections=sections["role:witch@1.0.0"],
    )
    seer = EffectiveRoleProfile(
        board_ref=BOARD_REF,
        role_ref=VersionedRef(id="seer", version="1.0.0"),
        count=1,
        base_role=_Role(
            role_id="seer",
            name="预言家",
            public_summary="你每晚查验一名玩家。",
            source_refs=["official"],
        ),
        effective_rules={},
        override_claim_refs=(),
        sections=sections["role:seer@1.0.0"],
    )
    return CompiledKnowledgePackage(
        board_ref=BOARD_REF,
        documents=records,
        index=KnowledgeIndex.build(records),
        sections=MappingProxyType(sections),
        effective_roles={"witch": witch, "seer": seer},
        document_digests={},
        package_payload={
            "reading_plan": {"bootstrap_topics": ["board:overview"]},
        },
        manifest_payload={},
        canonical_package_json="{}",
        canonical_manifest_json="{}",
        package_identity="package-digest",
        manifest_sha256="manifest-digest",
    )


@pytest.fixture()
def service() -> KnowledgeService:
    return KnowledgeService(_compiled(), snapshot_id="snapshot-1")


@pytest.fixture()
def context() -> QueryContext:
    return QueryContext(game_id="game-1", snapshot_id="snapshot-1", seat=3, session_epoch=7)


def _receipt(
    *,
    context: QueryContext,
    tool: str,
    canonical_ref: str,
    result_id: str,
    receipt_id: str,
    **overrides: object,
) -> KnowledgeReceipt:
    values = {
        "receipt_id": receipt_id,
        "game_id": context.game_id,
        "snapshot_id": context.snapshot_id,
        "seat": context.seat,
        "session_epoch": context.session_epoch,
        "tool": tool,
        "canonical_ref": canonical_ref,
        "result_id": result_id,
        "created_at": datetime(2026, 9, 28, tzinfo=UTC),
    }
    values.update(overrides)
    return KnowledgeReceipt(**values)


def _required_receipts(
    service: KnowledgeService,
    context: QueryContext,
    *,
    role_id: str = "witch",
) -> list[KnowledgeReceipt]:
    board = service.get_board(context)
    role = service.get_role(context, role_id)
    return [
        _receipt(
            context=context,
            tool="get_board",
            canonical_ref=f"board:{board.document.id}@{board.document.version}",
            result_id=board.result_id,
            receipt_id="receipt-board",
        ),
        _receipt(
            context=context,
            tool="get_role",
            canonical_ref=f"role:{role.document.id}@{role.document.version}",
            result_id=role.result_id,
            receipt_id="receipt-role",
        ),
    ]


def test_bootstrap_card_is_short_and_seat_specific(
    service: KnowledgeService,
    context: QueryContext,
) -> None:
    card = build_knowledge_bootstrap_card(service, context, "witch")

    assert isinstance(card, KnowledgeBootstrapCard)
    assert card.board.id == "classic"
    assert card.board.version == "1.0.0"
    assert card.board.name == "经典预女猎白"
    assert card.your_role.id == "witch"
    assert card.your_role.name == "女巫"
    assert card.snapshot_id == "snapshot-1"
    assert [(item.tool, item.id) for item in card.required_reads] == [
        ("get_board", "classic"),
        ("get_role", "witch"),
    ]
    assert card.knowledge_policy == DEFAULT_KNOWLEDGE_POLICY
    dumped = card.model_dump(mode="json")
    assert "预言家" in card.board.summary
    assert "effective_rules" not in str(dumped)
    assert dumped["your_role"] == {
        "id": "witch",
        "name": "女巫",
        "summary": "你属于好人阵营，拥有一瓶解药和一瓶毒药。",
    }


def test_ready_requires_current_board_and_own_role_receipts(
    service: KnowledgeService,
    context: QueryContext,
) -> None:
    gate = KnowledgeReadyGate(service, context, "witch")

    assert gate.is_ready(_required_receipts(service, context))
    assert gate.check(_required_receipts(service, context))

    only_board = _required_receipts(service, context)[:1]
    assert not gate.is_ready(only_board)

    other_role = _required_receipts(service, context, role_id="seer")
    assert not gate.is_ready(other_role)


@pytest.mark.parametrize(
    "override",
    [
        {"game_id": "other-game"},
        {"snapshot_id": "snapshot-old"},
        {"seat": 4},
        {"session_epoch": 6},
    ],
)
def test_stale_or_other_binding_receipts_do_not_satisfy_gate(
    service: KnowledgeService,
    context: QueryContext,
    override: dict[str, object],
) -> None:
    receipts = _required_receipts(service, context)
    receipts[1] = _receipt(
        context=context,
        tool="get_role",
        canonical_ref="role:witch@1.0.0",
        result_id=service.get_role(context, "witch").result_id,
        receipt_id="receipt-role-replaced",
        **override,
    )
    assert not KnowledgeReadyGate(service, context, "witch").is_ready(receipts)


def test_model_claim_without_server_receipts_is_not_ready(
    service: KnowledgeService,
    context: QueryContext,
) -> None:
    gate = KnowledgeReadyGate(service, context, "witch")

    assert not gate.is_ready(())
    with pytest.raises(KnowledgeNotReadyError, match="KNOWLEDGE_NOT_READY"):
        gate.require_ready(())


def test_gate_rejects_context_bound_to_another_snapshot(
    service: KnowledgeService,
    context: QueryContext,
) -> None:
    stale_context = context.model_copy(update={"snapshot_id": "snapshot-old"})
    assert not KnowledgeReadyGate(service, stale_context, "witch").is_ready(())


def test_card_rejects_alias_instead_of_exposing_an_untrusted_role(
    service: KnowledgeService,
    context: QueryContext,
) -> None:
    with pytest.raises(ValueError, match="canonical role ID"):
        build_knowledge_bootstrap_card(service, context, "女巫")
