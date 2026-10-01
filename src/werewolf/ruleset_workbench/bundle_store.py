"""Durable storage for the canonical offline research bundle.

The bundle is the first artifact written after a research job is created.  A
``bundle.json`` file is written atomically, and a separate SHA-256 marker is
written only after the JSON write succeeds.  The marker therefore acts as the
completion record: a JSON file without it is an incomplete artifact that may
be safely retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

from werewolf.persistence import (
    PathSecurityError,
    atomic_write_bytes,
    atomic_write_text,
    resolve_contained_path,
)

from .bundle_codec import DEFAULT_MAX_BUNDLE_BYTES, bundle_sha256, decode_bundle, encode_bundle
from .bundles import ResearchBundle
from .store import WorkbenchJobStore

_BUNDLE_FILENAME = "bundle.json"
_MARKER_FILENAME = "bundle.sha256"
_MARKER_RE = re.compile(r"^[0-9a-f]{64}$")
_SAVE_LOCKS: dict[Path, asyncio.Lock] = {}


def _save_lock(bundle_path: Path) -> asyncio.Lock:
    """Return the process-local save lock for one bundle path."""

    return _SAVE_LOCKS.setdefault(bundle_path, asyncio.Lock())


class WorkbenchBundleStoreError(ValueError):
    """Raised when a bundle cannot be safely persisted or loaded."""


class BundleAlreadyExistsError(WorkbenchBundleStoreError):
    """Raised when a completed bundle would be overwritten."""


class CorruptBundleError(WorkbenchBundleStoreError):
    """Raised when a bundle or its completion marker is incomplete or invalid."""


class BundleTooLargeError(WorkbenchBundleStoreError):
    """Raised when a bundle exceeds the configured persistence limit."""


def _read_bounded_bytes(path: Path, max_bytes: int) -> bytes:
    """Read at most one byte beyond a file limit for a useful rejection."""

    with path.open("rb") as bundle_file:
        data = bundle_file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise BundleTooLargeError(
            f"{path.name} exceeds the maximum size ({max_bytes} bytes)",
        )
    return data


def _path_exists(path: Path) -> bool:
    """Return whether a resolved path exists, including a directory at its name."""

    return path.exists()


def _is_file(path: Path) -> bool:
    """Return whether a resolved artifact path is a regular file."""

    return path.is_file()


class WorkbenchBundleStore:
    """Persist and load one canonical bundle below each job directory."""

    def __init__(self, job_store: WorkbenchJobStore) -> None:
        if not isinstance(job_store, WorkbenchJobStore):
            raise TypeError("job_store must be a WorkbenchJobStore")
        self._job_store = job_store

    def _bundle_paths(self, job_dir_name: str) -> tuple[Path, Path]:
        """Resolve both artifact paths strictly inside the selected job dir."""

        try:
            job_dir = resolve_contained_path(self._job_store.root, job_dir_name)
            bundle_path = resolve_contained_path(job_dir, _BUNDLE_FILENAME)
            marker_path = resolve_contained_path(job_dir, _MARKER_FILENAME)
        except PathSecurityError as exc:
            raise WorkbenchBundleStoreError(
                "bundle path escapes the workbench job directory",
            ) from exc
        return bundle_path, marker_path

    async def save_bundle(
        self,
        job_dir_name: str,
        bundle: ResearchBundle,
    ) -> str:
        """Write one validated bundle and return its canonical SHA-256 digest.

        The request is loaded before any artifact write.  A marker that is
        already present makes the bundle complete and permanently immutable;
        when the marker is absent, an earlier orphan ``bundle.json`` may be
        replaced and retried safely.
        """

        # ``encode_bundle`` revalidates nested mutable values before any I/O.
        encoded = encode_bundle(bundle)
        if len(encoded) > DEFAULT_MAX_BUNDLE_BYTES:
            raise BundleTooLargeError(
                f"bundle JSON exceeds the maximum size ({DEFAULT_MAX_BUNDLE_BYTES} bytes)",
            )

        request = await self._job_store.load_request(job_dir_name)
        if bundle.board_name != request.board_name or bundle.locale != request.locale:
            raise WorkbenchBundleStoreError(
                "bundle board_name and locale must match request.json exactly",
            )

        bundle_path, marker_path = self._bundle_paths(job_dir_name)
        digest = hashlib.sha256(encoded).hexdigest()

        # The marker check and both writes must be one critical section.  The
        # marker is the completion record, so two callers checking it before
        # either writes would otherwise overwrite each other's bundle.
        async with _save_lock(bundle_path):
            if await asyncio.to_thread(_path_exists, marker_path):
                raise BundleAlreadyExistsError(
                    f"completed bundle already exists for job {job_dir_name}",
                )

            # The marker is deliberately the final write.  If this write
            # fails, the JSON remains an uncommitted orphan and a later call
            # may retry it.
            await atomic_write_bytes(bundle_path, encoded)
            await atomic_write_text(marker_path, digest)
            return digest

    async def load_bundle(self, job_dir_name: str) -> ResearchBundle:
        """Load and verify a completed bundle for one research job."""

        request = await self._job_store.load_request(job_dir_name)
        bundle_path, marker_path = self._bundle_paths(job_dir_name)

        if not await asyncio.to_thread(_is_file, marker_path):
            raise CorruptBundleError(
                "bundle.sha256 is required before bundle.json is loadable",
            )
        try:
            marker_bytes = await asyncio.to_thread(_read_bounded_bytes, marker_path, 64)
        except BundleTooLargeError as exc:
            raise CorruptBundleError(
                "bundle.sha256 must be 64 lowercase hexadecimal characters",
            ) from exc
        except (OSError, UnicodeError) as exc:
            raise CorruptBundleError("bundle.sha256 could not be read") from exc

        try:
            marker = marker_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise CorruptBundleError("bundle.sha256 must contain ASCII hex") from exc
        if _MARKER_RE.fullmatch(marker) is None:
            raise CorruptBundleError("bundle.sha256 must be 64 lowercase hexadecimal characters")

        if not await asyncio.to_thread(_is_file, bundle_path):
            raise CorruptBundleError("bundle.json is required when bundle.sha256 is present")
        try:
            encoded = await asyncio.to_thread(
                _read_bounded_bytes,
                bundle_path,
                DEFAULT_MAX_BUNDLE_BYTES,
            )
        except BundleTooLargeError as exc:
            raise CorruptBundleError(
                f"bundle.json exceeds the maximum size ({DEFAULT_MAX_BUNDLE_BYTES} bytes)",
            ) from exc
        except (OSError, UnicodeError) as exc:
            raise CorruptBundleError("bundle.json could not be read") from exc

        raw_digest = hashlib.sha256(encoded).hexdigest()
        if raw_digest != marker:
            raise CorruptBundleError("bundle.sha256 does not match bundle.json")

        try:
            bundle = decode_bundle(encoded, max_bytes=DEFAULT_MAX_BUNDLE_BYTES)
        except (TypeError, ValueError) as exc:
            raise CorruptBundleError("bundle.json failed schema or reference validation") from exc

        # Persisted bundles are required to be canonical, so equivalent JSON
        # with different whitespace or ordering cannot bypass the marker.
        if bundle_sha256(bundle) != marker:
            raise CorruptBundleError("bundle.json is not canonical UTF-8 JSON")

        if bundle.board_name != request.board_name or bundle.locale != request.locale:
            raise CorruptBundleError(
                "bundle board_name and locale do not match request.json",
            )
        return bundle


__all__ = [
    "BundleAlreadyExistsError",
    "BundleTooLargeError",
    "CorruptBundleError",
    "WorkbenchBundleStore",
    "WorkbenchBundleStoreError",
]
