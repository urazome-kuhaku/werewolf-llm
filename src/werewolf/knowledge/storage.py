"""Read-only loading of trusted knowledge Markdown documents.

This module is intentionally a syntax boundary only.  It resolves one caller
supplied relative Markdown path below a trusted root, reads bounded original
bytes, computes their digest, and parses YAML frontmatter plus the Markdown
body.  Document-specific validation and reference resolution belong to later
knowledge compilation stages.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import JsonValue

from werewolf.persistence import PathSecurityError, resolve_contained_path

from .frontmatter import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    FrontmatterParseError,
    ParsedMarkdown,
    parse_markdown,
)

DEFAULT_MAX_MARKDOWN_BYTES = DEFAULT_MAX_DOCUMENT_BYTES


class KnowledgeMarkdownPathError(PathSecurityError):
    """Raised when a requested Markdown path is not safely contained."""


class KnowledgeMarkdownTooLargeError(FrontmatterParseError):
    """Raised when a Markdown file exceeds the configured byte limit."""


@dataclass(frozen=True, slots=True)
class KnowledgeMarkdownDocument:
    """The syntax-only result of loading one trusted Markdown document."""

    relative_path: str
    parsed: ParsedMarkdown
    content_sha256: str

    @property
    def frontmatter(self) -> dict[str, JsonValue]:
        """Return the parsed YAML frontmatter mapping."""

        return self.parsed.frontmatter

    @property
    def body(self) -> str:
        """Return the normalized Markdown body."""

        return self.parsed.body

    @property
    def sha256(self) -> str:
        """Return the SHA-256 digest of the original file bytes."""

        return self.content_sha256

    @property
    def raw_sha256(self) -> str:
        """Descriptive alias for the digest of the original file bytes."""

        return self.content_sha256


# This alias makes the result's role explicit for callers that prefer the
# operation-oriented name while retaining one canonical dataclass type.
KnowledgeMarkdownLoad = KnowledgeMarkdownDocument


def _normalize_relative_markdown_path(value: str | os.PathLike[str]) -> str:
    """Validate and return a canonical POSIX relative ``.md`` path."""

    try:
        raw_value = value.as_posix() if isinstance(value, Path) else os.fspath(value)
    except TypeError as exc:
        raise KnowledgeMarkdownPathError(
            "Markdown path must be a string or path-like value",
        ) from exc
    if isinstance(raw_value, bytes):
        raise KnowledgeMarkdownPathError("Markdown path must be text")
    if not isinstance(raw_value, str) or not raw_value:
        raise KnowledgeMarkdownPathError("Markdown path must be a non-empty string")
    if "\x00" in raw_value:
        raise KnowledgeMarkdownPathError("Markdown path must not contain a NUL character")

    # Published/workbench paths use POSIX separators as their stable logical
    # representation.  Rejecting backslashes also prevents a Windows path
    # from changing meaning when it crosses hosts.
    if "\\" in raw_value or ":" in raw_value:
        raise KnowledgeMarkdownPathError(
            "Markdown path must be a drive-free relative POSIX path",
        )

    posix_path = PurePosixPath(raw_value)
    windows_path = PureWindowsPath(raw_value)
    parts = tuple(raw_value.split("/"))
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or posix_path.parts != parts
        or any(part in {"", ".", ".."} for part in parts)
        or posix_path.as_posix() != raw_value
        or posix_path.suffix != ".md"
    ):
        raise KnowledgeMarkdownPathError(
            "Markdown path must be a normalized relative .md path",
        )
    return raw_value


def _read_and_parse(path: Path, max_document_bytes: int) -> tuple[ParsedMarkdown, str]:
    """Read, hash, and parse one file in a worker thread."""

    with path.open("rb") as markdown_file:
        data = markdown_file.read(max_document_bytes + 1)
    if len(data) > max_document_bytes:
        raise KnowledgeMarkdownTooLargeError(
            f"Markdown document exceeds the maximum of {max_document_bytes} bytes",
        )
    parsed = parse_markdown(data, max_document_bytes=max_document_bytes)
    return parsed, hashlib.sha256(data).hexdigest()


class KnowledgeMarkdownStore:
    """Load Markdown documents below one trusted root without writing files."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_document_bytes: int = DEFAULT_MAX_MARKDOWN_BYTES,
    ) -> None:
        if type(max_document_bytes) is not int or max_document_bytes <= 0:
            raise ValueError("max_document_bytes must be a positive integer")
        self._root = root
        self._max_document_bytes = max_document_bytes

    async def load(self, relative_path: str | os.PathLike[str]) -> KnowledgeMarkdownDocument:
        """Load one normalized, contained Markdown document asynchronously.

        The path is validated before it is joined to the trusted root.  The
        containment resolver follows existing symlinks and rejects a resolved
        target outside that root.  Frontmatter values are only parsed here;
        they never influence the file path.
        """

        normalized_path = _normalize_relative_markdown_path(relative_path)
        try:
            resolved_path = resolve_contained_path(self._root, normalized_path)
        except PathSecurityError as exc:
            raise KnowledgeMarkdownPathError(
                "Markdown path is outside the trusted knowledge root",
            ) from exc

        parsed, digest = await asyncio.to_thread(
            _read_and_parse,
            resolved_path,
            self._max_document_bytes,
        )
        return KnowledgeMarkdownDocument(
            relative_path=normalized_path,
            parsed=parsed,
            content_sha256=digest,
        )


__all__ = [
    "DEFAULT_MAX_MARKDOWN_BYTES",
    "KnowledgeMarkdownDocument",
    "KnowledgeMarkdownLoad",
    "KnowledgeMarkdownPathError",
    "KnowledgeMarkdownStore",
    "KnowledgeMarkdownTooLargeError",
]
