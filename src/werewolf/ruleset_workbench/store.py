"""Durable storage for ruleset research workbench jobs.

The workbench writes each job into its own directory.  A directory is first
assembled below the configured workbench root and only becomes visible under
its final name after both required metadata files have been atomically
written.  This keeps partially written jobs out of the set that a caller can
load after a process or machine failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import UUID4, BaseModel, ConfigDict, ValidationError

from werewolf.persistence import (
    PathSecurityError,
    atomic_write_text,
    resolve_contained_path,
)

from .bundles import Locale
from .jobs import BoardName, ResearchJob, ResearchJobStatus

WORKBENCH_SCHEMA_VERSION: Literal[1] = 1
_JSON_FILE_MAX_BYTES = 64 * 1024
_JOB_DIR_RE = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6}Z)_(?P<job_id>"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_JOB_TRANSITION_LOCKS: dict[Path, asyncio.Lock] = {}


def _job_transition_lock(job_path: Path) -> asyncio.Lock:
    """Return the process-local critical-section lock for one job file."""

    return _JOB_TRANSITION_LOCKS.setdefault(job_path, asyncio.Lock())


class WorkbenchJobStoreError(ValueError):
    """Raised when a workbench job cannot be safely created or loaded."""


class JobAlreadyExistsError(WorkbenchJobStoreError):
    """Raised when a final job directory already exists."""


class JobNotFoundError(FileNotFoundError, WorkbenchJobStoreError):
    """Raised when a validly named job directory is not present."""


class CorruptJobError(WorkbenchJobStoreError):
    """Raised when a job directory contains invalid or inconsistent metadata."""


class JobRequest(BaseModel):
    """The immutable request metadata written beside a research job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    job_id: UUID4
    board_name: BoardName
    locale: Locale


def _json_text(value: object) -> str:
    """Serialize persisted metadata with stable formatting and UTF-8 text."""

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _create_directory(path: Path) -> None:
    """Create a directory without accepting an existing path."""

    path.mkdir(parents=True, exist_ok=False)


def _ensure_directory(path: Path) -> None:
    """Create the configured workbench root if needed."""

    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise NotADirectoryError(path)


def _rename_staging(staging: Path, final: Path) -> None:
    """Atomically publish a staging directory without replacing a job."""

    if final.exists() or final.is_symlink():
        raise JobAlreadyExistsError(f"job directory already exists: {final.name}")
    # Both paths are children of the same root, so this is one atomic rename.
    # ``rename`` rather than ``replace`` makes an existing final directory a
    # hard failure instead of silently deleting a previously completed job.
    try:
        os.rename(staging, final)
    except FileExistsError as exc:
        raise JobAlreadyExistsError(f"job directory already exists: {final.name}") from exc


def _remove_tree(path: Path) -> None:
    """Remove a private staging directory after an unsuccessful publish."""

    if path.exists() or path.is_symlink():
        shutil.rmtree(path)


def _read_text(path: Path) -> str:
    """Read one bounded UTF-8 metadata file in a worker thread."""

    with path.open("rb") as metadata_file:
        data = metadata_file.read(_JSON_FILE_MAX_BYTES + 1)
    if len(data) > _JSON_FILE_MAX_BYTES:
        raise ValueError("metadata file exceeds the maximum size")
    return data.decode("utf-8")


def _parse_job_dir_name(job_dir_name: str) -> tuple[datetime, UUID]:
    """Validate the Windows-safe directory name and return its components."""

    if not isinstance(job_dir_name, str):
        raise TypeError("job_dir_name must be a string")
    match = _JOB_DIR_RE.fullmatch(job_dir_name)
    if match is None:
        raise WorkbenchJobStoreError(
            "job directory must match YYYYMMDDTHHMMSSZ_<uuid4>",
        )
    try:
        timestamp = datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=UTC,
        )
        job_id = UUID(match.group("job_id"))
    except ValueError as exc:
        raise WorkbenchJobStoreError("job directory has an invalid timestamp or UUID") from exc
    if job_id.version != 4:
        raise WorkbenchJobStoreError("job directory suffix must be a UUIDv4")
    return timestamp, job_id


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate object keys rather than silently keeping the last one."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    """Reject JSON extensions that encode NaN or infinity."""

    raise ValueError(f"non-finite JSON number: {value}")


def _load_json(text: str, *, filename: str) -> object:
    """Decode one metadata file and normalize parse failures."""

    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CorruptJobError(f"{filename} is not valid JSON") from exc


class WorkbenchJobStore:
    """Persist and load research jobs below one trusted workbench root."""

    def __init__(self, root: os.PathLike[str] | str) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        """Return the canonical root used for workbench job directories."""

        return self._root

    async def create_job(
        self,
        board_name: str,
        locale: str,
        at_utc: datetime,
    ) -> tuple[ResearchJob, str]:
        """Create and atomically publish a new research job.

        ``at_utc`` is both the job creation timestamp and the timestamp encoded
        in the directory name.  The UUID in the directory name is the same
        UUID persisted in ``request.json`` and ``job.json``.
        """

        job_id = uuid4()
        job = ResearchJob(
            job_id=job_id,
            board_name=board_name,
            created_at=at_utc,
            updated_at=at_utc,
        )
        request = JobRequest(
            schema_version=WORKBENCH_SCHEMA_VERSION,
            job_id=job.job_id,
            board_name=job.board_name,
            locale=locale,
        )

        directory_name = f"{job.created_at.strftime('%Y%m%dT%H%M%SZ')}_{job.job_id}"
        final_dir = resolve_contained_path(self._root, directory_name)
        staging_name = f".{directory_name}.staging-{uuid4()}"
        staging_dir = resolve_contained_path(self._root, staging_name)
        committed = False
        staging_created = False

        try:
            await asyncio.to_thread(_ensure_directory, self._root)
            await asyncio.to_thread(_create_directory, staging_dir)
            staging_created = True
            await atomic_write_text(
                staging_dir / "request.json",
                _json_text(request.model_dump(mode="json")),
            )
            await atomic_write_text(
                staging_dir / "job.json",
                _json_text(job.model_dump(mode="json")),
            )
            await asyncio.to_thread(_rename_staging, staging_dir, final_dir)
            committed = True
        finally:
            if not committed and staging_created:
                await asyncio.to_thread(_remove_tree, staging_dir)

        return job, directory_name

    async def _load_metadata(self, job_dir_name: str) -> tuple[JobRequest, ResearchJob]:
        """Load and cross-check both metadata records in one job directory."""

        directory_timestamp, directory_job_id = _parse_job_dir_name(job_dir_name)
        job_dir, request_path, job_path = self._metadata_paths(job_dir_name)

        if not await asyncio.to_thread(job_dir.is_dir):
            raise JobNotFoundError(f"job directory does not exist: {job_dir_name}")
        if not await asyncio.to_thread(request_path.is_file) or not await asyncio.to_thread(
            job_path.is_file,
        ):
            raise CorruptJobError("job directory must contain request.json and job.json")

        try:
            request_value, job_value = await asyncio.gather(
                asyncio.to_thread(_read_text, request_path),
                asyncio.to_thread(_read_text, job_path),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise CorruptJobError("job metadata could not be read") from exc

        request_payload = _load_json(request_value, filename="request.json")
        job_payload = _load_json(job_value, filename="job.json")
        try:
            request = JobRequest.model_validate(request_payload)
            job = ResearchJob.model_validate(job_payload)
        except ValidationError as exc:
            raise CorruptJobError("job metadata failed schema validation") from exc

        if request.schema_version != WORKBENCH_SCHEMA_VERSION or job.schema_version != 1:
            raise CorruptJobError("unsupported job schema_version")
        if request.job_id != job.job_id or request.job_id != directory_job_id:
            raise CorruptJobError("request, job, and directory job_id values disagree")
        if request.board_name != job.board_name:
            raise CorruptJobError("request and job board_name values disagree")
        if job.created_at.strftime("%Y%m%dT%H%M%SZ") != directory_timestamp.strftime(
            "%Y%m%dT%H%M%SZ",
        ):
            raise CorruptJobError("job created_at does not match its directory timestamp")
        return request, job

    def _metadata_paths(self, job_dir_name: str) -> tuple[Path, Path, Path]:
        """Resolve a job directory and both metadata paths under the root."""

        try:
            job_dir = resolve_contained_path(self._root, job_dir_name)
            request_path = resolve_contained_path(
                self._root,
                Path(job_dir_name) / "request.json",
            )
            job_path = resolve_contained_path(
                self._root,
                Path(job_dir_name) / "job.json",
            )
        except PathSecurityError as exc:
            raise WorkbenchJobStoreError("job path escapes the workbench root") from exc
        return job_dir, request_path, job_path

    async def _update_job(
        self,
        job_dir_name: str,
        update: Callable[[ResearchJob], ResearchJob],
    ) -> ResearchJob:
        """Apply one pure state transition inside a per-job critical section."""

        _, _, job_path = self._metadata_paths(job_dir_name)
        async with _job_transition_lock(job_path):
            # Re-read after acquiring the lock.  A caller must never validate
            # against a stale in-memory copy and then overwrite a newer state.
            _, current = await self._load_metadata(job_dir_name)
            updated = update(current)
            await atomic_write_text(
                job_path,
                _json_text(updated.model_dump(mode="json")),
            )
            return updated

    async def load_request(self, job_dir_name: str) -> JobRequest:
        """Load a job's request after validating it against the job metadata."""

        request, _ = await self._load_metadata(job_dir_name)
        return request

    async def load_job(self, job_dir_name: str) -> ResearchJob:
        """Load and cross-check one published research job directory."""

        _, job = await self._load_metadata(job_dir_name)
        return job

    async def advance_job(
        self,
        job_dir_name: str,
        target: ResearchJobStatus,
        at: datetime,
    ) -> ResearchJob:
        """Advance one job through a validated normal state transition."""

        return await self._update_job(
            job_dir_name,
            lambda current: current.transition_to(target, at=at),
        )

    async def fail_job(
        self,
        job_dir_name: str,
        reason: str,
        resume_from: ResearchJobStatus,
        at: datetime,
    ) -> ResearchJob:
        """Record a failure and its durable recovery checkpoint."""

        return await self._update_job(
            job_dir_name,
            lambda current: current.fail(reason, resume_from=resume_from, at=at),
        )

    async def recover_job(
        self,
        job_dir_name: str,
        at: datetime,
    ) -> ResearchJob:
        """Recover one failed job to the state recorded by its checkpoint."""

        return await self._update_job(
            job_dir_name,
            lambda current: current.recover(at=at),
        )


__all__ = [
    "CorruptJobError",
    "JobRequest",
    "JobAlreadyExistsError",
    "JobNotFoundError",
    "WORKBENCH_SCHEMA_VERSION",
    "WorkbenchJobStore",
    "WorkbenchJobStoreError",
]
