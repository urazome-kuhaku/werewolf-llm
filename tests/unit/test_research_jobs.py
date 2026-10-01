"""Boundary tests for the ruleset research job state machine."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from werewolf.ruleset_workbench import (
    InvalidResearchJobTransition,
    ResearchJob,
    ResearchJobStatus,
)

BASE_TIME = datetime(2026, 9, 27, 12, tzinfo=UTC)


def _job(**overrides: object) -> ResearchJob:
    values: dict[str, object] = {
        "job_id": uuid4(),
        "board_name": "classic-12",
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME,
    }
    values.update(overrides)
    return ResearchJob.model_validate(values)


def _at(minutes: int) -> datetime:
    return BASE_TIME + timedelta(minutes=minutes)


def test_new_job_starts_created_with_stable_schema_and_uuid4() -> None:
    job = _job()

    assert job.schema_version == 1
    assert job.job_id.version == 4
    assert job.status is ResearchJobStatus.CREATED
    assert job.failure_reason is None
    assert job.resume_status is None


def test_normal_workflow_allows_only_direct_edges() -> None:
    job = _job()
    sequence = [
        ResearchJobStatus.SEARCHING,
        ResearchJobStatus.EVIDENCE_COLLECTED,
        ResearchJobStatus.EXTRACTING,
        ResearchJobStatus.ANALYZING,
        ResearchJobStatus.DRAFTED,
        ResearchJobStatus.VALIDATING,
        ResearchJobStatus.NEEDS_DECISION,
        ResearchJobStatus.VALIDATING,
        ResearchJobStatus.READY_TO_PUBLISH,
        ResearchJobStatus.PUBLISHED,
    ]

    for minute, target in enumerate(sequence, start=1):
        job = job.transition_to(target, at=_at(minute))

    assert job.status is ResearchJobStatus.PUBLISHED
    assert job.updated_at == _at(len(sequence))


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ResearchJobStatus.CREATED, ResearchJobStatus.EVIDENCE_COLLECTED),
        (ResearchJobStatus.SEARCHING, ResearchJobStatus.EXTRACTING),
        (ResearchJobStatus.DRAFTED, ResearchJobStatus.PUBLISHED),
        (ResearchJobStatus.VALIDATING, ResearchJobStatus.PUBLISHED),
        (ResearchJobStatus.NEEDS_DECISION, ResearchJobStatus.READY_TO_PUBLISH),
    ],
)
def test_jump_transitions_are_rejected(
    current: ResearchJobStatus,
    target: ResearchJobStatus,
) -> None:
    job = _job(status=current, updated_at=_at(1))

    with pytest.raises(InvalidResearchJobTransition):
        job.transition_to(target, at=_at(2))


@pytest.mark.parametrize(
    ("current", "checkpoint"),
    [
        (ResearchJobStatus.SEARCHING, ResearchJobStatus.CREATED),
        (ResearchJobStatus.EXTRACTING, ResearchJobStatus.EVIDENCE_COLLECTED),
    ],
)
def test_failure_records_explicit_checkpoint_without_mutating_input(
    current: ResearchJobStatus,
    checkpoint: ResearchJobStatus,
) -> None:
    searching = _job(
        status=current,
        updated_at=_at(1),
    )

    failed = searching.fail(
        "  provider timed out  ",
        resume_from=checkpoint,
        at=_at(2),
    )

    assert searching.status is current
    assert failed.status is ResearchJobStatus.FAILED
    assert failed.failure_reason == "provider timed out"
    assert failed.resume_status is checkpoint
    assert failed.updated_at == _at(2)


def test_recovery_returns_to_recorded_state_and_clears_failure_metadata() -> None:
    failed = _job(status=ResearchJobStatus.ANALYZING, updated_at=_at(1)).fail(
        "temporary extractor failure",
        resume_from=ResearchJobStatus.ANALYZING,
        at=_at(2),
    )

    recovered = failed.recover(at=_at(3))

    assert recovered.status is ResearchJobStatus.ANALYZING
    assert recovered.failure_reason is None
    assert recovered.resume_status is None
    assert recovered.updated_at == _at(3)
    assert failed.status is ResearchJobStatus.FAILED


def test_failure_and_recovery_are_allowed_at_each_non_terminal_work_state() -> None:
    statuses = [
        ResearchJobStatus.CREATED,
        ResearchJobStatus.SEARCHING,
        ResearchJobStatus.EVIDENCE_COLLECTED,
        ResearchJobStatus.EXTRACTING,
        ResearchJobStatus.ANALYZING,
        ResearchJobStatus.DRAFTED,
        ResearchJobStatus.VALIDATING,
        ResearchJobStatus.NEEDS_DECISION,
        ResearchJobStatus.READY_TO_PUBLISH,
    ]

    for status in statuses:
        failed = _job(status=status, updated_at=_at(1)).fail(
            "recoverable",
            resume_from=status,
            at=_at(2),
        )
        assert failed.resume_status is status
        assert failed.recover(at=_at(3)).status is status


def test_published_and_failed_jobs_cannot_continue_or_fail_again() -> None:
    published = _job(status=ResearchJobStatus.PUBLISHED, updated_at=_at(1))
    with pytest.raises(InvalidResearchJobTransition):
        published.transition_to(ResearchJobStatus.PUBLISHED, at=_at(2))
    with pytest.raises(InvalidResearchJobTransition):
        published.fail(
            "too late",
            resume_from=ResearchJobStatus.READY_TO_PUBLISH,
            at=_at(2),
        )

    failed = _job().fail("temporary", resume_from=ResearchJobStatus.CREATED, at=_at(1))
    with pytest.raises(InvalidResearchJobTransition):
        failed.transition_to(ResearchJobStatus.SEARCHING, at=_at(2))
    with pytest.raises(InvalidResearchJobTransition):
        failed.fail("again", resume_from=ResearchJobStatus.CREATED, at=_at(2))


def test_recovery_without_resume_state_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _job(
            status=ResearchJobStatus.FAILED,
            failure_reason="missing checkpoint",
            resume_status=None,
        )


def test_failure_requires_an_explicit_checkpoint() -> None:
    with pytest.raises(TypeError):
        _job().fail("missing checkpoint", at=_at(1))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("current", "checkpoint"),
    [
        (ResearchJobStatus.SEARCHING, ResearchJobStatus.EVIDENCE_COLLECTED),
        (ResearchJobStatus.EXTRACTING, ResearchJobStatus.ANALYZING),
        (ResearchJobStatus.SEARCHING, ResearchJobStatus.NEEDS_DECISION),
        (ResearchJobStatus.CREATED, ResearchJobStatus.FAILED),
        (ResearchJobStatus.CREATED, ResearchJobStatus.PUBLISHED),
    ],
)
def test_failure_rejects_future_or_terminal_checkpoint(
    current: ResearchJobStatus,
    checkpoint: ResearchJobStatus,
) -> None:
    job = _job(status=current, updated_at=_at(1))

    with pytest.raises(InvalidResearchJobTransition):
        job.fail("invalid checkpoint", resume_from=checkpoint, at=_at(2))


@pytest.mark.parametrize(
    "timestamp",
    [
        BASE_TIME - timedelta(seconds=1),
        datetime(2026, 9, 27, 12),
        datetime(2026, 9, 27, 20, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_transition_rejects_time_that_is_backward_or_not_utc(timestamp: datetime) -> None:
    job = _job(updated_at=_at(1))

    with pytest.raises((InvalidResearchJobTransition, ValueError, TypeError)):
        job.transition_to(ResearchJobStatus.SEARCHING, at=timestamp)


def test_model_rejects_invalid_uuid_schema_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _job(job_id="not-a-uuid")
    with pytest.raises(ValidationError):
        _job(schema_version=2)
    with pytest.raises(ValidationError):
        _job(unexpected=True)


def test_model_rejects_inconsistent_failure_metadata_and_time_order() -> None:
    with pytest.raises(ValidationError):
        _job(failure_reason="reason")
    with pytest.raises(ValidationError):
        _job(
            status=ResearchJobStatus.FAILED,
            failure_reason="reason",
            resume_status=ResearchJobStatus.PUBLISHED,
        )
    with pytest.raises(ValidationError):
        _job(updated_at=BASE_TIME - timedelta(seconds=1))


def test_failure_reason_must_be_non_empty_and_recovery_alias_works() -> None:
    job = _job()
    with pytest.raises(ValueError):
        job.fail("  ", resume_from=ResearchJobStatus.CREATED, at=_at(1))

    failed = job.fail(
        "temporary",
        resume_from=ResearchJobStatus.CREATED,
        at=_at(1),
    )
    assert failed.recover_from_failure(at=_at(2)).status is ResearchJobStatus.CREATED
