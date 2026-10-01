"""Deterministic claim-to-Markdown draft generation for the workbench.

The research and analysis stages deliberately stop before they create a
knowledge document.  This module is the small, explicit boundary between
those stages and the publisher.  A caller supplies a machine-readable
document template (the values in that template are already a reviewed
design choice); this module only adds closed claim/source provenance,
validates the published knowledge schema, and renders stable Markdown bytes.

It is intentionally *not* an LLM renderer.  Missing execution fields are an
error instead of an invitation to infer a "classic" rule.  Unsupported,
unverified, or conflicting claims cannot be attached to a document.  The
coverage report is produced alongside the draft and remains a publish gate;
this module never calls the publisher and never changes an existing file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.frontmatter import FrontmatterParseError, parse_markdown
from werewolf.knowledge.interaction import InteractionDefinition
from werewolf.knowledge.mechanic import MechanicDefinition
from werewolf.knowledge.role import RoleDefinition
from werewolf.persistence import atomic_write_bytes, resolve_contained_path

from .bundle_codec import bundle_sha256
from .bundles import ResearchBundle
from .claims import ClaimScope, ClaimStatus, RuleClaim
from .coverage import CoverageReport, CoverageRequirement, analyze_coverage
from .coverage_artifacts import render_coverage_artifacts

DraftKind = Literal["board", "role", "mechanic", "interaction"]
_DRAFT_KINDS: tuple[DraftKind, ...] = ("board", "role", "mechanic", "interaction")
_KIND_SCOPE: dict[DraftKind, ClaimScope] = {
    "board": ClaimScope.BOARD,
    "role": ClaimScope.ROLE,
    "mechanic": ClaimScope.MECHANIC,
    "interaction": ClaimScope.INTERACTION,
}
_KIND_DIRECTORY: dict[DraftKind, str] = {
    "board": "boards",
    "role": "roles",
    "mechanic": "mechanics",
    "interaction": "interactions",
}
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", re.ASCII)
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$",
    re.ASCII,
)
_PLACEHOLDER_REVIEWER = "pending-human-review"
_MATERIALIZE_LOCKS: dict[Path, asyncio.Lock] = {}


class DraftGenerationError(ValueError):
    """Base error for an invalid or incomplete deterministic draft request."""


class DraftGenerationBlockedError(DraftGenerationError):
    """Raised when a requested document cites a claim that is not publishable."""

    def __init__(
        self,
        message: str,
        *,
        claim_ids: Sequence[str] = (),
        coverage: CoverageReport | None = None,
    ) -> None:
        super().__init__(message)
        self.claim_ids = tuple(claim_ids)
        self.coverage = coverage


class DraftOutputExistsError(DraftGenerationError):
    """Raised when materialization would overwrite an existing workbench file."""


@dataclass(frozen=True, slots=True)
class DraftDocumentTemplate:
    """One explicit schema template used by :func:`generate_draft_package`.

    ``frontmatter`` contains all machine execution fields.  The generator
    does not invent any missing rule field; it only fills identity, review
    placeholder, and provenance metadata.  ``claim_ids`` is mandatory for
    non-board documents so role/mechanic/interaction claims cannot leak into
    one another through a broad key prefix.
    """

    kind: DraftKind
    document_id: str
    version: str
    name: str
    frontmatter: Mapping[str, object]
    body: str
    claim_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _DRAFT_KINDS:
            raise DraftGenerationError(f"unsupported draft kind: {self.kind!r}")
        if _ID_PATTERN.fullmatch(self.document_id) is None:
            raise DraftGenerationError("document_id must be a lowercase logical ID")
        if _VERSION_PATTERN.fullmatch(self.version) is None:
            raise DraftGenerationError("version must use X.Y.Z semantic version form")
        if not isinstance(self.name, str) or not self.name.strip():
            raise DraftGenerationError("document name must not be empty")
        if not isinstance(self.frontmatter, Mapping):
            raise TypeError("frontmatter must be a mapping")
        if not isinstance(self.body, str) or not self.body.strip():
            raise DraftGenerationError("draft body must not be empty")
        if len(self.claim_ids) != len(set(self.claim_ids)):
            raise DraftGenerationError("claim_ids must not contain duplicates")
        for claim_id in self.claim_ids:
            if _ID_PATTERN.fullmatch(claim_id) is None:
                raise DraftGenerationError(f"invalid claim ID: {claim_id!r}")

    @property
    def relative_path(self) -> str:
        """Return the canonical workbench-relative Markdown path."""

        return f"{_KIND_DIRECTORY[self.kind]}/{self.document_id}/{self.version}/{self.kind}.md"


@dataclass(frozen=True, slots=True)
class DraftDocument:
    """Rendered and schema-validated Markdown document."""

    kind: DraftKind
    document_id: str
    version: str
    relative_path: str
    content: bytes
    claim_ids: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DraftGenerationContext:
    """Stable metadata supplied by the caller for one candidate package."""

    candidate_id: str
    board_ref: str
    reviewed_at: date
    reviewed_by: str = _PLACEHOLDER_REVIEWER

    def __post_init__(self) -> None:
        if _ID_PATTERN.fullmatch(self.candidate_id) is None:
            raise DraftGenerationError("candidate_id must be a lowercase logical ID")
        if self.board_ref.count("@") != 1:
            raise DraftGenerationError("board_ref must have the form board-id@version")
        board_id, version = self.board_ref.split("@", 1)
        if _ID_PATTERN.fullmatch(board_id) is None or _VERSION_PATTERN.fullmatch(version) is None:
            raise DraftGenerationError("board_ref must have the form board-id@X.Y.Z")
        if not isinstance(self.reviewed_at, date):
            raise TypeError("reviewed_at must be a date")
        if not isinstance(self.reviewed_by, str) or not self.reviewed_by.strip():
            raise DraftGenerationError("reviewed_by must not be empty")


@dataclass(frozen=True, slots=True)
class DraftPackage:
    """Immutable draft documents and their independent coverage artifact."""

    candidate_id: str
    board_ref: str
    documents: tuple[DraftDocument, ...]
    coverage: CoverageReport
    coverage_json: bytes
    publish_manifest_json: bytes
    human_review_required: bool = True

    @property
    def files(self) -> tuple[tuple[str, bytes], ...]:
        """Return all files that the workbench materializer may write."""

        document_files = tuple(
            (f"draft/{document.relative_path}", document.content) for document in self.documents
        )
        return (
            *document_files,
            ("coverage.json", self.coverage_json),
            ("publish-manifest.json", self.publish_manifest_json),
        )

    @property
    def publish_ready(self) -> bool:
        """Whether machine gates pass; human approval remains a separate gate."""

        return self.coverage.passed and self.human_review_required is False


_MODEL_BY_KIND: dict[DraftKind, type[object]] = {
    "board": BoardDefinition,
    "role": RoleDefinition,
    "mechanic": MechanicDefinition,
    "interaction": InteractionDefinition,
}


def _canonical_value(value: object) -> object:
    """Sort mapping keys while preserving semantic list order."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _yaml_bytes(frontmatter: Mapping[str, object], body: str) -> bytes:
    """Render one LF-normalized, deterministic UTF-8 Markdown document."""

    try:
        encoded_yaml = (
            cast(
                str,
                yaml.safe_dump(
                    cast(Any, _canonical_value(frontmatter)),
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                    width=120,
                ),
            )
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise DraftGenerationError("draft frontmatter is not safe YAML") from exc
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    return ("---\n" + encoded_yaml + "---\n" + normalized_body).encode("utf-8")


def _claim_key_identity(claim: RuleClaim) -> tuple[str, str]:
    return claim.scope.value, json.dumps(
        {"key": claim.key, "conditions": claim.conditions},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _default_requirements(
    bundle: ResearchBundle,
    candidate_id: str,
) -> tuple[CoverageRequirement, ...]:
    """Build a conservative matrix when a caller has no explicit matrix.

    The threshold deliberately stays at two independent sources.  A source
    marked official is still not silently promoted here; authority decisions
    belong to the host's explicit requirement matrix or human review record.
    """

    claims = [claim for claim in bundle.claims if claim.ruleset_candidate_id == candidate_id]
    grouped: dict[tuple[str, str], RuleClaim] = {}
    for claim in sorted(claims, key=lambda item: item.claim_id):
        grouped.setdefault(_claim_key_identity(claim), claim)
    requirements: list[CoverageRequirement] = []
    for claim in grouped.values():
        suffix = ""
        if claim.conditions:
            encoded_conditions = json.dumps(
                claim.conditions,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            digest = hashlib.sha256(encoded_conditions).hexdigest()[:8]
            suffix = f"-{digest}"
        requirement_id = f"{claim.scope.value.lower()}-{claim.key.replace('.', '-')}{suffix}"
        requirements.append(
            CoverageRequirement(
                requirement_id=requirement_id[:64],
                scope=claim.scope,
                key=claim.key,
                conditions=claim.conditions,
                required=True,
                min_independent_evidence_count=2,
            )
        )
    return tuple(requirements)


def _selected_claims(
    bundle: ResearchBundle,
    context: DraftGenerationContext,
    template: DraftDocumentTemplate,
) -> tuple[RuleClaim, ...]:
    claims = {claim.claim_id: claim for claim in bundle.claims}
    claim_ids = template.claim_ids
    explicit_claim_refs = template.frontmatter.get("claim_refs", ())
    if explicit_claim_refs is not None:
        if not isinstance(explicit_claim_refs, (list, tuple)):
            raise DraftGenerationError("claim_refs must be a list when provided")
        claim_ids = tuple(dict.fromkeys([*claim_ids, *(str(item) for item in explicit_claim_refs)]))
    if not claim_ids and template.kind == "board":
        claim_ids = tuple(
            claim.claim_id
            for claim in sorted(bundle.claims, key=lambda item: item.claim_id)
            if claim.ruleset_candidate_id == context.candidate_id
            and claim.scope is ClaimScope.BOARD
        )
    if not claim_ids:
        raise DraftGenerationError(
            f"{template.kind} document {template.document_id!r} must declare claim_ids"
        )
    selected: list[RuleClaim] = []
    unsupported: list[str] = []
    for claim_id in claim_ids:
        claim = claims.get(claim_id)
        if claim is None:
            raise DraftGenerationError(f"document references missing claim: {claim_id}")
        if claim.ruleset_candidate_id != context.candidate_id:
            raise DraftGenerationError(f"claim {claim_id} belongs to another candidate")
        expected_scope = _KIND_SCOPE[template.kind]
        if claim.scope is not expected_scope:
            raise DraftGenerationError(
                f"claim {claim_id} has scope {claim.scope.value}; "
                f"{template.kind} documents require {expected_scope.value} claims"
            )
        if claim.status is not ClaimStatus.SUPPORTED:
            unsupported.append(f"{claim_id} ({claim.status.value})")
        selected.append(claim)
    if unsupported:
        raise DraftGenerationBlockedError(
            "document claims are not supported: " + ", ".join(unsupported),
            claim_ids=tuple(
                claim.claim_id for claim in selected if claim.status is not ClaimStatus.SUPPORTED
            ),
        )
    return tuple(sorted(selected, key=lambda item: item.claim_id))


def _document_frontmatter(
    template: DraftDocumentTemplate,
    context: DraftGenerationContext,
    claims: Sequence[RuleClaim],
    available_source_ids: set[str],
) -> dict[str, object]:
    """Add identity/review/provenance without replacing template rule fields."""

    frontmatter = dict(template.frontmatter)
    identity = {
        "kind": template.kind,
        "id": template.document_id,
        "version": template.version,
        "name": template.name,
    }
    mismatches = [
        key
        for key, expected in identity.items()
        if key in frontmatter and frontmatter[key] != expected
    ]
    if mismatches:
        raise DraftGenerationError(
            f"{template.relative_path} frontmatter identity does not match template: "
            + ", ".join(sorted(mismatches))
        )
    frontmatter.setdefault("schema_version", 1)
    frontmatter.update(identity)
    frontmatter.setdefault("status", "published")
    frontmatter.setdefault("reviewed_by", context.reviewed_by)
    frontmatter.setdefault("reviewed_at", context.reviewed_at.isoformat())
    if frontmatter["status"] != "published":
        raise DraftGenerationError(
            f"{template.relative_path} must use status: published for publisher compatibility"
        )
    explicit_claim_refs = frontmatter.get("claim_refs", ())
    if not isinstance(explicit_claim_refs, (list, tuple)):
        raise DraftGenerationError("claim_refs must be a list when provided")
    claim_refs = tuple(
        dict.fromkeys([*map(str, explicit_claim_refs), *(claim.claim_id for claim in claims)])
    )
    evidence_ids = tuple(
        sorted({evidence_id for claim in claims for evidence_id in claim.evidence_ids})
    )
    explicit_source_refs = frontmatter.get("source_refs", ())
    if not isinstance(explicit_source_refs, (list, tuple)):
        raise DraftGenerationError("source_refs must be a list when provided")
    explicit_sources = tuple(dict.fromkeys(map(str, explicit_source_refs)))
    unclosed_sources = sorted(set(explicit_sources) - set(evidence_ids))
    if unclosed_sources:
        raise DraftGenerationError(
            "document source_refs are not closed by selected claims: " + ", ".join(unclosed_sources)
        )
    source_refs = tuple(dict.fromkeys([*explicit_sources, *evidence_ids]))
    missing_sources = sorted(set(source_refs) - available_source_ids)
    if missing_sources:
        raise DraftGenerationError(
            "document references missing source IDs: " + ", ".join(missing_sources)
        )
    frontmatter["claim_refs"] = list(claim_refs)
    frontmatter["source_refs"] = list(source_refs)
    return frontmatter


def _provenance_body(body: str, claims: Sequence[RuleClaim]) -> str:
    """Append a non-executable provenance section for human review."""

    normalized = body.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    if "{#provenance}" in normalized:
        return normalized + "\n"
    lines = [normalized, "", "## Provenance {#provenance}", ""]
    lines.extend(
        f"- `{claim.claim_id}` — `{claim.scope.value}.{claim.key}`; evidence: "
        f"{', '.join(f'`{item}`' for item in claim.evidence_ids)}"
        for claim in claims
    )
    return "\n".join(lines) + "\n"


def _validate_model(kind: DraftKind, frontmatter: Mapping[str, object]) -> object:
    model_type = _MODEL_BY_KIND[kind]
    try:
        # The published models intentionally accept their compact YAML
        # ``id@version`` reference spelling through pre-validators.  Strict
        # model validation would reject that safe textual representation
        # before those reference parsers run; the frontmatter parser has
        # already constrained YAML to safe scalar/list/mapping values.
        return model_type.model_validate(frontmatter, strict=False)  # type: ignore[attr-defined]
    except (ValidationError, TypeError, ValueError) as exc:
        raise DraftGenerationError(
            f"{kind} frontmatter does not satisfy published knowledge schema"
        ) from exc


def _manifest_bytes(context: DraftGenerationContext, documents: Sequence[DraftDocument]) -> bytes:
    entries = [
        {"path": document.relative_path, "sha256": hashlib.sha256(document.content).hexdigest()}
        for document in sorted(documents, key=lambda item: item.relative_path)
    ]
    payload = {
        "schema_version": 1,
        "package_id": context.board_ref,
        "board_ref": context.board_ref,
        "candidate_id": context.candidate_id,
        "status": "CANDIDATE_PENDING_HUMAN_REVIEW",
        "files": entries,
    }
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _validate_dependency_closure(
    context: DraftGenerationContext,
    documents: Sequence[DraftDocument],
    models: Mapping[str, object],
) -> None:
    board_docs = [document for document in documents if document.kind == "board"]
    if len(board_docs) != 1:
        raise DraftGenerationError("a draft package must contain exactly one board document")
    board = cast(BoardDefinition, models[board_docs[0].relative_path])
    board_ref = getattr(board, "board_ref")
    if board_ref.format() != context.board_ref:
        raise DraftGenerationError("board document does not match context.board_ref")
    if board_ref.id != context.candidate_id:
        raise DraftGenerationError("board document ID does not match candidate_id")
    expected: set[str] = set()
    for binding in board.role_bindings:
        expected.add(f"roles/{binding.role_ref.id}/{binding.role_ref.version}/role.md")
    for reference in board.mechanic_refs:
        expected.add(f"mechanics/{reference.id}/{reference.version}/mechanic.md")
    for reference in board.interaction_refs:
        expected.add(f"interactions/{reference.id}/{reference.version}/interaction.md")
    actual = {document.relative_path for document in documents if document.kind != "board"}
    missing = sorted(expected - actual)
    if missing:
        raise DraftGenerationError("draft dependency closure is incomplete: " + ", ".join(missing))


def generate_draft_package(
    bundle: ResearchBundle,
    templates: Iterable[DraftDocumentTemplate],
    *,
    context: DraftGenerationContext,
    requirements: Iterable[CoverageRequirement | Mapping[str, object]] | None = None,
) -> DraftPackage:
    """Generate schema-valid Markdown and a conservative coverage artifact.

    Templates are the explicit source of machine rules.  Claims only provide
    provenance and authorization to carry those values into a draft.  The
    returned package remains ``CANDIDATE_PENDING_HUMAN_REVIEW`` even when the
    coverage matrix passes; callers must invoke the publisher with an exact
    human review record later.
    """

    if not isinstance(bundle, ResearchBundle):
        raise TypeError("generate_draft_package() expects a ResearchBundle")
    if not isinstance(context, DraftGenerationContext):
        raise TypeError("context must be a DraftGenerationContext")
    templates_snapshot = tuple(templates)
    if not templates_snapshot:
        raise DraftGenerationError("at least one draft template is required")
    candidate_claims = tuple(
        claim for claim in bundle.claims if claim.ruleset_candidate_id == context.candidate_id
    )
    if not candidate_claims:
        raise DraftGenerationError("bundle contains no claims for context.candidate_id")
    matrix = (
        tuple(requirements)
        if requirements is not None
        else _default_requirements(bundle, context.candidate_id)
    )
    coverage = analyze_coverage(bundle, context.candidate_id, matrix)
    coverage_bytes = render_coverage_artifacts(coverage, bundle_sha256(bundle)).coverage

    documents: list[DraftDocument] = []
    models: dict[str, object] = {}
    paths: set[str] = set()
    for template in sorted(templates_snapshot, key=lambda item: item.relative_path):
        if template.relative_path in paths:
            raise DraftGenerationError(f"duplicate draft path: {template.relative_path}")
        paths.add(template.relative_path)
        try:
            claims = _selected_claims(bundle, context, template)
        except DraftGenerationBlockedError as exc:
            if exc.coverage is not None:
                raise
            raise DraftGenerationBlockedError(
                str(exc),
                claim_ids=exc.claim_ids,
                coverage=coverage,
            ) from exc
        frontmatter = _document_frontmatter(
            template,
            context,
            claims,
            {source.source_id for source in bundle.sources},
        )
        model = _validate_model(template.kind, frontmatter)
        content = _yaml_bytes(frontmatter, _provenance_body(template.body, claims))
        try:
            parsed = parse_markdown(content)
        except (FrontmatterParseError, TypeError, ValueError) as exc:
            raise DraftGenerationError(
                f"rendered document cannot be parsed: {template.relative_path}"
            ) from exc
        if parsed.frontmatter != _canonical_value(frontmatter):
            raise DraftGenerationError(
                f"rendered frontmatter changed during YAML round trip: {template.relative_path}"
            )
        documents.append(
            DraftDocument(
                kind=template.kind,
                document_id=template.document_id,
                version=template.version,
                relative_path=template.relative_path,
                content=content,
                claim_ids=tuple(claim.claim_id for claim in claims),
                source_ids=tuple(
                    sorted({source for claim in claims for source in claim.evidence_ids})
                ),
            )
        )
        models[template.relative_path] = model
    _validate_dependency_closure(context, documents, models)
    return DraftPackage(
        candidate_id=context.candidate_id,
        board_ref=context.board_ref,
        documents=tuple(documents),
        coverage=coverage,
        coverage_json=coverage_bytes,
        publish_manifest_json=_manifest_bytes(context, documents),
    )


async def materialize_draft_package(package: DraftPackage, job_root: str | Path) -> None:
    """Write a package below one job directory without overwriting anything.

    Bytes are built in a private staging directory first.  The publish
    manifest is committed last, and any failure during the commit removes the
    files committed by this attempt.  A retry therefore cannot mistake a
    partially written candidate for a complete package.
    """

    if not isinstance(package, DraftPackage):
        raise TypeError("materialize_draft_package() expects a DraftPackage")
    root = Path(job_root).resolve()
    lock = _MATERIALIZE_LOCKS.setdefault(root, asyncio.Lock())
    async with lock:
        writes = tuple(
            (relative, content, resolve_contained_path(root, relative))
            for relative, content in package.files
        )
        occupied = [path for _, _, path in writes if path.exists() or path.is_symlink()]
        if occupied:
            # A validation job can fail after this method has committed every
            # file but before its state transition is persisted.  Retrying
            # that job must recognize the exact same immutable package as an
            # already-completed write.  Any partial package, symlink, or byte
            # mismatch remains a hard conflict and is never overwritten.
            if len(occupied) == len(writes) and await _existing_package_matches(
                writes,
            ):
                return
            raise DraftOutputExistsError(
                "refusing to overwrite existing workbench files: "
                + ", ".join(path.relative_to(root).as_posix() for path in occupied)
            )

        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".draft-package-{uuid.uuid4().hex}"
        committed: list[Path] = []
        try:
            for relative, content in package.files:
                stage_path = resolve_contained_path(staging, relative)
                stage_path.parent.mkdir(parents=True, exist_ok=True)
                await atomic_write_bytes(stage_path, content)

            # The marker is deliberately the final rename.  Readers only
            # treat a package as complete after this file is present.
            commit_items = sorted(
                zip(package.files, writes, strict=True),
                key=lambda item: item[0][0] == "publish-manifest.json",
            )
            for (relative, _), (relative_target, _, target) in commit_items:
                if relative != relative_target:
                    raise DraftGenerationError("draft package paths changed during materialization")
                if target.exists() or target.is_symlink():
                    raise DraftOutputExistsError(
                        f"refusing to overwrite existing workbench files: {relative}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(resolve_contained_path(staging, relative), target)
                committed.append(target)
        except Exception:
            for path in reversed(committed):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            shutil.rmtree(staging, ignore_errors=True)
            raise
        else:
            shutil.rmtree(staging, ignore_errors=True)


async def _existing_package_matches(
    writes: Sequence[tuple[str, bytes, Path]],
) -> bool:
    """Return whether every existing package path is the exact expected file.

    This helper is intentionally conservative: symlinks and anything other
    than regular files are conflicts even when reading them would happen to
    produce the expected bytes.  The caller has already established that all
    paths are occupied, so a missing or unreadable path is a mismatch.
    """

    for _, expected, path in writes:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            actual = await asyncio.to_thread(path.read_bytes)
        except OSError:
            return False
        if actual != expected:
            return False
    return True


DraftGenerator = generate_draft_package


__all__ = [
    "DraftDocument",
    "DraftDocumentTemplate",
    "DraftGenerationBlockedError",
    "DraftGenerationContext",
    "DraftGenerationError",
    "DraftGenerator",
    "DraftOutputExistsError",
    "DraftPackage",
    "generate_draft_package",
    "materialize_draft_package",
]
