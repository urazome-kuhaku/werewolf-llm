"""Immutable, bounded storage for fetched Provider source bodies.

``FetchedDocument.body`` is the text returned by a research Provider.  It is
not an HTTP response capture and this module deliberately does not claim that
it is one.  The body is stored as the exact UTF-8 encoding of that text under
the current workbench job only; published and compiled vaults are outside this
module's boundary.

The archive is intentionally separate from the source excerpt artifacts.  An
excerpt may be enough for claim extraction, while this archive gives a
reviewer a reproducible body against which the evidence hash and excerpt can
be checked.  Existing content is immutable: writing the same bytes is
idempotent, and a different body for the same source ID is a conflict.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from werewolf.persistence import PathSecurityError, atomic_write_bytes, resolve_contained_path

from .evidence import SourceEvidence
from .research_provider import FetchedDocument

DEFAULT_MAX_SOURCE_BYTES: Final[int] = 2_000_000
"""Maximum UTF-8 bytes in one Provider body."""

DEFAULT_MAX_ARCHIVE_BYTES: Final[int] = 8_000_000
"""Maximum bytes occupied by all files in one job's raw source directory."""

RAW_SOURCE_DIRECTORY: Final[str] = "sources/raw"
"""Relative directory containing Provider body text files."""

_SOURCE_ID_PATTERN = re.compile(r"[a-z0-9_-]+", re.ASCII)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    },
)


class SourceArchiveError(ValueError):
    """Base error for an invalid or unsafe source archive operation."""


class SourceArchiveValidationError(SourceArchiveError):
    """The document and evidence do not describe the same source body."""


class SourceArchivePathError(SourceArchiveError):
    """The job root or source path is not a safe regular-file location."""


class SourceArchiveConflictError(SourceArchiveError):
    """An immutable source ID already contains different bytes."""


class SourceArchiveCorruptError(SourceArchiveError):
    """An archived source cannot be read or fails its expected digest."""


class SourceArchiveSizeError(SourceArchiveError):
    """A single body or the raw directory exceeds its configured bound."""


@dataclass(frozen=True, slots=True)
class SourceArchiveResult:
    """Receipt returned after a source body is archived or found unchanged."""

    source_id: str
    relative_path: str
    content_sha256: str
    byte_length: int
    already_present: bool


ArchiveReceipt = SourceArchiveResult


_ARCHIVE_LOCKS: dict[Path, asyncio.Lock] = {}


def _archive_lock(raw_directory: Path) -> asyncio.Lock:
    """Return the process-local lock protecting one raw source directory."""

    return _ARCHIVE_LOCKS.setdefault(raw_directory, asyncio.Lock())


def _validate_limit(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _source_id(source_id: object) -> str:
    """Validate a logical ID before deriving a Windows-safe filename."""

    if not isinstance(source_id, str) or _SOURCE_ID_PATTERN.fullmatch(source_id) is None:
        raise SourceArchiveValidationError(
            "source_id must contain only lowercase ASCII letters, digits, '-' or '_'",
        )
    if source_id.casefold() in _WINDOWS_RESERVED_NAMES:
        raise SourceArchiveValidationError("source_id cannot use a reserved Windows file name")
    # Keep this check explicit even though SourceEvidence currently limits IDs
    # to 64 characters; model_construct and future schema versions must not
    # turn a logical identifier into an unexpectedly large filename.
    if len(source_id) > 64:
        raise SourceArchiveValidationError("source_id exceeds the maximum length")
    return source_id


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb") as source_file:
            data = source_file.read(maximum + 1)
    except (OSError, ValueError) as exc:
        raise SourceArchiveCorruptError("archived source could not be read") from exc
    if len(data) > maximum:
        raise SourceArchiveSizeError("archived source exceeds its single-file size limit")
    return data


def _contains_symlink(path: Path) -> bool:
    """Return true for a symlink path without following its target."""

    try:
        return path.is_symlink()
    except OSError as exc:
        raise SourceArchivePathError("could not inspect source archive path") from exc


def _ensure_directory(path: Path, description: str) -> None:
    """Create one archive directory while refusing links and non-directories."""

    if _contains_symlink(path):
        raise SourceArchivePathError(f"{description} must not be a symbolic link")
    try:
        if path.exists():
            if not path.is_dir():
                raise SourceArchivePathError(f"{description} must be a directory")
            return
        path.mkdir()
    except SourceArchivePathError:
        raise
    except OSError as exc:
        raise SourceArchivePathError(f"could not create {description}") from exc


def _assert_no_symlink(path: Path, description: str) -> None:
    if _contains_symlink(path):
        raise SourceArchivePathError(f"{description} must not be a symbolic link")


def _read_total(raw_directory: Path, maximum_file: int, maximum_total: int) -> int:
    """Count regular raw files without traversing links or subdirectories."""

    total = 0
    try:
        entries = tuple(raw_directory.iterdir())
    except OSError as exc:
        raise SourceArchivePathError("raw source directory could not be listed") from exc

    for entry in entries:
        _assert_no_symlink(entry, "raw source entries")
        try:
            if not entry.is_file():
                raise SourceArchivePathError("raw source directory may contain regular files only")
            size = entry.stat().st_size
        except SourceArchivePathError:
            raise
        except OSError as exc:
            raise SourceArchivePathError("raw source entry could not be inspected") from exc
        if size > maximum_file:
            raise SourceArchiveSizeError("an archived source exceeds its single-file size limit")
        total += size
        if total > maximum_total:
            raise SourceArchiveSizeError("raw source directory exceeds its total size limit")
    return total


def _safe_job_root(value: os.PathLike[str] | str) -> Path:
    try:
        raw = Path(value)
    except (TypeError, ValueError) as exc:
        raise SourceArchivePathError("job_root must be a filesystem path") from exc
    if _contains_symlink(raw):
        raise SourceArchivePathError("job_root must not be a symbolic link")
    try:
        root = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourceArchivePathError("job_root must be an existing directory") from exc
    if _contains_symlink(root):
        raise SourceArchivePathError("job_root must not be a symbolic link")
    try:
        if not root.is_dir():
            raise SourceArchivePathError("job_root must be a directory")
    except OSError as exc:
        raise SourceArchivePathError("job_root could not be inspected") from exc
    return root


class SourceArchive:
    """Archive exact Provider text below one already-selected job directory."""

    def __init__(
        self,
        job_root: os.PathLike[str] | str,
        *,
        max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> None:
        self._job_root = _safe_job_root(job_root)
        self._max_source_bytes = _validate_limit(max_source_bytes, "max_source_bytes")
        self._max_archive_bytes = _validate_limit(max_archive_bytes, "max_archive_bytes")

    @property
    def job_root(self) -> Path:
        """Return the canonical, caller-authorized job directory."""

        return self._job_root

    def _raw_directory(self) -> Path:
        """Create and validate ``sources/raw`` without following links."""

        lexical_sources = self._job_root / "sources"
        lexical_raw = lexical_sources / "raw"
        _assert_no_symlink(lexical_sources, "sources directory")
        _assert_no_symlink(lexical_raw, "raw source directory")
        try:
            sources = resolve_contained_path(self._job_root, "sources")
            raw = resolve_contained_path(self._job_root, RAW_SOURCE_DIRECTORY)
        except PathSecurityError as exc:
            raise SourceArchivePathError("raw source path escapes the job directory") from exc
        _assert_no_symlink(sources, "sources directory")
        _assert_no_symlink(raw, "raw source directory")
        _ensure_directory(sources, "sources directory")
        # Resolve again after creating the parent so a replaced parent link is
        # caught before any body is written.
        try:
            raw = resolve_contained_path(self._job_root, RAW_SOURCE_DIRECTORY)
        except PathSecurityError as exc:
            raise SourceArchivePathError("raw source path escapes the job directory") from exc
        _assert_no_symlink(raw, "raw source directory")
        _ensure_directory(raw, "raw source directory")
        return raw

    def _destination(self, raw_directory: Path, source_id: str) -> Path:
        lexical_destination = self._job_root / RAW_SOURCE_DIRECTORY / f"{source_id}.txt"
        _assert_no_symlink(lexical_destination, "source archive destination")
        try:
            destination = resolve_contained_path(
                self._job_root,
                Path(RAW_SOURCE_DIRECTORY) / f"{source_id}.txt",
            )
        except PathSecurityError as exc:
            raise SourceArchivePathError("source archive path escapes the job directory") from exc
        # ``resolve_contained_path`` catches an outside target, while this
        # explicit check rejects links even when they point back inside.
        _assert_no_symlink(destination, "source archive destination")
        try:
            if destination.parent != raw_directory.resolve(strict=True):
                raise SourceArchivePathError("source archive path is not below sources/raw")
        except (OSError, RuntimeError) as exc:
            raise SourceArchivePathError("raw source directory could not be resolved") from exc
        return destination

    @staticmethod
    def _validate_pair(document: FetchedDocument, evidence: SourceEvidence) -> tuple[str, bytes]:
        if not isinstance(document, FetchedDocument):
            raise TypeError("document must be a validated FetchedDocument")
        if not isinstance(evidence, SourceEvidence):
            raise TypeError("evidence must be a validated SourceEvidence")
        source_id = _source_id(evidence.source_id)
        if str(document.url) != str(evidence.url):
            raise SourceArchiveValidationError("document URL does not match source evidence URL")
        try:
            body = document.body.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SourceArchiveValidationError("document body is not valid UTF-8 text") from exc
        actual_digest = _digest(body)
        if (
            not isinstance(evidence.content_sha256, str)
            or _SHA256_PATTERN.fullmatch(
                evidence.content_sha256,
            )
            is None
        ):
            raise SourceArchiveValidationError("source evidence content_sha256 is invalid")
        if actual_digest != evidence.content_sha256:
            raise SourceArchiveValidationError(
                "document body UTF-8 SHA-256 does not match source evidence",
            )
        if evidence.excerpt not in document.body:
            raise SourceArchiveValidationError(
                "source evidence excerpt is not contained in document",
            )
        return source_id, body

    async def archive(
        self,
        document: FetchedDocument,
        evidence: SourceEvidence,
    ) -> SourceArchiveResult:
        """Validate and atomically archive one exact Provider body."""

        source_id, body = self._validate_pair(document, evidence)
        if len(body) > self._max_source_bytes:
            raise SourceArchiveSizeError("document body exceeds its single-file size limit")

        raw_directory = self._raw_directory()
        destination = self._destination(raw_directory, source_id)
        relative_path = f"{RAW_SOURCE_DIRECTORY}/{source_id}.txt"

        async with _archive_lock(raw_directory):
            # Revalidate every path after acquiring the lock.  This rejects a
            # symlink introduced between validation and the write.
            raw_directory = self._raw_directory()
            destination = self._destination(raw_directory, source_id)
            if destination.exists() or destination.is_symlink():
                _assert_no_symlink(destination, "source archive destination")
                if not destination.is_file():
                    raise SourceArchivePathError(
                        "source archive destination must be a regular file",
                    )
                existing = _read_bounded(destination, self._max_source_bytes)
                if existing == body:
                    return SourceArchiveResult(
                        source_id=source_id,
                        relative_path=relative_path,
                        content_sha256=evidence.content_sha256,
                        byte_length=len(body),
                        already_present=True,
                    )
                raise SourceArchiveConflictError(
                    f"source archive already contains different content for {source_id}",
                )

            current_total = _read_total(
                raw_directory,
                self._max_source_bytes,
                self._max_archive_bytes,
            )
            if current_total + len(body) > self._max_archive_bytes:
                raise SourceArchiveSizeError("raw source directory exceeds its total size limit")
            try:
                await atomic_write_bytes(destination, body)
            except OSError as exc:
                raise SourceArchivePathError("source body could not be written atomically") from exc
            # A read-back check makes the write boundary self-verifying and
            # catches an unexpected filesystem replacement before returning.
            written = _read_bounded(destination, self._max_source_bytes)
            if written != body or _digest(written) != evidence.content_sha256:
                raise SourceArchiveCorruptError("archived source failed its post-write hash check")
            return SourceArchiveResult(
                source_id=source_id,
                relative_path=relative_path,
                content_sha256=evidence.content_sha256,
                byte_length=len(body),
                already_present=False,
            )

    async def archive_document(
        self,
        document: FetchedDocument,
        evidence: SourceEvidence,
    ) -> SourceArchiveResult:
        """Compatibility spelling for :meth:`archive`."""

        return await self.archive(document, evidence)

    async def read(
        self,
        source: SourceEvidence | str,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        """Read one body as UTF-8 and verify its stored or supplied digest.

        Passing ``SourceEvidence`` is preferred because it verifies the
        evidence digest directly.  Passing a source ID is useful for a
        diagnostic read and still verifies that the archive bytes decode as
        UTF-8; callers may provide ``expected_sha256`` for the full check.
        """

        expected: str | None
        if isinstance(source, SourceEvidence):
            source_id = _source_id(source.source_id)
            expected = source.content_sha256
        elif isinstance(source, str):
            source_id = _source_id(source)
            expected = expected_sha256
        else:
            raise TypeError("source must be a SourceEvidence or source_id string")

        if expected is not None and (
            not isinstance(expected, str) or _SHA256_PATTERN.fullmatch(expected) is None
        ):
            raise SourceArchiveValidationError("expected_sha256 must be a lowercase SHA-256 digest")

        raw_directory = self._raw_directory()
        destination = self._destination(raw_directory, source_id)
        if not destination.exists():
            raise SourceArchiveCorruptError(f"archived source does not exist: {source_id}")
        _assert_no_symlink(destination, "source archive destination")
        if not destination.is_file():
            raise SourceArchivePathError("source archive destination must be a regular file")
        data = _read_bounded(destination, self._max_source_bytes)
        actual_digest = _digest(data)
        if expected is not None and actual_digest != expected:
            raise SourceArchiveCorruptError(
                "archived source SHA-256 does not match expected digest",
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceArchiveCorruptError("archived source is not valid UTF-8") from exc

    async def read_source(self, evidence: SourceEvidence) -> str:
        """Read and hash-check a body using its complete source evidence."""

        return await self.read(evidence)


WorkbenchSourceArchive = SourceArchive


async def archive_source(
    job_root: os.PathLike[str] | str,
    document: FetchedDocument,
    evidence: SourceEvidence,
    *,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
) -> SourceArchiveResult:
    """One-shot convenience wrapper around :class:`SourceArchive`."""

    return await SourceArchive(
        job_root,
        max_source_bytes=max_source_bytes,
        max_archive_bytes=max_archive_bytes,
    ).archive(document, evidence)


__all__ = [
    "ArchiveReceipt",
    "DEFAULT_MAX_ARCHIVE_BYTES",
    "DEFAULT_MAX_SOURCE_BYTES",
    "RAW_SOURCE_DIRECTORY",
    "SourceArchive",
    "SourceArchiveConflictError",
    "SourceArchiveCorruptError",
    "SourceArchiveError",
    "SourceArchivePathError",
    "SourceArchiveResult",
    "SourceArchiveSizeError",
    "SourceArchiveValidationError",
    "WorkbenchSourceArchive",
    "archive_source",
]
