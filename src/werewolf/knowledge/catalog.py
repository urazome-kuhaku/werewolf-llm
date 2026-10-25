"""Immutable experimental package staging, gate activation, and game leases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from werewolf.cli_support.play_setup import PlaySetupError, _validate_manifest, build_play_setup
from werewolf.domain.admission import CatalogAdmission
from werewolf.domain.scenario import ScenarioError, load_scenario, scenario_sha256
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.refs import VersionedRef
from werewolf.persistence import PathSecurityError, atomic_write_bytes, resolve_contained_path
from werewolf.rules.models import ExecutionPackage

_REGISTRY_SCHEMA = 1
_MANIFEST_NAME = "catalog-manifest.json"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.ASCII)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)


class CatalogError(ValueError):
    """Base error for catalog integrity and lifecycle operations."""


class CatalogConflictError(CatalogError):
    """An immutable catalog identity is already occupied by different bytes."""


class CatalogAdmissionError(CatalogError):
    """A game could not acquire, verify, or release its durable package lease."""


class CatalogSelection(BaseModel):
    """Small config binding emitted by ``play init --catalog-root``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    catalog_root: Annotated[str, StringConstraints(min_length=1, max_length=4096, strict=True)]
    catalog_id: Annotated[str, StringConstraints(min_length=1, max_length=64, strict=True)]
    board_id: Annotated[str, StringConstraints(min_length=1, max_length=64, strict=True)]
    board_version: Annotated[str, StringConstraints(min_length=5, max_length=64, strict=True)]
    package_identity: Annotated[str, StringConstraints(min_length=64, max_length=64, strict=True)]
    gate_sha256: Annotated[str, StringConstraints(min_length=64, max_length=64, strict=True)]
    stage_digest: Annotated[str, StringConstraints(min_length=64, max_length=64, strict=True)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("catalog_root")
    @classmethod
    def canonical_catalog_root(cls, value: str) -> str:
        supplied = Path(value).expanduser()
        if str(supplied.resolve()) != value:
            raise ValueError("catalog_root must be a canonical absolute path")
        return value

    @field_validator("catalog_id", "board_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("catalog and board IDs must be lowercase ASCII identifiers")
        return value

    @field_validator("board_version")
    @classmethod
    def validate_board_version(cls, value: str) -> str:
        try:
            VersionedRef(id="catalog-selection", version=value)
        except ValueError as exc:
            raise ValueError("board_version must be a semantic version") from exc
        return value

    @field_validator("package_identity", "gate_sha256", "stage_digest")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError("catalog digests must be lowercase SHA-256 values")
        return value

    @property
    def board_ref(self) -> VersionedRef:
        return VersionedRef(id=self.board_id, version=self.board_version)

    def store(self) -> CatalogStore:
        return CatalogStore(self.catalog_root)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    ref: VersionedRef
    catalog_id: str
    path: Path
    manifest: Mapping[str, object]
    stage_digest: str
    package_identity: str
    gate_sha256: str

    @property
    def candidate_root(self) -> Path:
        return self.path / "candidate"

    @property
    def compiled_root(self) -> Path:
        return self.path / "compiled"

    @property
    def scenario_paths(self) -> tuple[tuple[str, Path, str], ...]:
        raw = self.manifest.get("scenarios")
        if not isinstance(raw, list):
            return ()
        output: list[tuple[str, Path, str]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            scenario_id = item.get("scenario_id")
            relative = item.get("path")
            digest = item.get("sha256")
            if (
                isinstance(scenario_id, str)
                and isinstance(relative, str)
                and isinstance(digest, str)
            ):
                output.append((scenario_id, self.path / relative, digest))
        return tuple(output)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CatalogError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, Mapping):
        raise CatalogError(f"{label} must be a JSON object")
    if raw != _canonical_json(value):
        raise CatalogError(f"{label} is not canonical JSON")
    return cast(Mapping[str, object], value)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_relative(relative: object) -> str:
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or relative.startswith("/")
        or "\x00" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise CatalogError("catalog manifest contains an unsafe relative path")
    return relative


def _registry_template() -> dict[str, object]:
    return {
        "schema_version": _REGISTRY_SCHEMA,
        "revision": 0,
        "active": {},
        "retired": [],
        "leases": {},
        "lease_generations": {},
        "tombstones": {},
        "entry_revisions": {},
        "gate_runs": {},
    }


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _draft_file_inventory(draft: Path) -> set[str]:
    if draft.is_symlink() or not draft.is_dir():
        raise CatalogError("candidate draft must be a regular directory")
    result: set[str] = set()
    for item in draft.rglob("*"):
        if item.is_symlink():
            raise CatalogError("candidate closure must not contain symlinks")
        if item.is_file():
            result.add(item.relative_to(draft).as_posix())
    return result


class CatalogStore:
    """A local, hash-verified registry of immutable candidate packages."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        supplied_path = Path(root).expanduser()
        if supplied_path.is_symlink():
            raise CatalogError("catalog root must not be a symlink")
        supplied = supplied_path.resolve()
        if supplied.exists() and not supplied.is_dir():
            raise CatalogError("catalog root must be a directory")
        self.root = supplied

    def entry_path(self, board_ref: str | VersionedRef) -> Path:
        ref = board_ref if isinstance(board_ref, VersionedRef) else VersionedRef.parse(board_ref)
        try:
            return resolve_contained_path(
                self.root / "entries", Path(ref.id) / ref.version
            )
        except PathSecurityError as exc:
            raise CatalogError("catalog board reference escapes its root") from exc

    async def stage(
        self,
        workbench_root: str | os.PathLike[str],
        board_ref: str | VersionedRef,
        scenarios: Sequence[str | os.PathLike[str]],
    ) -> dict[str, object]:
        """Validate, compile, and atomically stage one complete candidate closure."""

        ref = board_ref if isinstance(board_ref, VersionedRef) else VersionedRef.parse(board_ref)
        supplied_workbench = Path(workbench_root).expanduser()
        if supplied_workbench.is_symlink():
            raise CatalogError("candidate workbench root must not be a symlink")
        workbench = supplied_workbench.resolve()
        try:
            manifest, source_manifest_sha, draft = await asyncio.to_thread(
                _validate_manifest,
                workbench,
                board_ref=ref,
            )
        except (OSError, PlaySetupError, ValueError) as exc:
            raise CatalogError(f"candidate workbench is invalid: {exc}") from exc
        package_loader = KnowledgePackageLoader(draft)
        try:
            package = await package_loader.load(ref)
            compiled = KnowledgePackageCompiler().compile(package)
        except Exception as exc:
            raise CatalogError(f"candidate package did not compile: {exc}") from exc
        if compiled.execution is None or compiled.action_registry is None:
            raise CatalogError("catalog candidates require a frozen executable package")

        listed = manifest.get("files")
        if not isinstance(listed, list):
            raise CatalogError("candidate manifest files are invalid")
        listed_paths = sorted(
            _validate_relative(item.get("path") if isinstance(item, Mapping) else None)
            for item in listed
        )
        package_paths = sorted(document.relative_path for document in package.documents)
        execution_relative = f"boards/{ref.id}/{ref.version}/execution.yaml"
        if package.execution_definition is not None:
            if execution_relative not in listed_paths:
                raise CatalogError("execution.yaml must be included in the candidate manifest")
            package_paths.append(execution_relative)
            package_paths.sort()
        if listed_paths != package_paths:
            raise CatalogError("candidate manifest does not match the exact dependency closure")
        actual_draft_files = await asyncio.to_thread(_draft_file_inventory, draft)
        if actual_draft_files != set(listed_paths):
            raise CatalogError("candidate draft contains missing or unmanifested files")

        if not scenarios:
            raise CatalogError("catalog staging requires at least one declarative scenario")
        scenario_records: list[dict[str, str]] = []
        scenario_source_by_id: dict[str, tuple[Path, bytes, str]] = {}
        for supplied_path in scenarios:
            scenario_source = Path(supplied_path).expanduser()
            if scenario_source.is_symlink():
                raise CatalogError("scenario paths must be regular files")
            path = scenario_source.resolve()
            if not path.is_file():
                raise CatalogError("scenario paths must be regular files")
            try:
                definition, data = await asyncio.gather(
                    asyncio.to_thread(load_scenario, path),
                    asyncio.to_thread(path.read_bytes),
                )
            except (OSError, ScenarioError) as exc:
                raise CatalogError(f"catalog scenario is invalid: {path.name}") from exc
            if definition.board_ref != ref.format():
                raise CatalogError("catalog scenario board_ref does not match staged board")
            if definition.scenario_id in scenario_source_by_id:
                raise CatalogError("catalog scenario IDs must be unique")
            digest = scenario_sha256(definition)
            relative = f"scenarios/{definition.scenario_id}.yaml"
            scenario_source_by_id[definition.scenario_id] = (path, data, digest)
            scenario_records.append(
                {"scenario_id": definition.scenario_id, "path": relative, "sha256": digest}
            )
        scenario_records.sort(key=lambda item: item["scenario_id"])

        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".catalog-stage-", dir=self.root))
        try:
            candidate_root = staging / "candidate"
            (candidate_root / "draft").mkdir(parents=True)
            await asyncio.to_thread(
                _copy_file,
                workbench / "publish-manifest.json",
                candidate_root / "publish-manifest.json",
            )
            for relative in listed_paths:
                source = resolve_contained_path(draft, relative)
                await asyncio.to_thread(
                    _copy_file,
                    source,
                    resolve_contained_path(candidate_root / "draft", relative),
                )
            for scenario_id, (_, data, _) in scenario_source_by_id.items():
                target = staging / "scenarios" / f"{scenario_id}.yaml"
                await asyncio.to_thread(_write_bytes, target, data)

            compiled_store = CompiledKnowledgeStore(staging / "compiled")
            await compiled_store.publish(compiled)
            package_identity = compiled.package_identity
            file_hashes = await asyncio.to_thread(_tree_hashes, staging)
            gate_digest_payload = {
                "board_ref": ref.format(),
                "package_identity": package_identity,
                "scenarios": scenario_records,
                "schema_version": 1,
            }
            gate_sha256 = _sha256(_canonical_json(gate_digest_payload))
            staged_manifest: dict[str, object] = {
                "schema_version": 1,
                "catalog_id": ref.id,
                "board_ref": ref.format(),
                "status": "CANDIDATE_PENDING_HUMAN_REVIEW",
                "source_manifest_sha256": source_manifest_sha,
                "package_identity": package_identity,
                "gate_sha256": gate_sha256,
                "scenarios": scenario_records,
                "files": file_hashes,
            }
            manifest_bytes = _canonical_json(staged_manifest)
            (staging / _MANIFEST_NAME).write_bytes(manifest_bytes)
            stage_digest = _sha256(manifest_bytes)
            destination = self.entry_path(ref)
            destination.parent.mkdir(parents=True, exist_ok=True)
            async with self._writer_lock():
                if destination.exists() or destination.is_symlink():
                    existing = self.load_entry(ref)
                    if existing.stage_digest != stage_digest:
                        raise CatalogConflictError(
                            "same catalog board ID/version already contains different content"
                        )
                    return self._entry_report(existing, status="already-staged")
                await asyncio.to_thread(os.rename, staging, destination)
            entry = self.load_entry(ref)
            return self._entry_report(entry, status="staged")
        finally:
            if staging.exists():
                await asyncio.to_thread(shutil.rmtree, staging, True)

    def load_entry(self, board_ref: str | VersionedRef) -> CatalogEntry:
        ref = board_ref if isinstance(board_ref, VersionedRef) else VersionedRef.parse(board_ref)
        path = self.entry_path(ref)
        if path.is_symlink() or not path.is_dir():
            raise CatalogError(f"catalog candidate is not staged: {ref.format()}")
        manifest_path = path / _MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise CatalogError("catalog manifest is missing or unsafe")
        manifest = _read_json(manifest_path, label="catalog manifest")
        if manifest.get("schema_version") != 1 or manifest.get("board_ref") != ref.format():
            raise CatalogError("catalog manifest identity does not match requested board")
        if manifest.get("catalog_id") != ref.id:
            raise CatalogError("catalog ID does not match board ID")
        file_entries = manifest.get("files")
        if not isinstance(file_entries, list):
            raise CatalogError("catalog manifest files are invalid")
        expected: dict[str, str] = {}
        for entry in file_entries:
            if not isinstance(entry, Mapping):
                raise CatalogError("catalog file entry is invalid")
            relative = _validate_relative(entry.get("path"))
            digest = entry.get("sha256")
            if (
                relative in expected
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise CatalogError("catalog file inventory contains invalid identity")
            expected[relative] = digest
        actual_paths: set[str] = set()
        for item in path.rglob("*"):
            if item.is_symlink():
                raise CatalogError("staged catalog contains a symlink")
            if item.is_file() and item.name != _MANIFEST_NAME:
                actual_paths.add(item.relative_to(path).as_posix())
        if actual_paths != set(expected):
            raise CatalogError("staged catalog file inventory changed")
        for relative, digest in expected.items():
            file_path = resolve_contained_path(path, relative)
            if _sha256(file_path.read_bytes()) != digest:
                raise CatalogError(f"staged catalog file hash mismatch: {relative}")
        package_identity = manifest.get("package_identity")
        gate_sha256 = manifest.get("gate_sha256")
        if not isinstance(package_identity, str) or len(package_identity) != 64:
            raise CatalogError("catalog package identity is invalid")
        if not isinstance(gate_sha256, str) or len(gate_sha256) != 64:
            raise CatalogError("catalog gate identity is invalid")
        compiled_path = CompiledKnowledgeStore(path / "compiled").package_path(ref)
        compiled_manifest = _read_json(compiled_path / "manifest.json", label="compiled manifest")
        if (
            compiled_manifest.get("package_sha256") != package_identity
            or compiled_manifest.get("package_id") != ref.format()
            or compiled_manifest.get("board_ref") != ref.format()
        ):
            raise CatalogError("staged compiled package identity changed")
        return CatalogEntry(
            ref=ref,
            catalog_id=ref.id,
            path=path,
            manifest=manifest,
            stage_digest=_sha256(manifest_path.read_bytes()),
            package_identity=package_identity,
            gate_sha256=gate_sha256,
        )

    def verify_selection(self, selection: CatalogSelection) -> CatalogEntry:
        if not isinstance(selection, CatalogSelection):
            raise TypeError("selection must be a CatalogSelection")
        if self.root != Path(selection.catalog_root).expanduser().resolve():
            raise CatalogError("catalog selection root does not match the configured catalog")
        entry = self.load_entry(selection.board_ref)
        if (
            entry.catalog_id != selection.catalog_id
            or entry.package_identity != selection.package_identity
            or entry.gate_sha256 != selection.gate_sha256
            or entry.stage_digest != selection.stage_digest
        ):
            raise CatalogError("catalog selection differs from its immutable staged entry")
        return entry

    def resolve_active(self, board: str) -> tuple[CatalogEntry, CatalogSelection]:
        """Resolve an active default or an exact active board reference."""

        if "@" in board:
            requested = VersionedRef.parse(board)
            board_id = requested.id
            wanted_version = requested.version
        else:
            if (
                not board
                or not board.isascii()
                or not board.replace("-", "").replace("_", "").isalnum()
            ):
                raise CatalogError("board must be an ID or exact ID@X.Y.Z reference")
            board_id = board
            wanted_version = None
        registry = self._load_registry()
        active = _mapping(registry.get("active"), "active catalog pointers")
        version = active.get(board_id)
        if not isinstance(version, str):
            raise CatalogError(f"catalog has no active default for {board_id}")
        if wanted_version is not None and wanted_version != version:
            raise CatalogError("requested board version is not the active default")
        ref = VersionedRef(id=board_id, version=version)
        retired = _string_set(registry.get("retired"), "retired refs")
        if ref.format() in retired:
            raise CatalogError("catalog board is retired and cannot start new games")
        entry = self.load_entry(ref)
        selection = CatalogSelection(
            schema_version=1,
            catalog_root=str(self.root),
            catalog_id=entry.catalog_id,
            board_id=ref.id,
            board_version=ref.version,
            package_identity=entry.package_identity,
            gate_sha256=entry.gate_sha256,
            stage_digest=entry.stage_digest,
        )
        return entry, selection

    async def activate(self, board_ref: str | VersionedRef) -> dict[str, object]:
        """Run every staged scenario, aggregate package-wide skill evidence, then flip default."""

        ref = board_ref if isinstance(board_ref, VersionedRef) else VersionedRef.parse(board_ref)
        entry = self.load_entry(ref)
        initial_registry = self._load_registry()
        entry_revisions = _mapping(initial_registry.get("entry_revisions"), "entry revisions")
        revision_before = entry_revisions.get(ref.format(), 0)
        if type(revision_before) is not int or revision_before < 0:
            raise CatalogError("catalog entry revision is invalid")
        if not entry.scenario_paths:
            raise CatalogError("catalog entry has no staged scenarios")

        run_id = f"{ref.id}-{ref.version}-{uuid4().hex[:12]}"
        run_root = self.root / "runs" / run_id
        run_root.mkdir(parents=True, exist_ok=False)
        run_reports: list[dict[str, object]] = []
        player_uses: set[str] = set()
        host_uses: set[str] = set()
        automatic_uses: set[str] = set()
        for scenario_id, scenario_path, expected_scenario_sha in entry.scenario_paths:
            scenario = load_scenario(scenario_path)
            if scenario_sha256(scenario) != expected_scenario_sha:
                raise CatalogError(f"staged scenario hash changed: {scenario_id}")
            setup_dir = run_root / scenario_id / "setup"
            try:
                setup_report = await asyncio.to_thread(
                    build_play_setup,
                    setup_dir,
                    all_scripted=True,
                    board_ref=ref.format(),
                    workbench=entry.candidate_root,
                    project_root=Path(__file__).resolve().parents[2],
                    game_id=f"cat-{uuid4().hex[:20]}",
                )
            except (PlaySetupError, OSError, ValueError) as exc:
                run_reports.append(
                    {"scenario_id": scenario_id, "status": "FAIL", "failure": str(exc)[:500]}
                )
                continue
            report_path = run_root / scenario_id / "report.json"
            from werewolf.moderator.scenario_runner import run_scenario

            report = await run_scenario(
                Path(str(setup_report["config_path"])),
                scenario_path,
                report_path,
                experimental_preview_enabled=True,
                expected_package_identity=entry.package_identity,
                enforce_mandatory_coverage=False,
            )
            run_reports.append(report)
            if report.get("status") != "PASS":
                continue
            for key, target in (
                ("player_casts", player_uses),
                ("host_ledger_uses", host_uses),
                ("automatic_ledger_uses", automatic_uses),
            ):
                values = report.get(key)
                if isinstance(values, list):
                    target.update(
                        str(item["skill_id"])
                        for item in values
                        if isinstance(item, Mapping) and isinstance(item.get("skill_id"), str)
                    )

        package_load = await CompiledKnowledgeStore(entry.compiled_root).load(
            ref, expected_board_ref=ref
        )
        executable = package_load.package_payload.get("executable")
        if not isinstance(executable, Mapping):
            raise CatalogError("staged package has no executable definition")
        execution_raw = executable.get("execution")
        if not isinstance(execution_raw, Mapping):
            raise CatalogError("staged package has no execution language payload")
        execution = ExecutionPackage.model_validate(execution_raw)
        required_player, required_automatic = _mandatory_skill_ids(execution)
        required_host = {
            skill
            for scenario in (load_scenario(path) for _, path, _ in entry.scenario_paths)
            for skill in scenario.required_host_skill_ids
        }
        missing_player = sorted(required_player - player_uses)
        missing_automatic = sorted(required_automatic - automatic_uses)
        missing_host = sorted(required_host - host_uses)
        scenario_failures = [
            str(report.get("scenario_id"))
            for report in run_reports
            if report.get("status") != "PASS"
        ]
        gate_passed = not scenario_failures and not (
            missing_player or missing_automatic or missing_host
        )
        gate_report = {
            "schema_version": 1,
            "catalog_id": ref.id,
            "board_ref": ref.format(),
            "stage_digest": entry.stage_digest,
            "package_identity": entry.package_identity,
            "gate_sha256": entry.gate_sha256,
            "status": "PASS" if gate_passed else "FAIL",
            "scenario_statuses": [
                {"scenario_id": item.get("scenario_id"), "status": item.get("status")}
                for item in run_reports
            ],
            "required_player_skill_ids": sorted(required_player),
            "covered_player_skill_ids": sorted(required_player & player_uses),
            "missing_player_skill_ids": missing_player,
            "required_automatic_skill_ids": sorted(required_automatic),
            "covered_automatic_skill_ids": sorted(required_automatic & automatic_uses),
            "missing_automatic_skill_ids": missing_automatic,
            "required_host_skill_ids": sorted(required_host),
            "covered_host_skill_ids": sorted(required_host & host_uses),
            "missing_host_skill_ids": missing_host,
            "reports": run_reports,
        }
        await atomic_write_bytes(run_root / "activation-report.json", _canonical_json(gate_report))
        if not gate_passed:
            return {**gate_report, "run_root": str(run_root)}

        # The complete gate runs outside the exclusive writer lock. Re-read
        # both the immutable entry and its exact digest after acquiring it.
        checked_entry = self.load_entry(ref)
        if checked_entry.stage_digest != entry.stage_digest:
            raise CatalogConflictError("staged catalog content changed while its gate ran")
        async with self._writer_lock():
            registry = self._load_registry()
            current_revisions = _mapping(registry.get("entry_revisions"), "entry revisions")
            if current_revisions.get(ref.format(), 0) != revision_before:
                raise CatalogConflictError("catalog lifecycle changed while activation gate ran")
            if self.load_entry(ref).stage_digest != entry.stage_digest:
                raise CatalogConflictError("staged catalog digest changed before activation")
            active = _mapping(registry.get("active"), "active catalog pointers")
            active[ref.id] = ref.version
            retired = _string_set(registry.get("retired"), "retired refs")
            retired.discard(ref.format())
            revisions = dict(current_revisions)
            revisions[ref.format()] = revision_before + 1
            gate_runs = _mapping(registry.get("gate_runs"), "catalog gate runs")
            gate_runs[ref.format()] = {
                "status": "PASS",
                "stage_digest": entry.stage_digest,
                "gate_sha256": entry.gate_sha256,
                "report_path": str(run_root / "activation-report.json"),
            }
            registry["active"] = active
            registry["retired"] = sorted(retired)
            registry["entry_revisions"] = revisions
            registry["gate_runs"] = gate_runs
            registry["revision"] = _revision(registry) + 1
            await self._write_registry(registry)
        return {**gate_report, "status": "ACTIVE", "run_root": str(run_root)}

    async def retire(self, board_ref: str | VersionedRef) -> dict[str, object]:
        ref = board_ref if isinstance(board_ref, VersionedRef) else VersionedRef.parse(board_ref)
        entry = self.load_entry(ref)
        async with self._writer_lock():
            registry = self._load_registry()
            active = _mapping(registry.get("active"), "active catalog pointers")
            if active.get(ref.id) == ref.version:
                del active[ref.id]
            retired = _string_set(registry.get("retired"), "retired refs")
            retired.add(ref.format())
            registry["active"] = active
            registry["retired"] = sorted(retired)
            _bump_entry_revision(registry, ref)
            registry["revision"] = _revision(registry) + 1
            await self._write_registry(registry)
        return self._entry_report(entry, status="retired")

    async def unload(self, board_ref: str | VersionedRef) -> dict[str, object]:
        ref = board_ref if isinstance(board_ref, VersionedRef) else VersionedRef.parse(board_ref)
        entry = self.load_entry(ref)
        async with self._writer_lock():
            registry = self._load_registry()
            active = _mapping(registry.get("active"), "active catalog pointers")
            if active.get(ref.id) == ref.version:
                raise CatalogError("active catalog versions must be retired before unload")
            retired = _string_set(registry.get("retired"), "retired refs")
            if ref.format() not in retired:
                raise CatalogError("catalog version must be retired before unload")
            leases = _mapping(registry.get("leases"), "catalog leases")
            for game_id, record in leases.items():
                if not isinstance(record, Mapping) or record.get("status") != "ACTIVE":
                    continue
                if record.get("board_ref") == ref.format():
                    raise CatalogError(f"catalog version has a live game lease: {game_id}")
            await asyncio.to_thread(shutil.rmtree, entry.path)
            retired.discard(ref.format())
            registry["retired"] = sorted(retired)
            revisions = _mapping(registry.get("entry_revisions"), "entry revisions")
            revisions.pop(ref.format(), None)
            registry["entry_revisions"] = revisions
            runs = _mapping(registry.get("gate_runs"), "catalog gate runs")
            runs.pop(ref.format(), None)
            registry["gate_runs"] = runs
            registry["revision"] = _revision(registry) + 1
            await self._write_registry(registry)
        return {"status": "unloaded", "board_ref": ref.format(), "stage_digest": entry.stage_digest}

    async def acquire_lease(
        self,
        *,
        selection: CatalogSelection,
        game_id: str,
        package_identity: str,
    ) -> CatalogAdmission:
        """Durably acquire a generation-numbered reference at the real START boundary."""

        entry = self.verify_selection(selection)
        if entry.package_identity != package_identity:
            raise CatalogAdmissionError(
                "game compiled package identity differs from staged version"
            )
        async with self._writer_lock():
            registry = self._load_registry()
            # This short digest check occurs under the same exclusive lock as
            # insertion. Long-running gate work never holds this lock.
            if self.load_entry(entry.ref).stage_digest != selection.stage_digest:
                raise CatalogAdmissionError("staged package changed before game admission")
            retired = _string_set(registry.get("retired"), "retired refs")
            active = _mapping(registry.get("active"), "active catalog pointers")
            if entry.ref.format() in retired or active.get(entry.ref.id) != entry.ref.version:
                raise CatalogAdmissionError("catalog version is retired or no longer active")
            tombstones = _mapping(registry.get("tombstones"), "game tombstones")
            if game_id in tombstones:
                raise CatalogAdmissionError("CLOSED_ARCHIVE game IDs cannot be reused")
            leases = _mapping(registry.get("leases"), "catalog leases")
            previous = leases.get(game_id)
            if isinstance(previous, Mapping):
                same_identity = all(
                    previous.get(name) == value
                    for name, value in {
                        "catalog_id": selection.catalog_id,
                        "board_ref": selection.board_ref.format(),
                        "package_identity": package_identity,
                        "gate_sha256": selection.gate_sha256,
                    }.items()
                )
                if not same_identity:
                    raise CatalogAdmissionError(
                        "game ID is already bound to a different catalog identity"
                    )
                if previous.get("status") == "ACTIVE":
                    raise CatalogAdmissionError("game already has an active catalog lease")
                if previous.get("status") == "RELEASED":
                    raise CatalogAdmissionError("released catalog game IDs cannot be reused")
            counters = _mapping(registry.get("lease_generations"), "lease generations")
            current_generation = counters.get(game_id, 0)
            if type(current_generation) is not int or not 0 <= current_generation < 2**31 - 1:
                raise CatalogAdmissionError("lease generation counter is invalid or exhausted")
            generation = current_generation + 1
            admission = CatalogAdmission.build(
                catalog_id=selection.catalog_id,
                game_id=game_id,
                board_id=selection.board_id,
                board_version=selection.board_version,
                package_identity=package_identity,
                gate_sha256=selection.gate_sha256,
                lease_generation=generation,
            )
            leases[game_id] = {
                **admission.model_dump(mode="json"),
                "board_ref": selection.board_ref.format(),
                "status": "ACTIVE",
            }
            counters[game_id] = generation
            registry["leases"] = leases
            registry["lease_generations"] = counters
            registry["revision"] = _revision(registry) + 1
            await self._write_registry(registry)
            return admission

    async def cancel_lease(self, admission: CatalogAdmission, game_id: str) -> bool:
        """Release only this failed start's exact lease; retries advance generation."""

        admission.validate_for_game(game_id)
        async with self._writer_lock():
            registry = self._load_registry()
            leases = _mapping(registry.get("leases"), "catalog leases")
            current = leases.get(game_id)
            if not isinstance(current, Mapping) or current.get("lease_id") != admission.lease_id:
                return False
            if current.get("status") == "CANCELLED":
                return True
            if current.get("status") != "ACTIVE":
                return False
            leases[game_id] = {**dict(current), "status": "CANCELLED"}
            registry["leases"] = leases
            registry["revision"] = _revision(registry) + 1
            await self._write_registry(registry)
            return True

    async def close_lease_after_archive(
        self,
        admission: CatalogAdmission,
        game_id: str,
        *,
        archive_id: str,
        snapshot_id: str,
        final_state_revision: int,
    ) -> bool:
        """Close a lease only after the caller has independently verified archive evidence."""

        admission.validate_for_game(game_id)
        if not archive_id or not snapshot_id or type(final_state_revision) is not int:
            raise CatalogAdmissionError("archive tombstone evidence is invalid")
        async with self._writer_lock():
            registry = self._load_registry()
            tombstones = _mapping(registry.get("tombstones"), "game tombstones")
            old_tombstone = tombstones.get(game_id)
            if isinstance(old_tombstone, Mapping):
                if old_tombstone.get("lease_id") == admission.lease_id:
                    return True
                raise CatalogAdmissionError("closed archive tombstone belongs to another lease")
            leases = _mapping(registry.get("leases"), "catalog leases")
            current = leases.get(game_id)
            if not isinstance(current, Mapping) or current.get("lease_id") != admission.lease_id:
                return False
            if current.get("status") != "ACTIVE":
                return False
            identity_fields = {
                "catalog_id": admission.catalog_id,
                "board_ref": f"{admission.board_id}@{admission.board_version}",
                "package_identity": admission.package_identity,
                "gate_sha256": admission.gate_sha256,
                "lease_generation": admission.lease_generation,
            }
            if any(current.get(key) != value for key, value in identity_fields.items()):
                return False
            leases[game_id] = {**dict(current), "status": "RELEASED"}
            tombstones[game_id] = {
                "status": "CLOSED_ARCHIVE",
                "lease_id": admission.lease_id,
                "catalog_id": admission.catalog_id,
                "board_ref": f"{admission.board_id}@{admission.board_version}",
                "package_identity": admission.package_identity,
                "gate_sha256": admission.gate_sha256,
                "lease_generation": admission.lease_generation,
                "archive_id": archive_id,
                "snapshot_id": snapshot_id,
                "final_state_revision": final_state_revision,
            }
            registry["leases"] = leases
            registry["tombstones"] = tombstones
            registry["revision"] = _revision(registry) + 1
            await self._write_registry(registry)
            return True

    def status(self, board: str | None = None) -> dict[str, object]:
        registry = self._load_registry()
        active = _mapping(registry.get("active"), "active catalog pointers")
        retired = sorted(_string_set(registry.get("retired"), "retired refs"))
        leases = _mapping(registry.get("leases"), "catalog leases")
        tombstones = _mapping(registry.get("tombstones"), "game tombstones")
        candidates: list[dict[str, object]] = []
        refs: tuple[VersionedRef, ...]
        if board is not None:
            if "@" in board:
                refs = (VersionedRef.parse(board),)
            else:
                refs = tuple(
                    VersionedRef(id=board, version=path.name)
                    for path in (self.root / "entries" / board).glob("*")
                    if path.is_dir() and not path.is_symlink()
                )
        else:
            refs = tuple(
                VersionedRef(id=board_path.name, version=version_path.name)
                for board_path in (self.root / "entries").glob("*")
                if board_path.is_dir() and not board_path.is_symlink()
                for version_path in board_path.glob("*")
                if version_path.is_dir() and not version_path.is_symlink()
            )
        for ref in sorted(refs, key=lambda item: item.format()):
            try:
                entry = self.load_entry(ref)
            except CatalogError:
                candidates.append({"board_ref": ref.format(), "status": "CORRUPT"})
                continue
            candidates.append(
                {
                    **self._entry_report(
                        entry,
                        status="RETIRED" if ref.format() in retired else "STAGED",
                    ),
                    "active": active.get(ref.id) == ref.version,
                }
            )
        return {
            "schema_version": 1,
            "revision": _revision(registry),
            "active": dict(active),
            "candidates": candidates,
            "retired": retired,
            "live_leases": {
                game_id: dict(record)
                for game_id, record in leases.items()
                if isinstance(record, Mapping) and record.get("status") == "ACTIVE"
            },
            "closed_archives": dict(tombstones),
        }

    def _load_registry(self) -> dict[str, object]:
        path = self.root / "registry.json"
        if not path.exists():
            return _registry_template()
        if path.is_symlink() or not path.is_file():
            raise CatalogError("catalog registry is unsafe")
        loaded = dict(_read_json(path, label="catalog registry"))
        if loaded.get("schema_version") != _REGISTRY_SCHEMA:
            raise CatalogError("catalog registry schema_version is unsupported")
        template = _registry_template()
        for key, default in template.items():
            if key not in loaded:
                loaded[key] = default
        for key in (
            "active",
            "leases",
            "lease_generations",
            "tombstones",
            "entry_revisions",
            "gate_runs",
        ):
            if not isinstance(loaded.get(key), Mapping):
                raise CatalogError(f"catalog registry {key} is invalid")
        if not isinstance(loaded.get("retired"), list):
            raise CatalogError("catalog registry retired list is invalid")
        return loaded

    async def _write_registry(self, registry: Mapping[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        await atomic_write_bytes(self.root / "registry.json", _canonical_json(registry))

    @asynccontextmanager
    async def _writer_lock(self) -> AsyncIterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".catalog-writer.lock"
        await asyncio.to_thread(_acquire_exclusive, lock_path)
        try:
            yield
        finally:
            await asyncio.to_thread(_release_exclusive, lock_path)

    @staticmethod
    def _entry_report(entry: CatalogEntry, *, status: str) -> dict[str, object]:
        return {
            "status": status,
            "catalog_id": entry.catalog_id,
            "board_ref": entry.ref.format(),
            "package_identity": entry.package_identity,
            "gate_sha256": entry.gate_sha256,
            "stage_digest": entry.stage_digest,
            "scenario_count": len(entry.scenario_paths),
        }


def _acquire_exclusive(path: Path) -> None:
    deadline = time.monotonic() + 30.0
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
            os.close(descriptor)
            return
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise CatalogError("catalog writer lock timed out")
            time.sleep(0.025)


def _release_exclusive(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _tree_hashes(root: Path) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CatalogError("catalog staging tree contains a symlink")
        if path.is_file():
            output.append(
                {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path.read_bytes())}
            )
    return output


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CatalogError(f"{label} must be an object")
    return dict(cast(Mapping[str, object], value))


def _string_set(value: object, label: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CatalogError(f"{label} must be a string list")
    return set(cast(list[str], value))


def _revision(registry: Mapping[str, object]) -> int:
    value = registry.get("revision", 0)
    if type(value) is not int or value < 0:
        raise CatalogError("catalog registry revision is invalid")
    return value


def _bump_entry_revision(registry: dict[str, object], ref: VersionedRef) -> int:
    values = _mapping(registry.get("entry_revisions"), "entry revisions")
    current = values.get(ref.format(), 0)
    if type(current) is not int or current < 0:
        raise CatalogError("catalog entry revision is invalid")
    values[ref.format()] = current + 1
    registry["entry_revisions"] = values
    return current + 1


def _mandatory_skill_ids(execution: ExecutionPackage) -> tuple[set[str], set[str]]:
    skill_modes = {item.skill_id: item.mode for item in execution.skills}
    skills_by_id = {item.skill_id: item for item in execution.skills}
    required_player = {
        skill.skill_id
        for skill in execution.skills
        if skill.mode == "PLAYER" and skill.action_code != 299 and skill.grants
    }
    all_effects = [
        effect
        for skill in execution.skills
        for effect in (*skill.effects, *skill.pass_effects)
    ]
    all_effects.extend(
        effect
        for interaction in execution.interactions
        for effect in interaction.effects
    )
    for effect in all_effects:
        grant_id = effect.grant_skill_id if effect.effect_type == "ABILITY_GRANT" else None
        if (
            isinstance(grant_id, str)
            and skill_modes.get(grant_id) == "PLAYER"
            and skills_by_id[grant_id].action_code != 299
        ):
            required_player.add(grant_id)
    required_automatic = {
        skill.skill_id for skill in execution.skills if skill.mode == "AUTOMATIC"
    }
    return required_player, required_automatic


__all__ = [
    "CatalogAdmissionError",
    "CatalogConflictError",
    "CatalogEntry",
    "CatalogError",
    "CatalogSelection",
    "CatalogStore",
]
