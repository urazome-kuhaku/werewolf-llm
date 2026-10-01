"""Tests for the persisted DRAFTED-to-validation workbench boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_draft_generation import (
    _bundle,
    _context,
    _requirements,
    _templates,
)

from werewolf.ruleset_workbench import (
    ClaimStatus,
    DraftValidationService,
    ResearchBundle,
    ResearchJob,
    ResearchJobStatus,
    WorkbenchBundleStore,
    WorkbenchJobStore,
)


async def _drafted_job(
    tmp_path: Path,
    *,
    bundle: ResearchBundle,
) -> tuple[WorkbenchJobStore, str]:
    store = WorkbenchJobStore(tmp_path / "workbench")
    timestamp = datetime(2026, 9, 28, tzinfo=UTC)
    _, job_dir_name = await store.create_job("虚构测试板", "zh-CN", timestamp)
    current = timestamp
    for status in (
        ResearchJobStatus.SEARCHING,
        ResearchJobStatus.EVIDENCE_COLLECTED,
        ResearchJobStatus.EXTRACTING,
        ResearchJobStatus.ANALYZING,
        ResearchJobStatus.DRAFTED,
    ):
        current += timedelta(seconds=1)
        await store.advance_job(job_dir_name, status, current)
    await WorkbenchBundleStore(store).save_bundle(job_dir_name, bundle)
    return store, job_dir_name


@pytest.mark.asyncio
async def test_validation_materializes_package_and_stops_before_publish(tmp_path: Path) -> None:
    store, job_dir_name = await _drafted_job(tmp_path, bundle=_bundle())
    result = await DraftValidationService(store).validate(
        job_dir_name,
        context=_context(),
        templates=_templates(),
        requirements=_requirements(),
    )

    assert result.status is ResearchJobStatus.READY_TO_PUBLISH
    assert result.ready_to_publish is True
    assert result.package is not None
    assert result.package.publish_ready is False
    assert (store.root / job_dir_name / "coverage.json").is_file()
    assert (store.root / job_dir_name / "publish-manifest.json").is_file()
    assert (store.root / job_dir_name / "draft").is_dir()


@pytest.mark.asyncio
async def test_blocked_claim_enters_needs_decision_without_publishing(tmp_path: Path) -> None:
    store, job_dir_name = await _drafted_job(
        tmp_path,
        bundle=_bundle(role_status=ClaimStatus.UNVERIFIED),
    )
    result = await DraftValidationService(store).validate(
        job_dir_name,
        context=_context(),
        templates=_templates(),
        requirements=_requirements(),
    )

    assert result.status is ResearchJobStatus.NEEDS_DECISION
    assert result.package is None
    assert result.error is not None
    assert result.coverage is not None
    coverage_path = store.root / job_dir_name / "coverage.json"
    assert coverage_path.is_file()
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    assert coverage["passed"] is False
    assert not (store.root / job_dir_name / "publish-manifest.json").exists()


@pytest.mark.asyncio
async def test_validation_retry_after_commit_failure_reuses_identical_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, job_dir_name = await _drafted_job(tmp_path, bundle=_bundle())
    service = DraftValidationService(store)
    original_advance = store.advance_job
    failed_ready_transition = False

    async def fail_ready_once(
        job_name: str,
        target: ResearchJobStatus,
        at: datetime,
    ) -> ResearchJob:
        nonlocal failed_ready_transition
        if target is ResearchJobStatus.READY_TO_PUBLISH and not failed_ready_transition:
            failed_ready_transition = True
            raise OSError("simulated state commit failure")
        return await original_advance(job_name, target, at)

    monkeypatch.setattr(store, "advance_job", fail_ready_once)
    first = await service.validate(
        job_dir_name,
        context=_context(),
        templates=_templates(),
        requirements=_requirements(),
    )

    assert first.status is ResearchJobStatus.FAILED
    assert first.job.resume_status is ResearchJobStatus.VALIDATING
    assert (store.root / job_dir_name / "publish-manifest.json").is_file()

    failed = await store.load_job(job_dir_name)
    recovered = await store.recover_job(
        job_dir_name,
        failed.updated_at + timedelta(seconds=1),
    )
    assert recovered.status is ResearchJobStatus.VALIDATING
    second = await service.validate(
        job_dir_name,
        context=_context(),
        templates=_templates(),
        requirements=_requirements(),
    )

    assert second.status is ResearchJobStatus.READY_TO_PUBLISH
    assert second.package is not None
