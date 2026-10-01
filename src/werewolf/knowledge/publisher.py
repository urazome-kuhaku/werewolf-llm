"""Validated, immutable publication of one workbench knowledge package.

The publisher is deliberately a narrow boundary between a research job and
the runtime vault.  It reads a completed draft, validates the workbench
evidence artifacts and an explicit human approval record, then validates the
draft through the normal package loader/compiler before committing it.  No
reviewer identity or decision is invented by this module.

Publication is performed by building a complete sibling tree and swapping it
into place.  A failed validation therefore cannot leave a partly published
dependency closure.  Existing package versions are content-addressed and are
never overwritten; a repeat publication with the same document bytes is
idempotent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from werewolf.persistence import PathSecurityError, resolve_contained_path
from werewolf.ruleset_workbench.claims import ClaimStatus, RuleClaim
from werewolf.ruleset_workbench.evidence import SourceEvidence

from .action_contract import RoleActionContractError, validate_role_action_contract
from .compiled_store import (
    CompiledKnowledgePackageLoad,
    CompiledKnowledgeStore,
    CompiledPackageAlreadyExistsError,
    CompiledPackageNotFoundError,
)
from .compiler import CompiledKnowledgePackage, KnowledgePackageCompiler
from .frontmatter import FrontmatterParseError, parse_markdown
from .package_loader import KnowledgePackageError, KnowledgePackageLoader
from .refs import VersionedRef

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_ID_RE = re.compile(r"^[a-z0-9_-]+$", re.ASCII)
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$", re.ASCII)
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_YAML_BYTES = 4 * 1024 * 1024
_MAX_JSON_BYTES = 4 * 1024 * 1024
_PUBLISH_LOCKS: dict[Path, asyncio.Lock] = {}


class KnowledgePublishError(ValueError):
    """Base error raised when a workbench package cannot be published."""


class PublishManifestError(KnowledgePublishError):
    """Raised when the workbench publish manifest is incomplete or unsafe."""


class PublishGateError(KnowledgePublishError):
    """Raised when a research, coverage, conflict, or review gate fails."""


class PublishedPackageConflictError(KnowledgePublishError):
    """Raised when a package ID/version already has different content."""


class ReviewRecord(BaseModel):
    """An explicit approval supplied by a human review workflow.

    ``reviewer`` is accepted as a compatibility spelling, while
    ``reviewed_by`` remains the canonical persisted name.  The publisher
    requires this object from the caller and copies no identity from the
    process environment or the current model.
    """

    model_config = ConfigDict(
        alias_generator=None,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    reviewed_by: Annotated[
        str,
        StringConstraints(min_length=1, max_length=256, strict=True),
    ] = Field(
        validation_alias=AliasChoices("reviewed_by", "reviewer"),
    )
    reviewed_at: date
    decision: Literal["APPROVED"]
    approved_manifest_sha256: str
    note: str | None = None

    @field_validator("reviewed_by")
    @classmethod
    def validate_reviewer(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reviewed_by must be a non-empty human identity")
        normalized = "-".join(value.split()).casefold()
        if normalized.replace("_", "-") in {
            "anonymous",
            "auto",
            "automated",
            "none",
            "null",
            "n-a",
            "na",
            "pending",
            "pending-human-review",
            "pending-review",
            "system",
            "tbd",
            "todo",
            "unknown",
            "unspecified",
        }:
            raise ValueError("reviewed_by must identify a human reviewer")
        return value.strip()

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def normalize_reviewed_at(cls, value: object) -> object:
        if isinstance(value, datetime):
            if value.tzinfo is not None and value.utcoffset() != timedelta(0):
                raise ValueError("reviewed_at datetime must be UTC")
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("reviewed_at must be an ISO calendar date") from exc
        raise TypeError("reviewed_at must be a date or ISO date string")

    @field_validator("approved_manifest_sha256")
    @classmethod
    def validate_approved_digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("approved_manifest_sha256 must be lowercase SHA-256")
        return value

    @property
    def reviewer(self) -> str:
        """Compatibility spelling used by review callers."""

        return self.reviewed_by


class PublishResult(BaseModel):
    """Detached result of one successful, immutable publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    package_id: str
    board_ref: str
    published_root: Path
    compiled_path: Path
    package_sha256: str
    manifest_sha256: str
    idempotent: bool


# Descriptive aliases keep callers independent from the exact review naming
# used by the CLI and design documents.
HumanReviewRecord = ReviewRecord
RulesetReviewRecord = ReviewRecord
KnowledgePublishResult = PublishResult


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise PublishManifestError("publish artifact is not canonical JSON") from exc


def _decode_json(data: bytes, *, name: str, max_bytes: int = _MAX_JSON_BYTES) -> object:
    if len(data) > max_bytes:
        raise PublishGateError(f"{name} exceeds its size limit")
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PublishGateError(f"{name} is not valid UTF-8 JSON") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


class _StrictYamlLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader that rejects duplicate mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, object]:
        result: dict[str, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise yaml.YAMLError("YAML mapping keys must be strings")
            if key in result:
                raise yaml.YAMLError(f"duplicate YAML mapping key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _decode_yaml(data: bytes, *, name: str) -> object:
    if len(data) > _MAX_YAML_BYTES:
        raise PublishGateError(f"{name} exceeds its size limit")
    try:
        loader = _StrictYamlLoader(data.decode("utf-8"))
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError, MemoryError) as exc:
        raise PublishGateError(f"{name} is not valid safe YAML") from exc


def _read_bytes(path: Path, *, name: str, max_bytes: int = _MAX_ARTIFACT_BYTES) -> bytes:
    try:
        with path.open("rb") as source:
            data = source.read(max_bytes + 1)
    except OSError as exc:
        raise PublishGateError(f"cannot read {name}") from exc
    if len(data) > max_bytes:
        raise PublishGateError(f"{name} exceeds its size limit")
    return data


def _normalized_relative_path(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PublishManifestError(f"{field_name} must be a non-empty relative path")
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        "\\" in value
        or ":" in value
        or path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or path.parts != tuple(value.split("/"))
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise PublishManifestError(f"{field_name} must be a normalized relative POSIX path")
    return value


def _contained(root: Path, relative_path: str, *, field_name: str) -> Path:
    try:
        return resolve_contained_path(root, relative_path)
    except PathSecurityError as exc:
        raise PublishGateError(f"{field_name} escapes its trusted root") from exc


def _as_records(value: object, *, field_name: str) -> list[dict[str, object]]:
    if isinstance(value, dict):
        for key in (field_name, "items", "records", "sources", "claims"):
            nested = value.get(key)
            if isinstance(nested, list):
                value = nested
                break
        else:
            # A mapping keyed by logical ID is a convenient YAML form.
            records: list[dict[str, object]] = []
            for identifier, record in value.items():
                if not isinstance(identifier, str) or not isinstance(record, dict):
                    raise PublishGateError(f"{field_name} must contain object records")
                item = dict(record)
                id_field = "source_id" if field_name == "sources" else "claim_id"
                item.setdefault(id_field, identifier)
                records.append(item)
            return records
    if not isinstance(value, list) or not value:
        raise PublishGateError(f"{field_name} must be a non-empty array")
    if any(not isinstance(item, dict) for item in value):
        raise PublishGateError(f"{field_name} must contain object records")
    return [dict(cast(dict[str, object], item)) for item in value]


def _load_records(path: Path, *, field_name: str) -> list[dict[str, object]]:
    raw = _read_bytes(path, name=field_name)
    if path.suffix.lower() == ".jsonl":
        records: list[dict[str, object]] = []
        for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            value = _decode_json(line.encode("utf-8"), name=f"{field_name} line {line_number}")
            if not isinstance(value, dict):
                raise PublishGateError(f"{field_name} line {line_number} must be an object")
            records.append(dict(value))
        return _as_records(records, field_name=field_name)
    value = _decode_yaml(raw, name=field_name)
    return _as_records(value, field_name=field_name)


def _logical_ref(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise PublishGateError(f"{field_name} contains an invalid logical ID")
    return value


def _string_refs(value: object, *, field_name: str) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PublishGateError(f"{field_name} must be a string array")
    return {_logical_ref(item, field_name=field_name) for item in value}


def _collect_document_refs(value: object, *, claim_refs: set[str], source_refs: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"claim_refs", "override_claim_refs"}:
                claim_refs.update(_string_refs(child, field_name=key))
            elif key == "source_refs":
                source_refs.update(_string_refs(child, field_name=key))
            else:
                _collect_document_refs(child, claim_refs=claim_refs, source_refs=source_refs)
    elif isinstance(value, list):
        for child in value:
            _collect_document_refs(child, claim_refs=claim_refs, source_refs=source_refs)


def _scan_blocking(value: object, *, path: str = "$") -> list[str]:
    blockers: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if (
                key_text
                in {
                    "resolution",
                    "status",
                    "state",
                    "decision",
                    "outcome",
                }
                and isinstance(child, str)
                and child.upper() in {"NEEDS_DECISION", "UNVERIFIED"}
            ):
                blockers.append(f"{path}.{key_text}={child}")
            if key_text in {"needs_human_decision", "unresolved"} and child is True:
                blockers.append(f"{path}.{key_text}=true")
            blockers.extend(_scan_blocking(child, path=f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            blockers.extend(_scan_blocking(child, path=f"{path}[{index}]"))
    return blockers


def _coverage_passed(value: object) -> None:
    if not isinstance(value, dict):
        raise PublishGateError("coverage.json must be an object")
    required_count = value.get("required_count")
    satisfied_count = value.get("satisfied_count")
    percentage = value.get("coverage_percentage")
    if value.get("passed") is not True:
        raise PublishGateError("coverage required gate did not pass")
    if not isinstance(required_count, int) or required_count <= 0:
        raise PublishGateError("coverage must contain at least one required item")
    if satisfied_count != required_count or percentage != 100.0:
        raise PublishGateError("coverage.required must be 100%")
    blocking = value.get("blocking_requirement_ids", [])
    if not isinstance(blocking, list) or blocking:
        raise PublishGateError("coverage contains blocking requirements")
    required = value.get("required")
    if isinstance(required, dict) and (
        required.get("total") != required_count
        or required.get("satisfied") != satisfied_count
        or required.get("percentage") != 100.0
    ):
        raise PublishGateError("coverage required summary is inconsistent")
    items = value.get("items")
    if not isinstance(items, list) or len(items) < required_count:
        raise PublishGateError("coverage items are incomplete")
    required_items = [
        item for item in items if isinstance(item, dict) and item.get("required") is True
    ]
    if len(required_items) != required_count or any(
        item.get("status") != "SATISFIED" or item.get("blocking") is True for item in required_items
    ):
        raise PublishGateError("every required coverage item must be SATISFIED")


def _manifest_files(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise PublishManifestError("publish manifest must be a JSON object")
    raw_files = value.get("files", value.get("documents"))
    if not isinstance(raw_files, list) or not raw_files:
        raise PublishManifestError("publish manifest files must be a non-empty array")
    entries: list[tuple[str, str]] = []
    for index, raw in enumerate(raw_files):
        if isinstance(raw, str):
            relative_path = _normalized_relative_path(raw, field_name=f"files[{index}]")
            digest = ""
        elif isinstance(raw, dict):
            relative_path = _normalized_relative_path(
                raw.get("path", raw.get("relative_path")),
                field_name=f"files[{index}].path",
            )
            digest_value = raw.get("sha256", raw.get("content_sha256", ""))
            if not isinstance(digest_value, str) or (
                digest_value and _SHA256_RE.fullmatch(digest_value) is None
            ):
                raise PublishManifestError(f"files[{index}].sha256 is invalid")
            digest = digest_value
        else:
            raise PublishManifestError(f"files[{index}] must be a path or object")
        if not relative_path.endswith(".md"):
            raise PublishManifestError("publish manifest files must be Markdown documents")
        if not (
            relative_path.startswith("boards/")
            or relative_path.startswith("roles/")
            or relative_path.startswith("mechanics/")
            or relative_path.startswith("interactions/")
        ):
            raise PublishManifestError(f"unsupported published document path: {relative_path}")
        entries.append((relative_path, digest))
    if len({path for path, _ in entries}) != len(entries):
        raise PublishManifestError("publish manifest contains duplicate file paths")
    return tuple(sorted(entries))


def _package_ref(manifest: Mapping[str, object]) -> VersionedRef:
    board_ref_value = manifest.get("board_ref")
    package_id_value = manifest.get("package_id")
    if (
        board_ref_value is not None
        and package_id_value is not None
        and board_ref_value != package_id_value
    ):
        raise PublishManifestError("publish manifest board_ref and package_id disagree")
    raw_ref = board_ref_value if board_ref_value is not None else package_id_value
    if raw_ref is None and isinstance(manifest.get("board"), str):
        raw_ref = manifest["board"]
    if not isinstance(raw_ref, str):
        board_id = manifest.get("id", manifest.get("board_id"))
        version = manifest.get("version")
        if isinstance(board_id, str) and isinstance(version, str):
            raw_ref = f"{board_id}@{version}"
    if not isinstance(raw_ref, str):
        raise PublishManifestError("publish manifest must contain board_ref/package_id")
    try:
        return VersionedRef.parse(raw_ref)
    except (TypeError, ValueError) as exc:
        raise PublishManifestError("publish manifest board_ref is invalid") from exc


def _manifest_content_digest(entries: Sequence[tuple[str, str]]) -> str:
    payload = [{"path": path, "sha256": digest} for path, digest in sorted(entries)]
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _logical_manifest(
    package_ref: VersionedRef, entries: Sequence[tuple[str, str]]
) -> dict[str, object]:
    """Return the canonical, reviewable identity of a candidate manifest."""

    return {
        "board_ref": package_ref.format(),
        "documents": [{"path": path, "sha256": digest} for path, digest in sorted(entries)],
        "package_id": package_ref.format(),
        "schema_version": 1,
    }


def _logical_manifest_digest(package_ref: VersionedRef, entries: Sequence[tuple[str, str]]) -> str:
    return hashlib.sha256(_canonical_json(_logical_manifest(package_ref, entries))).hexdigest()


def _manifest_output(
    *,
    package_ref: VersionedRef,
    entries: Sequence[tuple[str, str]],
    candidate_manifest_sha256: str,
    source_ids: Sequence[str],
    claim_ids: Sequence[str],
    review: ReviewRecord,
    compiled: CompiledKnowledgePackage,
) -> dict[str, object]:
    logical = _logical_manifest(package_ref, entries)
    return {
        **logical,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "claims": sorted(set(claim_ids)),
        "compiled_package_sha256": compiled.package_identity,
        "documents_sha256": _manifest_content_digest(entries),
        "manifest_sha256": hashlib.sha256(_canonical_json(logical)).hexdigest(),
        "review": {
            "approved_manifest_sha256": review.approved_manifest_sha256,
            "reviewed_at": review.reviewed_at.isoformat(),
            "reviewed_by": review.reviewed_by,
            "decision": review.decision,
        },
        "sources": sorted(set(source_ids)),
    }


def _manifest_file_bytes(payload: Mapping[str, object]) -> bytes:
    return _canonical_json(payload) + b"\n"


def _manifest_identity(value: object) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise PublishedPackageConflictError("existing publication manifest is invalid")
    package_id = value.get("package_id")
    digest = value.get("documents_sha256")
    compiled_digest = value.get("compiled_package_sha256")
    if not isinstance(package_id, str) or not isinstance(digest, str):
        raise PublishedPackageConflictError("existing publication manifest lacks content identity")
    return package_id, digest, compiled_digest if isinstance(compiled_digest, str) else ""


def _copy_tree_without_symlinks(source: Path, target: Path) -> None:
    if source.exists():
        if source.is_symlink() or not source.is_dir():
            raise PublishGateError("published root must be a regular directory")
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            if item.is_symlink():
                raise PublishGateError(f"published tree contains a symlink: {relative}")
            destination = target / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, destination)


def _materialize_review_metadata(data: bytes, review: ReviewRecord) -> bytes:
    """Apply explicit review metadata to a staged Markdown copy.

    Workbench documents are candidate inputs and remain byte-for-byte
    unchanged. Parsing first keeps this rewrite within the same bounded YAML
    contract used by the runtime loader.
    """

    try:
        parsed = parse_markdown(data)
        frontmatter = dict(parsed.frontmatter)
        frontmatter["reviewed_by"] = review.reviewed_by
        frontmatter["reviewed_at"] = review.reviewed_at.isoformat()
        yaml_text = cast(
            str,
            yaml.safe_dump(
                frontmatter,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
                width=120,
            ),
        )
    except (FrontmatterParseError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise PublishGateError("draft document review metadata could not be materialized") from exc
    return ("---\n" + yaml_text + "---\n" + parsed.body).encode("utf-8")


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _swap_directory(stage: Path, final: Path) -> None:
    backup: Path | None = None
    try:
        if final.exists() or final.is_symlink():
            backup = final.with_name(f".{final.name}.backup-{os.getpid()}-{uuid4().hex}")
            os.rename(final, backup)
        os.rename(stage, final)
    except BaseException:
        if backup is not None and not (final.exists() or final.is_symlink()) and backup.exists():
            os.rename(backup, final)
        raise
    else:
        if backup is not None:
            _remove_tree(backup)


class RulesetPublisher:
    """Publish a validated draft below one vault root."""

    def __init__(
        self,
        vault_root: str | os.PathLike[str],
        *,
        compiled_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._vault_root = Path(vault_root).resolve()
        self._published_root = self._vault_root / "published"
        self._compiled_root = (
            Path(compiled_root).resolve()
            if compiled_root is not None
            else self._vault_root / "compiled"
        )
        self._compiled_store = CompiledKnowledgeStore(self._compiled_root)

    @property
    def vault_root(self) -> Path:
        return self._vault_root

    @property
    def published_root(self) -> Path:
        return self._published_root

    async def publish(
        self,
        job: str | os.PathLike[str],
        review_record: ReviewRecord | Mapping[str, object] | None = None,
        *,
        review: ReviewRecord | Mapping[str, object] | None = None,
    ) -> PublishResult:
        """Validate and immutably publish one workbench draft.

        ``review_record`` is required explicitly.  ``review`` is a keyword
        alias for integrations that use the design-document terminology.
        """

        if review_record is not None and review is not None:
            raise TypeError("pass only one of review_record or review")
        supplied_review = review_record if review_record is not None else review
        if supplied_review is None:
            raise PublishGateError("an explicit human review record is required")
        try:
            approval = (
                supplied_review
                if isinstance(supplied_review, ReviewRecord)
                else ReviewRecord.model_validate(supplied_review, strict=False)
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise PublishGateError("review record is not an explicit APPROVED decision") from exc
        job_dir = self._job_path(job)
        lock = _PUBLISH_LOCKS.setdefault(self._published_root, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._publish_sync, job_dir, approval)

    async def publish_package(
        self,
        job: str | os.PathLike[str],
        *,
        review_record: ReviewRecord | Mapping[str, object],
    ) -> PublishResult:
        """Compatibility spelling for callers naming the operation package publication."""

        return await self.publish(job, review_record)

    def _job_path(self, job: str | os.PathLike[str]) -> Path:
        try:
            raw = os.fspath(job)
        except TypeError as exc:
            raise PublishGateError("job must be a path or workbench job ID") from exc
        if isinstance(raw, bytes):
            raw = os.fsdecode(raw)
        job_path = Path(raw)
        if job_path.is_absolute():
            resolved = job_path.resolve()
            try:
                resolved.relative_to((self._vault_root / "_workbench").resolve())
            except ValueError as exc:
                raise PublishGateError("job path must be below vault/_workbench") from exc
            return resolved
        return _contained(self._vault_root / "_workbench", str(job), field_name="job")

    def _publish_sync(self, job_dir: Path, review: ReviewRecord) -> PublishResult:
        if not job_dir.is_dir() or job_dir.is_symlink():
            raise PublishGateError("workbench job directory is missing")
        draft_dir = _contained(job_dir, "draft", field_name="draft")
        if not draft_dir.is_dir() or draft_dir.is_symlink():
            raise PublishGateError("workbench draft directory is missing")

        manifest_path = job_dir / "publish-manifest.json"
        if not manifest_path.is_file():
            alternate = draft_dir / "publish-manifest.json"
            if alternate.is_file():
                manifest_path = alternate
            else:
                raise PublishManifestError("publish-manifest.json is missing")
        manifest = _decode_json(
            _read_bytes(manifest_path, name="publish-manifest.json", max_bytes=_MAX_MANIFEST_BYTES),
            name="publish-manifest.json",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        if not isinstance(manifest, dict):
            raise PublishManifestError("publish-manifest.json must be an object")
        package_ref = _package_ref(manifest)
        entries = _manifest_files(manifest)
        source_path = self._artifact_path(job_dir, draft_dir, manifest, "sources", "sources.yaml")
        claim_path = self._artifact_path(job_dir, draft_dir, manifest, "claims", "claims.yaml")
        conflict_path = self._artifact_path(
            job_dir, draft_dir, manifest, "conflicts", "conflicts.json"
        )
        coverage_path = self._artifact_path(
            job_dir, draft_dir, manifest, "coverage", "coverage.json"
        )
        source_records = _load_records(source_path, field_name="sources.yaml")
        claim_records = _load_records(claim_path, field_name="claims.yaml")
        try:
            sources = [
                SourceEvidence.model_validate(record, strict=False) for record in source_records
            ]
            claims = [RuleClaim.model_validate(record, strict=False) for record in claim_records]
        except (TypeError, ValidationError, ValueError) as exc:
            raise PublishGateError("sources.yaml or claims.yaml failed schema validation") from exc

        source_ids = [source.source_id for source in sources]
        claim_ids = [claim.claim_id for claim in claims]
        if len(source_ids) != len(set(source_ids)) or len(claim_ids) != len(set(claim_ids)):
            raise PublishGateError("sources and claims must have unique logical IDs")
        source_id_set = set(source_ids)
        missing_claim_evidence = sorted(
            {
                evidence_id
                for claim in claims
                for evidence_id in claim.evidence_ids
                if evidence_id not in source_id_set
            }
        )
        if missing_claim_evidence:
            raise PublishGateError(
                "claims reference missing source IDs: " + ", ".join(missing_claim_evidence)
            )
        candidate_ids = {claim.ruleset_candidate_id for claim in claims}
        if (
            candidate_ids
            and package_ref.id not in candidate_ids
            and package_ref.format() not in candidate_ids
        ):
            raise PublishGateError("claims do not belong to the published board candidate")
        unverified_claims = sorted(
            claim.claim_id for claim in claims if claim.status is ClaimStatus.UNVERIFIED
        )
        if unverified_claims:
            raise PublishGateError(
                "unverified claims block publication: " + ", ".join(unverified_claims)
            )

        conflicts = _decode_json(
            _read_bytes(conflict_path, name="conflicts.json"), name="conflicts.json"
        )
        blockers = _scan_blocking(conflicts)
        if blockers:
            raise PublishGateError(
                "conflicts.json contains unresolved blockers: " + "; ".join(blockers)
            )
        coverage = _decode_json(
            _read_bytes(coverage_path, name="coverage.json"), name="coverage.json"
        )
        _coverage_passed(coverage)

        draft_files = self._validate_draft_documents(
            draft_dir,
            entries,
            package_ref,
            source_id_set,
            {claim.claim_id: claim for claim in claims},
        )
        expected_entries = tuple(
            (relative_path, digest or hashlib.sha256(draft_files[relative_path]).hexdigest())
            for relative_path, digest in entries
        )
        for relative_path, digest in expected_entries:
            actual = hashlib.sha256(draft_files[relative_path]).hexdigest()
            if actual != digest:
                raise PublishManifestError(f"publish manifest hash mismatch: {relative_path}")

        # The review must approve this exact candidate dependency closure.  Do
        # this before the existing-publication fast path so an idempotent
        # retry cannot use a generic approval record for different content.
        candidate_manifest_digest = _logical_manifest_digest(package_ref, expected_entries)
        if review.approved_manifest_sha256 != candidate_manifest_digest:
            raise PublishGateError("review record does not approve this publish manifest")

        # Only after the explicit review has approved the original candidate
        # hashes do we materialize review metadata into staging. The
        # workbench candidate itself is never rewritten.
        materialized_files = {
            relative_path: _materialize_review_metadata(content, review)
            for relative_path, content in draft_files.items()
        }
        final_entries = tuple(
            (relative_path, hashlib.sha256(materialized_files[relative_path]).hexdigest())
            for relative_path, _ in expected_entries
        )

        manifest_path_in_published = (
            self._published_root / "manifests" / f"{package_ref.format()}.json"
        )
        existing_payload = self._load_existing_manifest(manifest_path_in_published)
        expected_documents_digest = _manifest_content_digest(final_entries)
        if existing_payload is not None:
            existing_package, existing_digest, _ = _manifest_identity(existing_payload)
            if (
                existing_package != package_ref.format()
                or existing_digest != expected_documents_digest
            ):
                raise PublishedPackageConflictError(
                    f"published version already contains different content: {package_ref.format()}"
                )
            if existing_payload.get("candidate_manifest_sha256") != candidate_manifest_digest:
                raise PublishedPackageConflictError(
                    f"published version has a different candidate manifest: {package_ref.format()}"
                )
            existing_review = existing_payload.get("review")
            expected_review = {
                "approved_manifest_sha256": review.approved_manifest_sha256,
                "reviewed_at": review.reviewed_at.isoformat(),
                "reviewed_by": review.reviewed_by,
                "decision": review.decision,
            }
            if existing_review != expected_review:
                raise PublishedPackageConflictError(
                    f"published version has a different review record: {package_ref.format()}"
                )
            existing_compiled = self._load_compiled_if_present(package_ref)
            if existing_compiled is None:
                raise PublishedPackageConflictError(
                    "published package has no matching compiled package"
                )
            return self._result_from_existing(package_ref, existing_payload, existing_compiled)

        for relative_path, _ in expected_entries:
            existing_document = self._published_root / relative_path
            if existing_document.exists() or existing_document.is_symlink():
                raise PublishedPackageConflictError(
                    "published document exists without an immutable package manifest: "
                    + relative_path
                )

        stage = Path(
            tempfile.mkdtemp(
                prefix=f".{self._published_root.name}.staging-", dir=self._published_root.parent
            )
        )
        try:
            _copy_tree_without_symlinks(self._published_root, stage)
            for relative_path, content in materialized_files.items():
                destination = _contained(stage, relative_path, field_name="draft document")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

            loader = KnowledgePackageLoader(stage)
            try:
                package = asyncio.run(loader.load(package_ref))
            except (KnowledgePackageError, OSError, ValueError) as exc:
                raise PublishGateError(
                    "staged Markdown dependency closure failed validation"
                ) from exc
            loaded_paths = {document.relative_path for document in package.documents}
            listed_paths = {path for path, _ in expected_entries}
            if loaded_paths != listed_paths:
                missing = sorted(loaded_paths - listed_paths)
                extra = sorted(listed_paths - loaded_paths)
                details = []
                if missing:
                    details.append("missing=" + ",".join(missing))
                if extra:
                    details.append("extra=" + ",".join(extra))
                raise PublishGateError(
                    "publish manifest must equal the complete dependency closure: "
                    + "; ".join(details)
                )
            try:
                # Load the process-wide action registry only at the publication
                # boundary.  This keeps knowledge models independent from the
                # game reducer while ensuring every effective role is checked
                # before compiler output or a published-tree swap can happen.
                from werewolf.game.actions import load_action_registry

                validate_role_action_contract(
                    {role_id: document.model for role_id, document in package.roles.items()},
                    load_action_registry(),
                )
            except RoleActionContractError as exc:
                raise PublishGateError(f"staged role/action contract failed: {exc}") from exc
            except Exception as exc:
                raise PublishGateError("staged action registry failed validation") from exc
            try:
                compiled = KnowledgePackageCompiler().compile(package)
            except (Exception,) as exc:
                # Compiler errors are part of the publication gate, but do not
                # expose arbitrary parser internals as the audit result.
                raise PublishGateError("staged Markdown failed compiler validation") from exc
            published_manifest = _manifest_output(
                package_ref=package_ref,
                entries=final_entries,
                candidate_manifest_sha256=candidate_manifest_digest,
                source_ids=source_ids,
                claim_ids=claim_ids,
                review=review,
                compiled=compiled,
            )
            manifest_destination = _contained(
                stage,
                f"manifests/{package_ref.format()}.json",
                field_name="published manifest",
            )
            manifest_destination.parent.mkdir(parents=True, exist_ok=True)
            manifest_destination.write_bytes(_manifest_file_bytes(published_manifest))

            existing_compiled = self._load_compiled_if_present(package_ref)
            if (
                existing_compiled is not None
                and existing_compiled.package_identity != compiled.package_identity
            ):
                raise PublishedPackageConflictError(
                    f"compiled package already contains different content: {package_ref.format()}"
                )

            try:
                compiled_path = asyncio.run(self._compiled_store.publish(compiled))
            except CompiledPackageAlreadyExistsError as exc:
                raise PublishedPackageConflictError(str(exc)) from exc
            # The compiled package is the final immutable runtime artifact.
            # Commit it before swapping the staged Markdown tree so a failure
            # in the compiled store cannot leave a visible Vault publication
            # without its corresponding runtime package.
            _swap_directory(stage, self._published_root)
            stage = Path()
            manifest_bytes = _manifest_file_bytes(published_manifest)
            return PublishResult(
                package_id=package_ref.format(),
                board_ref=package_ref.format(),
                published_root=self._published_root,
                compiled_path=compiled_path,
                package_sha256=compiled.package_identity,
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                idempotent=False,
            )
        finally:
            if stage != Path() and (stage.exists() or stage.is_symlink()):
                _remove_tree(stage)

    @staticmethod
    def _artifact_path(
        job_dir: Path,
        draft_dir: Path,
        manifest: Mapping[str, object],
        key: str,
        default_name: str,
    ) -> Path:
        value = manifest.get(f"{key}_path", manifest.get(key))
        relative = value if isinstance(value, str) else default_name
        relative = _normalized_relative_path(relative, field_name=f"{key}_path")
        candidates = (job_dir / relative, draft_dir / relative)
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        raise PublishGateError(f"{default_name} is missing")

    @staticmethod
    def _validate_draft_documents(
        draft_dir: Path,
        entries: Sequence[tuple[str, str]],
        package_ref: VersionedRef,
        source_ids: set[str],
        claims: Mapping[str, RuleClaim],
    ) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        listed = {path for path, _ in entries}
        for path in listed:
            source = _contained(draft_dir, path, field_name="draft document")
            if not source.is_file() or source.is_symlink():
                raise PublishGateError(f"draft document is missing or not regular: {path}")
            data = _read_bytes(source, name=path)
            try:
                parsed = parse_markdown(data)
            except (FrontmatterParseError, ValueError, TypeError) as exc:
                raise PublishGateError(f"draft document has invalid frontmatter: {path}") from exc
            frontmatter = parsed.frontmatter
            if frontmatter.get("status") != "published":
                raise PublishGateError(f"draft document is not marked published: {path}")
            document_claims: set[str] = set()
            document_sources: set[str] = set()
            _collect_document_refs(
                frontmatter, claim_refs=document_claims, source_refs=document_sources
            )
            missing_claims = sorted(document_claims - set(claims))
            missing_sources = sorted(document_sources - source_ids)
            unsupported_claims = sorted(
                claim_id
                for claim_id in document_claims
                if claim_id in claims and claims[claim_id].status is not ClaimStatus.SUPPORTED
            )
            uncovered_claim_sources = sorted(
                {
                    source_id
                    for claim_id in document_claims
                    if claim_id in claims
                    for source_id in claims[claim_id].evidence_ids
                    if source_id not in document_sources
                }
            )
            if missing_claims or missing_sources or unsupported_claims or uncovered_claim_sources:
                details = []
                if missing_claims:
                    details.append("claims=" + ",".join(missing_claims))
                if missing_sources:
                    details.append("sources=" + ",".join(missing_sources))
                if unsupported_claims:
                    details.append("claims_not_supported=" + ",".join(unsupported_claims))
                if uncovered_claim_sources:
                    details.append(
                        "claim_evidence_not_referenced=" + ",".join(uncovered_claim_sources)
                    )
                raise PublishGateError(
                    f"document reference closure failed for {path}: {'; '.join(details)}"
                )
            result[path] = data

        # An unlisted Markdown file is an audit ambiguity: it would not be
        # validated or included in the immutable package.
        for markdown_path in draft_dir.rglob("*.md"):
            if markdown_path.is_symlink():
                raise PublishGateError("draft contains a Markdown symlink")
            relative = markdown_path.relative_to(draft_dir).as_posix()
            if relative not in listed:
                raise PublishManifestError(
                    f"draft Markdown is absent from publish manifest: {relative}"
                )
        board_path = f"boards/{package_ref.id}/{package_ref.version}/board.md"
        if board_path not in listed:
            raise PublishManifestError(
                "publish manifest does not include the requested board document"
            )
        return result

    def _load_existing_manifest(self, path: Path) -> dict[str, object] | None:
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file():
            raise PublishedPackageConflictError(
                "existing publication manifest is not a regular file"
            )
        value = _decode_json(
            _read_bytes(path, name="published manifest", max_bytes=_MAX_MANIFEST_BYTES),
            name="published manifest",
        )
        if not isinstance(value, dict):
            raise PublishedPackageConflictError("existing publication manifest is invalid")
        return value

    def _load_compiled_if_present(
        self, package_ref: VersionedRef
    ) -> CompiledKnowledgePackageLoad | None:
        try:
            return asyncio.run(self._compiled_store.load(package_ref))
        except CompiledPackageNotFoundError:
            return None

    def _result_from_existing(
        self,
        package_ref: VersionedRef,
        payload: Mapping[str, object],
        compiled: CompiledKnowledgePackageLoad,
    ) -> PublishResult:
        manifest_path = self._published_root / "manifests" / f"{package_ref.format()}.json"
        raw = _read_bytes(manifest_path, name="published manifest", max_bytes=_MAX_MANIFEST_BYTES)
        return PublishResult(
            package_id=package_ref.format(),
            board_ref=package_ref.format(),
            published_root=self._published_root,
            compiled_path=compiled.path,
            package_sha256=compiled.package_identity,
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            idempotent=True,
        )


KnowledgePublisher = RulesetPublisher


__all__ = [
    "HumanReviewRecord",
    "KnowledgePublishError",
    "KnowledgePublishResult",
    "KnowledgePublisher",
    "PublishGateError",
    "PublishManifestError",
    "PublishedPackageConflictError",
    "PublishResult",
    "ReviewRecord",
    "RulesetPublisher",
    "RulesetReviewRecord",
]
