"""Pure Markdown section extraction for published knowledge documents.

Knowledge documents use explicit anchors on their level-two headings, for
example ``## 胜负条件 {#victory}``.  The anchor is the persisted identity of
the section.  Titles are presentation text and are deliberately never used
to derive an identifier; this keeps Chinese titles and later copy edits from
changing references such as ``board:victory``.

This module only operates on an in-memory string.  Frontmatter and filesystem
loading belong to the surrounding knowledge pipeline.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, overload

SECTION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[a-z](?:[a-z0-9]*)(?:_[a-z0-9]+)*",
    re.ASCII,
)
INTRO_SECTION_ID: Final[str] = "intro"


class SectionParseError(ValueError):
    """Raised when Markdown does not satisfy the section contract."""


class InvalidSectionIdError(SectionParseError):
    """Raised when an explicit heading anchor is malformed."""


class DuplicateSectionIdError(SectionParseError):
    """Raised when two headings declare the same explicit ID."""


class MissingSectionIdError(SectionParseError):
    """Raised when a required level-two heading has no explicit ID."""


class SectionNotFoundError(KeyError):
    """Raised when :meth:`MarkdownDocument.require` cannot resolve a ref."""


@dataclass(frozen=True, slots=True)
class MarkdownSection:
    """One stable section extracted from a Markdown document.

    ``body`` contains the Markdown between this level-two heading and the
    next level-two heading.  Nested headings remain in that body because the
    published navigation contract is the level-two section ID.  ``order`` is
    the zero-based position in the returned document and is deterministic for
    a fixed normalized input.
    """

    id: str
    title: str
    body: str
    level: int = 2
    order: int = 0

    @property
    def section_id(self) -> str:
        """Compatibility spelling for callers that name the field explicitly."""

        return self.id

    @property
    def content(self) -> str:
        """Return the body under the common ``content`` spelling."""

        return self.body


@dataclass(frozen=True, slots=True)
class MarkdownDocument(Sequence[MarkdownSection]):
    """An immutable, ordered section collection with reference resolution."""

    sections: tuple[MarkdownSection, ...]
    title: str | None = None
    normalized_markdown: str = ""
    _by_id: Mapping[str, MarkdownSection] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        ids = tuple(section.id for section in self.sections)
        if len(ids) != len(set(ids)):
            raise ValueError("MarkdownDocument cannot contain duplicate section IDs")
        if any(not isinstance(section, MarkdownSection) for section in self.sections):
            raise TypeError("sections must contain MarkdownSection values")

        object.__setattr__(self, "_by_id", MappingProxyType(dict(zip(ids, self.sections))))

    def __len__(self) -> int:
        return len(self.sections)

    @overload
    def __getitem__(self, index: int) -> MarkdownSection: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[MarkdownSection, ...]: ...

    def __getitem__(self, index: int | slice) -> MarkdownSection | tuple[MarkdownSection, ...]:
        return self.sections[index]

    def __iter__(self) -> Iterator[MarkdownSection]:
        return iter(self.sections)

    @property
    def section_ids(self) -> tuple[str, ...]:
        """Return IDs in source order, suitable for deterministic manifests."""

        return tuple(section.id for section in self.sections)

    def resolve(
        self,
        reference: str | object,
        *,
        expected_kind: str = "board",
    ) -> MarkdownSection | None:
        """Resolve a direct ID or a compact ``kind:id`` reading reference.

        ``ReadingPlan`` stores references as ``KnowledgeRef`` instances, while
        YAML and API boundaries commonly use strings.  Supporting both forms
        here keeps validation independent of a package loader.  A direct ID is
        interpreted in ``expected_kind``; a typed reference with another kind
        is not a board section and returns ``None``.
        """

        section_id: object
        kind: object
        if isinstance(reference, str):
            if reference.count(":") == 0:
                kind, section_id = expected_kind, reference
            elif reference.count(":") == 1:
                kind, section_id = reference.split(":")
            else:
                return None
        else:
            kind = getattr(reference, "kind", None)
            section_id = getattr(reference, "id", None)

        if kind != expected_kind or not isinstance(section_id, str):
            return None
        if SECTION_ID_PATTERN.fullmatch(section_id) is None:
            return None
        return self._by_id.get(section_id)

    def require(
        self,
        reference: str | object,
        *,
        expected_kind: str = "board",
    ) -> MarkdownSection:
        """Resolve one reference or raise a clear validation-friendly error."""

        section = self.resolve(reference, expected_kind=expected_kind)
        if section is None:
            raise SectionNotFoundError(f"unknown Markdown section reference: {reference!r}")
        return section

    # The explicit alias reads naturally at compiler call sites.
    resolve_reference = resolve


@dataclass(frozen=True, slots=True)
class _Heading:
    """Internal heading location in normalized source lines."""

    line: int
    level: int
    title: str
    section_id: str | None


_ATX_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<marks>#{1,6})(?:[ \t]+(?P<text>.*?)|[ \t]*)[ \t]*$",
)
_ANCHOR_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<title>.*?)[ \t]+\{#(?P<id>[^{}]*)\}[ \t]*(?:#+[ \t]*)?$",
)
_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]{0,3}(?P<mark>`{3,}|~{3,})")


def _validate_section_id(value: str, *, context: str) -> str:
    """Validate the lowercase snake_case ID used by a heading anchor."""

    if SECTION_ID_PATTERN.fullmatch(value) is None:
        raise InvalidSectionIdError(
            f"{context} must be a lowercase snake_case identifier: {value!r}",
        )
    if len(value) > 64:
        raise InvalidSectionIdError(f"{context} exceeds the maximum length of 64")
    return value


def _parse_heading(line: str, line_number: int) -> tuple[int, str, str | None] | None:
    """Parse one ATX heading, including and validating an optional anchor."""

    match = _ATX_HEADING_RE.fullmatch(line.rstrip("\n"))
    if match is None:
        return None

    level = len(match.group("marks"))
    raw_title = match.group("text").strip(" \t")
    explicit_anchor = "{#" in raw_title
    anchor_match = _ANCHOR_RE.fullmatch(raw_title)
    if explicit_anchor and anchor_match is None:
        raise InvalidSectionIdError(
            f"malformed explicit section anchor on heading at line {line_number}",
        )

    if anchor_match is None:
        title = re.sub(r"[ \t]+#+[ \t]*$", "", raw_title).rstrip(" \t")
        return level, title, None

    title = anchor_match.group("title").rstrip(" \t")
    if not title:
        raise InvalidSectionIdError(
            f"heading at line {line_number} must have a title before its anchor",
        )
    section_id = _validate_section_id(
        anchor_match.group("id"),
        context=f"section anchor on line {line_number}",
    )
    return level, title, section_id


def _headings(lines: Sequence[str]) -> tuple[_Heading, ...]:
    """Find headings outside fenced code blocks in normalized lines."""

    result: list[_Heading] = []
    fence_mark: str | None = None
    for line_number, line in enumerate(lines, start=1):
        fence = _FENCE_RE.match(line)
        if fence is not None:
            mark = fence.group("mark")
            if fence_mark is None:
                fence_mark = mark[0] * len(mark)
            elif mark[0] == fence_mark[0] and len(mark) >= len(fence_mark):
                fence_mark = None
            continue
        if fence_mark is not None:
            continue

        parsed = _parse_heading(line, line_number)
        if parsed is None:
            continue
        level, title, section_id = parsed
        result.append(_Heading(line_number - 1, level, title, section_id))
    return tuple(result)


def _trim_body(value: str) -> str:
    """Drop structural blank lines while preserving all Markdown content."""

    return value.strip("\n")


def _intro_body(lines: Sequence[str], headings: Sequence[_Heading], end: int) -> str:
    """Return pre-section prose, excluding a document title heading."""

    title_lines = {
        heading.line for heading in headings if heading.level == 1 and heading.line < end
    }
    return _trim_body(
        "".join(line for index, line in enumerate(lines[:end]) if index not in title_lines)
    )


def extract_sections(
    markdown: str,
    *,
    required_ids: Iterable[str] = (),
    include_intro: bool = True,
) -> MarkdownDocument:
    """Extract stable level-two Markdown sections from normalized source.

    Every level-two heading must carry a terminal ``{#lowercase_snake_case}``
    anchor.  Level-one headings are document titles and level-three-or-deeper
    headings stay inside their containing section body.  An optional preamble
    becomes the reserved ``intro`` section, which is useful when a document
    has explanatory prose before its first anchored heading.

    ``required_ids`` adds document-type requirements such as ``overview`` and
    ``victory``.  Missing IDs are reported after parsing so callers can use
    the same function for board and non-board documents.  Line endings are
    normalized to LF before any slicing; no path or title is consulted when
    constructing IDs.
    """

    if not isinstance(markdown, str):
        raise TypeError("extract_sections() expects a string")
    if not isinstance(include_intro, bool):
        raise TypeError("include_intro must be a boolean")

    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines(keepends=True)
    headings = _headings(lines)

    explicit_ids: dict[str, int] = {}
    for heading in headings:
        if heading.section_id is None:
            continue
        previous_line = explicit_ids.get(heading.section_id)
        if previous_line is not None:
            raise DuplicateSectionIdError(
                f"duplicate heading section ID {heading.section_id!r} "
                f"on lines {previous_line + 1} and {heading.line + 1}",
            )
        explicit_ids[heading.section_id] = heading.line

    level_two = tuple(heading for heading in headings if heading.level == 2)
    for heading in level_two:
        if heading.section_id is None:
            raise MissingSectionIdError(
                f"level-2 heading {heading.title!r} on line {heading.line + 1} "
                "requires an explicit {#section_id} anchor",
            )

    required = _required_ids(required_ids)
    if include_intro and _intro_body(
        lines, headings, level_two[0].line if level_two else len(lines)
    ):
        if INTRO_SECTION_ID in explicit_ids:
            raise DuplicateSectionIdError(
                "reserved intro section ID conflicts with an explicit heading anchor",
            )

    sections: list[MarkdownSection] = []
    first_section_line = level_two[0].line if level_two else len(lines)
    if include_intro:
        intro = _intro_body(lines, headings, first_section_line)
        if intro:
            sections.append(
                MarkdownSection(
                    id=INTRO_SECTION_ID,
                    title="",
                    body=intro,
                    level=0,
                    order=len(sections),
                ),
            )

    for index, heading in enumerate(level_two):
        body_start = heading.line + 1
        body_end = level_two[index + 1].line if index + 1 < len(level_two) else len(lines)
        if heading.section_id is None:  # guarded above; keeps type checkers narrow
            continue
        sections.append(
            MarkdownSection(
                id=heading.section_id,
                title=heading.title,
                body=_trim_body("".join(lines[body_start:body_end])),
                level=heading.level,
                order=len(sections),
            ),
        )

    available = {section.id for section in sections}
    missing = tuple(section_id for section_id in required if section_id not in available)
    if missing:
        raise SectionParseError(
            "missing required section IDs: "
            + ", ".join(repr(section_id) for section_id in missing),
        )

    document_title = next(
        (heading.title for heading in headings if heading.level == 1 and heading.title),
        None,
    )
    return MarkdownDocument(
        sections=tuple(sections),
        title=document_title,
        normalized_markdown=normalized,
    )


def _required_ids(values: Iterable[str]) -> tuple[str, ...]:
    """Validate and freeze required IDs while preserving caller order."""

    if isinstance(values, str):
        values = (values,)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("required_ids entries must be strings")
        section_id = _validate_section_id(value, context="required section ID")
        if section_id not in seen:
            result.append(section_id)
            seen.add(section_id)
    return tuple(result)


# Naming aliases keep adapters readable while retaining one implementation.
parse_sections = extract_sections
parse_markdown_sections = extract_sections
split_sections = extract_sections


__all__ = [
    "DuplicateSectionIdError",
    "INTRO_SECTION_ID",
    "InvalidSectionIdError",
    "MarkdownDocument",
    "MarkdownSection",
    "MissingSectionIdError",
    "SECTION_ID_PATTERN",
    "SectionNotFoundError",
    "SectionParseError",
    "extract_sections",
    "parse_markdown_sections",
    "parse_sections",
    "split_sections",
]
