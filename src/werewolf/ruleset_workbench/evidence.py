"""Validated source records for the ruleset research workbench.

The workbench stores source metadata separately from extracted rule claims.  A
source class is a fact used during conflict analysis; it is not an automatic
authority ranking and must not be treated as one by callers.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, StringConstraints, field_validator

from werewolf.knowledge.refs import KnowledgeId

TITLE_MAX_LENGTH = 512
PUBLISHER_MAX_LENGTH = 256
EXCERPT_MAX_LENGTH = 20_000
RETRIEVAL_METHOD_MAX_LENGTH = 64

_SOURCE_ID_PATTERN = re.compile(r"[a-z0-9_-]+", re.ASCII)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)


@unique
class SourceClass(StrEnum):
    """Classification used as one input to source conflict analysis.

    The values are stable uppercase strings for persisted records.  The class
    intentionally does not encode a universal authority order: applicability,
    version, and context are evaluated by the research workflow.
    """

    OFFICIAL_EVENT = "OFFICIAL_EVENT"
    PLATFORM_RULES = "PLATFORM_RULES"
    MATURE_RULE_GUIDE = "MATURE_RULE_GUIDE"
    COMMUNITY = "COMMUNITY"
    STRATEGY_GUIDE = "STRATEGY_GUIDE"
    OTHER = "OTHER"


class SourceEvidence(BaseModel):
    """A bounded, reproducible record of one researched source.

    ``source_id`` is a logical knowledge identifier.  It deliberately cannot
    contain URL or filesystem path syntax, so storage locations may change
    without changing references to the evidence.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal[1] = 1
    source_id: KnowledgeId
    url: AnyHttpUrl
    title: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=TITLE_MAX_LENGTH,
            strip_whitespace=True,
            strict=True,
        ),
    ]
    publisher: (
        Annotated[
            str,
            StringConstraints(
                min_length=1,
                max_length=PUBLISHER_MAX_LENGTH,
                strip_whitespace=True,
                strict=True,
            ),
        ]
        | None
    ) = None
    source_class: SourceClass
    published_at: datetime | None = None
    fetched_at: datetime
    content_sha256: Annotated[str, StringConstraints(strict=True)]
    excerpt: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=EXCERPT_MAX_LENGTH,
            strip_whitespace=True,
            strict=True,
        ),
    ]
    retrieval_method: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=RETRIEVAL_METHOD_MAX_LENGTH,
            strip_whitespace=True,
            strict=True,
        ),
    ]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        """Keep source IDs logical and independent of URL or filesystem paths."""

        if _SOURCE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "source_id must contain only lowercase ASCII letters, digits, '-' or '_'",
            )
        return value

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Allow only HTTP(S) sources, even if URL support expands later."""

        if value.scheme not in {"http", "https"}:
            raise ValueError("url must use the http or https scheme")
        if value.username is not None or value.password is not None:
            raise ValueError("url must not contain username or password")
        return value

    @field_validator("published_at", "fetched_at")
    @classmethod
    def validate_utc_timestamp(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        """Require an aware timestamp whose UTC offset is zero."""

        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        if value.utcoffset() != timedelta(0):
            raise ValueError("timestamps must be expressed in UTC")
        return value

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        """Require the canonical 32-byte SHA-256 hexadecimal representation."""

        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "content_sha256 must be exactly 64 lowercase hexadecimal characters",
            )
        return value
