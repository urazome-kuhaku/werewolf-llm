"""Structured models for board-specific knowledge navigation.

The models in this module cover the small, reusable pieces shared by board
documents and their reading plans.  A :class:`VersionedRef` is required for
formal published dependencies.  ``KnowledgeRef`` is deliberately a separate
logical ``kind:id`` reference used only by a ``ReadingPlan`` to point at a
topic or document that a player should read; it is not a substitute for a
versioned package dependency.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from werewolf.domain.enums import GamePhase

from .refs import KnowledgeId, VersionedRef

_LOGICAL_REF_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.ASCII)
_RULE_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.ASCII)

KnowledgeKind = Literal["board", "role", "mechanic", "interaction", "topic"]

NonEmptyText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, strip_whitespace=True, strict=True),
]


def _validate_logical_ref_id(value: str, *, field_name: str) -> str:
    """Validate an ID used for a logical navigation or claim reference."""

    if _LOGICAL_REF_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must contain only lowercase ASCII letters, digits, '.', '-' or '_',"
        )
    if value == "latest" or ".latest" in value:
        raise ValueError(f"{field_name} must not use the unpinned 'latest' reference")
    return value


def _duplicate_keys(values: list[tuple[str, str]]) -> tuple[str, ...]:
    """Return repeated logical reference keys in first-repeat order."""

    seen: set[tuple[str, str]] = set()
    duplicates: list[str] = []
    duplicate_set: set[tuple[str, str]] = set()
    for kind, identifier in values:
        key = (kind, identifier)
        if key in seen and key not in duplicate_set:
            duplicates.append(f"{kind}:{identifier}")
            duplicate_set.add(key)
        seen.add(key)
    return tuple(duplicates)


class KnowledgeRef(BaseModel):
    """A strict logical ``kind:id`` navigation reference.

    This type intentionally has no version field.  It is suitable for
    ``ReadingPlan`` recommendations, where a topic is resolved inside the
    already version-pinned board snapshot.  Formal dependencies between
    published documents must use :class:`VersionedRef` instead.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    kind: KnowledgeKind
    id: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, strict=True),
    ]

    @model_validator(mode="before")
    @classmethod
    def parse_compact_reference(cls, value: object) -> object:
        """Accept the documented compact ``kind:id`` spelling."""

        if not isinstance(value, str):
            return value
        if value.count(":") != 1:
            raise ValueError("knowledge reference must have the form 'kind:id'")
        kind, identifier = value.split(":")
        return {"kind": kind, "id": identifier}

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Reject paths, empty IDs, and unpinned aliases."""

        return _validate_logical_ref_id(value, field_name="id")

    def format(self) -> str:
        """Return the canonical compact representation."""

        return f"{self.kind}:{self.id}"

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse one compact logical reference."""

        parsed = cls.model_validate(value)
        return parsed

    def __str__(self) -> str:
        return self.format()


class BoardRoleBinding(BaseModel):
    """One role's count and board-specific effective rule overrides.

    ``override_claim_refs`` contains logical claim IDs from the board's
    workbench/published evidence.  At least one claim is required whenever
    ``effective_rules`` contains an override, so a generated value cannot be
    published without provenance.  The model validates reference shape and
    presence, while claim-to-source status remains a workbench concern.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    role_ref: VersionedRef
    count: Annotated[int, Field(gt=0)]
    effective_rules: dict[str, JsonValue]
    override_claim_refs: list[KnowledgeId]

    @field_validator("effective_rules")
    @classmethod
    def validate_effective_rules(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Require non-empty, path-free rule keys."""

        for key in value:
            if _RULE_KEY_PATTERN.fullmatch(key) is None:
                raise ValueError(
                    "effective_rules keys must be non-empty lowercase logical identifiers",
                )
            if key == "latest" or ".latest" in key:
                raise ValueError("effective_rules keys must not use 'latest'")
        return value

    @field_validator("override_claim_refs")
    @classmethod
    def validate_override_claim_refs(cls, value: list[str]) -> list[str]:
        """Require distinct, non-empty logical claim IDs."""

        validated = [
            _validate_logical_ref_id(claim_ref, field_name="override_claim_refs item")
            for claim_ref in value
        ]
        if len(validated) != len(set(validated)):
            raise ValueError("override_claim_refs must not contain duplicate claim IDs")
        return value

    @model_validator(mode="after")
    def validate_override_provenance(self) -> Self:
        """Require claim provenance for every non-empty override map."""

        if self.effective_rules and not self.override_claim_refs:
            raise ValueError(
                "effective_rules requires at least one override_claim_refs entry",
            )
        return self


class ReadingPlan(BaseModel):
    """Version-pinned board navigation and phase-specific reading hints.

    Topic references are resolved within ``board_ref``'s frozen snapshot.  The
    refs therefore intentionally carry only a strict logical ``kind:id`` pair;
    they do not grant a dependency on ``latest`` or on an arbitrary path.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    board_ref: VersionedRef
    bootstrap_topics: list[KnowledgeRef] = Field(min_length=1)
    role_required_topics: dict[
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=64, strict=True),
        ],
        list[KnowledgeRef],
    ]
    phase_topics: dict[GamePhase, list[KnowledgeRef]]
    high_risk_topics: list[KnowledgeRef]
    suggested_queries: list[NonEmptyText]

    @field_validator("role_required_topics")
    @classmethod
    def validate_role_required_topics(
        cls,
        value: dict[str, list[KnowledgeRef]],
    ) -> dict[str, list[KnowledgeRef]]:
        """Validate role keys and reject empty or repeated topic lists."""

        for role_id, topics in value.items():
            _validate_logical_ref_id(role_id, field_name="role_required_topics key")
            cls._validate_topic_list(topics, f"role_required_topics[{role_id!r}]")
        return value

    @field_validator("phase_topics", mode="before")
    @classmethod
    def normalize_phase_keys(cls, value: object) -> object:
        """Normalize YAML string keys to ``GamePhase`` and reject bad phases."""

        if not isinstance(value, dict):
            return value
        normalized: dict[GamePhase, object] = {}
        for phase, topics in value.items():
            if isinstance(phase, GamePhase):
                parsed_phase = phase
            elif isinstance(phase, str):
                try:
                    parsed_phase = GamePhase(phase)
                except ValueError as exc:
                    raise ValueError(
                        f"phase_topics contains an invalid game phase: {phase!r}"
                    ) from exc
            else:
                raise TypeError("phase_topics keys must be GamePhase values or their strings")
            if parsed_phase in normalized:
                raise ValueError(f"phase_topics contains duplicate phase: {parsed_phase.value}")
            normalized[parsed_phase] = topics
        return normalized

    @field_validator("phase_topics")
    @classmethod
    def validate_phase_topics(
        cls,
        value: dict[GamePhase, list[KnowledgeRef]],
    ) -> dict[GamePhase, list[KnowledgeRef]]:
        """Reject empty or repeated topic lists for every known phase."""

        for phase, topics in value.items():
            cls._validate_topic_list(topics, f"phase_topics[{phase.value!r}]")
        return value

    @field_validator("bootstrap_topics", "high_risk_topics")
    @classmethod
    def validate_topic_fields(cls, value: list[KnowledgeRef], info: object) -> list[KnowledgeRef]:
        """Reject repeated navigation refs in a topic list."""

        field_name = getattr(info, "field_name", "topic list")
        if field_name == "bootstrap_topics" and not value:
            raise ValueError("bootstrap_topics must contain at least one topic")
        if field_name == "high_risk_topics" and value is None:
            raise ValueError("high_risk_topics must be a list")
        cls._validate_topic_list(value, field_name)
        return value

    @field_validator("suggested_queries")
    @classmethod
    def validate_suggested_queries(cls, value: list[str]) -> list[str]:
        """Reject blank and repeated query prompts."""

        normalized = [query.strip() for query in value]
        if any(not query for query in normalized):
            raise ValueError("suggested_queries must not contain blank queries")
        if len(normalized) != len(set(normalized)):
            raise ValueError("suggested_queries must not contain duplicate queries")
        return value

    @staticmethod
    def _validate_topic_list(topics: list[KnowledgeRef], field_name: str) -> None:
        if not topics:
            raise ValueError(f"{field_name} must contain at least one topic")
        duplicates = _duplicate_keys([(topic.kind, topic.id) for topic in topics])
        if duplicates:
            raise ValueError(
                f"{field_name} contains duplicate topic references: {', '.join(duplicates)}"
            )


__all__ = ["BoardRoleBinding", "KnowledgeKind", "KnowledgeRef", "ReadingPlan"]
