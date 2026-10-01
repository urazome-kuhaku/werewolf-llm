"""Read-only runtime queries over one compiled knowledge package.

The service is deliberately small: a caller supplies a trusted
``QueryContext`` and this module can only resolve documents present in the
already compiled package.  It has no filesystem access, no network access,
and no notion of a player's secret role or current game state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .compiler import CompiledKnowledgePackage, EffectiveRoleProfile
from .indexes import KnowledgeIndexDocument, KnowledgeKind, normalize_text

SERVICE_SCHEMA_VERSION = 1
DEFAULT_RESULT_LIMIT = 8
MAX_RESULT_BYTES = 64 * 1024
MAX_QUERY_LENGTH = 512
MAX_INTERACTION_HINTS = 4

ResultDocumentKind = Literal["board", "role", "mechanic", "interaction", "topic"]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class QueryContext(_StrictFrozenModel):
    """Trusted game/session binding created by the Gateway.

    The service accepts this object as an already authenticated binding.  It
    never parses a client supplied snapshot path or game identifier.
    """

    game_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    seat: int
    session_epoch: int


class SearchQuery(_StrictFrozenModel):
    """Bounded lexical search request for the current package only."""

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    kinds: tuple[KnowledgeKind, ...] | None = None
    limit: int = Field(default=DEFAULT_RESULT_LIMIT, ge=1, le=DEFAULT_RESULT_LIMIT)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @field_validator("kinds")
    @classmethod
    def validate_kinds(
        cls,
        value: tuple[KnowledgeKind, ...] | None,
    ) -> tuple[KnowledgeKind, ...] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("kinds must not contain duplicates")
        return value


class SnapshotBinding(_StrictFrozenModel):
    id: str
    board: str


class KnowledgeSection(_StrictFrozenModel):
    section_id: str
    title: str
    content: str


class KnowledgeCitation(_StrictFrozenModel):
    source_id: str
    title: str
    applies_to: tuple[str, ...]


class KnowledgeReference(_StrictFrozenModel):
    kind: ResultDocumentKind
    id: str


class KnowledgeDocument(_StrictFrozenModel):
    kind: ResultDocumentKind
    id: str
    version: str
    title: str
    summary: str
    sections: tuple[KnowledgeSection, ...]
    effective_rules: dict[str, Any] = Field(default_factory=dict)


class KnowledgeResult(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    result_id: str
    receipt_id: str
    snapshot: SnapshotBinding
    status: Literal["ok"] = "ok"
    document: KnowledgeDocument
    citations: tuple[KnowledgeCitation, ...]
    recommended_next_reads: tuple[KnowledgeReference, ...]
    truncated: bool = False


class KnowledgeSearchHit(_StrictFrozenModel):
    kind: ResultDocumentKind
    id: str
    version: str
    title: str
    summary: str
    score: float
    matched_terms: tuple[str, ...]
    citations: tuple[KnowledgeCitation, ...]

    @property
    def ref(self) -> str:
        """Return the canonical document reference used by callers."""

        return f"{self.kind}:{self.id}@{self.version}"


class KnowledgeSearchResult(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    result_id: str
    receipt_id: str
    snapshot: SnapshotBinding
    status: Literal["ok"] = "ok"
    query: str
    hits: tuple[KnowledgeSearchHit, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeInteractionSuggestion:
    """A public, current-snapshot hint for repairing an exact query.

    Suggestions contain only logical interaction keys from the already
    authorized package.  They never contain a game ID, seat, token, or any
    runtime state.  Keeping this as a small immutable value also prevents an
    error response from accidentally exposing the full interaction body.
    """

    canonical_ref: str
    subjects: tuple[str, ...]
    situation_key: str


class KnowledgeServiceError(ValueError):
    """Base error with a stable machine-readable error code."""

    code: ClassVar[str] = "KNOWLEDGE_ERROR"

    def __init__(self, message: str, *, candidates: Iterable[str] = ()) -> None:
        self.message = message
        self.candidates = tuple(candidates)
        suffix = f"; candidates={self.candidates!r}" if self.candidates else ""
        super().__init__(f"{self.code}: {message}{suffix}")


class KnowledgeNotFoundError(KnowledgeServiceError):
    code = "NOT_FOUND"


class KnowledgeInteractionNotFoundError(KnowledgeNotFoundError):
    """An exact interaction miss with bounded repair hints."""

    def __init__(
        self,
        message: str,
        *,
        suggestions: Iterable[KnowledgeInteractionSuggestion] = (),
    ) -> None:
        values = tuple(suggestions)[:MAX_INTERACTION_HINTS]
        if any(not isinstance(value, KnowledgeInteractionSuggestion) for value in values):
            raise TypeError("suggestions must contain KnowledgeInteractionSuggestion values")
        self.suggestions = values
        super().__init__(message, candidates=(value.canonical_ref for value in values))


class KnowledgeAmbiguousError(KnowledgeServiceError):
    code = "AMBIGUOUS"


class KnowledgeForbiddenError(KnowledgeServiceError):
    code = "FORBIDDEN"


class KnowledgeIntegrityError(KnowledgeServiceError):
    code = "KNOWLEDGE_INTEGRITY_ERROR"


class KnowledgeInvalidQueryError(KnowledgeServiceError):
    code = "INVALID_QUERY"


# This alias is useful to adapters that want one query-facing exception type.
KnowledgeQueryError = KnowledgeServiceError


@dataclass(frozen=True, slots=True)
class _ResolvedDocument:
    record: KnowledgeIndexDocument
    profile: EffectiveRoleProfile | None = None


def _canonical_json(value: object) -> str:
    """Serialize result identity data without retaining mutable package data."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class KnowledgeService:
    """Serve bounded read-only queries from one immutable compiled package."""

    def __init__(
        self,
        package: CompiledKnowledgePackage,
        *,
        snapshot_id: str | None = None,
        max_result_bytes: int = MAX_RESULT_BYTES,
    ) -> None:
        if not isinstance(package, CompiledKnowledgePackage):
            raise TypeError("package must be a CompiledKnowledgePackage")
        if snapshot_id is not None and (not isinstance(snapshot_id, str) or not snapshot_id):
            raise ValueError("snapshot_id must be a non-empty string")
        if not isinstance(max_result_bytes, int) or max_result_bytes < 1024:
            raise ValueError("max_result_bytes must be at least 1024")
        self._package = package
        self._snapshot_id = snapshot_id or package.package_identity
        self._max_result_bytes = min(max_result_bytes, MAX_RESULT_BYTES)
        self._snapshot = SnapshotBinding(
            id=self._snapshot_id,
            board=package.board_ref.format(),
        )
        self._validate_package()

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def board_ref(self) -> str:
        return self._package.board_ref.format()

    def get_board(self, context: QueryContext) -> KnowledgeResult:
        self._validate_context(context)
        board = self._board_record()
        return self._result_for(context, "get_board", board)

    def get_role(self, context: QueryContext, role: str) -> KnowledgeResult:
        self._validate_context(context)
        resolved = self._resolve_effective_role(role)
        return self._result_for(context, "get_role", resolved.record, profile=resolved.profile)

    def get_mechanic(self, context: QueryContext, mechanic: str) -> KnowledgeResult:
        self._validate_context(context)
        resolved = self._resolve_record(mechanic, "mechanic")
        return self._result_for(context, "get_mechanic", resolved.record)

    def get_interaction(
        self,
        context: QueryContext,
        subjects: tuple[str, ...],
        situation: str | None = None,
    ) -> KnowledgeResult:
        self._validate_context(context)
        if not isinstance(subjects, tuple) or len(subjects) < 2:
            raise KnowledgeInvalidQueryError("subjects must contain at least two IDs")
        if any(not isinstance(subject, str) or not subject.strip() for subject in subjects):
            raise KnowledgeInvalidQueryError("subjects must contain non-empty strings")
        if situation is not None and (not isinstance(situation, str) or not situation.strip()):
            raise KnowledgeInvalidQueryError("situation must be non-empty when supplied")

        candidates = set(self._package.index.documents)
        candidates = {doc for doc in candidates if doc.kind == "interaction"}
        for subject in subjects:
            candidates &= set(self._package.index.lookup_relation(subject, kind="interaction"))
        if situation is not None:
            candidates &= set(self._package.index.lookup_relation(situation, kind="interaction"))
        ordered = tuple(sorted(candidates, key=lambda item: item.key))
        if not ordered:
            raise KnowledgeInteractionNotFoundError(
                "no exact interaction matches; search current interaction rules "
                "for the exact subjects and situation_key",
                suggestions=self._interaction_suggestions(subjects, situation),
            )
        if len(ordered) > 1:
            raise KnowledgeAmbiguousError(
                "multiple interactions match the supplied subjects and situation",
                candidates=(item.key for item in ordered),
            )
        return self._result_for(context, "get_interaction", ordered[0])

    def _interaction_suggestions(
        self,
        subjects: tuple[str, ...],
        situation: str | None,
    ) -> tuple[KnowledgeInteractionSuggestion, ...]:
        """Return a small, authorized set of query repair candidates.

        Interaction records store their subjects and situation key in the
        relation index.  Rank records by exact situation and subject overlap,
        then expose only their logical keys.  A miss never turns a partial
        match into a successful rule result.
        """

        wanted_subjects = {normalize_text(value) for value in subjects}
        wanted_situation = normalize_text(situation) if situation is not None else None
        board_ref = normalize_text(self.board_ref)
        board_typed_ref = normalize_text(f"board:{self.board_ref}")
        ranked: list[tuple[int, str, KnowledgeInteractionSuggestion]] = []
        for document in self._package.index.documents:
            if document.kind != "interaction":
                continue
            relation_keys = tuple(
                normalize_text(value)
                for value in document.related_ids
                if normalize_text(value) not in {board_ref, board_typed_ref}
            )
            situation_keys = tuple(value for value in relation_keys if "." in value)
            if len(situation_keys) != 1:
                # Published InteractionDefinition requires one normalized
                # dotted situation key; malformed records are ignored here.
                continue
            situation_key = situation_keys[0]
            candidate_subjects = tuple(value for value in relation_keys if value != situation_key)
            overlap = len(wanted_subjects.intersection(candidate_subjects))
            situation_match = int(wanted_situation == situation_key) if wanted_situation else 0
            if overlap == 0 and situation_match == 0:
                continue
            score = (situation_match * 4) + (overlap * 2)
            suggestion = KnowledgeInteractionSuggestion(
                canonical_ref=document.key,
                subjects=candidate_subjects,
                situation_key=situation_key,
            )
            ranked.append((score, document.key, suggestion))
        ranked.sort(key=lambda value: (-value[0], value[1]))
        return tuple(value[2] for value in ranked[:MAX_INTERACTION_HINTS])

    def get_rule_topic(self, context: QueryContext, topic: str) -> KnowledgeResult:
        self._validate_context(context)
        if not isinstance(topic, str) or not topic.strip():
            raise KnowledgeInvalidQueryError("topic must be a non-empty string")
        value = topic.strip()
        if value.startswith("board:"):
            value = value.split(":", 1)[1]
        elif value.startswith("topic:"):
            value = value.split(":", 1)[1]
        # Topic IDs are generated from the current board's stable sections.
        # A supplied version is still accepted only when it names this package.
        if "@" in value:
            record = self._package.index.lookup_exact(f"topic:{value}", kind="topic")
        else:
            record = self._topic_by_id(value)
        if record is None:
            raise KnowledgeNotFoundError(f"unknown board topic {topic!r}")
        return self._result_for(context, "get_rule_topic", record)

    def search_rules(
        self,
        context: QueryContext,
        query: SearchQuery | str,
    ) -> KnowledgeSearchResult:
        self._validate_context(context)
        request = SearchQuery(query=query) if isinstance(query, str) else query
        if not isinstance(request, SearchQuery):
            raise KnowledgeInvalidQueryError("query must be a SearchQuery")
        matches = self._package.index.search(
            request.query,
            kinds=request.kinds,
            limit=request.limit,
        )
        hits = tuple(
            KnowledgeSearchHit(
                kind=match.document.kind,
                id=match.document.id,
                version=match.document.version,
                title=match.document.title,
                summary=_summary(match.document.body),
                score=match.score,
                matched_terms=tuple(match.matched_terms),
                citations=self._citations(match.document),
            )
            for match in matches
        )
        identity = {
            "snapshot": self._snapshot_id,
            "tool": "search_rules",
            "query": request.model_dump(mode="json"),
            "hits": [hit.model_dump(mode="json") for hit in hits],
        }
        result_id = _result_id(identity)
        result = KnowledgeSearchResult(
            result_id=result_id,
            receipt_id=f"receipt_{result_id[3:]}",
            snapshot=self._snapshot,
            query=request.query,
            hits=hits,
        )
        return self._fit_search_result(result)

    def _validate_package(self) -> None:
        board_key = f"board:{self._package.board_ref.format()}"
        board = self._package.index.lookup_exact(board_key)
        if board is None or board.kind != "board":
            raise KnowledgeIntegrityError("compiled package has no exact current board document")
        if board_key not in self._package.sections:
            raise KnowledgeIntegrityError("compiled package is missing board sections")
        for role_id, profile in self._package.effective_roles.items():
            if profile.board_ref != self._package.board_ref or role_id != profile.role_id:
                raise KnowledgeIntegrityError(f"invalid effective role profile {role_id!r}")

    def _validate_context(self, context: QueryContext) -> None:
        if not isinstance(context, QueryContext):
            raise KnowledgeInvalidQueryError("context must be a QueryContext")
        if context.snapshot_id != self._snapshot_id:
            raise KnowledgeForbiddenError(
                "query context is bound to a different knowledge snapshot",
                candidates=(self._snapshot_id,),
            )

    def _board_record(self) -> KnowledgeIndexDocument:
        record = self._package.index.lookup_exact(f"board:{self.board_ref}")
        if record is None:
            raise KnowledgeIntegrityError("current board document disappeared from compiled index")
        return record

    def _topic_by_id(self, topic_id: str) -> KnowledgeIndexDocument | None:
        candidates = tuple(
            document
            for document in self._package.index.documents
            if document.kind == "topic" and normalize_text(document.id) == normalize_text(topic_id)
        )
        if len(candidates) > 1:
            raise KnowledgeAmbiguousError(
                f"board topic {topic_id!r} is ambiguous",
                candidates=(item.key for item in candidates),
            )
        return candidates[0] if candidates else None

    def _resolve_effective_role(self, role: str) -> _ResolvedDocument:
        if not isinstance(role, str) or not role.strip():
            raise KnowledgeInvalidQueryError("role must be a non-empty string")
        value = role.strip()
        profiles = self._package.effective_roles
        direct = tuple(
            role_id for role_id in profiles if normalize_text(role_id) == normalize_text(value)
        )
        if len(direct) > 1:
            raise KnowledgeAmbiguousError("role ID is ambiguous", candidates=direct)
        if len(direct) == 1:
            role_id = direct[0]
            record = self._package.index.lookup_exact(f"role:{profiles[role_id].role_ref.format()}")
            if record is None:
                raise KnowledgeIntegrityError(f"missing role index record for {role_id!r}")
            return _ResolvedDocument(record, profiles[role_id])

        titles = tuple(
            document
            for document in self._package.index.documents
            if document.kind == "role"
            and document.id in profiles
            and normalize_text(document.title) == normalize_text(value)
        )
        if len(titles) > 1:
            raise KnowledgeAmbiguousError(
                f"role title {role!r} is ambiguous",
                candidates=(item.key for item in titles),
            )
        if len(titles) == 1:
            selected = titles[0]
            return _ResolvedDocument(selected, profiles[selected.id])

        aliases = self._package.index.lookup_alias(value, kind="role")
        aliases = tuple(item for item in aliases if item.id in profiles)
        if not aliases:
            raise KnowledgeNotFoundError(f"unknown role {role!r}")
        if len(aliases) > 1:
            raise KnowledgeAmbiguousError(
                f"role alias {role!r} is ambiguous",
                candidates=(item.key for item in aliases),
            )
        selected = aliases[0]
        return _ResolvedDocument(selected, profiles[selected.id])

    def _resolve_record(self, value: str, kind: KnowledgeKind) -> _ResolvedDocument:
        if not isinstance(value, str) or not value.strip():
            raise KnowledgeInvalidQueryError(f"{kind} must be a non-empty string")
        value = value.strip()
        exact = self._package.index.lookup_exact(value, kind=kind)
        if exact is not None:
            return _ResolvedDocument(exact)

        identifier_matches = tuple(
            document
            for document in self._package.index.documents
            if document.kind == kind and normalize_text(document.id) == normalize_text(value)
        )
        if len(identifier_matches) > 1:
            raise KnowledgeAmbiguousError(
                f"{kind} ID {value!r} has multiple versions",
                candidates=(item.key for item in identifier_matches),
            )
        if len(identifier_matches) == 1:
            return _ResolvedDocument(identifier_matches[0])

        title_matches = tuple(
            document
            for document in self._package.index.documents
            if document.kind == kind and normalize_text(document.title) == normalize_text(value)
        )
        if len(title_matches) > 1:
            raise KnowledgeAmbiguousError(
                f"{kind} title {value!r} is ambiguous",
                candidates=(item.key for item in title_matches),
            )
        if len(title_matches) == 1:
            return _ResolvedDocument(title_matches[0])

        aliases = self._package.index.lookup_alias(value, kind=kind)
        if not aliases:
            raise KnowledgeNotFoundError(f"unknown {kind} {value!r}")
        if len(aliases) > 1:
            raise KnowledgeAmbiguousError(
                f"{kind} alias {value!r} is ambiguous",
                candidates=(item.key for item in aliases),
            )
        return _ResolvedDocument(aliases[0])

    def _result_for(
        self,
        context: QueryContext,
        tool: str,
        record: KnowledgeIndexDocument,
        *,
        profile: EffectiveRoleProfile | None = None,
    ) -> KnowledgeResult:
        document = self._document_payload(record, profile=profile)
        recommendations = self._recommendations(record)
        citations = self._citations(record, profile=profile)
        identity = {
            "snapshot": self._snapshot_id,
            "tool": tool,
            "document": document.model_dump(mode="json"),
            "recommendations": [item.model_dump(mode="json") for item in recommendations],
            "citations": [item.model_dump(mode="json") for item in citations],
        }
        result_id = _result_id(identity)
        result = KnowledgeResult(
            result_id=result_id,
            receipt_id=f"receipt_{result_id[3:]}",
            snapshot=self._snapshot,
            document=document,
            citations=citations,
            recommended_next_reads=recommendations,
        )
        return self._fit_result(result)

    def _document_payload(
        self,
        record: KnowledgeIndexDocument,
        *,
        profile: EffectiveRoleProfile | None = None,
    ) -> KnowledgeDocument:
        if record.kind == "topic":
            sections: tuple[KnowledgeSection, ...] = (
                KnowledgeSection(section_id=record.id, title=record.title, content=record.body),
            )
        else:
            key = record.key
            parsed = self._package.sections.get(key)
            if parsed is None:
                raise KnowledgeIntegrityError(f"compiled package is missing sections for {key!r}")
            sections = tuple(
                KnowledgeSection(section_id=item.id, title=item.title, content=item.body)
                for item in parsed
            )
        if profile is not None:
            summary = cast(str, getattr(profile.base_role, "public_summary", record.body))
            effective_rules = dict(profile.effective_rules)
        else:
            summary = _summary(record.body)
            effective_rules = {}
        return KnowledgeDocument(
            kind=record.kind,
            id=record.id,
            version=record.version,
            title=record.title,
            summary=summary,
            sections=sections,
            effective_rules=effective_rules,
        )

    def _citations(
        self,
        record: KnowledgeIndexDocument,
        *,
        profile: EffectiveRoleProfile | None = None,
    ) -> tuple[KnowledgeCitation, ...]:
        source_refs = (
            tuple(cast(Sequence[str], getattr(profile.base_role, "source_refs", ())))
            if profile
            else ()
        )
        canonical_ref = record.key
        values: list[KnowledgeCitation] = [
            KnowledgeCitation(
                source_id=canonical_ref,
                title=record.title,
                applies_to=(canonical_ref,),
            )
        ]
        for source_id in source_refs:
            if source_id == canonical_ref:
                continue
            values.append(
                KnowledgeCitation(
                    source_id=source_id,
                    title=record.title,
                    applies_to=(canonical_ref,),
                )
            )
        return tuple(values)

    def _recommendations(self, record: KnowledgeIndexDocument) -> tuple[KnowledgeReference, ...]:
        raw_plan = self._package.package_payload.get("reading_plan")
        if not isinstance(raw_plan, Mapping):
            return ()
        raw: object
        if record.kind == "board":
            raw = raw_plan.get("bootstrap_topics", ())
        elif record.kind == "role":
            role_topics = raw_plan.get("role_required_topics", {})
            raw = role_topics.get(record.id, ()) if isinstance(role_topics, Mapping) else ()
        else:
            raw = raw_plan.get("high_risk_topics", ())
        values: list[KnowledgeReference] = []
        seen: set[tuple[str, str]] = set()
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                if isinstance(item, Mapping):
                    kind = item.get("kind")
                    identifier = item.get("id")
                elif isinstance(item, str) and ":" in item:
                    kind, identifier = item.split(":", 1)
                else:
                    continue
                if not isinstance(kind, str) or not isinstance(identifier, str):
                    continue
                pair = (kind, identifier)
                if pair in seen:
                    continue
                seen.add(pair)
                values.append(
                    KnowledgeReference(kind=cast(ResultDocumentKind, kind), id=identifier)
                )
        return tuple(values)

    def _fit_result(self, result: KnowledgeResult) -> KnowledgeResult:
        if _serialized_size(result) <= self._max_result_bytes:
            return result
        sections = list(result.document.sections)
        # Preserve section order while shrinking each section.  The package is
        # immutable; only this detached response is changed.
        while sections and _serialized_size(result) > self._max_result_bytes:
            largest = max(range(len(sections)), key=lambda index: len(sections[index].content))
            section = sections[largest]
            if not section.content:
                sections.pop(largest)
                continue
            shortened = _truncate_utf8(
                section.content,
                max(0, len(section.content.encode("utf-8")) // 2),
            )
            sections[largest] = section.model_copy(update={"content": shortened})
            result = result.model_copy(
                update={
                    "document": result.document.model_copy(update={"sections": tuple(sections)}),
                    "truncated": True,
                }
            )
        if _serialized_size(result) > self._max_result_bytes:
            result = result.model_copy(update={"truncated": True})
        return result

    def _fit_search_result(self, result: KnowledgeSearchResult) -> KnowledgeSearchResult:
        if _serialized_size(result) <= self._max_result_bytes:
            return result
        hits = list(result.hits)
        while hits and _serialized_size(result) > self._max_result_bytes:
            hits.pop()
            result = result.model_copy(update={"hits": tuple(hits), "truncated": True})
        return result


def _summary(body: str, *, limit: int = 300) -> str:
    compact = " ".join(body.replace("\r", "\n").split())
    return _truncate_utf8(compact, limit)


def _result_id(identity: Mapping[str, object]) -> str:
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"kr_{digest}"


def _serialized_size(value: BaseModel) -> int:
    return len(value.model_dump_json().encode("utf-8"))


__all__ = [
    "KnowledgeAmbiguousError",
    "KnowledgeCitation",
    "KnowledgeDocument",
    "KnowledgeForbiddenError",
    "KnowledgeInteractionNotFoundError",
    "KnowledgeInteractionSuggestion",
    "KnowledgeIntegrityError",
    "KnowledgeInvalidQueryError",
    "KnowledgeNotFoundError",
    "KnowledgeQueryError",
    "KnowledgeReference",
    "KnowledgeSearchHit",
    "KnowledgeSearchResult",
    "KnowledgeResult",
    "KnowledgeSection",
    "KnowledgeService",
    "KnowledgeServiceError",
    "QueryContext",
    "SearchQuery",
    "SERVICE_SCHEMA_VERSION",
    "MAX_INTERACTION_HINTS",
]
