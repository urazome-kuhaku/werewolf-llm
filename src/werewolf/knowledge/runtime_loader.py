"""Restore a read-only knowledge service from one verified game snapshot.

The published Vault and the compiled package store are build-time inputs.  A
running game must be able to restart from its immutable ``ruleset`` directory
alone.  This module therefore verifies the snapshot again, verifies the
embedded compiled package at the snapshot root, and rebuilds the in-memory
objects consumed by :class:`KnowledgeService` without consulting either
source tree.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError

from .board import BoardDefinition
from .compiled_store import (
    _PACKAGE_FILES,
    CompiledKnowledgePackageLoad,
    CompiledKnowledgeStore,
    _verify_manifest_and_payload,
)
from .compiler import CompiledKnowledgePackage, EffectiveRoleProfile
from .indexes import KnowledgeIndex, KnowledgeIndexDocument
from .models import BoardRoleBinding, ReadingPlan
from .preview import experimental_preview_enabled
from .refs import VersionedRef
from .role import RoleDefinition
from .sections import SECTION_ID_PATTERN, MarkdownDocument, MarkdownSection
from .service import MAX_RESULT_BYTES, KnowledgeService
from .snapshot import (
    CorruptKnowledgeSnapshotError,
    KnowledgeSnapshot,
    KnowledgeSnapshotBuilder,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PACKAGE_KEYS = frozenset(
    {
        "schema_version",
        "package_id",
        "board_ref",
        "board_definition",
        "manifest",
        "documents",
        "sections",
        "effective_roles",
        "reading_plan",
        "indexes",
    }
)
_RECORD_KEYS = frozenset(
    {"kind", "id", "version", "title", "aliases", "body", "topics", "related_ids"}
)
_SECTION_DOCUMENT_KEYS = frozenset({"document", "title", "sections"})
_SECTION_KEYS = frozenset({"id", "title", "body", "level", "order"})
_PROFILE_METADATA_KEYS = frozenset(
    {"role_id", "board_ref", "count", "effective_rules", "override_claim_refs", "sections"}
)


class RuntimeKnowledgeLoaderError(ValueError):
    """Raised when a verified snapshot cannot be reconstructed safely."""


class _RuntimePayloadError(RuntimeKnowledgeLoaderError):
    """Internal error used to normalize malformed detached JSON payloads."""


@dataclass(frozen=True, slots=True)
class RuntimeKnowledgeBundle:
    """Verified runtime objects reconstructed only from one game snapshot.

    ``board`` is the machine-readable board definition that was serialized
    into the compiled package.  Keeping it beside the query service makes the
    runtime entry point usable by the authoritative game coordinators without
    allowing them to fall back to a live Vault or workbench path.
    """

    service: KnowledgeService
    board: BoardDefinition
    package: CompiledKnowledgePackage


def _canonical_json(value: object) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _RuntimePayloadError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise _RuntimePayloadError(f"{field_name} contains a non-string key")
    return value


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], *, field_name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise _RuntimePayloadError(f"{field_name} fields are invalid: {'; '.join(details)}")


def _string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise _RuntimePayloadError(f"{field_name} must be a non-empty string")
    return value


def _string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _RuntimePayloadError(f"{field_name} must be a list of strings")
    return tuple(value)


def _integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _RuntimePayloadError(f"{field_name} must be an integer")
    return value


def _snapshot_matches(expected: KnowledgeSnapshot, actual: KnowledgeSnapshot) -> bool:
    """Compare every verified field, including the bound physical location."""

    return (
        expected.game_id == actual.game_id
        and expected.snapshot_id == actual.snapshot_id
        and expected.package_id == actual.package_id
        and expected.board_ref == actual.board_ref
        and expected.package_identity == actual.package_identity
        and expected.compiler_version == actual.compiler_version
        and expected.created_at == actual.created_at
        and expected.root == actual.root
        and dict(expected.file_digests) == dict(actual.file_digests)
        and expected.manifest_sha256 == actual.manifest_sha256
    )


async def _reverify_snapshot(snapshot: KnowledgeSnapshot) -> KnowledgeSnapshot:
    if not isinstance(snapshot, KnowledgeSnapshot):
        raise TypeError("snapshot must be a KnowledgeSnapshot")
    root = snapshot.root.resolve()
    if root != snapshot.root:
        raise CorruptKnowledgeSnapshotError("snapshot root is not canonical")

    # ``load`` validates the game ID and derives exactly ``<game>/ruleset``.
    # The compiled store argument is intentionally unused on this path; the
    # snapshot verifier reads only the supplied snapshot root.
    builder = KnowledgeSnapshotBuilder.from_game_root(
        CompiledKnowledgeStore(root.parent),
        root.parent,
        game_id=snapshot.game_id,
    )
    verified = await builder.load(snapshot.game_id)
    if not _snapshot_matches(snapshot, verified):
        raise CorruptKnowledgeSnapshotError(
            "snapshot argument does not match the verified snapshot directory"
        )
    return verified


def _load_package_from_snapshot(
    snapshot: KnowledgeSnapshot,
    package_load: CompiledKnowledgePackageLoad,
) -> CompiledKnowledgePackage:
    if package_load.root != snapshot.root:
        raise RuntimeKnowledgeLoaderError("compiled package was not loaded from snapshot.root")
    if package_load.package_id != snapshot.package_id:
        raise RuntimeKnowledgeLoaderError("compiled package ID does not match snapshot")
    if package_load.board_ref != snapshot.board_ref:
        raise RuntimeKnowledgeLoaderError(
            "compiled package board reference does not match snapshot"
        )
    if package_load.package_identity != snapshot.package_identity:
        raise RuntimeKnowledgeLoaderError("compiled package digest does not match snapshot")

    payload = _mapping(package_load.package_payload, field_name="package")
    _exact_keys(payload, _PACKAGE_KEYS, field_name="package")
    if payload.get("schema_version") != 1:
        raise _RuntimePayloadError("package schema_version is unsupported")
    package_id = _string(payload.get("package_id"), field_name="package.package_id")
    board_ref_text = _string(payload.get("board_ref"), field_name="package.board_ref")
    if package_id != snapshot.package_id or board_ref_text != snapshot.board_ref_text:
        raise _RuntimePayloadError("package identity disagrees with snapshot")
    if _sha256_json(payload) != snapshot.package_identity:
        raise _RuntimePayloadError("package identity digest does not match package.json")

    records = _load_records(payload.get("documents"))
    index = KnowledgeIndex.build(records)
    indexes = _mapping(payload.get("indexes"), field_name="package.indexes")
    if _index_payload(index) != indexes:
        raise _RuntimePayloadError("serialized indexes disagree with rebuilt KnowledgeIndex")

    sections = _load_sections(payload.get("sections"), records)
    _validate_topic_records(records, sections, snapshot.board_ref)
    effective_roles = _load_effective_roles(
        payload.get("effective_roles"), sections, records, snapshot
    )
    board = _load_board_definition(
        payload.get("board_definition"),
        snapshot,
        effective_roles=effective_roles,
        records=records,
    )
    reading_plan = _load_reading_plan(payload.get("reading_plan"), snapshot.board_ref)
    _validate_reading_plan(reading_plan, sections, records, effective_roles)

    logical_manifest = _mapping(payload.get("manifest"), field_name="package.manifest")
    _validate_logical_manifest(
        logical_manifest,
        snapshot,
        package_load,
        board_definition_raw=cast(Mapping[str, object], payload["board_definition"]),
        board_definition=board,
    )
    document_digests = _manifest_digests(logical_manifest)

    canonical_package_json = _canonical_json(payload)
    canonical_manifest_json = _canonical_json(logical_manifest)
    return CompiledKnowledgePackage(
        board_ref=snapshot.board_ref,
        documents=records,
        index=index,
        sections=sections,
        effective_roles=effective_roles,
        document_digests=document_digests,
        package_payload=payload,
        manifest_payload=logical_manifest,
        canonical_package_json=canonical_package_json,
        canonical_manifest_json=canonical_manifest_json,
        package_identity=snapshot.package_identity,
        manifest_sha256=hashlib.sha256(canonical_manifest_json.encode("utf-8")).hexdigest(),
    )


def _load_board_definition(
    raw: object,
    snapshot: KnowledgeSnapshot,
    *,
    effective_roles: Mapping[str, EffectiveRoleProfile],
    records: Sequence[KnowledgeIndexDocument],
) -> BoardDefinition:
    """Restore and validate the board model embedded in the frozen package."""

    value = _mapping(raw, field_name="package.board_definition")
    try:
        board = BoardDefinition.model_validate(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _RuntimePayloadError("package.board_definition is invalid") from exc

    _board_definition_payload(value, board)
    if board.board_ref != snapshot.board_ref:
        raise _RuntimePayloadError("package.board_definition reference disagrees with snapshot")
    if board.status != "published":
        raise _RuntimePayloadError("package.board_definition must be published")
    if board.reviewed_by == "pending-human-review" and not experimental_preview_enabled():
        raise _RuntimePayloadError("package.board_definition is awaiting human review")

    role_records = {record.id: record for record in records if record.kind == "role"}
    bindings = {binding.role_ref.id: binding for binding in board.role_bindings}
    if set(bindings) != set(effective_roles):
        raise _RuntimePayloadError(
            "package.board_definition role bindings do not match effective_roles"
        )
    if set(role_records) != set(bindings):
        raise _RuntimePayloadError(
            "package.board_definition role bindings do not match role documents"
        )

    for role_id, binding in bindings.items():
        profile = effective_roles[role_id]
        if profile.role_ref != binding.role_ref:
            raise _RuntimePayloadError(
                f"package.board_definition role binding disagrees with effective role {role_id!r}"
            )
        if profile.count != binding.count:
            raise _RuntimePayloadError(
                f"package.board_definition count disagrees with effective role {role_id!r}"
            )
        if dict(profile.effective_rules) != dict(binding.effective_rules):
            raise _RuntimePayloadError(
                f"package.board_definition rules disagree with effective role {role_id!r}"
            )
        if tuple(profile.override_claim_refs) != tuple(binding.override_claim_refs):
            raise _RuntimePayloadError(
                "package.board_definition claim provenance disagrees with effective role "
                f"{role_id!r}"
            )
        role_record = role_records[role_id]
        if profile.role_ref.version != role_record.version:
            raise _RuntimePayloadError(
                f"package.board_definition role binding version disagrees with role {role_id!r}"
            )

    dependency_refs = {
        (record.kind, record.id, record.version)
        for record in records
        if record.kind in {"mechanic", "interaction"}
    }
    for reference in board.mechanic_refs:
        if ("mechanic", reference.id, reference.version) not in dependency_refs:
            raise _RuntimePayloadError(
                f"package.board_definition mechanic dependency is missing: {reference.format()}"
            )
    for reference in board.interaction_refs:
        if ("interaction", reference.id, reference.version) not in dependency_refs:
            raise _RuntimePayloadError(
                f"package.board_definition interaction dependency is missing: {reference.format()}"
            )
    return board


def _board_definition_payload(
    raw: Mapping[str, object],
    board: BoardDefinition,
) -> dict[str, object]:
    """Return the canonical payload represented by a frozen board model.

    Packages written before ``knife_rule.plan_confirmation_required`` was
    published omit that one field.  Their payload and identity digests must
    remain stable, so the loader accepts exactly that legacy shape after the
    complete board has already passed Pydantic validation.
    """

    expected = cast(dict[str, object], board.model_dump(mode="json"))
    if _canonical_json(raw) == _canonical_json(expected):
        return expected

    raw_knife = raw.get("knife_rule")
    expected_knife = expected.get("knife_rule")
    if not isinstance(raw_knife, Mapping) or not isinstance(expected_knife, Mapping):
        raise _RuntimePayloadError("package.board_definition is not canonical")
    if "plan_confirmation_required" in raw_knife:
        raise _RuntimePayloadError("package.board_definition is not canonical")
    if expected_knife.get("plan_confirmation_required") is not False:
        raise _RuntimePayloadError("package.board_definition is not canonical")

    legacy_knife = dict(expected_knife)
    legacy_knife.pop("plan_confirmation_required")
    legacy = dict(expected)
    legacy["knife_rule"] = legacy_knife
    if _canonical_json(raw) != _canonical_json(legacy):
        raise _RuntimePayloadError("package.board_definition is not canonical")
    return legacy


def _verify_package_at_snapshot_root(
    snapshot: KnowledgeSnapshot,
) -> CompiledKnowledgePackageLoad:
    """Verify the embedded package while allowing snapshot.json beside it."""

    files: dict[str, bytes] = {}
    for filename in (*_PACKAGE_FILES, "manifest.json"):
        path = snapshot.root / filename
        if path.is_symlink() or not path.is_file():
            raise CorruptKnowledgeSnapshotError(
                f"snapshot compiled package entry is not a regular file: {filename}"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CorruptKnowledgeSnapshotError(
                f"snapshot compiled package entry could not be read: {filename}"
            ) from exc
        expected_digest = snapshot.file_digests.get(filename)
        if expected_digest is None or hashlib.sha256(data).hexdigest() != expected_digest:
            raise CorruptKnowledgeSnapshotError(f"snapshot hash mismatch: {filename}")
        files[filename] = data
    return _verify_manifest_and_payload(
        snapshot.root,
        files,
        expected_package_id=snapshot.package_id,
        expected_board_ref=snapshot.board_ref,
    )


def _load_records(raw: object) -> tuple[KnowledgeIndexDocument, ...]:
    if not isinstance(raw, list):
        raise _RuntimePayloadError("package.documents must be a list")
    records: list[KnowledgeIndexDocument] = []
    for index, item in enumerate(raw):
        value = _mapping(item, field_name=f"package.documents[{index}]")
        _exact_keys(value, _RECORD_KEYS, field_name=f"package.documents[{index}]")
        kind = _string(value.get("kind"), field_name=f"documents[{index}].kind")
        identifier = _string(value.get("id"), field_name=f"documents[{index}].id")
        version = _string(value.get("version"), field_name=f"documents[{index}].version")
        title = _string(value.get("title"), field_name=f"documents[{index}].title")
        body = value.get("body")
        if not isinstance(body, str):
            raise _RuntimePayloadError(f"documents[{index}].body must be a string")
        try:
            records.append(
                KnowledgeIndexDocument(
                    kind=cast(Any, kind),
                    id=identifier,
                    version=version,
                    title=title,
                    aliases=_string_list(
                        value.get("aliases"), field_name=f"documents[{index}].aliases"
                    ),
                    body=body,
                    topics=_string_list(
                        value.get("topics"), field_name=f"documents[{index}].topics"
                    ),
                    related_ids=_string_list(
                        value.get("related_ids"), field_name=f"documents[{index}].related_ids"
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            raise _RuntimePayloadError(f"invalid document record at index {index}") from exc
    if not records:
        raise _RuntimePayloadError("package.documents must not be empty")
    return tuple(records)


def _index_payload(index: KnowledgeIndex) -> dict[str, object]:
    return {
        "exact": dict(index.exact_index),
        "alias": {key: list(value) for key, value in index.alias_index.items()},
        "topic": {key: list(value) for key, value in index.topic_index.items()},
        "relation": {key: list(value) for key, value in index.relation_index.items()},
        "text": {key: list(value) for key, value in index.text_index.items()},
    }


def _section(value: object, *, field_name: str) -> MarkdownSection:
    raw = _mapping(value, field_name=field_name)
    _exact_keys(raw, _SECTION_KEYS, field_name=field_name)
    identifier = _string(raw.get("id"), field_name=f"{field_name}.id")
    if SECTION_ID_PATTERN.fullmatch(identifier) is None:
        raise _RuntimePayloadError(f"{field_name}.id is not a valid section ID")
    title = _string(raw.get("title"), field_name=f"{field_name}.title")
    body = raw.get("body")
    if not isinstance(body, str):
        raise _RuntimePayloadError(f"{field_name}.body must be a string")
    level = _integer(raw.get("level"), field_name=f"{field_name}.level")
    order = _integer(raw.get("order"), field_name=f"{field_name}.order")
    if level != 2 or order < 0:
        raise _RuntimePayloadError(f"{field_name} has invalid level or order")
    return MarkdownSection(id=identifier, title=title, body=body, level=level, order=order)


def _load_sections(
    raw: object,
    records: Sequence[KnowledgeIndexDocument],
) -> dict[str, MarkdownDocument]:
    if not isinstance(raw, list) or not raw:
        raise _RuntimePayloadError("package.sections must be a non-empty list")
    expected_documents = {record.key for record in records if record.kind != "topic"}
    result: dict[str, MarkdownDocument] = {}
    document_order: list[str] = []
    for index, item in enumerate(raw):
        value = _mapping(item, field_name=f"package.sections[{index}]")
        _exact_keys(value, _SECTION_DOCUMENT_KEYS, field_name=f"package.sections[{index}]")
        document = _string(value.get("document"), field_name=f"sections[{index}].document")
        title_value = value.get("title")
        if title_value is not None and not isinstance(title_value, str):
            raise _RuntimePayloadError(f"sections[{index}].title must be a string or null")
        raw_sections = value.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise _RuntimePayloadError(f"sections[{index}].sections must be a non-empty list")
        parsed = tuple(
            _section(section, field_name=f"sections[{index}].sections[{section_index}]")
            for section_index, section in enumerate(raw_sections)
        )
        if tuple(section.order for section in parsed) != tuple(range(len(parsed))):
            raise _RuntimePayloadError(f"sections[{index}] orders are not contiguous")
        if document in result:
            raise _RuntimePayloadError(f"duplicate section document: {document}")
        result[document] = MarkdownDocument(
            sections=parsed,
            title=title_value,
            normalized_markdown="",
        )
        document_order.append(document)
    if set(result) != expected_documents:
        raise _RuntimePayloadError("serialized section documents do not match package documents")
    if document_order != sorted(document_order):
        raise _RuntimePayloadError("package.sections are not sorted")
    return result


def _validate_topic_records(
    records: Sequence[KnowledgeIndexDocument],
    sections: Mapping[str, MarkdownDocument],
    board_ref: VersionedRef,
) -> None:
    board_key = f"board:{board_ref.format()}"
    board_sections = sections.get(board_key)
    if board_sections is None:
        raise _RuntimePayloadError("package has no current board sections")
    topic_records = {
        record.id: record
        for record in records
        if record.kind == "topic" and record.version == board_ref.version
    }
    if len(topic_records) != sum(record.kind == "topic" for record in records):
        raise _RuntimePayloadError("topic records contain an invalid version or duplicate ID")
    if set(topic_records) != set(board_sections.section_ids):
        raise _RuntimePayloadError("topic records do not match board sections")
    for section in board_sections:
        record = topic_records.get(section.id)
        if record is None or record.title != section.title or record.body != section.body:
            raise _RuntimePayloadError(f"topic record disagrees with board section {section.id!r}")


def _load_effective_roles(
    raw: object,
    sections: Mapping[str, MarkdownDocument],
    records: Sequence[KnowledgeIndexDocument],
    snapshot: KnowledgeSnapshot,
) -> dict[str, EffectiveRoleProfile]:
    if not isinstance(raw, list):
        raise _RuntimePayloadError("package.effective_roles must be a list")
    role_records = {record.id: record for record in records if record.kind == "role"}
    profiles: dict[str, EffectiveRoleProfile] = {}
    for index, item in enumerate(raw):
        value = _mapping(item, field_name=f"effective_roles[{index}]")
        role_fields = frozenset(RoleDefinition.model_fields)
        _exact_keys(
            value,
            role_fields | _PROFILE_METADATA_KEYS,
            field_name=f"effective_roles[{index}]",
        )
        role_id = _string(value.get("role_id"), field_name=f"effective_roles[{index}].role_id")
        if role_id in profiles:
            raise _RuntimePayloadError(f"duplicate effective role profile: {role_id}")
        if value.get("board_ref") != snapshot.board_ref_text:
            raise _RuntimePayloadError(f"effective role {role_id!r} has the wrong board reference")
        count = _integer(value.get("count"), field_name=f"effective_roles[{index}].count")
        effective_rules = _mapping(
            value.get("effective_rules"), field_name=f"effective_roles[{index}].effective_rules"
        )
        claim_refs = _string_list(
            value.get("override_claim_refs"),
            field_name=f"effective_roles[{index}].override_claim_refs",
        )
        sections_value = value.get("sections")
        profile_sections = _load_profile_sections(sections_value, index)
        model_values = {key: value[key] for key in role_fields}
        try:
            role = RoleDefinition.model_validate(model_values)
            role_ref = VersionedRef(id=role.role_id, version=role.version)
            BoardRoleBinding(
                role_ref=role_ref,
                count=count,
                effective_rules=cast(dict[str, Any], dict(effective_rules)),
                override_claim_refs=list(claim_refs),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise _RuntimePayloadError(f"effective role {role_id!r} is invalid") from exc
        if role.role_id != role_id:
            raise _RuntimePayloadError(f"effective role ID disagrees with role model: {role_id!r}")
        record = role_records.get(role_id)
        if record is None or record.version != role.version:
            raise _RuntimePayloadError(f"effective role {role_id!r} has no matching document")
        role_key = record.key
        source_sections = sections.get(role_key)
        if source_sections is None:
            raise _RuntimePayloadError(f"effective role {role_id!r} sections are missing")
        if not _same_sections(source_sections, profile_sections):
            raise _RuntimePayloadError(f"effective role {role_id!r} sections disagree with package")
        expected_profile = {
            "role_id": role_id,
            **role.model_dump(mode="json"),
            "board_ref": snapshot.board_ref_text,
            "count": count,
            "effective_rules": dict(effective_rules),
            "override_claim_refs": list(claim_refs),
            "sections": [_section_payload(section) for section in profile_sections],
        }
        if value != expected_profile:
            raise _RuntimePayloadError(f"effective role {role_id!r} is not canonical")
        profiles[role_id] = EffectiveRoleProfile(
            board_ref=snapshot.board_ref,
            role_ref=role_ref,
            count=count,
            base_role=role,
            effective_rules=cast(dict[str, Any], dict(effective_rules)),
            override_claim_refs=claim_refs,
            sections=source_sections,
        )
    if set(profiles) != set(role_records):
        raise _RuntimePayloadError("effective role profiles do not cover role documents")
    return profiles


def _load_profile_sections(raw: object, profile_index: int) -> MarkdownDocument:
    if not isinstance(raw, list) or not raw:
        raise _RuntimePayloadError(f"effective_roles[{profile_index}].sections must be non-empty")
    parsed = tuple(
        _section(section, field_name=f"effective_roles[{profile_index}].sections[{index}]")
        for index, section in enumerate(raw)
    )
    if tuple(section.order for section in parsed) != tuple(range(len(parsed))):
        raise _RuntimePayloadError(f"effective_roles[{profile_index}].sections orders are invalid")
    return MarkdownDocument(sections=parsed, title=None, normalized_markdown="")


def _section_payload(section: MarkdownSection) -> dict[str, object]:
    return {
        "id": section.id,
        "title": section.title,
        "body": section.body,
        "level": section.level,
        "order": section.order,
    }


def _same_sections(left: MarkdownDocument, right: MarkdownDocument) -> bool:
    """Compare serialized section content without losing source title metadata."""

    return tuple(_section_payload(section) for section in left) == tuple(
        _section_payload(section) for section in right
    )


def _load_reading_plan(raw: object, board_ref: VersionedRef) -> ReadingPlan:
    try:
        plan = ReadingPlan.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _RuntimePayloadError("package.reading_plan is invalid") from exc
    if plan.board_ref != board_ref:
        raise _RuntimePayloadError("reading_plan.board_ref disagrees with package board")
    return plan


def _validate_reading_plan(
    plan: ReadingPlan,
    sections: Mapping[str, MarkdownDocument],
    records: Sequence[KnowledgeIndexDocument],
    profiles: Mapping[str, EffectiveRoleProfile],
) -> None:
    document_ids = {
        kind: {document.id for document in records if document.kind == kind}
        for kind in ("role", "mechanic", "interaction")
    }
    board_sections = set(sections[f"board:{plan.board_ref.format()}"].section_ids)

    def check(reference: object) -> None:
        kind = getattr(reference, "kind", None)
        identifier = getattr(reference, "id", None)
        if kind == "board" and identifier in board_sections:
            return
        if kind in document_ids and identifier in document_ids[kind]:
            return
        raise _RuntimePayloadError(f"reading_plan reference does not resolve: {kind}:{identifier}")

    for reference in plan.bootstrap_topics:
        check(reference)
    for role_id, references in plan.role_required_topics.items():
        if role_id not in profiles:
            raise _RuntimePayloadError(f"reading_plan references unknown role: {role_id}")
        for reference in references:
            check(reference)
    for references in plan.phase_topics.values():
        for reference in references:
            check(reference)
    for reference in plan.high_risk_topics:
        check(reference)


def _validate_logical_manifest(
    manifest: Mapping[str, object],
    snapshot: KnowledgeSnapshot,
    package_load: CompiledKnowledgePackageLoad,
    *,
    board_definition_raw: Mapping[str, object],
    board_definition: BoardDefinition,
) -> None:
    expected_keys = frozenset(
        {
            "schema_version",
            "package_id",
            "board_ref",
            "board_definition_sha256",
            "documents",
        }
    )
    _exact_keys(manifest, expected_keys, field_name="package.manifest")
    if manifest.get("schema_version") != 1:
        raise _RuntimePayloadError("package.manifest schema_version is unsupported")
    if (
        manifest.get("package_id") != snapshot.package_id
        or manifest.get("board_ref") != snapshot.board_ref_text
    ):
        raise _RuntimePayloadError("package.manifest identity disagrees with snapshot")
    board_digest = _string(
        manifest.get("board_definition_sha256"),
        field_name="package.manifest.board_definition_sha256",
    )
    if _SHA256_RE.fullmatch(board_digest) is None:
        raise _RuntimePayloadError("package.manifest.board_definition_sha256 is invalid")
    if board_digest != _sha256_json(
        _board_definition_payload(board_definition_raw, board_definition)
    ):
        raise _RuntimePayloadError("package.manifest disagrees with board_definition")
    raw_documents = manifest.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise _RuntimePayloadError("package.manifest.documents must be a non-empty list")
    previous: str | None = None
    for index, item in enumerate(raw_documents):
        value = _mapping(item, field_name=f"package.manifest.documents[{index}]")
        _exact_keys(value, frozenset({"path", "sha256"}), field_name=f"manifest.documents[{index}]")
        path = _string(value.get("path"), field_name=f"manifest.documents[{index}].path")
        digest = _string(value.get("sha256"), field_name=f"manifest.documents[{index}].sha256")
        if _SHA256_RE.fullmatch(digest) is None:
            raise _RuntimePayloadError(f"manifest digest is invalid for {path}")
        if previous is not None and path <= previous:
            raise _RuntimePayloadError("package.manifest.documents are not sorted")
        previous = path
    physical_logical_digest = package_load.manifest_payload.get("logical_manifest_sha256")
    if physical_logical_digest != _sha256_json(manifest):
        raise _RuntimePayloadError("physical manifest disagrees with logical manifest")


def _manifest_digests(manifest: Mapping[str, object]) -> dict[str, str]:
    raw_documents = manifest["documents"]
    if not isinstance(raw_documents, list):
        raise _RuntimePayloadError("package.manifest.documents must be a list")
    result: dict[str, str] = {}
    for item in raw_documents:
        value = cast(Mapping[str, object], item)
        path = cast(str, value["path"])
        digest = cast(str, value["sha256"])
        if path in result:
            raise _RuntimePayloadError(f"duplicate manifest document path: {path}")
        result[path] = digest
    return result


async def load_runtime_knowledge_bundle_from_snapshot(
    snapshot: KnowledgeSnapshot,
    *,
    max_result_bytes: int = MAX_RESULT_BYTES,
) -> RuntimeKnowledgeBundle:
    """Verify and restore all runtime knowledge objects from ``snapshot.root``.

    The returned board, package, and service own only immutable in-memory
    values.  No package path from the Vault or the compiled store is accepted
    as an input.
    """

    verified_snapshot = await _reverify_snapshot(snapshot)
    package_load = await asyncio.to_thread(_verify_package_at_snapshot_root, verified_snapshot)
    package = _load_package_from_snapshot(verified_snapshot, package_load)
    board = _load_board_definition(
        package.package_payload.get("board_definition"),
        verified_snapshot,
        effective_roles=package.effective_roles,
        records=package.documents,
    )
    service = KnowledgeService(
        package,
        snapshot_id=verified_snapshot.snapshot_id,
        max_result_bytes=max_result_bytes,
    )
    return RuntimeKnowledgeBundle(service=service, board=board, package=package)


async def load_knowledge_bundle_from_snapshot(
    snapshot: KnowledgeSnapshot,
    *,
    max_result_bytes: int = MAX_RESULT_BYTES,
) -> RuntimeKnowledgeBundle:
    """Compatibility alias for the explicit runtime bundle loader."""

    return await load_runtime_knowledge_bundle_from_snapshot(
        snapshot,
        max_result_bytes=max_result_bytes,
    )


async def load_service_from_snapshot(
    snapshot: KnowledgeSnapshot,
    *,
    max_result_bytes: int = MAX_RESULT_BYTES,
) -> KnowledgeService:
    """Verify and restore only the query service from one game snapshot."""

    bundle = await load_runtime_knowledge_bundle_from_snapshot(
        snapshot,
        max_result_bytes=max_result_bytes,
    )
    return bundle.service


__all__ = [
    "RuntimeKnowledgeBundle",
    "RuntimeKnowledgeLoaderError",
    "load_knowledge_bundle_from_snapshot",
    "load_runtime_knowledge_bundle_from_snapshot",
    "load_service_from_snapshot",
]
