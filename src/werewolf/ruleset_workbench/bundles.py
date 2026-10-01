"""Closed, offline evidence bundles for the ruleset workbench.

An imported bundle is the deterministic boundary between a research provider
and the rest of the workbench.  It contains the request metadata together
with the source records and extracted claims that were produced for that
request.  This module validates structural completeness only: it does not
rank sources, infer source independence, change claim statuses, or authorize
publication.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .claims import RuleClaim
from .evidence import SourceEvidence

BOARD_NAME_MAX_LENGTH = 256
LOCALE_MAX_LENGTH = 35
REQUEST_VALUE_MAX_LENGTH = 256

BoardName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=BOARD_NAME_MAX_LENGTH,
        strip_whitespace=True,
        strict=True,
    ),
]
Locale = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=LOCALE_MAX_LENGTH,
        strip_whitespace=True,
        strict=True,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
    ),
]
RequestValue = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=REQUEST_VALUE_MAX_LENGTH,
        strip_whitespace=True,
        strict=True,
    ),
]


def _list_to_tuple(value: object) -> object:
    """Accept JSON arrays while keeping validated collections immutable."""

    if isinstance(value, list):
        return tuple(value)
    return value


def _duplicates(values: Sequence[str]) -> tuple[str, ...]:
    """Return duplicate values in their first repeated order."""

    seen: set[str] = set()
    duplicates: list[str] = []
    duplicate_set: set[str] = set()
    for value in values:
        if value in seen and value not in duplicate_set:
            duplicates.append(value)
            duplicate_set.add(value)
        seen.add(value)
    return tuple(duplicates)


class ResearchBundle(BaseModel):
    """A structurally complete offline evidence package.

    Both collections are required to be non-empty.  This deliberately keeps
    ``rules import-evidence`` from accepting a no-op package or a package that
    contains claims without the source records needed to inspect them.  A
    package can still contain conflicting, unverified, or rejected claims;
    their supplied statuses are preserved exactly for later analysis.

    The tuple collections preserve input iteration order while keeping the
    validated package immutable for deterministic lookup.  The model checks
    references when it is created, so every claim's ``evidence_ids`` points to
    one source in this same package.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    board_name: BoardName
    locale: Locale
    platform: RequestValue | None = None
    region: RequestValue | None = None
    constraints: Annotated[
        tuple[RequestValue, ...],
        BeforeValidator(_list_to_tuple),
    ] = ()
    sources: Annotated[
        tuple[SourceEvidence, ...],
        BeforeValidator(_list_to_tuple),
        Field(min_length=1),
    ]
    claims: Annotated[
        tuple[RuleClaim, ...],
        BeforeValidator(_list_to_tuple),
        Field(min_length=1),
    ]

    @model_validator(mode="after")
    def validate_collection_integrity(self) -> Self:
        """Reject duplicate IDs and references that are not closed locally."""

        source_ids = [source.source_id for source in self.sources]
        duplicate_source_ids = _duplicates(source_ids)
        if duplicate_source_ids:
            raise ValueError(
                "sources must not contain duplicate source_id values: "
                + ", ".join(duplicate_source_ids),
            )

        claim_ids = [claim.claim_id for claim in self.claims]
        duplicate_claim_ids = _duplicates(claim_ids)
        if duplicate_claim_ids:
            raise ValueError(
                "claims must not contain duplicate claim_id values: "
                + ", ".join(duplicate_claim_ids),
            )

        source_id_set = set(source_ids)
        missing_evidence_ids = sorted(
            {
                evidence_id
                for claim in self.claims
                for evidence_id in claim.evidence_ids
                if evidence_id not in source_id_set
            },
        )
        if missing_evidence_ids:
            raise ValueError(
                "claims reference evidence IDs absent from sources: "
                + ", ".join(missing_evidence_ids),
            )
        return self

    def source_by_id(self, source_id: str) -> SourceEvidence | None:
        """Return one source by logical ID, preserving package order."""

        return next(
            (source for source in self.sources if source.source_id == source_id),
            None,
        )

    def claim_by_id(self, claim_id: str) -> RuleClaim | None:
        """Return one claim by logical ID, preserving package order."""

        return next(
            (claim for claim in self.claims if claim.claim_id == claim_id),
            None,
        )


# ``EvidenceBundle`` is a descriptive compatibility alias for callers that
# use the import command's name instead of the design document's
# ``ResearchBundle`` protocol type.  Both names refer to the same strict model.
EvidenceBundle = ResearchBundle


__all__ = ["EvidenceBundle", "ResearchBundle"]
