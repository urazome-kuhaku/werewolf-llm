"""Tests for deterministic stable Markdown section extraction."""

from __future__ import annotations

import pytest

from werewolf.knowledge.sections import (
    DuplicateSectionIdError,
    InvalidSectionIdError,
    MissingSectionIdError,
    SectionNotFoundError,
    SectionParseError,
    extract_sections,
)


def test_extracts_anchored_sections_in_source_order_and_normalizes_lf() -> None:
    document = extract_sections(
        "# 预女猎白\r\n"
        "\r\n"
        "## 概览 {#overview}\r\n"
        "人数与定位。\r\n"
        "\r\n"
        "### 细节\r\n"
        "保留在概览正文。\r\n"
        "## 胜利条件 {#victory}\r\n"
        "屠边。\r\n",
        required_ids=("overview", "victory"),
    )

    assert document.title == "预女猎白"
    assert document.section_ids == ("overview", "victory")
    assert document[0].title == "概览"
    assert document[0].body == "人数与定位。\n\n### 细节\n保留在概览正文。"
    assert document[0].order == 0
    assert "\r" not in document.normalized_markdown


def test_intro_is_a_reserved_explicit_section_when_preamble_exists() -> None:
    document = extract_sections(
        "# 板子\n\n这是给主持人的说明。\n\n## 概览 {#overview}\n正文。\n",
    )

    assert document.section_ids == ("intro", "overview")
    assert document.require("board:overview").body == "正文。"
    assert document.resolve("board:missing") is None


def test_reading_plan_reference_object_can_be_resolved() -> None:
    class Ref:
        kind = "board"
        id = "victory"

    document = extract_sections("## 胜负 {#victory}\n屠边。\n")

    assert document.resolve(Ref()).id == "victory"
    with pytest.raises(SectionNotFoundError, match="unknown Markdown section"):
        document.require("board:missing")
    assert document.resolve("mechanic:voting") is None


@pytest.mark.parametrize(
    "markdown",
    [
        "## 概览\n正文\n",
        "## 概览 {#Overview}\n正文\n",
        "## 概览 {#overview-id}\n正文\n",
        "## 概览 {#overview extra}\n正文\n",
        "## 概览 {#overview\n正文\n",
    ],
)
def test_rejects_missing_or_malformed_level_two_anchors(markdown: str) -> None:
    with pytest.raises((InvalidSectionIdError, MissingSectionIdError), match="anchor|identifier"):
        extract_sections(markdown)


def test_rejects_duplicate_heading_ids_even_when_titles_differ() -> None:
    with pytest.raises(DuplicateSectionIdError, match="duplicate"):
        extract_sections(
            "## 概览 {#overview}\n一\n## 另一个标题 {#overview}\n二\n",
        )


def test_required_ids_are_checked_without_deriving_from_chinese_titles() -> None:
    with pytest.raises(SectionParseError, match="missing required"):
        extract_sections("## 介绍 {#introduction}\n正文\n", required_ids=("overview",))

    document = extract_sections("## 概览 {#overview}\n正文\n")
    assert document.resolve("board:概览") is None
    assert document.resolve("board:overview").title == "概览"


def test_headings_in_fenced_code_are_not_sections() -> None:
    document = extract_sections(
        "```markdown\n## 假标题 {#fake}\n```\n## 真标题 {#real}\n正文\n",
    )

    assert document.section_ids == ("intro", "real")
    assert "#fake" in document[0].body


def test_reserved_intro_id_cannot_be_redeclared() -> None:
    with pytest.raises(DuplicateSectionIdError, match="intro"):
        extract_sections(
            "前言\n# 标题 {#intro}\n## 概览 {#overview}\n正文\n",
        )
