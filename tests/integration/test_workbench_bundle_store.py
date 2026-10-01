"""Integration tests for the completed offline research bundle artifact."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.ruleset_workbench import (
    BundleAlreadyExistsError,
    BundleTooLargeError,
    ClaimScope,
    ClaimStatus,
    CorruptBundleError,
    ResearchBundle,
    SourceClass,
    WorkbenchBundleStore,
    WorkbenchJobStore,
    bundle_sha256,
    encode_bundle,
)

AT_UTC = datetime(2026, 9, 27, 15, 15, 5, tzinfo=UTC)


def _bundle(
    *,
    board_name: str = "fictional-board",
    locale: str = "zh-CN",
    excerpt: str = "A wholly fictional excerpt for a bundle-store test.",
) -> ResearchBundle:
    source_id = "fictional-source-alpha"
    return ResearchBundle.model_validate(
        {
            "board_name": board_name,
            "locale": locale,
            "platform": "fictional-platform",
            "region": "fictional-region",
            "constraints": ["offline", "fixture"],
            "sources": [
                {
                    "source_id": source_id,
                    "url": "https://fictional.example/source-alpha",
                    "title": "Fictional source",
                    "publisher": "Fictional Publisher",
                    "source_class": SourceClass.OTHER,
                    "published_at": None,
                    "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
                    "content_sha256": "a" * 64,
                    "excerpt": excerpt,
                    "retrieval_method": "fixture",
                },
            ],
            "claims": [
                {
                    "claim_id": "fictional-claim-alpha",
                    "ruleset_candidate_id": "fictional-board",
                    "key": "fictional.rule_enabled",
                    "value": {"enabled": True},
                    "scope": ClaimScope.MECHANIC,
                    "conditions": {"round": 1},
                    "evidence_ids": [source_id],
                    "confidence": 0.25,
                    "extraction_note": "This is fabricated test data.",
                    "status": ClaimStatus.UNVERIFIED,
                },
            ],
        },
    )


async def _stores(tmp_path: Path) -> tuple[WorkbenchJobStore, WorkbenchBundleStore, str]:
    job_store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")
    _, job_dir_name = await job_store.create_job("fictional-board", "zh-CN", AT_UTC)
    return job_store, WorkbenchBundleStore(job_store), job_dir_name


@pytest.mark.asyncio
async def test_round_trip_writes_only_bundle_and_completion_marker(tmp_path: Path) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    bundle = _bundle()

    digest = await bundle_store.save_bundle(job_dir_name, bundle)
    job_dir = job_store.root / job_dir_name

    assert digest == bundle_sha256(bundle)
    assert (job_dir / "bundle.json").read_bytes() == encode_bundle(bundle)
    assert (job_dir / "bundle.sha256").read_text(encoding="utf-8") == digest
    assert sorted(path.name for path in job_dir.iterdir()) == [
        "bundle.json",
        "bundle.sha256",
        "job.json",
        "request.json",
    ]
    assert await bundle_store.load_bundle(job_dir_name) == bundle


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["board_name", "locale"])
async def test_save_rejects_bundle_request_mismatch(tmp_path: Path, field: str) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    values = {"board_name": "fictional-board", "locale": "zh-CN"}
    values[field] = "other-board" if field == "board_name" else "en-US"
    mismatched = _bundle(**values)

    with pytest.raises(ValueError, match="match request"):
        await bundle_store.save_bundle(job_dir_name, mismatched)

    job_dir = job_store.root / job_dir_name
    assert not (job_dir / "bundle.json").exists()
    assert not (job_dir / "bundle.sha256").exists()


@pytest.mark.asyncio
async def test_completed_bundle_cannot_be_overwritten(tmp_path: Path) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    original = _bundle()
    await bundle_store.save_bundle(job_dir_name, original)
    job_dir = job_store.root / job_dir_name
    original_json = (job_dir / "bundle.json").read_bytes()
    original_marker = (job_dir / "bundle.sha256").read_bytes()

    with pytest.raises(BundleAlreadyExistsError):
        await bundle_store.save_bundle(
            job_dir_name,
            _bundle(excerpt="A replacement must never overwrite a completed artifact."),
        )

    assert (job_dir / "bundle.json").read_bytes() == original_json
    assert (job_dir / "bundle.sha256").read_bytes() == original_marker


@pytest.mark.asyncio
async def test_concurrent_saves_allow_one_writer_and_preserve_completed_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    first = _bundle(excerpt="The first concurrent save wins.")
    second = _bundle(excerpt="The second concurrent save must be rejected.")
    import werewolf.ruleset_workbench.bundle_store as bundle_store_module

    original_write = bundle_store_module.atomic_write_bytes
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    write_calls = 0

    async def paused_first_write(path: Path, data: bytes) -> None:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            first_write_started.set()
            await release_first_write.wait()
        await original_write(path, data)

    monkeypatch.setattr(bundle_store_module, "atomic_write_bytes", paused_first_write)
    first_task = asyncio.create_task(bundle_store.save_bundle(job_dir_name, first))
    await first_write_started.wait()
    second_task = asyncio.create_task(bundle_store.save_bundle(job_dir_name, second))
    await asyncio.sleep(0)
    assert not second_task.done()

    release_first_write.set()
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)

    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, BundleAlreadyExistsError) for result in results) == 1
    assert write_calls == 1
    assert await bundle_store.load_bundle(job_dir_name) == first


@pytest.mark.asyncio
async def test_tampered_json_is_rejected_by_marker_hash(tmp_path: Path) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    await bundle_store.save_bundle(job_dir_name, _bundle())
    bundle_path = job_store.root / job_dir_name / "bundle.json"
    bundle_path.write_bytes(bundle_path.read_bytes().replace(b"fictional", b"tampered", 1))

    with pytest.raises(CorruptBundleError, match="does not match"):
        await bundle_store.load_bundle(job_dir_name)


@pytest.mark.asyncio
async def test_missing_marker_rejects_load_and_allows_safe_retry(tmp_path: Path) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    bundle = _bundle()
    await bundle_store.save_bundle(job_dir_name, bundle)
    job_dir = job_store.root / job_dir_name
    (job_dir / "bundle.sha256").unlink()

    with pytest.raises(CorruptBundleError, match="required"):
        await bundle_store.load_bundle(job_dir_name)

    assert await bundle_store.save_bundle(job_dir_name, bundle) == bundle_sha256(bundle)
    assert await bundle_store.load_bundle(job_dir_name) == bundle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker",
    ["A" * 64, "g" * 64, "0" * 63, "0" * 65, "0" * 63 + "\n"],
)
async def test_bad_marker_format_is_rejected(tmp_path: Path, marker: str) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    await bundle_store.save_bundle(job_dir_name, _bundle())
    marker_path = job_store.root / job_dir_name / "bundle.sha256"
    marker_path.write_text(marker, encoding="ascii")

    with pytest.raises(CorruptBundleError, match="64 lowercase"):
        await bundle_store.load_bundle(job_dir_name)


@pytest.mark.asyncio
async def test_bad_json_is_rejected_after_valid_marker_shape(tmp_path: Path) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    job_dir = job_store.root / job_dir_name
    bad_json = b"{broken"
    (job_dir / "bundle.json").write_bytes(bad_json)
    (job_dir / "bundle.sha256").write_text(
        hashlib.sha256(bad_json).hexdigest(),
        encoding="ascii",
    )

    with pytest.raises(CorruptBundleError, match="schema or reference"):
        await bundle_store.load_bundle(job_dir_name)


@pytest.mark.asyncio
async def test_oversized_bundle_is_rejected_and_orphan_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_store, bundle_store, job_dir_name = await _stores(tmp_path)
    bundle = _bundle()
    import werewolf.ruleset_workbench.bundle_store as bundle_store_module

    monkeypatch.setattr(bundle_store_module, "DEFAULT_MAX_BUNDLE_BYTES", 100)
    with pytest.raises(BundleTooLargeError):
        await bundle_store.save_bundle(job_dir_name, bundle)

    job_dir = job_store.root / job_dir_name
    assert not (job_dir / "bundle.json").exists()
    assert not (job_dir / "bundle.sha256").exists()

    # An incomplete prior write is replaceable once the size policy is restored.
    monkeypatch.setattr(bundle_store_module, "DEFAULT_MAX_BUNDLE_BYTES", 4 * 1024 * 1024)
    (job_dir / "bundle.json").write_bytes(b"orphan")
    assert await bundle_store.save_bundle(job_dir_name, bundle) == bundle_sha256(bundle)
    assert json.loads((job_dir / "bundle.json").read_text(encoding="utf-8"))["schema_version"] == 1
