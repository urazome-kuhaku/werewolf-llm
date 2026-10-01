"""Unit coverage for the bounded Provider source-body archive."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.ruleset_workbench import SourceClass, SourceEvidence
from werewolf.ruleset_workbench.research_provider import FetchedDocument
from werewolf.ruleset_workbench.source_archive import (
    SourceArchive,
    SourceArchiveConflictError,
    SourceArchiveCorruptError,
    SourceArchivePathError,
    SourceArchiveSizeError,
    SourceArchiveValidationError,
)


def _document(body: str, *, url: str = "https://example.test/rules") -> FetchedDocument:
    return FetchedDocument.model_validate({"url": url, "body": body, "title": "Rules"})


def _evidence(
    document: FetchedDocument,
    *,
    source_id: str = "source-example",
    excerpt: str | None = None,
    content_sha256: str | None = None,
) -> SourceEvidence:
    body = document.body
    return SourceEvidence.model_validate(
        {
            "source_id": source_id,
            "url": str(document.url),
            "title": "Rules",
            "publisher": "Example",
            "source_class": SourceClass.PLATFORM_RULES,
            "fetched_at": datetime(2026, 9, 28, 1, tzinfo=UTC),
            "content_sha256": content_sha256 or hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "excerpt": excerpt or body[0 : min(12, len(body))],
            "retrieval_method": "https",
        },
    )


async def test_archive_preserves_exact_provider_utf8_body_and_reads_with_hash_check(
    tmp_path: Path,
) -> None:
    body = "前导\r\n规则正文\r尾部"
    document = _document(body)
    evidence = _evidence(document, excerpt="规则正文")
    archive = SourceArchive(tmp_path)

    result = await archive.archive(document, evidence)

    assert result.already_present is False
    assert result.relative_path == "sources/raw/source-example.txt"
    path = tmp_path / "sources" / "raw" / "source-example.txt"
    assert path.read_bytes() == body.encode("utf-8")
    assert await archive.read_source(evidence) == body


async def test_same_body_is_idempotent_but_different_body_is_rejected(tmp_path: Path) -> None:
    first = _document("same body")
    first_evidence = _evidence(first)
    archive = SourceArchive(tmp_path)

    created = await archive.archive(first, first_evidence)
    repeated = await archive.archive(first, first_evidence)

    assert created.already_present is False
    assert repeated.already_present is True
    changed = _document("different body")
    changed_evidence = _evidence(changed, source_id=first_evidence.source_id)
    with pytest.raises(SourceArchiveConflictError, match="different content"):
        await archive.archive(changed, changed_evidence)


async def test_url_hash_and_excerpt_mismatches_are_rejected(tmp_path: Path) -> None:
    document = _document("the exact rule body")
    archive = SourceArchive(tmp_path)

    with pytest.raises(SourceArchiveValidationError, match="URL"):
        await archive.archive(
            document,
            _evidence(
                _document(document.body, url="https://example.test/other"),
            ),
        )

    with pytest.raises(SourceArchiveValidationError, match="SHA-256"):
        await archive.archive(document, _evidence(document, content_sha256="a" * 64))

    with pytest.raises(SourceArchiveValidationError, match="not contained"):
        await archive.archive(document, _evidence(document, excerpt="missing excerpt"))


async def test_read_rejects_tampered_body(tmp_path: Path) -> None:
    document = _document("immutable source")
    evidence = _evidence(document)
    archive = SourceArchive(tmp_path)
    await archive.archive(document, evidence)
    (tmp_path / "sources" / "raw" / "source-example.txt").write_bytes(b"tampered")

    with pytest.raises(SourceArchiveCorruptError, match="SHA-256"):
        await archive.read_source(evidence)


async def test_unsafe_source_id_is_rejected_even_if_model_validation_is_bypassed(
    tmp_path: Path,
) -> None:
    document = _document("body")
    evidence = _evidence(document)
    unsafe = SourceEvidence.model_construct(
        **{
            **evidence.model_dump(),
            "source_id": "../outside",
        },
    )

    with pytest.raises(SourceArchiveValidationError, match="source_id"):
        await SourceArchive(tmp_path).archive(document, unsafe)


async def test_symlinked_raw_directory_and_destination_are_rejected(tmp_path: Path) -> None:
    document = _document("body")
    evidence = _evidence(document)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    try:
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "raw").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows host")

    with pytest.raises(SourceArchivePathError, match="symbolic link"):
        await SourceArchive(tmp_path).archive(document, evidence)


async def test_symlinked_destination_is_rejected(tmp_path: Path) -> None:
    document = _document("body")
    evidence = _evidence(document)
    raw = tmp_path / "sources" / "raw"
    raw.mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-destination"
    outside.write_bytes(b"outside")
    try:
        (raw / "source-example.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows host")

    with pytest.raises(SourceArchivePathError, match="symbolic link"):
        await SourceArchive(tmp_path).archive(document, evidence)


async def test_single_file_and_total_archive_limits_are_enforced(tmp_path: Path) -> None:
    first = _document("1234")
    first_evidence = _evidence(first)
    archive = SourceArchive(tmp_path, max_source_bytes=4, max_archive_bytes=7)
    await archive.archive(first, first_evidence)

    too_large = _document("12345")
    with pytest.raises(SourceArchiveSizeError, match="single-file"):
        await archive.archive(too_large, _evidence(too_large, source_id="source-too-large"))

    second = _document("5678")
    with pytest.raises(SourceArchiveSizeError, match="total"):
        await archive.archive(second, _evidence(second, source_id="source-second"))
