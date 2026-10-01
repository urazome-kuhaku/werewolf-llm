"""Validated atomic rule claims for the ruleset research workbench.

Claims are deliberately small, source-linked records.  This model validates
the shape and references of one claim; source credibility, conflict
resolution, extraction, and publication remain responsibilities of the
workbench workflow.
"""

from __future__ import annotations

import math
import re
from enum import StrEnum, unique
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator

from werewolf.knowledge.refs import KnowledgeId

CLAIM_KEY_MAX_LENGTH = 128

_LOGICAL_ID_PATTERN = re.compile(r"[a-z0-9_-]+", re.ASCII)
_KEY_SEGMENT = r"[a-z](?:[a-z0-9]*)(?:_[a-z0-9]+)*"
_CLAIM_KEY_PATTERN = re.compile(
    rf"{_KEY_SEGMENT}(?:\.{_KEY_SEGMENT})+",
    re.ASCII,
)


@unique
class ClaimScope(StrEnum):
    """The ruleset dimension to which an atomic claim belongs."""

    BOARD = "BOARD"
    ROLE = "ROLE"
    MECHANIC = "MECHANIC"
    INTERACTION = "INTERACTION"


@unique
class ClaimStatus(StrEnum):
    """Research status recorded for one claim."""

    SUPPORTED = "SUPPORTED"
    CONFLICTING = "CONFLICTING"
    UNVERIFIED = "UNVERIFIED"
    REJECTED = "REJECTED"


class RuleClaim(BaseModel):
    """One source-linked, atomic rule assertion.

    ``evidence_ids`` are logical source identifiers, not URLs or filesystem
    paths.  This model does not rank sources and cannot by itself authorize a
    claim for publication; callers must apply the research and release gates.
    """

    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    claim_id: KnowledgeId
    ruleset_candidate_id: KnowledgeId
    key: Annotated[
        str,
        StringConstraints(
            min_length=3,
            max_length=CLAIM_KEY_MAX_LENGTH,
            strict=True,
        ),
    ]
    value: JsonValue
    scope: ClaimScope
    conditions: dict[str, JsonValue]
    evidence_ids: list[KnowledgeId]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    extraction_note: str | None = None
    status: ClaimStatus

    @field_validator("claim_id", "ruleset_candidate_id")
    @classmethod
    def validate_logical_id(cls, value: str) -> str:
        """Keep claim identifiers independent of URLs and filesystem paths."""

        if _LOGICAL_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "identifier must contain only lowercase ASCII letters, digits, '-' or '_'",
            )
        return value

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        """Require a dotted key made of lowercase snake_case segments."""

        if _CLAIM_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "key must contain at least two lowercase snake_case segments separated by '.'",
            )
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        """Require a non-empty set of logical, distinct evidence references."""

        if not value:
            raise ValueError("evidence_ids must contain at least one source identifier")
        if any(_LOGICAL_ID_PATTERN.fullmatch(evidence_id) is None for evidence_id in value):
            raise ValueError(
                "evidence_ids must contain only lowercase ASCII letters, digits, '-' or '_',",
            )
        if len(value) != len(set(value)):
            raise ValueError("evidence_ids must not contain duplicate source identifiers")
        return value

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        """Reject NaN and infinities before the inclusive range is persisted."""

        if not math.isfinite(value):
            raise ValueError("confidence must be finite")
        return value


__all__ = ["ClaimScope", "ClaimStatus", "RuleClaim"]
