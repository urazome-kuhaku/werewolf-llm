"""Stable, immutable references to versioned knowledge documents."""

from __future__ import annotations

import re
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

ID_MAX_LENGTH = 64
VERSION_MAX_LENGTH = 64

_ID_PATTERN = re.compile(r"[a-z0-9_-]+", re.ASCII)
_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
    re.ASCII,
)

KnowledgeId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=ID_MAX_LENGTH, strict=True),
]
SemanticVersion = Annotated[
    str,
    StringConstraints(min_length=5, max_length=VERSION_MAX_LENGTH, strict=True),
]


class VersionedRef(BaseModel):
    """A stable ``id@version`` reference to an immutable knowledge document.

    IDs are logical identifiers and are deliberately narrower than file names.
    They may be moved between storage locations without changing a reference.
    The version is the three-component SemVer core used by published knowledge;
    prerelease and build metadata are excluded so references remain unambiguous.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: KnowledgeId
    version: SemanticVersion

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Reject path syntax, whitespace, and non-ASCII identifier characters."""

        if _ID_PATTERN.fullmatch(value) is None:
            raise ValueError("id must contain only lowercase ASCII letters, digits, '-' or '_'")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        """Require a plain X.Y.Z semantic version without leading zeroes."""

        if _VERSION_PATTERN.fullmatch(value) is None:
            raise ValueError("version must be a plain semantic version in X.Y.Z form")
        return value

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse one stable ``id@version`` reference.

        Splitting is intentionally strict: an ID or version containing another
        ``@`` is rejected before Pydantic validates the individual components.
        """

        if not isinstance(value, str):
            raise TypeError("VersionedRef.parse() expects a string")
        if value.count("@") != 1:
            raise ValueError("reference must have the form 'id@version'")

        ref_id, version = value.split("@")
        return cls(id=ref_id, version=version)

    def format(self) -> str:
        """Return the canonical ``id@version`` representation."""

        return f"{self.id}@{self.version}"

    def __str__(self) -> str:
        return self.format()
