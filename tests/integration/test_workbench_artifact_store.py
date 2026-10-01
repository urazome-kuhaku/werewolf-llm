"""Integration coverage for durable rendered workbench artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.ruleset_workbench import (
    WORKBENCH_ARTIFACT_MANIFEST_FILENAME,
    ArtifactAlreadyExistsError,
    ClaimScope,
    ClaimStatus,
    CorruptWorkbenchArtifactsError,
    ResearchBundle,
    SourceClass,
    WorkbenchArtifactStore,
    WorkbenchArtifactStoreError,
    WorkbenchBundleStore,
    WorkbenchJobStore,
)

AT_UTC = datetime(2026, 9, 27, 15, 15, 5, tzinfo=UTC)


def _source(source_id: str, *, excerpt: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"虚构来源 {source_id}",
        "publisher": "虚构出版者",
        "source_class": SourceClass.OTHER,
        "published_at": datetime(2026, 9, 1, 12, tzinfo=UTC),
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": "a" * 64,
        "excerpt": excerpt,
        "retrieval_method": "fixture",
    }


def _claim(claim_id: str, source_id: str) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": "fictional-board",
        "key": f"fictional.{claim_id.replace('-', '_')}",
        "value": {"enabled": True, "说明": "中文值"},
        "scope": ClaimScope.MECHANIC,
        "conditions": {"阶段": "夜晚"},
        "evidence_ids": [source_id],
        "confidence": 0.75,
        "extraction_note": "仅用于测试。",
        "status": ClaimStatus.SUPPORTED,
    }


def _bundle() -> ResearchBundle:
    return ResearchBundle.model_validate(
        {
            "board_name": "虚构测试板子",
            "locale": "zh-CN",
            "sources": [
                _source("fictional-source-zeta", excerpt="甲行\r\n乙行"),
                _source("fictional-source-alpha", excerpt="只保留必要摘录。"),
            ],
            "claims": [
                _claim("fictional-claim-zeta", "fictional-source-zeta"),
                _claim("fictional-claim-alpha", "fictional-source-alpha"),
            ],
        },
    )


async def _stores(
    tmp_path: Path,
) -> tuple[
    WorkbenchJobStore,
    WorkbenchBundleStore,
    WorkbenchArtifactStore,
    str,
]:
    job_store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")
    _, job_dir_name = await job_store.create_job("虚构测试板子", "zh-CN", AT_UTC)
    bundle_store = WorkbenchBundleStore(job_store)
    await bundle_store.save_bundle(job_dir_name, _bundle())
    artifact_store = WorkbenchArtifactStore(job_store, bundle_store)
    return job_store, bundle_store, artifact_store, job_dir_name


@pytest.mark.asyncio
async def test_materialize_round_trip_writes_manifest_last_and_verifies(
    tmp_path: Path,
) -> None:
    job_store, _, artifact_store, job_dir_name = await _stores(tmp_path)

    manifest = await artifact_store.materialize(job_dir_name)
    job_dir = job_store.root / job_dir_name
    manifest_path = job_dir / WORKBENCH_ARTIFACT_MANIFEST_FILENAME

    assert manifest_path.is_file()
    assert [item.relative_path for item in manifest.files] == sorted(
        item.relative_path for item in manifest.files
    )
    assert (job_dir / "sources" / "index.json").is_file()
    assert (job_dir / "claims.jsonl").is_file()
    assert await artifact_store.verify(job_dir_name) == manifest

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["bundle_sha256"] == manifest.bundle_sha256
    for item in manifest.files:
        content = (job_dir / Path(item.relative_path)).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item.sha256

    with pytest.raises(ArtifactAlreadyExistsError):
        await artifact_store.materialize(job_dir_name)


@pytest.mark.asyncio
async def test_verification_rejects_missing_or_tampered_files_and_manifest(
    tmp_path: Path,
) -> None:
    job_store, _, artifact_store, job_dir_name = await _stores(tmp_path)
    await artifact_store.materialize(job_dir_name)
    job_dir = job_store.root / job_dir_name

    claims_path = job_dir / "claims.jsonl"
    claims_path.write_bytes(claims_path.read_bytes() + b"tampered")
    with pytest.raises(CorruptWorkbenchArtifactsError, match="digest mismatch"):
        await artifact_store.verify(job_dir_name)

    claims_path.unlink()
    with pytest.raises(CorruptWorkbenchArtifactsError, match="missing"):
        await artifact_store.verify(job_dir_name)


@pytest.mark.asyncio
async def test_manifest_tampering_is_rejected_even_when_file_digests_are_changed(
    tmp_path: Path,
) -> None:
    job_store, _, artifact_store, job_dir_name = await _stores(tmp_path)
    await artifact_store.materialize(job_dir_name)
    manifest_path = job_store.root / job_dir_name / WORKBENCH_ARTIFACT_MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["bundle_sha256"] = "0" * 64
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CorruptWorkbenchArtifactsError, match="canonical"):
        await artifact_store.verify(job_dir_name)


@pytest.mark.asyncio
async def test_missing_manifest_allows_safe_retry_of_partial_outputs(tmp_path: Path) -> None:
    job_store, _, artifact_store, job_dir_name = await _stores(tmp_path)
    await artifact_store.materialize(job_dir_name)
    manifest_path = job_store.root / job_dir_name / WORKBENCH_ARTIFACT_MANIFEST_FILENAME
    manifest_path.unlink()

    retried = await artifact_store.materialize(job_dir_name)
    assert await artifact_store.verify(job_dir_name) == retried


@pytest.mark.asyncio
async def test_concurrent_materialize_calls_allow_one_completed_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_store, _, artifact_store, job_dir_name = await _stores(tmp_path)
    import werewolf.ruleset_workbench.artifact_store as artifact_store_module

    original_write = artifact_store_module.atomic_write_bytes
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

    monkeypatch.setattr(artifact_store_module, "atomic_write_bytes", paused_first_write)
    first_task = asyncio.create_task(artifact_store.materialize(job_dir_name))
    await first_write_started.wait()
    second_task = asyncio.create_task(artifact_store.materialize(job_dir_name))
    await asyncio.sleep(0)
    assert not second_task.done()

    release_first_write.set()
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)
    assert sum(hasattr(result, "bundle_sha256") for result in results) == 1
    assert sum(isinstance(result, ArtifactAlreadyExistsError) for result in results) == 1
    assert write_calls == 4  # index, two excerpts, and claims are all atomic writes
    assert await artifact_store.verify(job_dir_name)


@pytest.mark.asyncio
async def test_materialize_rejects_source_directory_symlink_escape(tmp_path: Path) -> None:
    job_store, _, artifact_store, job_dir_name = await _stores(tmp_path)
    job_dir = job_store.root / job_dir_name
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (job_dir / "sources").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(WorkbenchArtifactStoreError, match="escapes"):
        await artifact_store.materialize(job_dir_name)
    assert not (outside / "index.json").exists()


@pytest.mark.asyncio
async def test_materialize_rejects_manifest_symlink_escape(tmp_path: Path) -> None:
    job_store, _, artifact_store, job_dir_name = await _stores(tmp_path)
    job_dir = job_store.root / job_dir_name
    outside_manifest = tmp_path / "outside-manifest.json"
    outside_manifest.write_text("sentinel", encoding="utf-8")
    try:
        (job_dir / WORKBENCH_ARTIFACT_MANIFEST_FILENAME).symlink_to(outside_manifest)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(WorkbenchArtifactStoreError, match="escapes"):
        await artifact_store.materialize(job_dir_name)
    assert outside_manifest.read_text(encoding="utf-8") == "sentinel"
