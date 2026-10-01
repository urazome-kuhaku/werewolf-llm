"""Safe path resolution and durable, atomic file writes.

Callers choose and authorize the storage root.  ``resolve_contained_path``
then turns a user- or configuration-derived relative path into a canonical
path under that root, including resolving symlinks that already exist.  The
write helpers intentionally do not accept a root: they operate on a path that
the caller has already authorized with this function or through an equivalent
policy.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path, PureWindowsPath
from typing import TypeAlias

PathLike: TypeAlias = str | os.PathLike[str]


class PathSecurityError(ValueError):
    """Raised when a path cannot be contained by the caller's storage root."""


def _path_from_os_path(value: PathLike) -> Path:
    """Convert an OS path value to ``Path`` while retaining strict errors."""

    raw_value = os.fspath(value)
    if isinstance(raw_value, bytes):
        raw_value = os.fsdecode(raw_value)
    if "\x00" in raw_value:
        raise PathSecurityError("paths must not contain a NUL character")
    return Path(raw_value)


def _reject_unsafe_child(child: Path) -> None:
    """Reject absolute, drive-qualified, or parent-traversing child paths.

    ``PureWindowsPath`` is checked in addition to the host ``Path``.  This
    keeps a Windows drive/UNC path from being treated as an ordinary relative
    filename if a value crosses a platform boundary, and makes the policy
    explicit for the Windows deployment target.
    """

    windows_child = PureWindowsPath(os.fspath(child))
    if child.is_absolute() or windows_child.is_absolute() or windows_child.drive:
        raise PathSecurityError("child path must be relative and drive-free")

    if ".." in child.parts or ".." in windows_child.parts:
        raise PathSecurityError("child path must not contain '..'")


def resolve_contained_path(root: PathLike, child: PathLike) -> Path:
    """Resolve ``child`` and require that it remains inside ``root``.

    The root is resolved first, so a root supplied through a symlink is
    treated as its canonical storage location.  Existing symlinks in the
    child path are resolved before containment is checked; this catches a
    link inside the root that points outside it.  Non-existent final path
    components are retained, allowing callers to validate a destination
    before an atomic write creates it.

    ``child`` must be relative.  Parent components, drive-qualified paths,
    UNC paths, and absolute paths are rejected even when their normalized
    result would happen to point inside the root.
    """

    root_path = _path_from_os_path(root).resolve()
    child_path = _path_from_os_path(child)
    _reject_unsafe_child(child_path)

    candidate = (root_path / child_path).resolve(strict=False)
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise PathSecurityError("resolved child path escapes its root") from exc
    return candidate


def _atomic_write_bytes_sync(path: Path, data: bytes) -> None:
    """Synchronously write bytes through a same-directory temporary file."""

    temporary_path: Path | None = None
    file_descriptor: int | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            file_descriptor = None
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _normalize_text(text: str) -> str:
    """Normalize all supported newline spellings to LF."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


async def atomic_write_bytes(path: PathLike, data: bytes) -> None:
    """Durably replace ``path`` with ``data`` using a same-directory temp.

    The blocking filesystem operations run in a worker thread.  The parent
    directory must already exist; this helper deliberately does not create
    directories or authorize a caller-supplied root.
    """

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    await asyncio.to_thread(_atomic_write_bytes_sync, _path_from_os_path(path), data)


async def atomic_write_text(path: PathLike, text: str) -> None:
    """Normalize ``text`` to UTF-8/LF and atomically replace ``path``."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    data = _normalize_text(text).encode("utf-8")
    await asyncio.to_thread(_atomic_write_bytes_sync, _path_from_os_path(path), data)


__all__ = [
    "PathSecurityError",
    "atomic_write_bytes",
    "atomic_write_text",
    "resolve_contained_path",
]
