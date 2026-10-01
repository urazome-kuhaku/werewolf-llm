"""Pure state machine models for ruleset research jobs.

The research workbench persists a job independently from the artifacts it
produces.  This module only validates the job record and returns a new record
for every transition; it deliberately does not perform storage, research, or
network I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import Annotated, Literal, Self

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

BOARD_NAME_MAX_LENGTH = 256
FAILURE_REASON_MAX_LENGTH = 2_000

BoardName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=BOARD_NAME_MAX_LENGTH,
        strip_whitespace=True,
        strict=True,
    ),
]
FailureReason = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=FAILURE_REASON_MAX_LENGTH,
        strip_whitespace=True,
        strict=True,
    ),
]


@unique
class ResearchJobStatus(StrEnum):
    """Persisted states in the ruleset research workflow."""

    CREATED = "CREATED"
    SEARCHING = "SEARCHING"
    EVIDENCE_COLLECTED = "EVIDENCE_COLLECTED"
    EXTRACTING = "EXTRACTING"
    ANALYZING = "ANALYZING"
    DRAFTED = "DRAFTED"
    VALIDATING = "VALIDATING"
    NEEDS_DECISION = "NEEDS_DECISION"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class InvalidResearchJobTransition(ValueError):
    """Raised when a job is asked to make an invalid state transition."""


_NORMAL_TRANSITIONS: dict[ResearchJobStatus, frozenset[ResearchJobStatus]] = {
    ResearchJobStatus.CREATED: frozenset({ResearchJobStatus.SEARCHING}),
    ResearchJobStatus.SEARCHING: frozenset({ResearchJobStatus.EVIDENCE_COLLECTED}),
    ResearchJobStatus.EVIDENCE_COLLECTED: frozenset({ResearchJobStatus.EXTRACTING}),
    ResearchJobStatus.EXTRACTING: frozenset({ResearchJobStatus.ANALYZING}),
    ResearchJobStatus.ANALYZING: frozenset({ResearchJobStatus.DRAFTED}),
    ResearchJobStatus.DRAFTED: frozenset({ResearchJobStatus.VALIDATING}),
    ResearchJobStatus.VALIDATING: frozenset(
        {
            ResearchJobStatus.NEEDS_DECISION,
            ResearchJobStatus.READY_TO_PUBLISH,
        },
    ),
    ResearchJobStatus.NEEDS_DECISION: frozenset({ResearchJobStatus.VALIDATING}),
    ResearchJobStatus.READY_TO_PUBLISH: frozenset({ResearchJobStatus.PUBLISHED}),
    ResearchJobStatus.PUBLISHED: frozenset(),
    ResearchJobStatus.FAILED: frozenset(),
}


def _require_utc_timestamp(value: datetime, *, field_name: str) -> datetime:
    """Require an aware timestamp represented in UTC (offset zero)."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be expressed in UTC")
    return value


def _require_transition_time(value: datetime, *, current: datetime) -> datetime:
    """Validate a transition timestamp and reject time moving backwards."""

    timestamp = _require_utc_timestamp(value, field_name="at")
    if timestamp < current:
        raise InvalidResearchJobTransition(
            "transition timestamp cannot be earlier than the job's updated_at",
        )
    return timestamp


def _require_status(value: ResearchJobStatus, *, field_name: str) -> ResearchJobStatus:
    """Keep method arguments strict even though Python does not enforce hints."""

    if not isinstance(value, ResearchJobStatus):
        raise TypeError(f"{field_name} must be a ResearchJobStatus")
    return value


def _require_failure_reason(value: str) -> str:
    """Validate and normalize a failure reason supplied to ``fail``."""

    if not isinstance(value, str):
        raise TypeError("reason must be a string")
    reason = value.strip()
    if not reason:
        raise ValueError("reason must not be empty")
    if len(reason) > FAILURE_REASON_MAX_LENGTH:
        raise ValueError(
            f"reason must be at most {FAILURE_REASON_MAX_LENGTH} characters",
        )
    return reason


def _checkpoint_can_reach(
    checkpoint: ResearchJobStatus,
    current: ResearchJobStatus,
) -> bool:
    """Return whether ``checkpoint`` is on a normal path to ``current``.

    The job model cannot verify that an artifact was actually persisted.  It
    can, however, reject checkpoints that are terminal, in the future, or on
    a state-machine path unrelated to the failed state.  The orchestrator is
    responsible for selecting a checkpoint only after it has verified the
    corresponding complete artifact.
    """

    if checkpoint is current:
        return True

    pending = [checkpoint]
    visited: set[ResearchJobStatus] = set()
    while pending:
        status = pending.pop()
        if status in visited:
            continue
        visited.add(status)
        pending.extend(_NORMAL_TRANSITIONS[status])
        if current in _NORMAL_TRANSITIONS[status]:
            return True
    return False


class ResearchJob(BaseModel):
    """Immutable, validated state for one ruleset research job.

    ``transition_to``, ``fail``, and ``recover`` are pure operations: each
    returns a new model and leaves the input instance unchanged.  All
    timestamps are supplied by the caller so persistence and orchestration
    layers can use their own transaction clock deterministically.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal[1] = 1
    job_id: UUID4
    board_name: BoardName
    created_at: datetime
    updated_at: datetime
    status: ResearchJobStatus = ResearchJobStatus.CREATED
    failure_reason: FailureReason | None = None
    resume_status: ResearchJobStatus | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_utc_timestamp(cls, value: datetime, info: ValidationInfo) -> datetime:
        """Reject timezone-free and non-UTC persisted timestamps."""

        return _require_utc_timestamp(
            value,
            field_name=info.field_name or "timestamp",
        )

    @model_validator(mode="after")
    def validate_temporal_and_failure_invariants(self) -> Self:
        """Validate relationships that cannot be checked field by field."""

        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")

        if self.status is ResearchJobStatus.FAILED:
            if self.failure_reason is None:
                raise ValueError("FAILED jobs must include failure_reason")
            if self.resume_status is None:
                raise ValueError("FAILED jobs must include resume_status")
            if self.resume_status in {
                ResearchJobStatus.FAILED,
                ResearchJobStatus.PUBLISHED,
            }:
                raise ValueError("FAILED jobs may resume only a non-terminal work state")
        elif self.failure_reason is not None or self.resume_status is not None:
            raise ValueError(
                "failure_reason and resume_status are allowed only when status is FAILED",
            )
        return self

    def transition_to(self, target: ResearchJobStatus, *, at: datetime) -> Self:
        """Advance to one directly reachable normal workflow state.

        Entering ``FAILED`` must go through :meth:`fail`, which records both
        the reason and the state from which recovery should continue.
        """

        target = _require_status(target, field_name="target")
        if self.status is ResearchJobStatus.PUBLISHED:
            raise InvalidResearchJobTransition("PUBLISHED is terminal and cannot transition")
        if self.status is ResearchJobStatus.FAILED:
            raise InvalidResearchJobTransition(
                "FAILED jobs must be recovered before another transition",
            )
        if target is ResearchJobStatus.FAILED:
            raise InvalidResearchJobTransition(
                "use fail(reason, resume_from=..., at=...) to enter FAILED",
            )
        if target not in _NORMAL_TRANSITIONS[self.status]:
            raise InvalidResearchJobTransition(
                f"cannot transition from {self.status} to {target}",
            )

        timestamp = _require_transition_time(at, current=self.updated_at)
        return self.model_copy(update={"status": target, "updated_at": timestamp})

    def transition(self, target: ResearchJobStatus, *, at: datetime) -> Self:
        """Alias for :meth:`transition_to` for callers using verb-only APIs."""

        return self.transition_to(target, at=at)

    def fail(
        self,
        reason: str,
        *,
        resume_from: ResearchJobStatus,
        at: datetime,
    ) -> Self:
        """Mark a non-terminal job failed with an explicit artifact checkpoint.

        ``resume_from`` is selected by the orchestration layer after it has
        confirmed that the corresponding complete artifact is durable.  This
        model validates only that the checkpoint is a reachable non-terminal
        state on the current workflow path.
        """

        if self.status is ResearchJobStatus.PUBLISHED:
            raise InvalidResearchJobTransition("PUBLISHED is terminal and cannot fail")
        if self.status is ResearchJobStatus.FAILED:
            raise InvalidResearchJobTransition("FAILED jobs must be recovered before failing again")

        resume_from = _require_status(resume_from, field_name="resume_from")
        if resume_from in {
            ResearchJobStatus.FAILED,
            ResearchJobStatus.PUBLISHED,
        }:
            raise InvalidResearchJobTransition(
                "a FAILED job may resume only from a non-terminal work state",
            )
        if not _checkpoint_can_reach(resume_from, self.status):
            raise InvalidResearchJobTransition(
                f"resume_from {resume_from} is not a checkpoint for {self.status}",
            )

        timestamp = _require_transition_time(at, current=self.updated_at)
        normalized_reason = _require_failure_reason(reason)
        return self.model_copy(
            update={
                "status": ResearchJobStatus.FAILED,
                "updated_at": timestamp,
                "failure_reason": normalized_reason,
                "resume_status": resume_from,
            },
        )

    def recover(self, *, at: datetime) -> Self:
        """Recover a failed job to the complete artifact's recorded state."""

        if self.status is not ResearchJobStatus.FAILED:
            raise InvalidResearchJobTransition("only FAILED jobs can be recovered")
        if self.resume_status is None:
            raise InvalidResearchJobTransition("FAILED job has no resume_status")

        timestamp = _require_transition_time(at, current=self.updated_at)
        target = self.resume_status
        return self.model_copy(
            update={
                "status": target,
                "updated_at": timestamp,
                "failure_reason": None,
                "resume_status": None,
            },
        )

    def recover_from_failure(self, *, at: datetime) -> Self:
        """Descriptive alias for :meth:`recover`."""

        return self.recover(at=at)


__all__ = [
    "InvalidResearchJobTransition",
    "ResearchJob",
    "ResearchJobStatus",
]
