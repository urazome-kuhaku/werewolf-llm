"""Build bounded source evidence from one fetched research document.

The fetched page is untrusted data. This module only records transport
metadata, hashes the complete body, and selects a contiguous excerpt. It does
not interpret page text or turn page instructions into workbench instructions.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import AnyHttpUrl

from .evidence import (
    EXCERPT_MAX_LENGTH,
    PUBLISHER_MAX_LENGTH,
    RETRIEVAL_METHOD_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    SourceClass,
    SourceEvidence,
)
from .research_provider import FetchedDocument

DEFAULT_EXCERPT_MAX_LENGTH: Final = 4_000
"""Maximum number of characters retained by default for one excerpt."""

SOURCE_ID_PREFIX: Final = "source-"
SOURCE_ID_HEX_LENGTH: Final = 56
MAX_FOCUS_MARKER_LENGTH: Final = EXCERPT_MAX_LENGTH


class EvidenceBuilderError(ValueError):
    """Base error for invalid evidence-building inputs."""


class FocusMarkerNotFoundError(EvidenceBuilderError):
    """The requested exact marker does not occur in the fetched body."""


class EvidenceBoundsError(EvidenceBuilderError):
    """A requested evidence field or excerpt exceeds a safe bound."""


def content_sha256(body: str) -> str:
    """Return the SHA-256 digest of the exact UTF-8 fetched body."""

    if not isinstance(body, str):
        raise TypeError("body must be text")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def stable_source_id(url: str | AnyHttpUrl, body: str) -> str:
    """Derive a bounded logical ID from the exact URL and fetched body.

    A separator prevents concatenation ambiguity. The 224-bit truncated
    digest fits SourceEvidence's 64-character logical-ID limit while remaining
    independent of filenames.
    """

    if not isinstance(body, str):
        raise TypeError("body must be text")
    url_text = str(url)
    digest = hashlib.sha256(
        url_text.encode("utf-8") + b"\x00" + body.encode("utf-8"),
    ).hexdigest()
    return f"{SOURCE_ID_PREFIX}{digest[:SOURCE_ID_HEX_LENGTH]}"


def build_source_evidence(
    document: FetchedDocument,
    *,
    source_class: SourceClass,
    publisher: str | None,
    retrieval_method: str,
    fetched_at: datetime | None = None,
    title: str | None = None,
    published_at: datetime | None = None,
    focus_marker: str | None = None,
    max_excerpt_length: int = DEFAULT_EXCERPT_MAX_LENGTH,
) -> SourceEvidence:
    """Convert one validated document into immutable, bounded source evidence.

    ``focus_marker`` is matched literally and case-sensitively. When present,
    the marker must occur in the returned excerpt; a missing marker raises
    :class:`FocusMarkerNotFoundError` instead of selecting an unrelated board
    from a multi-board page. The body is never normalized before hashing.
    """

    if not isinstance(document, FetchedDocument):
        raise TypeError("document must be a validated FetchedDocument")
    _validate_excerpt_bound(max_excerpt_length)
    _validate_metadata(title, publisher, retrieval_method)
    marker = _validate_focus_marker(focus_marker, max_excerpt_length)

    body = document.body
    excerpt = _select_excerpt(body, marker=marker, max_length=max_excerpt_length)
    if marker is not None and marker not in excerpt:
        # This guards against SourceEvidence's deliberate boundary whitespace
        # trimming changing the exact marker we selected.
        raise EvidenceBuilderError("focus marker cannot be preserved in excerpt")

    fetched_timestamp = _utc_timestamp(fetched_at or datetime.now(UTC), "fetched_at")
    published_timestamp = _utc_published_at(published_at)
    resolved_title = title if title is not None else document.title
    if resolved_title is None:
        resolved_title = "Fetched document"

    return SourceEvidence(
        source_id=stable_source_id(document.url, body),
        url=document.url,
        title=resolved_title,
        publisher=publisher,
        source_class=source_class,
        published_at=published_timestamp,
        fetched_at=fetched_timestamp,
        content_sha256=content_sha256(body),
        excerpt=excerpt,
        retrieval_method=retrieval_method,
    )


def _validate_excerpt_bound(max_excerpt_length: int) -> None:
    if isinstance(max_excerpt_length, bool) or not isinstance(max_excerpt_length, int):
        raise EvidenceBoundsError("max_excerpt_length must be an integer")
    if not 1 <= max_excerpt_length <= EXCERPT_MAX_LENGTH:
        raise EvidenceBoundsError(
            f"max_excerpt_length must be between 1 and {EXCERPT_MAX_LENGTH}",
        )


def _validate_metadata(
    title: str | None,
    publisher: str | None,
    retrieval_method: str,
) -> None:
    _validate_bounded_text(title, "title", TITLE_MAX_LENGTH, optional=True)
    _validate_bounded_text(publisher, "publisher", PUBLISHER_MAX_LENGTH, optional=True)
    _validate_bounded_text(
        retrieval_method,
        "retrieval_method",
        RETRIEVAL_METHOD_MAX_LENGTH,
    )


def _validate_bounded_text(
    value: str | None,
    name: str,
    maximum: int,
    *,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str):
        raise EvidenceBoundsError(f"{name} must be text")
    if not value.strip():
        raise EvidenceBoundsError(f"{name} must not be empty")
    if len(value) > maximum:
        raise EvidenceBoundsError(f"{name} exceeds its maximum length")


def _validate_focus_marker(marker: str | None, max_excerpt_length: int) -> str | None:
    if marker is None:
        return None
    if not isinstance(marker, str):
        raise EvidenceBoundsError("focus_marker must be text")
    if not marker or marker != marker.strip():
        raise EvidenceBoundsError("focus_marker must be non-empty without boundary whitespace")
    if len(marker) > MAX_FOCUS_MARKER_LENGTH:
        raise EvidenceBoundsError("focus_marker exceeds its maximum length")
    if len(marker) > max_excerpt_length:
        raise EvidenceBoundsError("focus_marker cannot fit within max_excerpt_length")
    return marker


def _select_excerpt(body: str, *, marker: str | None, max_length: int) -> str:
    if marker is None:
        first_non_whitespace = next(
            (index for index, character in enumerate(body) if not character.isspace()),
            None,
        )
        if first_non_whitespace is None:
            # FetchedDocument already rejects this, but keep this function safe
            # if it is reused with a less strict document type later.
            raise EvidenceBuilderError("document body must contain non-whitespace text")
        start = first_non_whitespace
        end = min(len(body), start + max_length)
    else:
        marker_start = body.find(marker)
        if marker_start < 0:
            raise FocusMarkerNotFoundError("focus marker was not found in document body")
        marker_end = marker_start + len(marker)
        context = max_length - len(marker)
        left = context // 2
        start = max(0, marker_start - left)
        end = min(len(body), start + max_length)
        if end - start < max_length:
            start = max(0, end - max_length)
        if start > marker_start or end < marker_end:
            start = max(0, marker_end - max_length)
            end = min(len(body), start + max_length)

    while start < end and body[start].isspace():
        start += 1
    while end > start and body[end - 1].isspace():
        end -= 1
    excerpt = body[start:end]
    if not excerpt:
        raise EvidenceBuilderError("document body has no bounded non-whitespace excerpt")
    return excerpt


def _utc_timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceBoundsError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise EvidenceBoundsError(f"{field_name} must be expressed in UTC")
    return value.astimezone(UTC)


def _utc_published_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _utc_timestamp(value, "published_at")


__all__ = [
    "DEFAULT_EXCERPT_MAX_LENGTH",
    "EvidenceBoundsError",
    "EvidenceBuilderError",
    "FocusMarkerNotFoundError",
    "MAX_FOCUS_MARKER_LENGTH",
    "SOURCE_ID_HEX_LENGTH",
    "SOURCE_ID_PREFIX",
    "build_source_evidence",
    "content_sha256",
    "stable_source_id",
]
