"""Integration coverage for durable workbench coverage reports."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.ruleset_workbench.analysis_store import (
    CorruptWorkbenchAnalysisError,
    WorkbenchAnalysisStore,
)
from werewolf.ruleset_workbench.artifact_store import (
    CorruptWorkbenchArtifactsError,
    WorkbenchArtifactStore,
)
from werewolf.ruleset_workbench.bundle_store import WorkbenchBundleStore
from werewolf.ruleset_workbench.bundles import ResearchBundle
from werewolf.ruleset_workbench.claims import ClaimScope, ClaimStatus
from werewolf.ruleset_workbench.coverage import CoverageRequirement, CoverageStatus
from werewolf.ruleset_workbench.coverage_store import (
    WORKBENCH_COVERAGE_MANIFEST_FILENAME,
    CorruptWorkbenchCoverageError,
    CoverageAlreadyExistsError,
    WorkbenchCoverageStore,
    WorkbenchCoverageStoreError,
)
from werewolf.ruleset_workbench.evidence import SourceClass
from werewolf.ruleset_workbench.store import WorkbenchJobStore

AT_UTC = datetime(2026, 9, 27, 15, 15, 5, tzinfo=UTC)


def _source(source_id: str, digest_letter: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"虚构来源 {source_id}",
        "publisher": "虚构出版者",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": digest_letter * 64,
        "excerpt": "只保留覆盖测试所需的来源摘录。",
        "retrieval_method": "fixture",
    }


def _claim(claim_id: str, source_id: str) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": "fictional-board",
        "key": "role.witch.can_self_heal",
        "value": False,
        "scope": ClaimScope.ROLE,
        "conditions": {},
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
            "sources": [
                _source("source-a", "a"),
                _source("source-b", "b"),
            ],
            "claims": [_claim("claim-a", "source-a")],
        },
    )


def _requirement(
    requirement_id: str = "witch-self-heal",
    *,
    key: str | None = None,
) -> CoverageRequirement:
    return CoverageRequirement.model_validate(
        {
            "requirement_id": requirement_id,
            "scope": ClaimScope.ROLE,
            "key": key or "role.witch.can_self_heal",
            "conditions": {},
            "required": True,
            "min_independent_evidence_count": 1,
        },
    )


async def _stores(
    tmp_path: Path,
) -> tuple[
    WorkbenchJobStore,
    WorkbenchArtifactStore,
    WorkbenchAnalysisStore,
    WorkbenchCoverageStore,
    str,
]:
    job_store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")
    _, job_dir_name = await job_store.create_job("虚构测试板子", "zh-CN", AT_UTC)
    bundle_store = WorkbenchBundleStore(job_store)
    await bundle_store.save_bundle(job_dir_name, _bundle())
    artifact_store = WorkbenchArtifactStore(job_store, bundle_store)
    analysis_store = WorkbenchAnalysisStore(job_store, bundle_store, artifact_store)
    coverage_store = WorkbenchCoverageStore(
        job_store,
        bundle_store,
        artifact_store,
        analysis_store,
    )
    return job_store, artifact_store, analysis_store, coverage_store, job_dir_name


async def _complete_predecessors(
    artifact_store: WorkbenchArtifactStore,
    analysis_store: WorkbenchAnalysisStore,
    job_dir_name: str,
) -> None:
    await artifact_store.materialize(job_dir_name)
    await analysis_store.materialize(job_dir_name)


@pytest.mark.asyncio
async def test_materialize_and_verify_complete_coverage(tmp_path: Path) -> None:
    job_store, artifact_store, analysis_store, coverage_store, job_dir_name = await _stores(
        tmp_path,
    )
    await _complete_predecessors(artifact_store, analysis_store, job_dir_name)

    manifest = await coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()])
    job_dir = job_store.root / job_dir_name
    payload = json.loads((job_dir / "coverage.json").read_text(encoding="utf-8"))

    assert [item.relative_path for item in manifest.files] == ["coverage.json"]
    assert (job_dir / WORKBENCH_COVERAGE_MANIFEST_FILENAME).is_file()
    assert payload["passed"] is True
    assert payload["coverage_percentage"] == 100.0
    assert (await coverage_store.verify(job_dir_name)) == manifest
    assert (await coverage_store.load(job_dir_name)).items[0].status is CoverageStatus.SATISFIED


@pytest.mark.asyncio
async def test_missing_requirement_is_reported_without_rejecting_materialization(
    tmp_path: Path,
) -> None:
    job_store, artifact_store, analysis_store, coverage_store, job_dir_name = await _stores(
        tmp_path,
    )
    await _complete_predecessors(artifact_store, analysis_store, job_dir_name)

    await coverage_store.materialize(
        job_dir_name,
        "fictional-board",
        [_requirement(), _requirement("missing-rule", key="role.witch.can_poison")],
    )
    report = await coverage_store.load(job_dir_name)

    assert report.is_complete is False
    assert report.blocking_requirement_ids == ("missing-rule",)
    assert (job_store.root / job_dir_name / "coverage.json").is_file()


@pytest.mark.asyncio
async def test_tampered_coverage_is_rejected(tmp_path: Path) -> None:
    job_store, artifact_store, analysis_store, coverage_store, job_dir_name = await _stores(
        tmp_path,
    )
    await _complete_predecessors(artifact_store, analysis_store, job_dir_name)
    await coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()])
    report_path = job_store.root / job_dir_name / "coverage.json"
    report_path.write_bytes(report_path.read_bytes() + b"tampered")

    with pytest.raises(CorruptWorkbenchCoverageError, match="digest mismatch"):
        await coverage_store.verify(job_dir_name)


@pytest.mark.asyncio
async def test_missing_marker_allows_retry_of_coverage_outputs(tmp_path: Path) -> None:
    job_store, artifact_store, analysis_store, coverage_store, job_dir_name = await _stores(
        tmp_path,
    )
    await _complete_predecessors(artifact_store, analysis_store, job_dir_name)
    first = await coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()])
    (job_store.root / job_dir_name / WORKBENCH_COVERAGE_MANIFEST_FILENAME).unlink()

    with pytest.raises(CorruptWorkbenchCoverageError, match="required"):
        await coverage_store.verify(job_dir_name)
    retried = await coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()])

    assert retried == first
    assert await coverage_store.verify(job_dir_name) == retried


@pytest.mark.asyncio
async def test_concurrent_materialize_calls_allow_one_completed_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_store, artifact_store, analysis_store, coverage_store, job_dir_name = await _stores(
        tmp_path,
    )
    await _complete_predecessors(artifact_store, analysis_store, job_dir_name)
    import werewolf.ruleset_workbench.coverage_store as coverage_store_module

    original_write = coverage_store_module.atomic_write_bytes
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

    monkeypatch.setattr(coverage_store_module, "atomic_write_bytes", paused_first_write)
    first_task = asyncio.create_task(
        coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()]),
    )
    await first_write_started.wait()
    second_task = asyncio.create_task(
        coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()]),
    )
    await asyncio.sleep(0)
    assert not second_task.done()

    release_first_write.set()
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)
    assert sum(isinstance(result, CoverageAlreadyExistsError) for result in results) == 1
    assert sum(hasattr(result, "bundle_sha256") for result in results) == 1
    assert write_calls == 1
    assert await coverage_store.verify(job_dir_name)


@pytest.mark.asyncio
async def test_incomplete_predecessors_are_rejected(tmp_path: Path) -> None:
    _, _, _, coverage_store, job_dir_name = await _stores(tmp_path)

    with pytest.raises(CorruptWorkbenchArtifactsError, match="manifest is required"):
        await coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()])

    job_store, artifact_store, _, coverage_store, job_dir_name = await _stores(
        tmp_path / "analysis",
    )
    await artifact_store.materialize(job_dir_name)
    with pytest.raises(CorruptWorkbenchAnalysisError, match="manifest is required"):
        await coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()])
    assert not (job_store.root / job_dir_name / "coverage.json").exists()


@pytest.mark.asyncio
async def test_coverage_path_escape_is_rejected_before_writing(tmp_path: Path) -> None:
    job_store, artifact_store, analysis_store, coverage_store, job_dir_name = await _stores(
        tmp_path,
    )
    await _complete_predecessors(artifact_store, analysis_store, job_dir_name)
    outside = tmp_path / "outside-coverage.json"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        (job_store.root / job_dir_name / "coverage.json").symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(WorkbenchCoverageStoreError, match="escapes"):
        await coverage_store.materialize(job_dir_name, "fictional-board", [_requirement()])
    assert outside.read_text(encoding="utf-8") == "sentinel"
