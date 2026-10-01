"""Integration coverage for durable workbench analysis artifacts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.ruleset_workbench import (
    WORKBENCH_ANALYSIS_MANIFEST_FILENAME,
    AnalysisAlreadyExistsError,
    ClaimScope,
    ClaimStatus,
    CorruptWorkbenchAnalysisError,
    CorruptWorkbenchArtifactsError,
    ResearchBundle,
    SourceClass,
    WorkbenchAnalysisStore,
    WorkbenchAnalysisStoreError,
    WorkbenchArtifactStore,
    WorkbenchBundleStore,
    WorkbenchJobStore,
)

AT_UTC = datetime(2026, 9, 27, 15, 15, 5, tzinfo=UTC)


def _source(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"虚构来源 {source_id}",
        "publisher": "虚构出版者",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": (source_id[-1] if source_id[-1] in "0123456789abcdef" else "a") * 64,
        "excerpt": "只保留分析测试所需的来源摘录。",
        "retrieval_method": "fixture",
    }


def _claim(claim_id: str, value: bool, source_id: str) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": "fictional-board",
        "key": "witch.can_self_heal",
        "value": value,
        "scope": ClaimScope.ROLE,
        "conditions": {"night": 1},
        "evidence_ids": [source_id],
        "confidence": 0.9,
        "extraction_note": "仅用于测试。",
        "status": ClaimStatus.SUPPORTED,
    }


def _bundle() -> ResearchBundle:
    return ResearchBundle.model_validate(
        {
            "board_name": "虚构测试板子",
            "locale": "zh-CN",
            "sources": [_source("source-a"), _source("source-b")],
            "claims": [
                _claim("claim-false", False, "source-a"),
                _claim("claim-true", True, "source-b"),
            ],
        },
    )


async def _stores(
    tmp_path: Path,
) -> tuple[
    WorkbenchJobStore,
    WorkbenchBundleStore,
    WorkbenchArtifactStore,
    WorkbenchAnalysisStore,
    str,
]:
    job_store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")
    _, job_dir_name = await job_store.create_job("虚构测试板子", "zh-CN", AT_UTC)
    bundle_store = WorkbenchBundleStore(job_store)
    await bundle_store.save_bundle(job_dir_name, _bundle())
    artifact_store = WorkbenchArtifactStore(job_store, bundle_store)
    analysis_store = WorkbenchAnalysisStore(job_store, bundle_store, artifact_store)
    return job_store, bundle_store, artifact_store, analysis_store, job_dir_name


@pytest.mark.asyncio
async def test_analysis_round_trip_keeps_conflict_decision_and_verifies_bytes(
    tmp_path: Path,
) -> None:
    job_store, _, artifact_store, analysis_store, job_dir_name = await _stores(tmp_path)
    await artifact_store.materialize(job_dir_name)

    manifest = await analysis_store.materialize(job_dir_name)
    job_dir = job_store.root / job_dir_name

    assert [item.relative_path for item in manifest.files] == [
        "conflicts.json",
        "variants.json",
    ]
    assert (job_dir / WORKBENCH_ANALYSIS_MANIFEST_FILENAME).is_file()
    assert await analysis_store.verify(job_dir_name) == manifest
    report = await analysis_store.load(job_dir_name)
    assert report.has_unresolved_conflicts is True
    assert (
        json.loads((job_dir / "conflicts.json").read_text(encoding="utf-8"))["needs_human_decision"]
        is True
    )
    with pytest.raises(AnalysisAlreadyExistsError):
        await analysis_store.materialize(job_dir_name)


@pytest.mark.asyncio
async def test_missing_or_tampered_analysis_artifacts_are_rejected(tmp_path: Path) -> None:
    job_store, _, artifact_store, analysis_store, job_dir_name = await _stores(tmp_path)
    await artifact_store.materialize(job_dir_name)
    await analysis_store.materialize(job_dir_name)
    job_dir = job_store.root / job_dir_name

    variants_path = job_dir / "variants.json"
    variants_path.write_bytes(variants_path.read_bytes() + b"tampered")
    with pytest.raises(CorruptWorkbenchAnalysisError, match="mismatch"):
        await analysis_store.verify(job_dir_name)

    variants_path.unlink()
    with pytest.raises(CorruptWorkbenchAnalysisError, match="missing"):
        await analysis_store.verify(job_dir_name)


@pytest.mark.asyncio
async def test_missing_marker_allows_retry_of_analysis_outputs(tmp_path: Path) -> None:
    job_store, _, artifact_store, analysis_store, job_dir_name = await _stores(tmp_path)
    await artifact_store.materialize(job_dir_name)
    first = await analysis_store.materialize(job_dir_name)
    marker_path = job_store.root / job_dir_name / WORKBENCH_ANALYSIS_MANIFEST_FILENAME
    marker_path.unlink()

    with pytest.raises(CorruptWorkbenchAnalysisError, match="required"):
        await analysis_store.load(job_dir_name)
    retried = await analysis_store.materialize(job_dir_name)
    assert retried == first
    assert await analysis_store.verify(job_dir_name) == retried


@pytest.mark.asyncio
async def test_concurrent_materialize_calls_allow_one_completed_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_store, _, artifact_store, analysis_store, job_dir_name = await _stores(tmp_path)
    await artifact_store.materialize(job_dir_name)
    import werewolf.ruleset_workbench.analysis_store as analysis_store_module

    original_write = analysis_store_module.atomic_write_bytes
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

    monkeypatch.setattr(analysis_store_module, "atomic_write_bytes", paused_first_write)
    first_task = asyncio.create_task(analysis_store.materialize(job_dir_name))
    await first_write_started.wait()
    second_task = asyncio.create_task(analysis_store.materialize(job_dir_name))
    await asyncio.sleep(0)
    assert not second_task.done()

    release_first_write.set()
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)
    assert sum(isinstance(result, WorkbenchAnalysisStoreError) for result in results) == 1
    assert sum(hasattr(result, "bundle_sha256") for result in results) == 1
    assert write_calls == 2
    assert await analysis_store.verify(job_dir_name)


@pytest.mark.asyncio
async def test_incomplete_source_artifacts_are_rejected_before_analysis(tmp_path: Path) -> None:
    job_store, _, _, analysis_store, job_dir_name = await _stores(tmp_path)

    with pytest.raises(CorruptWorkbenchArtifactsError, match="manifest is required"):
        await analysis_store.materialize(job_dir_name)

    job_dir = job_store.root / job_dir_name
    assert not (job_dir / "variants.json").exists()
    assert not (job_dir / "conflicts.json").exists()
    assert not (job_dir / WORKBENCH_ANALYSIS_MANIFEST_FILENAME).exists()


@pytest.mark.asyncio
async def test_analysis_output_symlink_escape_is_rejected(tmp_path: Path) -> None:
    job_store, _, artifact_store, analysis_store, job_dir_name = await _stores(tmp_path)
    await artifact_store.materialize(job_dir_name)
    job_dir = job_store.root / job_dir_name
    outside = tmp_path / "outside-variants.json"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        (job_dir / "variants.json").symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(WorkbenchAnalysisStoreError, match="escapes"):
        await analysis_store.materialize(job_dir_name)
    assert outside.read_text(encoding="utf-8") == "sentinel"
