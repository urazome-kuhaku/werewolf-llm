"""Unit coverage for the snapshot-bound read-only knowledge service."""

from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from werewolf.knowledge.compiler import CompiledKnowledgePackage, EffectiveRoleProfile
from werewolf.knowledge.indexes import KnowledgeIndex, KnowledgeIndexDocument
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.sections import MarkdownDocument, MarkdownSection
from werewolf.knowledge.service import (
    KnowledgeAmbiguousError,
    KnowledgeForbiddenError,
    KnowledgeInteractionNotFoundError,
    KnowledgeNotFoundError,
    KnowledgeService,
    QueryContext,
    SearchQuery,
)


class _RoleStub(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    name: str
    public_summary: str
    source_refs: list[str]


BOARD_REF = VersionedRef(id="classic", version="1.0.0")
WITCH_REF = VersionedRef(id="witch", version="1.0.0")
SEER_REF = VersionedRef(id="seer", version="1.0.0")


def _section(section_id: str, title: str, body: str) -> MarkdownDocument:
    return MarkdownDocument(
        sections=(MarkdownSection(id=section_id, title=title, body=body),),
        title=title,
    )


def _compiled() -> CompiledKnowledgePackage:
    records = (
        KnowledgeIndexDocument(
            kind="board",
            id="classic",
            version="1.0.0",
            title="经典预女猎白",
            aliases=("预女猎白",),
            body="十二人暗牌板子，包含警长与首夜遗言。",
        ),
        KnowledgeIndexDocument(
            kind="role",
            id="witch",
            version="1.0.0",
            title="女巫",
            aliases=("药师",),
            body="女巫拥有解药和毒药。",
            related_ids=(BOARD_REF.format(),),
        ),
        KnowledgeIndexDocument(
            kind="role",
            id="seer",
            version="1.0.0",
            title="预言家",
            aliases=("药师",),
            body="预言家每晚查验一名玩家。",
            related_ids=(BOARD_REF.format(),),
        ),
        KnowledgeIndexDocument(
            kind="mechanic",
            id="voting",
            version="1.0.0",
            title="投票",
            aliases=("放逐票",),
            body="白天存活玩家投票放逐。",
            related_ids=(BOARD_REF.format(),),
        ),
        KnowledgeIndexDocument(
            kind="interaction",
            id="witch-hunter",
            version="1.0.0",
            title="女巫与猎人",
            body="猎人被毒杀时不能开枪。",
            related_ids=("witch", "hunter", "witch.poison"),
        ),
        KnowledgeIndexDocument(
            kind="topic",
            id="overview",
            version="1.0.0",
            title="板子概览",
            body="板子概览和阅读入口。",
            topics=("overview",),
            related_ids=(f"board:{BOARD_REF.format()}",),
        ),
    )
    index = KnowledgeIndex.build(records)
    role_sections = {
        "role:witch@1.0.0": _section("abilities", "技能", "解药与毒药。"),
        "role:seer@1.0.0": _section("abilities", "技能", "查验。"),
    }
    sections = {
        "board:classic@1.0.0": _section("overview", "概览", "板子概览。"),
        **role_sections,
        "mechanic:voting@1.0.0": _section("vote", "投票", "投票规则。"),
        "interaction:witch-hunter@1.0.0": _section("resolution", "结算", "交互规则。"),
    }
    witch_profile = EffectiveRoleProfile(
        board_ref=BOARD_REF,
        role_ref=WITCH_REF,
        count=1,
        base_role=_RoleStub(
            role_id="witch",
            name="女巫",
            public_summary="女巫的板子有效规则。",
            source_refs=["src-official"],
        ),
        effective_rules={"can_self_heal": False},
        override_claim_refs=("claim-witch",),
        sections=sections["role:witch@1.0.0"],
    )
    seer_profile = EffectiveRoleProfile(
        board_ref=BOARD_REF,
        role_ref=SEER_REF,
        count=1,
        base_role=_RoleStub(
            role_id="seer",
            name="预言家",
            public_summary="预言家的板子有效规则。",
            source_refs=["src-official"],
        ),
        effective_rules={},
        override_claim_refs=(),
        sections=sections["role:seer@1.0.0"],
    )
    reading_plan = {
        "bootstrap_topics": [{"kind": "board", "id": "overview"}],
        "role_required_topics": {"witch": [{"kind": "interaction", "id": "witch-hunter"}]},
        "high_risk_topics": [{"kind": "mechanic", "id": "voting"}],
    }
    return CompiledKnowledgePackage(
        board_ref=BOARD_REF,
        documents=records,
        index=index,
        sections=MappingProxyType(sections),
        effective_roles={"witch": witch_profile, "seer": seer_profile},
        document_digests={},
        package_payload={"reading_plan": reading_plan},
        manifest_payload={},
        canonical_package_json="{}",
        canonical_manifest_json="{}",
        package_identity="package-digest",
        manifest_sha256="manifest-digest",
    )


@pytest.fixture()
def service() -> KnowledgeService:
    return KnowledgeService(_compiled(), snapshot_id="snapshot-1")


def _context(snapshot_id: str = "snapshot-1") -> QueryContext:
    return QueryContext(game_id="game-1", snapshot_id=snapshot_id, seat=3, session_epoch=1)


def test_queries_are_bound_to_the_current_snapshot(service: KnowledgeService) -> None:
    result = service.get_board(_context())

    assert result.snapshot.id == "snapshot-1"
    assert result.snapshot.board == "classic@1.0.0"
    assert result.document.kind == "board"
    assert result.document.id == "classic"
    assert result.document.sections[0].section_id == "overview"
    assert result.recommended_next_reads[0].id == "overview"
    assert result.citations[0].source_id == "board:classic@1.0.0"

    with pytest.raises(KnowledgeForbiddenError, match="FORBIDDEN"):
        service.get_board(_context("snapshot-2"))


def test_role_returns_effective_rules_and_detached_result(service: KnowledgeService) -> None:
    result = service.get_role(_context(), "女巫")

    assert result.document.effective_rules == {"can_self_heal": False}
    assert result.document.summary == "女巫的板子有效规则。"
    assert result.recommended_next_reads[0].id == "witch-hunter"
    result.document.effective_rules["can_self_heal"] = True
    assert service.get_role(_context(), "witch").document.effective_rules["can_self_heal"] is False


def test_alias_ambiguity_is_reported_without_guessing(service: KnowledgeService) -> None:
    with pytest.raises(KnowledgeAmbiguousError) as raised:
        service.get_role(_context(), "药师")

    assert raised.value.code == "AMBIGUOUS"
    assert set(raised.value.candidates) == {"role:seer@1.0.0", "role:witch@1.0.0"}


def test_missing_document_and_interaction_resolution(service: KnowledgeService) -> None:
    with pytest.raises(KnowledgeNotFoundError, match="NOT_FOUND"):
        service.get_mechanic(_context(), "missing")

    result = service.get_interaction(_context(), ("witch", "hunter"), "witch.poison")
    assert result.document.id == "witch-hunter"
    assert result.document.version == "1.0.0"


def test_missing_interaction_exposes_bounded_current_snapshot_repair_hint(
    service: KnowledgeService,
) -> None:
    with pytest.raises(KnowledgeInteractionNotFoundError) as raised:
        service.get_interaction(_context(), ("witch", "hunter"), "witch.poison_hunter")

    error = raised.value
    assert error.code == "NOT_FOUND"
    assert len(error.suggestions) == 1
    suggestion = error.suggestions[0]
    assert suggestion.canonical_ref == "interaction:witch-hunter@1.0.0"
    assert suggestion.subjects == ("witch", "hunter")
    assert suggestion.situation_key == "witch.poison"


def test_topic_and_search_are_limited_to_current_package(service: KnowledgeService) -> None:
    topic = service.get_rule_topic(_context(), "board:overview")
    assert topic.document.kind == "topic"
    assert topic.document.id == "overview"

    found = service.search_rules(_context(), SearchQuery(query="毒药", kinds=("role",), limit=8))
    assert found.hits
    assert all(hit.kind == "role" for hit in found.hits)
    assert len(found.hits) <= 8

    with pytest.raises(ValidationError):
        SearchQuery(query="投票", limit=9)
