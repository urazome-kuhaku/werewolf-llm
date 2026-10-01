"""Bounded, safe parsing for knowledge Markdown frontmatter.

Knowledge documents are untrusted input at the parsing boundary.  This module
keeps that boundary deliberately small: it accepts UTF-8 Markdown bytes,
extracts one YAML frontmatter mapping, and returns the normalized Markdown
body.  YAML is loaded with a restricted :class:`yaml.SafeLoader` subclass;
document/model-specific validation belongs to the compiler layer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Final

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, JsonValue
from yaml.constructor import ConstructorError  # type: ignore[import-untyped]
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode  # type: ignore[import-untyped]

# The limits are intentionally conservative for a single knowledge document.
# Callers can lower them for a particular compilation context.
DEFAULT_MAX_DOCUMENT_BYTES: Final = 1 * 1024 * 1024
DEFAULT_MAX_FRONTMATTER_BYTES: Final = 128 * 1024
DEFAULT_MAX_YAML_DEPTH: Final = 32
DEFAULT_MAX_YAML_NODES: Final = 4_096

_NULL_TAG = "tag:yaml.org,2002:null"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_STR_TAG = "tag:yaml.org,2002:str"
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_SEQ_TAG = "tag:yaml.org,2002:seq"
_MAP_TAG = "tag:yaml.org,2002:map"

_ALLOWED_SCALAR_TAGS = frozenset(
    {_NULL_TAG, _BOOL_TAG, _INT_TAG, _FLOAT_TAG, _STR_TAG, _TIMESTAMP_TAG},
)


class FrontmatterParseError(ValueError):
    """Raised when a Markdown document violates the parsing contract."""


class ParsedMarkdown(BaseModel):
    """The normalized, syntax-only result of parsing one Markdown document."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    frontmatter: dict[str, JsonValue]
    body: str


class _YamlLimitError(yaml.YAMLError):  # type: ignore[misc]
    """Internal YAML error used for bounded-composition failures."""


class _BoundedSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader with composition bounds and strict mapping keys."""

    def __init__(self, stream: str, *, max_depth: int, max_nodes: int) -> None:
        self._max_depth = max_depth
        self._max_nodes = max_nodes
        self._composition_depth = 0
        self._composed_nodes = 0
        super().__init__(stream)

    def compose_node(self, parent: Node | None, index: object) -> Node:
        """Compose one node while bounding both nesting and node count."""

        self._composed_nodes += 1
        if self._composed_nodes > self._max_nodes:
            raise _YamlLimitError(
                f"YAML node count exceeds the maximum of {self._max_nodes}",
            )

        self._composition_depth += 1
        if self._composition_depth > self._max_depth:
            self._composition_depth -= 1
            raise _YamlLimitError(
                f"YAML nesting depth exceeds the maximum of {self._max_depth}",
            )

        try:
            node = super().compose_node(parent, index)
        finally:
            self._composition_depth -= 1

        self._validate_node_tag(node)
        return node

    @staticmethod
    def _validate_node_tag(node: Node) -> None:
        """Allow only YAML tags that can become JSON-compatible values."""

        if isinstance(node, ScalarNode):
            if node.tag not in _ALLOWED_SCALAR_TAGS:
                raise _YamlLimitError(f"YAML tag is not allowed: {node.tag!r}")
            return
        if isinstance(node, SequenceNode):
            if node.tag != _SEQ_TAG:
                raise _YamlLimitError(f"YAML tag is not allowed: {node.tag!r}")
            return
        if isinstance(node, MappingNode):
            if node.tag != _MAP_TAG:
                raise _YamlLimitError(f"YAML tag is not allowed: {node.tag!r}")
            return
        raise _YamlLimitError(f"unsupported YAML node type: {type(node).__name__}")

    def construct_mapping(
        self,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[str, object]:
        """Construct mappings while rejecting non-string and repeated keys."""

        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None,
                None,
                "expected a mapping node",
                getattr(node, "start_mark", None),
            )

        mapping: dict[str, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if type(key) is not str:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "mapping keys must be strings",
                    key_node.start_mark,
                )
            if key in mapping:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _validated_limit(value: int | None, default: int, name: str) -> int:
    """Resolve and validate one caller-configurable positive integer limit."""

    resolved = default if value is None else value
    if type(resolved) is not int or resolved <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return resolved


def _is_delimiter(line: str) -> bool:
    """Return whether a complete line is a Markdown frontmatter delimiter."""

    return line == "---" or line.startswith("---") and not line[3:].strip(" \t")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Extract YAML and body from normalized Markdown text."""

    first_newline = text.find("\n")
    if first_newline < 0 or not _is_delimiter(text[:first_newline]):
        raise FrontmatterParseError("Markdown must start with a YAML frontmatter delimiter")

    content_start = first_newline + 1
    cursor = content_start
    while cursor <= len(text):
        next_newline = text.find("\n", cursor)
        if next_newline < 0:
            line = text[cursor:]
            after_line = len(text)
        else:
            line = text[cursor:next_newline]
            after_line = next_newline + 1

        if _is_delimiter(line):
            return text[content_start:cursor], text[after_line:]
        if next_newline < 0:
            break
        cursor = after_line

    raise FrontmatterParseError("Markdown frontmatter must have a closing delimiter")


def _load_yaml(
    source: str,
    *,
    max_depth: int,
    max_nodes: int,
) -> object:
    """Load one bounded YAML document using a SafeLoader subclass."""

    loader = _BoundedSafeLoader(source, max_depth=max_depth, max_nodes=max_nodes)
    try:
        return loader.get_single_data()
    except (yaml.YAMLError, RecursionError, MemoryError) as exc:
        raise FrontmatterParseError(f"invalid or unsafe YAML frontmatter: {exc}") from exc
    finally:
        loader.dispose()


def _normalize_json_value(
    value: object,
    *,
    active: set[int],
    depth: int,
    max_depth: int,
    count: list[int],
    max_nodes: int,
) -> JsonValue:
    """Convert SafeLoader output to JSON values and reject cycles/non-finite floats."""

    count[0] += 1
    if count[0] > max_nodes:
        raise FrontmatterParseError(
            f"YAML value count exceeds the maximum of {max_nodes}",
        )
    if depth > max_depth:
        raise FrontmatterParseError(
            f"YAML value depth exceeds the maximum of {max_depth}",
        )

    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise FrontmatterParseError("YAML frontmatter must not contain NaN or Infinity")
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()

    if type(value) is list:
        object_id = id(value)
        if object_id in active:
            raise FrontmatterParseError("YAML frontmatter contains a recursive alias")
        active.add(object_id)
        try:
            return [
                _normalize_json_value(
                    item,
                    active=active,
                    depth=depth + 1,
                    max_depth=max_depth,
                    count=count,
                    max_nodes=max_nodes,
                )
                for item in value
            ]
        finally:
            active.remove(object_id)

    if type(value) is dict:
        object_id = id(value)
        if object_id in active:
            raise FrontmatterParseError("YAML frontmatter contains a recursive alias")
        active.add(object_id)
        try:
            normalized: dict[str, JsonValue] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise FrontmatterParseError("YAML mapping keys must be strings")
                normalized[key] = _normalize_json_value(
                    item,
                    active=active,
                    depth=depth + 1,
                    max_depth=max_depth,
                    count=count,
                    max_nodes=max_nodes,
                )
            return normalized
        finally:
            active.remove(object_id)

    raise FrontmatterParseError(
        f"YAML value has unsupported type {type(value).__name__!r}",
    )


def parse_markdown(
    data: bytes,
    *,
    max_document_bytes: int | None = None,
    max_frontmatter_bytes: int | None = None,
    max_yaml_depth: int | None = None,
    max_yaml_nodes: int | None = None,
) -> ParsedMarkdown:
    """Parse bounded UTF-8 Markdown with a strict YAML frontmatter mapping.

    The input must be bytes and is decoded strictly as UTF-8.  CRLF and lone
    CR line endings are normalized to LF in both the returned body and the
    YAML source.  The frontmatter limits count the normalized UTF-8 payload
    between the two delimiter lines; delimiter bytes are excluded.
    """

    if type(data) is not bytes:
        raise TypeError("parse_markdown() expects bytes")

    document_limit = _validated_limit(
        max_document_bytes,
        DEFAULT_MAX_DOCUMENT_BYTES,
        "max_document_bytes",
    )
    frontmatter_limit = _validated_limit(
        max_frontmatter_bytes,
        DEFAULT_MAX_FRONTMATTER_BYTES,
        "max_frontmatter_bytes",
    )
    yaml_depth_limit = _validated_limit(
        max_yaml_depth,
        DEFAULT_MAX_YAML_DEPTH,
        "max_yaml_depth",
    )
    yaml_node_limit = _validated_limit(
        max_yaml_nodes,
        DEFAULT_MAX_YAML_NODES,
        "max_yaml_nodes",
    )

    if len(data) > document_limit:
        raise FrontmatterParseError(
            f"Markdown document exceeds the maximum of {document_limit} bytes",
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrontmatterParseError("Markdown must be valid UTF-8") from exc

    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    yaml_text, body = _split_frontmatter(normalized_text)
    yaml_bytes = yaml_text.encode("utf-8")
    if len(yaml_bytes) > frontmatter_limit:
        raise FrontmatterParseError(
            f"YAML frontmatter exceeds the maximum of {frontmatter_limit} bytes",
        )

    loaded = _load_yaml(
        yaml_text,
        max_depth=yaml_depth_limit,
        max_nodes=yaml_node_limit,
    )
    if loaded is None and not yaml_text.strip():
        loaded = {}
    if not isinstance(loaded, Mapping) or type(loaded) is not dict:
        raise FrontmatterParseError("YAML frontmatter must be a mapping")

    normalized = _normalize_json_value(
        loaded,
        active=set(),
        depth=1,
        max_depth=yaml_depth_limit,
        count=[0],
        max_nodes=yaml_node_limit,
    )
    if type(normalized) is not dict:
        raise FrontmatterParseError("YAML frontmatter must be a mapping")

    return ParsedMarkdown(frontmatter=normalized, body=body)


def parse_frontmatter(
    data: bytes,
    *,
    max_document_bytes: int | None = None,
    max_frontmatter_bytes: int | None = None,
    max_yaml_depth: int | None = None,
    max_yaml_nodes: int | None = None,
) -> ParsedMarkdown:
    """Compatibility spelling for :func:`parse_markdown`."""

    return parse_markdown(
        data,
        max_document_bytes=max_document_bytes,
        max_frontmatter_bytes=max_frontmatter_bytes,
        max_yaml_depth=max_yaml_depth,
        max_yaml_nodes=max_yaml_nodes,
    )


parse_markdown_frontmatter = parse_frontmatter


__all__ = [
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_MAX_FRONTMATTER_BYTES",
    "DEFAULT_MAX_YAML_DEPTH",
    "DEFAULT_MAX_YAML_NODES",
    "FrontmatterParseError",
    "ParsedMarkdown",
    "parse_frontmatter",
    "parse_markdown",
    "parse_markdown_frontmatter",
]
