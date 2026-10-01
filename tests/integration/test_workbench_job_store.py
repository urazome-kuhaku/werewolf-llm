"""Integration coverage for durable research workbench job directories."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from werewolf.ruleset_workbench import (
    CorruptJobError,
    InvalidResearchJobTransition,
    JobAlreadyExistsError,
    JobNotFoundError,
    JobRequest,
    ResearchJobStatus,
    WorkbenchJobStore,
)

AT_UTC = datetime(2026, 9, 27, 15, 15, 5, tzinfo=UTC)


@pytest.mark.asyncio
async def test_create_and_load_job_publishes_complete_directory(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "_workbench"
    store = WorkbenchJobStore(root)

    job, directory_name = await store.create_job("  classic-12  ", "zh-CN", AT_UTC)

    job_dir = root / directory_name
    assert directory_name == f"20260927T151505Z_{job.job_id}"
    assert job_dir.is_dir()
    assert sorted(path.name for path in job_dir.iterdir()) == ["job.json", "request.json"]
    request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
    assert request == {
        "board_name": "classic-12",
        "job_id": str(job.job_id),
        "locale": "zh-CN",
        "schema_version": 1,
    }
    assert await store.load_job(directory_name) == job
    loaded_request = await store.load_request(directory_name)
    assert isinstance(loaded_request, JobRequest)
    assert loaded_request.board_name == "classic-12"
    assert loaded_request.locale == "zh-CN"


@pytest.mark.asyncio
async def test_load_request_rejects_request_id_and_board_name_tampering(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault" / "_workbench"
    store = WorkbenchJobStore(root)
    job, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)
    request_path = root / directory_name / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))

    request["job_id"] = str(uuid4())
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(CorruptJobError):
        await store.load_request(directory_name)

    request["job_id"] = str(job.job_id)
    request["board_name"] = "other-board"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(CorruptJobError):
        await store.load_request(directory_name)


@pytest.mark.asyncio
async def test_directory_name_is_windows_safe_and_invalid_names_are_rejected(
    tmp_path: Path,
) -> None:
    store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")
    _, directory_name = await store.create_job("classic-12", "en-US", AT_UTC)

    assert directory_name.count("_") == 1
    assert "/" not in directory_name
    assert "\\" not in directory_name
    assert ".." not in directory_name

    with pytest.raises(ValueError):
        await store.load_job("../outside")
    with pytest.raises(ValueError):
        await store.load_job("20260927T151505Z_not-a-uuid")
    with pytest.raises(ValueError):
        await store.load_job(f"{directory_name}/nested")


@pytest.mark.asyncio
async def test_load_rejects_missing_and_corrupt_or_inconsistent_metadata(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault" / "_workbench"
    store = WorkbenchJobStore(root)
    job, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)
    job_dir = root / directory_name

    (job_dir / "job.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(CorruptJobError):
        await store.load_job(directory_name)

    (job_dir / "job.json").write_text(
        json.dumps(job.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
    request["board_name"] = "other-board"
    (job_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(CorruptJobError):
        await store.load_job(directory_name)

    (job_dir / "request.json").unlink()
    with pytest.raises(CorruptJobError):
        await store.load_job(directory_name)

    with pytest.raises(JobNotFoundError):
        await store.load_job(f"20260927T151505Z_{uuid4()}")


@pytest.mark.asyncio
async def test_load_rejects_oversized_and_invalid_utf8_metadata(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "_workbench"
    store = WorkbenchJobStore(root)
    _, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)
    job_path = root / directory_name / "job.json"

    oversized_json = b'{"padding":"' + b"x" * (64 * 1024) + b'"}'
    job_path.write_bytes(oversized_json)
    with pytest.raises(CorruptJobError):
        await store.load_job(directory_name)

    job_path.write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(CorruptJobError):
        await store.load_job(directory_name)


@pytest.mark.asyncio
async def test_load_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    root = tmp_path / "vault" / "_workbench"
    store = WorkbenchJobStore(root)
    job, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)
    job_path = root / directory_name / "job.json"

    job_payload = json.dumps(job.model_dump(mode="json"), sort_keys=True)
    duplicate_key_json = job_payload.replace(
        '"schema_version": 1',
        '"schema_version": 1, "schema_version": 1',
        1,
    )
    job_path.write_text(duplicate_key_json, encoding="utf-8")

    with pytest.raises(CorruptJobError):
        await store.load_job(directory_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("non_finite", ["NaN", "Infinity", "-Infinity"])
async def test_load_rejects_non_finite_json_numbers(
    tmp_path: Path,
    non_finite: str,
) -> None:
    root = tmp_path / "vault" / "_workbench"
    store = WorkbenchJobStore(root)
    job, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)
    job_path = root / directory_name / "job.json"

    job_payload = job.model_dump(mode="json")
    finite_timestamp = json.dumps(job_payload["updated_at"])
    job_json = json.dumps(job_payload, sort_keys=True).replace(
        finite_timestamp,
        non_finite,
        1,
    )
    job_path.write_text(job_json, encoding="utf-8")

    with pytest.raises(CorruptJobError):
        await store.load_job(directory_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["", "   ", "en_US"])
async def test_create_rejects_locales_outside_bundle_locale_format(
    tmp_path: Path,
    locale: str,
) -> None:
    store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")

    with pytest.raises(ValidationError):
        await store.create_job("classic-12", locale, AT_UTC)


@pytest.mark.asyncio
async def test_create_does_not_overwrite_existing_job_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import werewolf.ruleset_workbench.store as store_module

    fixed_id = uuid4()
    staging_ids = [uuid4(), uuid4()]
    generated_ids = iter([fixed_id, staging_ids[0], fixed_id, staging_ids[1]])
    monkeypatch.setattr(store_module, "uuid4", lambda: next(generated_ids))
    store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")

    job, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)
    original_job_json = (store.root / directory_name / "job.json").read_text(encoding="utf-8")

    with pytest.raises(JobAlreadyExistsError):
        await store.create_job("other-board", "en-US", AT_UTC)

    assert (store.root / directory_name / "job.json").read_text(
        encoding="utf-8",
    ) == original_job_json
    assert job.board_name == "classic-12"


@pytest.mark.asyncio
async def test_transition_methods_reload_and_atomically_persist_job_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault" / "_workbench"
    store = WorkbenchJobStore(root)
    _, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)
    request_path = root / directory_name / "request.json"
    original_request = request_path.read_bytes()

    searching = await store.advance_job(
        directory_name,
        ResearchJobStatus.SEARCHING,
        AT_UTC + timedelta(minutes=1),
    )
    assert searching.status is ResearchJobStatus.SEARCHING
    failed = await store.fail_job(
        directory_name,
        "provider unavailable",
        ResearchJobStatus.CREATED,
        AT_UTC + timedelta(minutes=2),
    )
    assert failed.status is ResearchJobStatus.FAILED
    assert failed.failure_reason == "provider unavailable"
    assert failed.resume_status is ResearchJobStatus.CREATED

    recovered = await store.recover_job(
        directory_name,
        AT_UTC + timedelta(minutes=3),
    )
    assert recovered.status is ResearchJobStatus.CREATED
    assert recovered.failure_reason is None
    assert recovered.resume_status is None
    assert await store.load_job(directory_name) == recovered
    assert request_path.read_bytes() == original_request


@pytest.mark.asyncio
async def test_transition_methods_reject_invalid_time_and_terminal_rewrites(
    tmp_path: Path,
) -> None:
    store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")
    _, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)

    with pytest.raises(InvalidResearchJobTransition):
        await store.advance_job(
            directory_name,
            ResearchJobStatus.SEARCHING,
            AT_UTC - timedelta(seconds=1),
        )
    with pytest.raises(InvalidResearchJobTransition):
        await store.advance_job(
            directory_name,
            ResearchJobStatus.EVIDENCE_COLLECTED,
            AT_UTC + timedelta(minutes=1),
        )

    transition_time = AT_UTC
    for target in (
        ResearchJobStatus.SEARCHING,
        ResearchJobStatus.EVIDENCE_COLLECTED,
        ResearchJobStatus.EXTRACTING,
        ResearchJobStatus.ANALYZING,
        ResearchJobStatus.DRAFTED,
        ResearchJobStatus.VALIDATING,
        ResearchJobStatus.READY_TO_PUBLISH,
        ResearchJobStatus.PUBLISHED,
    ):
        transition_time += timedelta(minutes=1)
        await store.advance_job(directory_name, target, transition_time)

    with pytest.raises(InvalidResearchJobTransition):
        await store.advance_job(
            directory_name,
            ResearchJobStatus.PUBLISHED,
            transition_time + timedelta(minutes=1),
        )
    with pytest.raises(InvalidResearchJobTransition):
        await store.fail_job(
            directory_name,
            "cannot rewrite published job",
            ResearchJobStatus.READY_TO_PUBLISH,
            transition_time + timedelta(minutes=1),
        )


@pytest.mark.asyncio
async def test_concurrent_transitions_reload_under_one_job_lock(
    tmp_path: Path,
) -> None:
    store = WorkbenchJobStore(tmp_path / "vault" / "_workbench")
    _, directory_name = await store.create_job("classic-12", "zh-CN", AT_UTC)

    results = await asyncio.gather(
        store.advance_job(
            directory_name,
            ResearchJobStatus.SEARCHING,
            AT_UTC + timedelta(minutes=1),
        ),
        store.advance_job(
            directory_name,
            ResearchJobStatus.SEARCHING,
            AT_UTC + timedelta(minutes=1),
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], InvalidResearchJobTransition)
    assert (await store.load_job(directory_name)).status is ResearchJobStatus.SEARCHING
