"""Persistence primitives shared by snapshots and published artifacts."""

from importlib import import_module

from .atomic import (
    PathSecurityError,
    atomic_write_bytes,
    atomic_write_text,
    resolve_contained_path,
)

_SNAPSHOT_EXPORTS = {
    "SNAPSHOT_MANIFEST_FILENAME",
    "SNAPSHOT_SCHEMA_VERSION",
    "CorruptSnapshotError",
    "FrozenRulesetSnapshot",
    "GameSnapshot",
    "GameSnapshotError",
    "GameSnapshotService",
    "GameSnapshotStore",
    "SnapshotAlreadyExistsError",
    "SnapshotBoundaryError",
    "SnapshotFile",
    "SnapshotManifest",
    "SnapshotResult",
    "SnapshotSecurityError",
    "SnapshotStore",
}

_ARCHIVE_EXPORTS = {
    "ARCHIVE_MANIFEST_FILENAME",
    "ARCHIVE_SCHEMA_VERSION",
    "ArchiveAlreadyExistsError",
    "ArchiveDecision",
    "ArchiveFile",
    "ArchiveInputError",
    "ArchiveManifest",
    "ArchiveResult",
    "ArchiveService",
    "ArchiveSecurityError",
    "ArchiveStore",
    "CorruptArchiveError",
    "GameArchive",
    "GameArchiveError",
    "GameArchiveService",
    "GameArchiveStore",
    "RecoveryAssessment",
}


def __getattr__(name: str) -> object:
    """Load snapshot types lazily to keep atomic imports cycle-free.

    Knowledge storage imports the atomic path helpers while the game package
    imports runtime and knowledge modules.  Eagerly importing the snapshot
    service here would make that otherwise valid import graph circular.
    """

    if name in _SNAPSHOT_EXPORTS:
        return getattr(import_module(".snapshot", __name__), name)
    if name in _ARCHIVE_EXPORTS:
        return getattr(import_module(".archive", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PathSecurityError",
    "atomic_write_bytes",
    "atomic_write_text",
    "resolve_contained_path",
    "CorruptSnapshotError",
    "FrozenRulesetSnapshot",
    "GameSnapshot",
    "GameSnapshotError",
    "GameSnapshotService",
    "GameSnapshotStore",
    "SNAPSHOT_MANIFEST_FILENAME",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotAlreadyExistsError",
    "SnapshotBoundaryError",
    "SnapshotFile",
    "SnapshotManifest",
    "SnapshotResult",
    "SnapshotSecurityError",
    "SnapshotStore",
    "ARCHIVE_MANIFEST_FILENAME",
    "ARCHIVE_SCHEMA_VERSION",
    "ArchiveAlreadyExistsError",
    "ArchiveDecision",
    "ArchiveFile",
    "ArchiveInputError",
    "ArchiveManifest",
    "ArchiveResult",
    "ArchiveService",
    "ArchiveSecurityError",
    "ArchiveStore",
    "CorruptArchiveError",
    "GameArchive",
    "GameArchiveError",
    "GameArchiveService",
    "GameArchiveStore",
    "RecoveryAssessment",
]
