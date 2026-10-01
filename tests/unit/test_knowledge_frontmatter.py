"""Security and normalization tests for Markdown frontmatter parsing."""

from __future__ import annotations

import pytest

from werewolf.knowledge.frontmatter import FrontmatterParseError, parse_markdown


def test_parses_chinese_frontmatter_and_preserves_markdown_body() -> None:
    parsed = parse_markdown(
        "---\n"
        "schema_version: 1\n"
        "name: 女巫\n"
        "aliases: [药师]\n"
        "enabled: false\n"
        "reviewed_at: 2026-09-27\n"
        "---\n"
        "# 女巫\n\n正文。\n".encode(),
    )

    assert parsed.frontmatter == {
        "schema_version": 1,
        "name": "女巫",
        "aliases": ["药师"],
        "enabled": False,
        "reviewed_at": "2026-09-27",
    }
    assert parsed.body == "# 女巫\n\n正文。\n"


def test_normalizes_crlf_and_lone_cr_in_yaml_and_body() -> None:
    parsed = parse_markdown(
        b"---\r\nname: test\rvalue: 1\r\n---\r\nbody\r\nnext\r",
    )

    assert parsed.frontmatter == {"name": "test", "value": 1}
    assert parsed.body == "body\nnext\n"


@pytest.mark.parametrize(
    "data",
    [
        b"name: test\n",
        b"---\nname: test\n",
        b"---\nname: test\n...\n",
    ],
)
def test_requires_paired_frontmatter_delimiters(data: bytes) -> None:
    with pytest.raises(FrontmatterParseError, match="frontmatter"):
        parse_markdown(data)


def test_rejects_invalid_utf8() -> None:
    with pytest.raises(FrontmatterParseError, match="UTF-8"):
        parse_markdown(b"---\nname: \xff\n---\n")


def test_rejects_document_and_frontmatter_size_limits() -> None:
    with pytest.raises(FrontmatterParseError, match="document"):
        parse_markdown(b"---\na: 1\n---\nbody", max_document_bytes=10)

    with pytest.raises(FrontmatterParseError, match="frontmatter"):
        parse_markdown(
            b"---\nname: 123456789\n---\n",
            max_frontmatter_bytes=5,
        )


def test_rejects_duplicate_keys_and_non_string_keys() -> None:
    with pytest.raises(FrontmatterParseError, match="duplicate"):
        parse_markdown(b"---\na: 1\na: 2\n---\n")

    with pytest.raises(FrontmatterParseError, match="keys must be strings"):
        parse_markdown(b"---\n1: one\n---\n")


def test_rejects_arbitrary_tags_without_constructing_objects() -> None:
    with pytest.raises(FrontmatterParseError, match="tag"):
        parse_markdown(b"---\nvalue: !!python/object/apply:os.system ['echo bad']\n---\n")

    with pytest.raises(FrontmatterParseError, match="tag"):
        parse_markdown(b"---\nvalue: !custom-tag value\n---\n")


def test_rejects_recursive_aliases() -> None:
    with pytest.raises(FrontmatterParseError, match="recursive|unsafe YAML"):
        parse_markdown(b"---\nvalue: &loop [*loop]\n---\n")


def test_rejects_non_finite_numbers() -> None:
    with pytest.raises(FrontmatterParseError, match="NaN|Infinity"):
        parse_markdown(b"---\nvalue: .nan\n---\n")
    with pytest.raises(FrontmatterParseError, match="NaN|Infinity"):
        parse_markdown(b"---\nvalue: .inf\n---\n")


def test_rejects_deep_and_large_yaml_values() -> None:
    with pytest.raises(FrontmatterParseError, match="depth"):
        parse_markdown(
            b"---\na:\n  b:\n    c: 1\n---\n",
            max_yaml_depth=2,
        )
    with pytest.raises(FrontmatterParseError, match="node"):
        parse_markdown(
            b"---\na: 1\nb: 2\n---\n",
            max_yaml_nodes=3,
        )


def test_empty_frontmatter_is_an_empty_mapping() -> None:
    parsed = parse_markdown(b"---\n---\nbody")

    assert parsed.frontmatter == {}
    assert parsed.body == "body"


def test_rejects_non_mapping_frontmatter() -> None:
    with pytest.raises(FrontmatterParseError, match="mapping"):
        parse_markdown(b"---\n- one\n- two\n---\n")
