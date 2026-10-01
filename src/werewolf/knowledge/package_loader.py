"""Read and validate one immutable published knowledge package.

The package loader is the boundary between the published Markdown tree and
the runtime.  It derives every path from a validated :class:`VersionedRef`,
then validates the document's machine fields and the complete dependency
closure before returning a typed aggregate.  It deliberately has no write or
publish operations.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Generic, TypeVar, cast

from pydantic import BaseModel, ValidationError

from .board import BoardDefinition
from .interaction import InteractionDefinition
from .mechanic import MechanicDefinition
from .models import KnowledgeRef, ReadingPlan
from .refs import VersionedRef
from .role import RoleDefinition
from .storage import KnowledgeMarkdownDocument, KnowledgeMarkdownStore


class KnowledgePackageError(ValueError):
    """Base error for an invalid or incomplete published package."""


class KnowledgePackageDocumentError(KnowledgePackageError):
    """Raised when one package document cannot satisfy its requested ref."""


class KnowledgePackageReferenceError(KnowledgePackageError):
    """Raised when the dependency or reading-plan closure is not complete."""


ModelT = TypeVar("ModelT", bound=BaseModel, covariant=True)


@dataclass(frozen=True, slots=True)
class PublishedKnowledgeDocument(Generic[ModelT]):
    """A parsed published document together with its stable source metadata."""

    ref: VersionedRef
    model: ModelT
    markdown: KnowledgeMarkdownDocument

    @property
    def relative_path(self) -> str:
        """Return the canonical path used to load this document."""

        return self.markdown.relative_path

    @property
    def path(self) -> str:
        """Compatibility alias for the canonical relative path."""

        return self.relative_path

    @property
    def body(self) -> str:
        """Return the normalized Markdown body."""

        return self.markdown.body

    @property
    def content_sha256(self) -> str:
        """Return the digest of the original document bytes."""

        return self.markdown.content_sha256

    @property
    def sha256(self) -> str:
        """Return the digest of the original document bytes."""

        return self.content_sha256

    @property
    def frontmatter(self) -> Mapping[str, object]:
        """Return the parsed machine fields as a read-only view."""

        return MappingProxyType(cast(dict[str, object], self.markdown.frontmatter))

    @property
    def source(self) -> KnowledgeMarkdownDocument:
        """Return the syntax-level source document."""

        return self.markdown


@dataclass(frozen=True, slots=True)
class KnowledgePackage:
    """The validated, read-only dependency closure for one board version.

    ``unresolved_reading_refs`` contains board section and generic topic refs.
    They are intentionally surfaced for the compiler's section/topic index;
    this loader validates all document-kind refs that it can resolve.
    """

    published_root: Path
    board: PublishedKnowledgeDocument[BoardDefinition]
    roles: Mapping[str, PublishedKnowledgeDocument[RoleDefinition]]
    mechanics: Mapping[str, PublishedKnowledgeDocument[MechanicDefinition]]
    interactions: Mapping[str, PublishedKnowledgeDocument[InteractionDefinition]]
    unresolved_reading_refs: tuple[KnowledgeRef, ...] = ()

    def __post_init__(self) -> None:
        """Freeze mapping containers at the aggregate boundary."""

        object.__setattr__(self, "published_root", self.published_root.resolve())
        object.__setattr__(self, "roles", MappingProxyType(dict(self.roles)))
        object.__setattr__(self, "mechanics", MappingProxyType(dict(self.mechanics)))
        object.__setattr__(self, "interactions", MappingProxyType(dict(self.interactions)))

    @property
    def board_ref(self) -> VersionedRef:
        """Return the package's fixed board reference."""

        return self.board.ref

    @property
    def reading_plan(self) -> ReadingPlan:
        """Return the already validated board reading plan."""

        return self.board.model.reading_plan

    @property
    def documents(self) -> tuple[PublishedKnowledgeDocument[BaseModel], ...]:
        """Return documents in deterministic board, role, mechanic, interaction order."""

        values: list[PublishedKnowledgeDocument[BaseModel]] = [
            cast(PublishedKnowledgeDocument[BaseModel], self.board)
        ]
        for collection in (self.roles, self.mechanics, self.interactions):
            values.extend(
                cast(PublishedKnowledgeDocument[BaseModel], item) for item in collection.values()
            )
        return tuple(values)

    def get_document(self, ref: VersionedRef | str) -> PublishedKnowledgeDocument[BaseModel]:
        """Resolve one exact reference inside this package."""

        requested = _coerce_ref(ref)
        if requested == self.board.ref:
            return cast(PublishedKnowledgeDocument[BaseModel], self.board)
        for collection in (self.roles, self.mechanics, self.interactions):
            document = collection.get(requested.id)
            if document is not None and document.ref == requested:
                return cast(PublishedKnowledgeDocument[BaseModel], document)
        raise KnowledgePackageReferenceError(
            f"reference {requested.format()!r} is not present in package"
        )

    def iter_dependencies(self) -> Iterator[PublishedKnowledgeDocument[BaseModel]]:
        """Iterate the package dependency closure in stable order."""

        yield from self.documents


class KnowledgePackageLoader:
    """Load a complete package from a trusted published root."""

    def __init__(
        self,
        published_root: str | Path,
        *,
        store: KnowledgeMarkdownStore | None = None,
    ) -> None:
        self._published_root = Path(published_root)
        self._store = store or KnowledgeMarkdownStore(self._published_root)

    async def load(self, board_ref: VersionedRef | str) -> KnowledgePackage:
        """Load and validate the fixed dependency closure for ``board_ref``."""

        requested_board = _coerce_ref(board_ref)
        board_document = await self._load_document(
            requested_board,
            kind="board",
            model_type=BoardDefinition,
        )
        board = board_document
        if board.model.board_ref != requested_board:
            raise KnowledgePackageDocumentError(
                "board document identity does not match the requested board reference"
            )

        role_refs = tuple(binding.role_ref for binding in board.model.role_bindings)
        mechanic_refs = tuple(board.model.mechanic_refs)
        interaction_refs = tuple(board.model.interaction_refs)
        _reject_duplicate_logical_ids(
            role_refs=role_refs,
            mechanic_refs=mechanic_refs,
            interaction_refs=interaction_refs,
        )

        role_documents, mechanic_documents, interaction_documents = await asyncio.gather(
            self._load_collection(role_refs, "role", RoleDefinition),
            self._load_collection(mechanic_refs, "mechanic", MechanicDefinition),
            self._load_collection(interaction_refs, "interaction", InteractionDefinition),
        )

        _validate_board_affinity(
            requested_board,
            role_documents.values(),
            field_name="board_compatibility",
            optional=True,
        )
        _validate_board_affinity(
            requested_board,
            mechanic_documents.values(),
            field_name="board_refs",
        )
        _validate_board_affinity(
            requested_board,
            interaction_documents.values(),
            field_name="board_refs",
        )
        unresolved_reading_refs = _validate_reading_plan(
            board.model,
            role_documents,
            mechanic_documents,
            interaction_documents,
        )

        return KnowledgePackage(
            published_root=self._published_root,
            board=board,
            roles=role_documents,
            mechanics=mechanic_documents,
            interactions=interaction_documents,
            unresolved_reading_refs=unresolved_reading_refs,
        )

    async def _load_collection(
        self,
        refs: tuple[VersionedRef, ...],
        kind: str,
        model_type: type[ModelT],
    ) -> dict[str, PublishedKnowledgeDocument[ModelT]]:
        documents = await asyncio.gather(
            *(self._load_document(ref, kind=kind, model_type=model_type) for ref in refs)
        )
        return {ref.id: document for ref, document in zip(refs, documents)}

    async def _load_document(
        self,
        ref: VersionedRef,
        *,
        kind: str,
        model_type: type[ModelT],
    ) -> PublishedKnowledgeDocument[ModelT]:
        relative_path = f"{kind}s/{ref.id}/{ref.version}/{kind}.md"
        try:
            markdown = await self._store.load(relative_path)
        except FileNotFoundError as exc:
            raise KnowledgePackageDocumentError(
                f"missing published {kind} document for {ref.format()!r}"
            ) from exc
        try:
            model = model_type.model_validate(
                _normalize_frontmatter_for_model(kind, markdown.frontmatter)
            )
        except ValidationError as exc:
            raise KnowledgePackageDocumentError(
                f"invalid published {kind} document at {relative_path!r}: {exc}"
            ) from exc
        _validate_document_identity(model, ref, kind, relative_path)
        return PublishedKnowledgeDocument(ref=ref, model=model, markdown=markdown)


# The longer name makes call sites self-documenting while preserving a short
# name for runtime code.
PublishedKnowledgePackageLoader = KnowledgePackageLoader


async def load_knowledge_package(
    published_root: str | Path,
    board_ref: VersionedRef | str,
) -> KnowledgePackage:
    """Convenience wrapper around :class:`KnowledgePackageLoader`."""

    return await KnowledgePackageLoader(published_root).load(board_ref)


def _coerce_ref(value: VersionedRef | str) -> VersionedRef:
    if isinstance(value, VersionedRef):
        return value
    if isinstance(value, str):
        return VersionedRef.parse(value)
    raise TypeError("knowledge package references must be VersionedRef or id@version strings")


def _normalize_frontmatter_for_model(
    kind: str, frontmatter: Mapping[str, object]
) -> dict[str, object]:
    """Normalize compact documented references before strict model parsing.

    The formal Markdown examples use ``id@version`` strings for nested refs,
    while Pydantic's strict nested models intentionally accept only structured
    mappings.  Convert those two board-owned nested fields without changing
    the parsed source document or treating any value as a file path.
    """

    values = copy.deepcopy(dict(frontmatter))
    if kind != "board":
        return values
    bindings = values.get("role_bindings", values.get("roles"))
    if isinstance(bindings, list):
        normalized_bindings: list[object] = []
        for binding in bindings:
            if isinstance(binding, dict) and isinstance(binding.get("role_ref"), str):
                item = dict(binding)
                item["role_ref"] = VersionedRef.parse(item["role_ref"]).model_dump()
                normalized_bindings.append(item)
            else:
                normalized_bindings.append(binding)
        if "role_bindings" in values:
            values["role_bindings"] = normalized_bindings
        else:
            values["roles"] = normalized_bindings
    plan = values.get("reading_plan")
    if isinstance(plan, dict) and isinstance(plan.get("board_ref"), str):
        normalized_plan = dict(plan)
        normalized_plan["board_ref"] = VersionedRef.parse(normalized_plan["board_ref"]).model_dump()
        values["reading_plan"] = normalized_plan
    return values


def _validate_document_identity(
    model: BaseModel,
    ref: VersionedRef,
    kind: str,
    relative_path: str,
) -> None:
    values = model.model_dump(mode="python")
    if values.get("kind") != kind:
        raise KnowledgePackageDocumentError(
            f"document {relative_path!r} has kind {values.get('kind')!r}, expected {kind!r}"
        )
    actual_id = values.get("board_id", values.get("role_id", values.get("id")))
    if actual_id != ref.id or values.get("version") != ref.version:
        raise KnowledgePackageDocumentError(
            f"document {relative_path!r} identity does not match {ref.format()!r}"
        )
    if values.get("status") != "published":
        raise KnowledgePackageDocumentError(
            f"document {relative_path!r} must have status 'published'"
        )


def _reject_duplicate_logical_ids(
    *,
    role_refs: tuple[VersionedRef, ...],
    mechanic_refs: tuple[VersionedRef, ...],
    interaction_refs: tuple[VersionedRef, ...],
) -> None:
    seen: dict[str, VersionedRef] = {}
    for ref in (*role_refs, *mechanic_refs, *interaction_refs):
        previous = seen.get(ref.id)
        if previous is not None:
            raise KnowledgePackageReferenceError(
                "duplicate logical dependency ID "
                f"{ref.id!r}: {previous.format()} and {ref.format()}"
            )
        seen[ref.id] = ref


def _validate_board_affinity(
    board_ref: VersionedRef,
    documents: Iterable[PublishedKnowledgeDocument[BaseModel]],
    *,
    field_name: str,
    optional: bool = False,
) -> None:
    for document in documents:
        if isinstance(document.model, RoleDefinition):
            references = document.model.board_compatibility
        elif isinstance(document.model, MechanicDefinition):
            references = document.model.board_refs
        elif isinstance(document.model, InteractionDefinition):
            references = document.model.board_refs
        else:
            raise AssertionError(f"unsupported package document type: {type(document.model)!r}")
        if optional and not references:
            continue
        if board_ref not in references:
            formatted = ", ".join(reference.format() for reference in references)
            raise KnowledgePackageReferenceError(
                f"{document.ref.format()} {field_name} does not reference requested board "
                f"{board_ref.format()}; references: {formatted or '<none>'}"
            )


def _validate_reading_plan(
    board: BoardDefinition,
    roles: Mapping[str, PublishedKnowledgeDocument[RoleDefinition]],
    mechanics: Mapping[str, PublishedKnowledgeDocument[MechanicDefinition]],
    interactions: Mapping[str, PublishedKnowledgeDocument[InteractionDefinition]],
) -> tuple[KnowledgeRef, ...]:
    available = {
        "role": frozenset(roles),
        "mechanic": frozenset(mechanics),
        "interaction": frozenset(interactions),
    }
    plan = board.reading_plan
    for role_id in plan.role_required_topics:
        if role_id not in roles:
            raise KnowledgePackageReferenceError(
                f"reading plan role_required_topics contains unknown role {role_id!r}"
            )
    refs = list(plan.bootstrap_topics)
    refs.extend(topic for topics in plan.role_required_topics.values() for topic in topics)
    refs.extend(topic for topics in plan.phase_topics.values() for topic in topics)
    refs.extend(plan.high_risk_topics)
    unresolved: list[KnowledgeRef] = []
    for logical_ref in refs:
        if _validate_reading_ref(logical_ref, board, available):
            unresolved.append(logical_ref)
    return tuple(unresolved)


def _validate_reading_ref(
    logical_ref: KnowledgeRef,
    board: BoardDefinition,
    available: Mapping[str, frozenset[str]],
) -> bool:
    if logical_ref.kind == "board":
        return logical_ref.id != board.board_id
    if logical_ref.kind == "topic":
        return True
    if logical_ref.id not in available[logical_ref.kind]:
        raise KnowledgePackageReferenceError(
            f"reading plan contains unresolved reference {logical_ref.format()!r}"
        )
    return False


__all__ = [
    "KnowledgePackage",
    "KnowledgePackageDocumentError",
    "KnowledgePackageError",
    "KnowledgePackageLoader",
    "KnowledgePackageReferenceError",
    "PublishedKnowledgeDocument",
    "PublishedKnowledgePackageLoader",
    "load_knowledge_package",
]
