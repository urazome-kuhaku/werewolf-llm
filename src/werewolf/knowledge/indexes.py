"""Deterministic in-memory indexes for published knowledge documents.

The compiler can use this module without importing the package loader or any
filesystem code.  A :class:`KnowledgeIndexDocument` is the small, typed
boundary between compiled documents and the query layer; callers may build
these records from Markdown, JSON, or another source.

The index is deliberately lexical.  It normalizes Unicode text with NFKC,
case-folding, and whitespace collapsing, then indexes Latin words and CJK
single, double, and triple character n-grams.  This keeps Chinese lookup
deterministic and dependency-free while retaining a useful substring score
for natural-language queries.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

KnowledgeKind = Literal["board", "role", "mechanic", "interaction", "topic"]
KNOWLEDGE_KINDS: frozenset[str] = frozenset(
    {"board", "role", "mechanic", "interaction", "topic"},
)

# Keep a hard upper bound on work returned by an untrusted caller.  The
# default is intentionally smaller so tools can use the result directly.
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50

_LATIN_WORD = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3]+")


def normalize_text(value: str) -> str:
    """Normalize text for all human-facing indexes and lookups.

    NFKC makes visually equivalent forms compare equally.  ``casefold``
    handles Latin case, and ``split`` collapses every Unicode whitespace run
    to one ASCII space.  The function intentionally does not remove spaces:
    Latin tokenization uses them as boundaries and the complete normalized
    value is retained for substring matching.
    """

    if not isinstance(value, str):
        raise TypeError("knowledge index text must be a string")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _as_text_tuple(values: Sequence[str] | Iterable[str], field_name: str) -> tuple[str, ...]:
    """Validate and freeze one string collection used by a document."""

    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} entries must be strings")
        if not value.strip():
            continue
        result.append(value)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class KnowledgeIndexDocument:
    """A loader-independent document record consumed by :class:`KnowledgeIndex`.

    ``related_ids`` may contain a bare ID (``witch``), a typed ID
    (``role:witch``), or a fully versioned key (``role:witch@1.0.0``).  The
    index stores each spelling in its normalized relation postings, so callers
    can use whichever stable reference they already have.
    """

    kind: KnowledgeKind
    id: str
    version: str
    title: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    body: str = ""
    topics: tuple[str, ...] = field(default_factory=tuple)
    related_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in KNOWLEDGE_KINDS:
            raise ValueError(f"unsupported knowledge kind: {self.kind!r}")
        for name in ("id", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("title", "body"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
        for name in ("aliases", "topics", "related_ids"):
            object.__setattr__(self, name, _as_text_tuple(getattr(self, name), name))

    @property
    def key(self) -> str:
        """Return the canonical exact index key ``kind:id@version``."""

        return f"{self.kind}:{self.id}@{self.version}"

    @property
    def typed_id(self) -> str:
        """Return the unversioned typed ID used by relation lookups."""

        return f"{self.kind}:{self.id}"


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    """One deterministic text-search hit."""

    document: KnowledgeIndexDocument
    score: float
    matched_terms: tuple[str, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        """Expose the stable document key for callers sorting or displaying hits."""

        return self.document.key


@dataclass(frozen=True, slots=True)
class KnowledgeLookupResult:
    """Result of exact/alias resolution without silently choosing ambiguity."""

    match_type: Literal["exact", "alias", "ambiguous", "not_found"]
    candidates: tuple[KnowledgeIndexDocument, ...] = field(default_factory=tuple)

    @property
    def document(self) -> KnowledgeIndexDocument | None:
        """Return the sole match, or ``None`` when missing/ambiguous."""

        if len(self.candidates) == 1 and self.match_type != "ambiguous":
            return self.candidates[0]
        return None

    @property
    def is_ambiguous(self) -> bool:
        """Whether multiple alias candidates require caller disambiguation."""

        return self.match_type == "ambiguous"


def _stable_documents(
    documents: Iterable[KnowledgeIndexDocument],
) -> tuple[KnowledgeIndexDocument, ...]:
    """Freeze documents in the one ordering used by all postings and results."""

    values = tuple(documents)
    if any(not isinstance(document, KnowledgeIndexDocument) for document in values):
        raise TypeError("documents must contain KnowledgeIndexDocument values")
    ordered = tuple(
        sorted(values, key=lambda document: (document.kind, document.id, document.version))
    )
    keys = [document.key for document in ordered]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate knowledge document keys: {', '.join(duplicates)}")
    return ordered


def _tokens(value: str) -> tuple[str, ...]:
    """Return deterministic Latin and CJK tokens from normalized text."""

    normalized = normalize_text(value)
    result: set[str] = set(_LATIN_WORD.findall(normalized))
    for match in _CJK_RUN.finditer(normalized):
        run = match.group(0)
        result.update(
            run[index : index + size] for size in (1, 2, 3) for index in range(len(run) - size + 1)
        )
    return tuple(sorted(result))


def _document_text(document: KnowledgeIndexDocument) -> tuple[str, ...]:
    """Return the normalized fields participating in lexical search."""

    return tuple(
        normalize_text(value)
        for value in (document.title, *document.aliases, *document.topics, document.body)
        if value.strip()
    )


def _relation_keys(value: str) -> tuple[str, ...]:
    """Return normalized spellings accepted for one relation reference."""

    normalized = normalize_text(value)
    if not normalized:
        return ()
    keys = {normalized}
    # A relation can be emitted as ``role:witch@1.0.0`` while a query often
    # uses the shorter ``witch`` spelling.  Keep all stable forms in the same
    # posting without interpreting or resolving them through a package loader.
    if ":" in normalized:
        typed, _, version = normalized.partition("@")
        _, _, identifier = typed.partition(":")
        if identifier:
            keys.add(identifier)
            keys.add(typed)
        if version and identifier:
            keys.add(f"{identifier}@{version}")
    elif "@" in normalized:
        identifier, _, _ = normalized.partition("@")
        if identifier:
            keys.add(identifier)
    return tuple(sorted(keys))


@dataclass(frozen=True, slots=True)
class KnowledgeIndex:
    """Immutable exact, alias, topic, relation, and text indexes.

    The mappings contain tuples of canonical document keys.  Keeping postings
    as sorted tuples makes construction and serialization deterministic while
    keeping the records themselves available through ``documents``.
    """

    documents: tuple[KnowledgeIndexDocument, ...]
    exact_index: Mapping[str, str]
    alias_index: Mapping[str, tuple[str, ...]]
    topic_index: Mapping[str, tuple[str, ...]]
    relation_index: Mapping[str, tuple[str, ...]]
    text_index: Mapping[str, tuple[str, ...]]
    _by_key: Mapping[str, KnowledgeIndexDocument] = field(repr=False, compare=False)
    _text_fields: Mapping[str, tuple[str, ...]] = field(repr=False, compare=False)

    @classmethod
    def build(cls, documents: Iterable[KnowledgeIndexDocument]) -> KnowledgeIndex:
        """Build all postings from records without I/O or external state."""

        ordered = _stable_documents(documents)
        by_key = {document.key: document for document in ordered}
        aliases: defaultdict[str, set[str]] = defaultdict(set)
        topics: defaultdict[str, set[str]] = defaultdict(set)
        relations: defaultdict[str, set[str]] = defaultdict(set)
        text: defaultdict[str, set[str]] = defaultdict(set)
        text_fields: dict[str, tuple[str, ...]] = {}

        for document in ordered:
            key = document.key
            for alias in document.aliases:
                aliases[normalize_text(alias)].add(key)
            for topic in document.topics:
                topics[normalize_text(topic)].add(key)
            # Relations are declared by the document that describes the
            # interaction/mechanic.  Indexing only the declared target keeps a
            # query such as ``witch -> interactions`` from returning the role
            # document itself.
            for relation in document.related_ids:
                for relation_key in _relation_keys(relation):
                    relations[relation_key].add(key)
            fields = _document_text(document)
            text_fields[key] = fields
            for value in fields:
                for token in _tokens(value):
                    text[token].add(key)

        def freeze_postings(postings: Mapping[str, set[str]]) -> Mapping[str, tuple[str, ...]]:
            return MappingProxyType(
                {term: tuple(sorted(keys)) for term, keys in sorted(postings.items()) if term},
            )

        return cls(
            documents=ordered,
            exact_index=MappingProxyType({document.key: document.key for document in ordered}),
            alias_index=freeze_postings(aliases),
            topic_index=freeze_postings(topics),
            relation_index=freeze_postings(relations),
            text_index=freeze_postings(text),
            _by_key=MappingProxyType(by_key),
            _text_fields=MappingProxyType(text_fields),
        )

    def _documents_for_keys(self, keys: Iterable[str]) -> tuple[KnowledgeIndexDocument, ...]:
        """Resolve postings in stable document order and discard unknown keys."""

        return tuple(self._by_key[key] for key in sorted(set(keys)) if key in self._by_key)

    def lookup_exact(
        self,
        value: str | KnowledgeIndexDocument,
        *,
        kind: KnowledgeKind | None = None,
        identifier: str | None = None,
        version: str | None = None,
    ) -> KnowledgeIndexDocument | None:
        """Look up a document by exact typed ID and version.

        The compact form is ``kind:id@version``.  ``kind``, ``identifier``,
        and ``version`` may be supplied separately for callers that already
        have parsed fields.  An untyped ``id@version`` is accepted only when
        it identifies one document uniquely.
        """

        if isinstance(value, KnowledgeIndexDocument):
            key = value.key
        elif identifier is not None or version is not None:
            if not isinstance(value, str) or kind is None or identifier is None or version is None:
                return None
            key = f"{kind}:{identifier}@{version}"
        else:
            normalized = normalize_text(value)
            key = normalized
            if kind is not None and "@" in normalized and ":" not in normalized:
                key = f"{kind}:{normalized}"
            elif kind is not None and ":" not in normalized:
                # A kind plus bare identifier has no version and is not exact.
                return None
            elif ":" not in normalized and "@" in normalized:
                suffix = normalized
                candidates = tuple(
                    document
                    for document in self.documents
                    if normalize_text(f"{document.id}@{document.version}") == suffix
                )
                return candidates[0] if len(candidates) == 1 else None
        document_key = next(
            (
                candidate
                for candidate in self.exact_index
                if normalize_text(candidate) == normalize_text(key)
            ),
            None,
        )
        return self._by_key.get(document_key) if document_key is not None else None

    def lookup_alias(
        self,
        alias: str,
        *,
        kind: KnowledgeKind | None = None,
    ) -> tuple[KnowledgeIndexDocument, ...]:
        """Return every document matching an alias, preserving ambiguity."""

        candidates = self._documents_for_keys(self.alias_index.get(normalize_text(alias), ()))
        if kind is not None:
            candidates = tuple(document for document in candidates if document.kind == kind)
        return candidates

    def lookup_topic(
        self,
        topic: str,
        *,
        kind: KnowledgeKind | None = None,
    ) -> tuple[KnowledgeIndexDocument, ...]:
        """Return documents tagged with an exact normalized topic."""

        candidates = self._documents_for_keys(self.topic_index.get(normalize_text(topic), ()))
        if kind is not None:
            candidates = tuple(document for document in candidates if document.kind == kind)
        return candidates

    def lookup_relation(
        self,
        related_id: str,
        *,
        kind: KnowledgeKind | None = None,
    ) -> tuple[KnowledgeIndexDocument, ...]:
        """Return documents related to a bare, typed, or versioned reference."""

        candidates = self._documents_for_keys(
            self.relation_index.get(normalize_text(related_id), ())
        )
        if kind is not None:
            candidates = tuple(document for document in candidates if document.kind == kind)
        return candidates

    # Short aliases keep the query service adapter uncomplicated.
    related = lookup_relation

    def lookup(self, value: str, *, kind: KnowledgeKind | None = None) -> KnowledgeLookupResult:
        """Resolve exact ID first, then alias, without guessing ambiguity."""

        exact = self.lookup_exact(value, kind=kind)
        if exact is not None:
            return KnowledgeLookupResult("exact", (exact,))
        aliases = self.lookup_alias(value, kind=kind)
        if not aliases:
            return KnowledgeLookupResult("not_found")
        if len(aliases) > 1:
            return KnowledgeLookupResult("ambiguous", aliases)
        return KnowledgeLookupResult("alias", aliases)

    def search(
        self,
        query: str,
        *,
        kinds: Iterable[KnowledgeKind] | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> tuple[KnowledgeSearchResult, ...]:
        """Search title, aliases, topics, and body with stable lexical scores.

        Chinese text contributes single, double, and triple character grams;
        Latin text contributes words.  Complete normalized substring matches
        receive a bonus, so a longer natural-language query still finds a
        document even when its terms are not independently indexed.  Results
        are always tied by canonical document key and are bounded by
        ``MAX_SEARCH_LIMIT``.
        """

        if not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit <= 0:
            return ()
        bounded_limit = min(limit, MAX_SEARCH_LIMIT)
        normalized_query = normalize_text(query)
        if not normalized_query:
            return ()
        allowed_kinds = set(kinds) if kinds is not None else None
        if allowed_kinds is not None and not allowed_kinds.issubset(KNOWLEDGE_KINDS):
            raise ValueError("kinds contains an unsupported knowledge kind")

        query_tokens = _tokens(normalized_query)
        candidate_keys: set[str] = set()
        for token in query_tokens:
            candidate_keys.update(self.text_index.get(token, ()))
        # Short queries and punctuation-only forms can have no tokens.  The
        # bounded scan is still deterministic and contains the substring path.
        if not candidate_keys:
            candidate_keys = set(self._by_key)

        query_token_set = set(query_tokens)
        results: list[KnowledgeSearchResult] = []
        for key in sorted(candidate_keys):
            document = self._by_key[key]
            if allowed_kinds is not None and document.kind not in allowed_kinds:
                continue
            fields = self._text_fields[key]
            field_tokens = set().union(*(_tokens(field) for field in fields)) if fields else set()
            matched = tuple(sorted(query_token_set & field_tokens))
            token_score = len(matched) / max(len(query_token_set), 1)
            substring_score = max(
                (
                    len(normalized_query) / max(len(field), 1)
                    for field in fields
                    if normalized_query in field
                ),
                default=0.0,
            )
            # Exact title/alias hits should outrank body-only n-gram matches.
            heading_score = max(
                (1.0 for field in fields[: 1 + len(document.aliases)] if normalized_query == field),
                default=0.0,
            )
            score = token_score + (0.75 * substring_score) + (2.0 * heading_score)
            if score > 0:
                results.append(KnowledgeSearchResult(document, score, matched))
        results.sort(key=lambda result: (-result.score, result.key))
        return tuple(results[:bounded_limit])


def build_knowledge_index(documents: Iterable[KnowledgeIndexDocument]) -> KnowledgeIndex:
    """Functional constructor for callers that do not need the class method."""

    return KnowledgeIndex.build(documents)


# A shorter record name is useful to compiler adapters and remains explicit in
# type checkers.  The canonical class above is retained for discoverability.
KnowledgeRecord = KnowledgeIndexDocument


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "KNOWLEDGE_KINDS",
    "MAX_SEARCH_LIMIT",
    "KnowledgeIndex",
    "KnowledgeIndexDocument",
    "KnowledgeLookupResult",
    "KnowledgeRecord",
    "KnowledgeSearchResult",
    "build_knowledge_index",
    "normalize_text",
]
