"""Deterministic in-memory compilation of one published knowledge package.

The package loader owns filesystem access and dependency closure validation.  This
module consumes its :class:`~werewolf.knowledge.package_loader.KnowledgePackage`
result and turns the closure into stable Markdown sections, effective role
profiles, search records, and canonical JSON digests.  It deliberately does not
write compiled files; the publisher/snapshot layer can serialize these values
later without changing their logical identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import BaseModel

from werewolf.rules.compiler import (
    compile_execution_definition,
    execution_artifact,
)
from werewolf.rules.models import ExecutionPackage

from .board import BoardDefinition
from .indexes import KnowledgeIndex, KnowledgeIndexDocument, KnowledgeKind
from .interaction import InteractionDefinition
from .mechanic import MechanicDefinition
from .models import KnowledgeRef
from .package_loader import KnowledgePackage, PublishedKnowledgeDocument
from .refs import VersionedRef
from .role import RoleDefinition
from .sections import MarkdownDocument, MarkdownSection, extract_sections

if TYPE_CHECKING:
    from werewolf.game.actions import ActionRegistry

JSON_DUMPS_KWARGS: Final[dict[str, object]] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}


class KnowledgeCompilerError(ValueError):
    """Base error raised when a loaded package cannot be compiled."""


class KnowledgeCompilerReferenceError(KnowledgeCompilerError):
    """Raised when a reading-plan or effective-role reference is incomplete."""


class KnowledgeCompilerTopicError(KnowledgeCompilerReferenceError):
    """Raised for a generic topic that has no V1 resolver yet."""


def _canonicalize(value: object) -> object:
    """Return JSON-safe data with mapping keys sorted recursively."""

    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return _canonicalize(getattr(value, "value"))
    return value


def _canonical_json(value: object) -> str:
    """Serialize one logical payload in the package's canonical JSON form."""

    return json.dumps(_canonicalize(value), **cast(Any, JSON_DUMPS_KWARGS))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _model_payload(model: BaseModel) -> object:
    return _canonicalize(model.model_dump(mode="json"))


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    """Deduplicate strings while preserving their deterministic source order."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return tuple(result)


def _document_kind(document: PublishedKnowledgeDocument[BaseModel]) -> KnowledgeKind:
    model = document.model
    if isinstance(model, BoardDefinition):
        return model.kind
    if isinstance(model, RoleDefinition):
        return model.kind
    if isinstance(model, MechanicDefinition):
        return model.kind
    if isinstance(model, InteractionDefinition):
        return model.kind
    raise KnowledgeCompilerError(f"unsupported package document model: {type(model)!r}")


def _document_id(document: PublishedKnowledgeDocument[BaseModel]) -> str:
    model = document.model
    if isinstance(model, BoardDefinition):
        return model.board_id
    if isinstance(model, RoleDefinition):
        return model.role_id
    if isinstance(model, (MechanicDefinition, InteractionDefinition)):
        return model.id
    raise KnowledgeCompilerError(f"unsupported package document model: {type(model)!r}")


def _document_key(document: PublishedKnowledgeDocument[BaseModel]) -> str:
    return f"{_document_kind(document)}:{_document_id(document)}@{document.ref.version}"


def _section_payload(section: MarkdownSection) -> dict[str, object]:
    return {
        "id": section.id,
        "title": section.title,
        "body": section.body,
        "level": section.level,
        "order": section.order,
    }


def _section_map_payload(sections: Mapping[str, MarkdownDocument]) -> list[dict[str, object]]:
    return [
        {
            "document": key,
            "title": document.title,
            "sections": [_section_payload(section) for section in document],
        }
        for key, document in sorted(sections.items())
    ]


def _ref_text(ref: VersionedRef) -> str:
    return ref.format()


@dataclass(frozen=True, slots=True)
class EffectiveRoleProfile:
    """A board-specific role view built without mutating the base role model."""

    board_ref: VersionedRef
    role_ref: VersionedRef
    count: int
    base_role: BaseModel
    effective_rules: Mapping[str, object]
    override_claim_refs: tuple[str, ...]
    sections: MarkdownDocument

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_rules", MappingProxyType(dict(self.effective_rules)))
        object.__setattr__(self, "override_claim_refs", tuple(self.override_claim_refs))

    @property
    def role_id(self) -> str:
        return cast(str, getattr(self.base_role, "role_id"))

    @property
    def name(self) -> str:
        return cast(str, getattr(self.base_role, "name"))

    @property
    def base(self) -> BaseModel:
        """Compatibility alias for callers naming the source role ``base``."""

        return self.base_role

    @property
    def override_provenance(self) -> Mapping[str, tuple[str, ...]]:
        """Expose claim provenance for every overridden rule key."""

        return MappingProxyType(
            {key: self.override_claim_refs for key in sorted(self.effective_rules)}
        )

    @property
    def payload(self) -> dict[str, object]:
        """Return the runtime-facing effective profile payload."""

        payload = cast(dict[str, object], _model_payload(self.base_role))
        payload["board_ref"] = self.board_ref.format()
        payload["count"] = self.count
        payload["effective_rules"] = _canonicalize(self.effective_rules)
        payload["override_claim_refs"] = list(self.override_claim_refs)
        payload["sections"] = [_section_payload(section) for section in self.sections]
        return payload


@dataclass(frozen=True, slots=True)
class CompiledKnowledgePackage:
    """All deterministic in-memory outputs for one loaded board package."""

    board_ref: VersionedRef
    documents: tuple[KnowledgeIndexDocument, ...]
    index: KnowledgeIndex
    sections: Mapping[str, MarkdownDocument]
    effective_roles: Mapping[str, EffectiveRoleProfile]
    document_digests: Mapping[str, str]
    package_payload: Mapping[str, object]
    manifest_payload: Mapping[str, object]
    canonical_package_json: str
    canonical_manifest_json: str
    package_identity: str
    manifest_sha256: str
    execution: ExecutionPackage | None = None
    action_registry: ActionRegistry | None = None
    execution_source: str | None = None
    execution_source_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", MappingProxyType(dict(self.sections)))
        object.__setattr__(self, "effective_roles", MappingProxyType(dict(self.effective_roles)))
        object.__setattr__(self, "document_digests", MappingProxyType(dict(self.document_digests)))
        object.__setattr__(self, "package_payload", MappingProxyType(dict(self.package_payload)))
        object.__setattr__(self, "manifest_payload", MappingProxyType(dict(self.manifest_payload)))

    @property
    def package_id(self) -> str:
        """Return the stable board reference used as the logical package ID."""

        return self.board_ref.format()

    @property
    def package_sha256(self) -> str:
        """Compatibility alias for the package identity digest."""

        return self.package_identity

    @property
    def manifest_digest(self) -> str:
        return self.manifest_sha256

    @property
    def package_json(self) -> str:
        return self.canonical_package_json

    @property
    def manifest_json(self) -> str:
        return self.canonical_manifest_json


class KnowledgePackageCompiler:
    """Compile one already loaded :class:`KnowledgePackage` in memory."""

    def compile(self, package: KnowledgePackage) -> CompiledKnowledgePackage:
        if not isinstance(package, KnowledgePackage):
            raise TypeError("compile() expects a KnowledgePackage")

        parsed_sections = self._parse_sections(package)
        self._resolve_reading_refs(package, parsed_sections)
        effective_roles = self._build_effective_roles(package, parsed_sections)
        records = self._build_index_documents(package, parsed_sections)
        index = KnowledgeIndex.build(records)
        document_digests = self._document_digests(package, parsed_sections)
        compiled_execution = compile_execution_definition(package)
        executable_payload = (
            execution_artifact(compiled_execution) if compiled_execution is not None else None
        )
        board_definition = cast(dict[str, object], _model_payload(package.board.model))
        manifest_payload = self._manifest_payload(
            package,
            document_digests,
            board_definition=board_definition,
            executable_payload=executable_payload,
        )
        package_payload = self._package_payload(
            package,
            parsed_sections,
            effective_roles,
            records,
            index,
            manifest_payload,
            board_definition=board_definition,
            executable_payload=executable_payload,
        )
        canonical_manifest_json = _canonical_json(manifest_payload)
        canonical_package_json = _canonical_json(package_payload)

        return CompiledKnowledgePackage(
            board_ref=package.board_ref,
            documents=records,
            index=index,
            sections=parsed_sections,
            effective_roles=effective_roles,
            document_digests=document_digests,
            package_payload=cast(Mapping[str, object], _canonicalize(package_payload)),
            manifest_payload=cast(Mapping[str, object], _canonicalize(manifest_payload)),
            canonical_package_json=canonical_package_json,
            canonical_manifest_json=canonical_manifest_json,
            package_identity=hashlib.sha256(canonical_package_json.encode("utf-8")).hexdigest(),
            manifest_sha256=hashlib.sha256(canonical_manifest_json.encode("utf-8")).hexdigest(),
            execution=compiled_execution.execution if compiled_execution is not None else None,
            action_registry=(
                compiled_execution.action_registry if compiled_execution is not None else None
            ),
            execution_source=compiled_execution.source if compiled_execution is not None else None,
            execution_source_sha256=(
                compiled_execution.source_sha256 if compiled_execution is not None else None
            ),
        )

    def _parse_sections(self, package: KnowledgePackage) -> dict[str, MarkdownDocument]:
        parsed: dict[str, MarkdownDocument] = {}
        for document in package.documents:
            key = _document_key(document)
            # SectionParseError is intentionally allowed to propagate.  It
            # preserves the parser's precise malformed/duplicate ID errors.
            parsed[key] = extract_sections(document.body)
        return parsed

    def _resolve_reading_refs(
        self,
        package: KnowledgePackage,
        sections: Mapping[str, MarkdownDocument],
    ) -> None:
        board_sections = sections[_document_key(package.board)]
        for reference in package.unresolved_reading_refs:
            if reference.kind == "topic":
                raise KnowledgeCompilerTopicError(
                    f"reading-plan topic {reference.format()!r} cannot be resolved in V1"
                )
            if reference.kind != "board":
                raise KnowledgeCompilerReferenceError(
                    f"unsupported unresolved reading reference: {reference.format()!r}"
                )
            if board_sections.resolve(reference) is None:
                raise KnowledgeCompilerReferenceError(
                    f"reading-plan board section {reference.format()!r} is missing"
                )

    def _build_effective_roles(
        self,
        package: KnowledgePackage,
        sections: Mapping[str, MarkdownDocument],
    ) -> dict[str, EffectiveRoleProfile]:
        profiles: dict[str, EffectiveRoleProfile] = {}
        for binding in package.board.model.role_bindings:
            document = package.roles.get(binding.role_ref.id)
            if document is None or document.ref != binding.role_ref:
                raise KnowledgeCompilerReferenceError(
                    f"role binding {binding.role_ref.format()!r} is missing from package"
                )
            role_id = _document_id(document)
            if role_id in profiles:
                raise KnowledgeCompilerReferenceError(
                    f"duplicate effective role profile for {role_id!r}"
                )
            profiles[role_id] = EffectiveRoleProfile(
                board_ref=package.board_ref,
                role_ref=binding.role_ref,
                count=binding.count,
                base_role=document.model,
                effective_rules=dict(binding.effective_rules),
                override_claim_refs=tuple(binding.override_claim_refs),
                sections=sections[_document_key(document)],
            )
        return profiles

    def _build_index_documents(
        self,
        package: KnowledgePackage,
        sections: Mapping[str, MarkdownDocument],
    ) -> tuple[KnowledgeIndexDocument, ...]:
        records: list[KnowledgeIndexDocument] = []
        plan_refs = self._reading_plan_refs(package)
        for document in package.documents:
            kind = _document_kind(document)
            identifier = _document_id(document)
            model = document.model
            section_document = sections[_document_key(document)]
            topics = [section.id for section in section_document]
            topics.extend(
                reference.id
                for reference in plan_refs
                if reference.kind == kind and reference.id == identifier
            )
            related = self._related_ids(document, package.board_ref)
            records.append(
                KnowledgeIndexDocument(
                    kind=kind,
                    id=identifier,
                    version=document.ref.version,
                    title=cast(str, getattr(model, "name", identifier)),
                    aliases=tuple(cast(Sequence[str], getattr(model, "aliases", []))),
                    body=document.body.replace("\r\n", "\n").replace("\r", "\n"),
                    topics=_unique(topics),
                    related_ids=_unique(related),
                )
            )

        board_sections = sections[_document_key(package.board)]
        board_record_key = _document_key(package.board)
        for section in board_sections:
            records.append(
                KnowledgeIndexDocument(
                    kind="topic",
                    id=section.id,
                    version=package.board_ref.version,
                    title=section.title or section.id,
                    body=section.body,
                    topics=(section.id,),
                    related_ids=(board_record_key, package.board_ref.format()),
                )
            )
        return tuple(records)

    def _reading_plan_refs(self, package: KnowledgePackage) -> tuple[KnowledgeRef, ...]:
        plan = package.reading_plan
        refs: list[KnowledgeRef] = list(plan.bootstrap_topics)
        refs.extend(topic for values in plan.role_required_topics.values() for topic in values)
        refs.extend(topic for values in plan.phase_topics.values() for topic in values)
        refs.extend(plan.high_risk_topics)
        return tuple(refs)

    def _related_ids(
        self,
        document: PublishedKnowledgeDocument[BaseModel],
        board_ref: VersionedRef,
    ) -> tuple[str, ...]:
        model = document.model
        values: list[str] = []
        if isinstance(model, RoleDefinition):
            values.extend(_ref_text(ref) for ref in model.board_compatibility)
        elif isinstance(model, MechanicDefinition):
            values.extend(_ref_text(ref) for ref in model.board_refs)
            values.extend(item.participant_id for item in model.participation)
            values.extend(item.input_id for item in model.inputs)
            values.extend(item.output_id for item in model.outputs)
        elif isinstance(model, InteractionDefinition):
            values.extend(_ref_text(ref) for ref in model.board_refs)
            values.extend(model.subjects)
            values.append(model.situation_key)
        elif isinstance(model, BoardDefinition):
            values.extend(_ref_text(binding.role_ref) for binding in model.role_bindings)
            values.extend(_ref_text(ref) for ref in model.mechanic_refs)
            values.extend(_ref_text(ref) for ref in model.interaction_refs)
        else:
            raise KnowledgeCompilerError(f"unsupported package document model: {type(model)!r}")
        if not isinstance(model, BoardDefinition) and board_ref.format() not in values:
            values.append(board_ref.format())
        return _unique(values)

    def _document_digests(
        self,
        package: KnowledgePackage,
        sections: Mapping[str, MarkdownDocument],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for document in package.documents:
            key = _document_key(document)
            payload = {
                "kind": _document_kind(document),
                "id": _document_id(document),
                "version": document.ref.version,
                "relative_path": document.relative_path,
                "frontmatter": _model_payload(document.model),
                "body": document.body.replace("\r\n", "\n").replace("\r", "\n"),
                "sections": [_section_payload(section) for section in sections[key]],
            }
            result[document.relative_path] = _sha256_json(payload)
        return dict(sorted(result.items()))

    def _manifest_payload(
        self,
        package: KnowledgePackage,
        document_digests: Mapping[str, str],
        *,
        board_definition: Mapping[str, object],
        executable_payload: Mapping[str, object] | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 2 if executable_payload is not None else 1,
            "package_id": package.board_ref.format(),
            "board_ref": package.board_ref.format(),
            "board_definition_sha256": _sha256_json(board_definition),
            "documents": [
                {
                    "path": path,
                    "sha256": digest,
                }
                for path, digest in sorted(document_digests.items())
            ],
        }
        if executable_payload is not None:
            payload["execution_sha256"] = _sha256_json(executable_payload)
        return payload

    def _package_payload(
        self,
        package: KnowledgePackage,
        sections: Mapping[str, MarkdownDocument],
        effective_roles: Mapping[str, EffectiveRoleProfile],
        records: Sequence[KnowledgeIndexDocument],
        index: KnowledgeIndex,
        manifest_payload: Mapping[str, object],
        *,
        board_definition: Mapping[str, object],
        executable_payload: Mapping[str, object] | None,
    ) -> dict[str, object]:
        record_payload = [
            {
                "kind": record.kind,
                "id": record.id,
                "version": record.version,
                "title": record.title,
                "aliases": list(record.aliases),
                "body": record.body,
                "topics": list(record.topics),
                "related_ids": list(record.related_ids),
            }
            for record in records
        ]
        index_payload = {
            "exact": dict(sorted(index.exact_index.items())),
            "alias": {key: list(value) for key, value in sorted(index.alias_index.items())},
            "topic": {key: list(value) for key, value in sorted(index.topic_index.items())},
            "relation": {key: list(value) for key, value in sorted(index.relation_index.items())},
            "text": {key: list(value) for key, value in sorted(index.text_index.items())},
        }
        payload: dict[str, object] = {
            "schema_version": 2 if executable_payload is not None else 1,
            "package_id": package.board_ref.format(),
            "board_ref": package.board_ref.format(),
            "board_definition": board_definition,
            "manifest": manifest_payload,
            "documents": record_payload,
            "sections": _section_map_payload(sections),
            "effective_roles": [
                {"role_id": role_id, **profile.payload}
                for role_id, profile in sorted(effective_roles.items())
            ],
            "reading_plan": _model_payload(package.reading_plan),
            "indexes": index_payload,
        }
        if executable_payload is not None:
            payload["executable"] = _canonicalize(executable_payload)
        return payload


def compile_knowledge_package(package: KnowledgePackage) -> CompiledKnowledgePackage:
    """Functional convenience wrapper around :class:`KnowledgePackageCompiler`."""

    return KnowledgePackageCompiler().compile(package)


__all__ = [
    "CompiledKnowledgePackage",
    "EffectiveRoleProfile",
    "KnowledgeCompilerError",
    "KnowledgeCompilerReferenceError",
    "KnowledgeCompilerTopicError",
    "KnowledgePackageCompiler",
    "compile_knowledge_package",
]
