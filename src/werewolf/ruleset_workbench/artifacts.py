"""Pure rendering of validated research bundles into workbench artifacts.

The research bundle is the input boundary for this module.  Rendering first
captures one canonical JSON byte representation and derives every output from
that representation.  This is intentional: ``RuleClaim`` contains mutable
JSON values, so rendering the model collection one file at a time could
otherwise observe different values if a caller mutates a nested object.

This module does not write files.  It returns immutable byte content together
with the relative paths a later storage step may persist below a job
directory.  Source Markdown contains only the excerpt that was captured in
the bundle; unrelated page content is never reconstructed or fetched here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .bundle_codec import encode_bundle
from .bundles import ResearchBundle

_SOURCE_ID_RE = re.compile(r"[a-z0-9_-]+", re.ASCII)
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


class WorkbenchArtifactError(ValueError):
    """Raised when canonical bundle content cannot become safe artifacts."""


@dataclass(frozen=True, slots=True)
class SourceExcerptArtifact:
    """One source excerpt and its safe relative workbench path."""

    source_id: str
    relative_path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class WorkbenchArtifacts:
    """Immutable content for the three 9.4 workbench artifact categories.

    ``source_excerpts`` is sorted by ``source_id``.  The ``files`` property
    exposes all paths in deterministic order and is suitable for a subsequent
    writer, while keeping this module free of filesystem I/O.
    """

    source_index: bytes
    source_excerpts: tuple[SourceExcerptArtifact, ...]
    claims: bytes

    @property
    def files(self) -> tuple[tuple[str, bytes], ...]:
        """Return deterministic relative paths and their byte contents."""

        excerpt_files = tuple(
            (excerpt.relative_path, excerpt.content) for excerpt in self.source_excerpts
        )
        return (
            ("sources/index.json", self.source_index),
            *excerpt_files,
            ("claims.jsonl", self.claims),
        )


def _normalise_lf(value: str) -> str:
    """Normalize source text line endings to LF without changing other text."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def _strict_json_bytes(value: object, *, newline: bool) -> bytes:
    """Encode JSON as stable UTF-8, rejecting non-standard numeric values."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # ``json.dumps`` currently emits LF, but normalize explicitly so this
        # remains true if the serializer implementation changes.
        text = _normalise_lf(text)
        if newline:
            text += "\n"
        return text.encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise WorkbenchArtifactError("workbench artifact JSON is not valid UTF-8") from exc


def _parse_canonical_bundle(encoded: bytes) -> dict[str, Any]:
    """Parse the one canonical bundle byte snapshot used by every renderer."""

    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkbenchArtifactError("canonical research bundle is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise WorkbenchArtifactError("canonical research bundle must be a JSON object")
    return decoded


def _safe_excerpt_path(source_id: object) -> str:
    """Return a Windows-safe POSIX relative path for one validated source ID."""

    if not isinstance(source_id, str) or _SOURCE_ID_RE.fullmatch(source_id) is None:
        raise WorkbenchArtifactError("source_id cannot be used as a workbench file name")
    if source_id.casefold() in _WINDOWS_RESERVED_NAMES:
        raise WorkbenchArtifactError("source_id cannot use a reserved Windows file name")

    relative = PurePosixPath("sources") / f"{source_id}.md"
    if relative.is_absolute() or relative.parts != ("sources", f"{source_id}.md"):
        raise WorkbenchArtifactError("source excerpt path must stay below sources/")
    return relative.as_posix()


def _source_index_payload(sources: list[object]) -> dict[str, object]:
    """Build the metadata index while replacing inline excerpts by paths."""

    index_sources: list[dict[str, object]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise WorkbenchArtifactError("sources must contain JSON objects")
        source_id = source.get("source_id")
        excerpt_path = _safe_excerpt_path(source_id)
        metadata = {key: value for key, value in source.items() if key != "excerpt"}
        metadata["excerpt_path"] = excerpt_path
        index_sources.append(metadata)
    return {"schema_version": 1, "sources": index_sources}


def render_workbench_artifacts(bundle: ResearchBundle) -> WorkbenchArtifacts:
    """Render one validated bundle into deterministic, unwritten artifacts.

    ``encode_bundle`` is called exactly once.  The canonical bytes are parsed
    into a detached JSON snapshot, and all source index, excerpt, and claims
    output is derived from that snapshot.  Consequently a caller cannot make
    one output observe a different nested mutable value than another output.
    """

    if not isinstance(bundle, ResearchBundle):
        raise TypeError("render_workbench_artifacts() expects a ResearchBundle")

    encoded = encode_bundle(bundle)
    payload = _parse_canonical_bundle(encoded)

    sources = payload.get("sources")
    claims = payload.get("claims")
    if not isinstance(sources, list) or not isinstance(claims, list):
        raise WorkbenchArtifactError("canonical research bundle collections must be arrays")

    source_index = _strict_json_bytes(_source_index_payload(sources), newline=True)

    excerpt_artifacts: list[SourceExcerptArtifact] = []
    for source in sources:
        if not isinstance(source, dict):
            raise WorkbenchArtifactError("sources must contain JSON objects")
        source_id = source.get("source_id")
        if not isinstance(source_id, str):
            raise WorkbenchArtifactError("source_id must be a string")
        relative_path = _safe_excerpt_path(source_id)
        excerpt = source.get("excerpt")
        if not isinstance(excerpt, str):
            raise WorkbenchArtifactError("source excerpts must be strings")
        content = (_normalise_lf(excerpt).rstrip("\n") + "\n").encode("utf-8")
        excerpt_artifacts.append(
            SourceExcerptArtifact(
                source_id=source_id,
                relative_path=relative_path,
                content=content,
            ),
        )

    claim_lines = tuple(_strict_json_bytes(claim, newline=False) for claim in claims)
    claims_content = b"".join(line + b"\n" for line in claim_lines)
    return WorkbenchArtifacts(
        source_index=source_index,
        source_excerpts=tuple(excerpt_artifacts),
        claims=claims_content,
    )


# ``render_artifacts`` is a short spelling for callers that already operate
# inside the workbench namespace.  Keep the descriptive name above as the
# canonical API for code search and documentation.
render_artifacts = render_workbench_artifacts


__all__ = [
    "SourceExcerptArtifact",
    "WorkbenchArtifactError",
    "WorkbenchArtifacts",
    "render_artifacts",
    "render_workbench_artifacts",
]
