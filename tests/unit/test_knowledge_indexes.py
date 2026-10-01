"""Deterministic lexical index tests for published knowledge records."""

import pytest

from werewolf.knowledge.indexes import (
    MAX_SEARCH_LIMIT,
    KnowledgeIndex,
    KnowledgeIndexDocument,
    build_knowledge_index,
    normalize_text,
)


def _documents() -> list[KnowledgeIndexDocument]:
    return [
        KnowledgeIndexDocument(
            kind="interaction",
            id="hunter-poison",
            version="1.0.0",
            title="猎人吃毒后的开枪规则",
            aliases=("猎人中毒",),
            body="猎人如果被女巫毒杀，结算时通常不能开枪；以本板子定义为准。",
            topics=("night.action", "role.interaction"),
            related_ids=("hunter", "witch", "role:witch@1.0.0"),
        ),
        KnowledgeIndexDocument(
            kind="role",
            id="witch",
            version="1.0.0",
            title="女巫",
            aliases=("药师",),
            body="女巫拥有解药和毒药。",
            topics=("night.action",),
        ),
        KnowledgeIndexDocument(
            kind="role",
            id="witch",
            version="2.0.0",
            title="女巫（新版）",
            aliases=("药师",),
            body="新版女巫规则。",
            topics=("night.action",),
        ),
        KnowledgeIndexDocument(
            kind="board",
            id="classic",
            version="1.0.0",
            title="经典板子",
            aliases=("预女猎白",),
            body="经典十二人板子的行动顺序。",
            topics=("board.setup",),
        ),
    ]


def test_normalize_text_uses_nfkc_casefold_and_collapsed_whitespace() -> None:
    assert normalize_text("  Ｗitch\t 女巫  ") == "witch 女巫"


def test_build_is_deterministic_and_rejects_duplicate_exact_keys() -> None:
    first = build_knowledge_index(_documents())
    second = build_knowledge_index(reversed(_documents()))

    assert tuple(document.key for document in first.documents) == tuple(
        document.key for document in second.documents
    )
    assert first.alias_index == second.alias_index
    assert first.topic_index == second.topic_index
    assert first.relation_index == second.relation_index
    assert first.text_index == second.text_index

    with pytest.raises(ValueError, match="duplicate knowledge document keys"):
        build_knowledge_index([_documents()[0], _documents()[0]])


def test_exact_lookup_has_priority_over_an_alias_collision() -> None:
    documents = _documents()
    documents.append(
        KnowledgeIndexDocument(
            kind="topic",
            id="witch-alias",
            version="1.0.0",
            title="别名示例",
            aliases=("role:witch@1.0.0",),
        ),
    )
    index = KnowledgeIndex.build(documents)

    result = index.lookup("role:witch@1.0.0")
    assert result.match_type == "exact"
    assert result.document is not None
    assert result.document.key == "role:witch@1.0.0"


def test_ambiguous_alias_returns_all_stable_candidates() -> None:
    index = KnowledgeIndex.build(_documents())

    result = index.lookup("药师")
    assert result.is_ambiguous
    assert result.document is None
    assert tuple(candidate.key for candidate in result.candidates) == (
        "role:witch@1.0.0",
        "role:witch@2.0.0",
    )
    assert tuple(document.key for document in index.lookup_alias("药师")) == (
        "role:witch@1.0.0",
        "role:witch@2.0.0",
    )


def test_topic_and_relation_indexes_resolve_normalized_references() -> None:
    index = KnowledgeIndex.build(_documents())

    assert tuple(document.key for document in index.lookup_topic(" NIGHT.ACTION ")) == (
        "interaction:hunter-poison@1.0.0",
        "role:witch@1.0.0",
        "role:witch@2.0.0",
    )
    assert tuple(document.key for document in index.lookup_relation(" witch ")) == (
        "interaction:hunter-poison@1.0.0",
    )
    assert tuple(document.key for document in index.lookup_relation("role:witch@1.0.0")) == (
        "interaction:hunter-poison@1.0.0",
    )


def test_chinese_ngram_search_finds_rule_and_has_stable_ties() -> None:
    index = KnowledgeIndex.build(_documents())

    results = index.search("猎人吃毒能否开枪", limit=2)
    assert results
    assert results[0].document.key == "interaction:hunter-poison@1.0.0"
    assert "猎人" in results[0].matched_terms
    assert "开枪" in results[0].matched_terms

    repeated = index.search("猎人吃毒能否开枪", limit=2)
    assert results == repeated


def test_search_filters_kinds_and_bounds_result_count() -> None:
    index = KnowledgeIndex.build(_documents())

    results = index.search("女巫", kinds=("role",), limit=MAX_SEARCH_LIMIT + 100)
    assert results
    assert len(results) <= MAX_SEARCH_LIMIT
    assert all(result.document.kind == "role" for result in results)
    assert index.search("女巫", limit=0) == ()


def test_record_freezes_collection_inputs() -> None:
    record = KnowledgeIndexDocument(
        kind="mechanic",
        id="voting",
        version="1.0.0",
        aliases=["投票"],  # type: ignore[arg-type]
        topics=["vote"],  # type: ignore[arg-type]
        related_ids=["board:classic"],  # type: ignore[arg-type]
    )
    assert record.aliases == ("投票",)
    assert record.topics == ("vote",)
    assert record.related_ids == ("board:classic",)
