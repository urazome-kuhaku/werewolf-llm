"""Pure rendering of coverage reports into the workbench ``coverage.json``.

``CoverageReport`` is an in-memory analysis result.  This module keeps the
serialization boundary deliberately small: it accepts an already validated
report and an explicit research-bundle digest, then returns immutable UTF-8
bytes for a caller to persist.  It never reads sources, fetches URLs, or
copies source excerpts into the artifact.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from .coverage import CoverageItem, CoverageReport, CoverageStatus

COVERAGE_ARTIFACT_SCHEMA_VERSION: Literal[1] = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_LOGICAL_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$", re.ASCII)


class CoverageArtifactError(ValueError):
    """Raised when a coverage report cannot become a safe artifact."""


@dataclass(frozen=True, slots=True)
class CoverageArtifacts:
    """Immutable contents for the single ``coverage.json`` workbench file."""

    coverage: bytes

    @property
    def coverage_json(self) -> bytes:
        """Return the bytes intended for ``coverage.json``."""

        return self.coverage

    @property
    def files(self) -> tuple[tuple[str, bytes], ...]:
        """Return the deterministic workbench-relative file and its bytes."""

        return (("coverage.json", self.coverage),)


def _validate_bundle_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CoverageArtifactError(
            "bundle_sha256 must be 64 lowercase hexadecimal characters",
        )
    return value


def _strict_json_bytes(payload: object) -> bytes:
    """Encode JSON as compact, deterministic UTF-8 with one LF ending."""

    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, UnicodeEncodeError, ValueError, OverflowError) as exc:
        raise CoverageArtifactError("coverage artifact is not valid strict JSON") from exc
    return (text.replace("\r\n", "\n").replace("\r", "\n") + "\n").encode("utf-8")


def _report_snapshot(report: CoverageReport) -> CoverageReport:
    """Detach and strictly revalidate a report before checking its semantics."""

    if not isinstance(report, CoverageReport):
        raise TypeError("render_coverage_artifacts() expects a CoverageReport")
    try:
        dumped = report.model_dump(mode="json", round_trip=True)
        encoded = _strict_json_bytes(dumped)
        snapshot = CoverageReport.model_validate_json(encoded, strict=True)
    except (TypeError, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise CoverageArtifactError("coverage report has an invalid structure") from exc
    if not isinstance(dumped, dict):
        raise CoverageArtifactError("coverage report must serialize to a JSON object")
    return snapshot


def _item_identity(item: CoverageItem) -> str:
    requirement_id = item.requirement.requirement_id
    if _LOGICAL_ID_RE.fullmatch(requirement_id) is None:
        raise CoverageArtifactError("coverage requirement has an invalid requirement ID")
    return requirement_id


def _canonical_model_dump(value: object) -> str:
    """Return a strict JSON identity for comparing detached model values."""

    try:
        dumped = cast(Any, value).model_dump(mode="json", round_trip=True)
        return json.dumps(
            dumped,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError, OverflowError) as exc:
        raise CoverageArtifactError("coverage report contains non-JSON values") from exc


def _validate_report_semantics(report: CoverageReport) -> tuple[CoverageItem, ...]:
    """Reject inconsistent counts or gate fields instead of repairing them."""

    candidate_id = report.ruleset_candidate_id
    if _LOGICAL_ID_RE.fullmatch(candidate_id) is None:
        raise CoverageArtifactError("coverage report has an invalid candidate ID")
    if not report.items or not any(item.requirement.required for item in report.items):
        raise CoverageArtifactError("coverage report must contain a required requirement")

    items = tuple(sorted(report.items, key=_item_identity))
    requirement_ids = tuple(item.requirement_id for item in items)
    if len(set(requirement_ids)) != len(requirement_ids):
        raise CoverageArtifactError("coverage report contains duplicate requirement IDs")

    for item in items:
        if not item.reason.strip():
            raise CoverageArtifactError("coverage item reason must not be empty")
        if len(set(item.matched_claim_ids)) != len(item.matched_claim_ids):
            raise CoverageArtifactError("coverage item contains duplicate matched claim IDs")
        if len(set(item.active_claim_ids)) != len(item.active_claim_ids):
            raise CoverageArtifactError("coverage item contains duplicate active claim IDs")
        if not set(item.active_claim_ids).issubset(item.matched_claim_ids):
            raise CoverageArtifactError("active claim IDs must be a subset of matched claim IDs")
        if len(set(item.evidence_ids)) != len(item.evidence_ids):
            raise CoverageArtifactError("coverage item contains duplicate evidence IDs")
        if item.independent_evidence_count > len(item.evidence_ids):
            raise CoverageArtifactError(
                "independent evidence count cannot exceed evidence ID count",
            )
        if item.status is CoverageStatus.MISSING and item.independent_evidence_count != 0:
            raise CoverageArtifactError("missing coverage items cannot have evidence")
        if item.status is CoverageStatus.SATISFIED and (
            item.independent_evidence_count < item.requirement.min_independent_evidence_count
        ):
            raise CoverageArtifactError(
                "satisfied coverage item does not meet its evidence threshold",
            )

    required_items = tuple(item for item in items if item.requirement.required)
    expected_required_count = len(required_items)
    expected_satisfied_count = sum(
        item.status is CoverageStatus.SATISFIED for item in required_items
    )
    expected_percentage = (expected_satisfied_count / expected_required_count) * 100.0
    if report.required_count != expected_required_count:
        raise CoverageArtifactError("coverage report required_count is inconsistent")
    if report.satisfied_count != expected_satisfied_count:
        raise CoverageArtifactError("coverage report satisfied_count is inconsistent")
    if not math.isfinite(report.coverage_percentage):
        raise CoverageArtifactError("coverage percentage must be finite")
    if report.coverage_percentage != expected_percentage:
        raise CoverageArtifactError("coverage report percentage is inconsistent")

    expected_blocking = tuple(
        item for item in required_items if item.status is not CoverageStatus.SATISFIED
    )
    expected_blocking_ids = tuple(item.requirement_id for item in expected_blocking)
    if set(report.blocking_requirement_ids) != set(expected_blocking_ids) or len(
        report.blocking_requirement_ids
    ) != len(expected_blocking_ids):
        raise CoverageArtifactError("coverage report blocking IDs are inconsistent")
    if len(report.blocking_items) != len(expected_blocking):
        raise CoverageArtifactError("coverage report blocking items are inconsistent")
    by_id = {item.requirement_id: item for item in items}
    for blocking_item in report.blocking_items:
        blocking_id = _item_identity(blocking_item)
        if blocking_id not in by_id or _canonical_model_dump(
            blocking_item
        ) != _canonical_model_dump(by_id[blocking_id]):
            raise CoverageArtifactError("coverage report blocking item does not match its item")
        if not blocking_item.blocking:
            raise CoverageArtifactError("coverage report contains a non-blocking blocking item")
    return items


def _item_payload(item: CoverageItem) -> dict[str, Any]:
    """Expose rule dimensions and trace counts without source material."""

    requirement = item.requirement.model_dump(mode="json", round_trip=True)
    return {
        "requirement": requirement,
        "requirement_id": item.requirement_id,
        "status": item.status.value,
        "required": item.requirement.required,
        "matched_claim_ids": sorted(item.matched_claim_ids),
        "active_claim_ids": sorted(item.active_claim_ids),
        "evidence_ids": sorted(item.evidence_ids),
        "evidence_count": item.independent_evidence_count,
        "independent_evidence_count": item.independent_evidence_count,
        "blocking": item.blocking,
        "reason": item.reason,
        "blocking_reason": item.reason if item.blocking else None,
    }


def render_coverage_artifacts(
    report: CoverageReport,
    bundle_sha256: str,
) -> CoverageArtifacts:
    """Render a validated report into deterministic ``coverage.json`` bytes.

    The digest is always supplied by the caller and copied into the artifact.
    Report inconsistencies are errors; this function never upgrades an
    unsatisfied item or recomputes a passing gate from incomplete data.
    """

    digest = _validate_bundle_sha256(bundle_sha256)
    snapshot = _report_snapshot(report)
    items = _validate_report_semantics(snapshot)
    required_count = snapshot.required_count
    satisfied_count = snapshot.satisfied_count
    ratio = satisfied_count / required_count
    item_payloads = [_item_payload(item) for item in items]
    blocking_items = [item for item in items if item.blocking]
    payload = {
        "schema_version": COVERAGE_ARTIFACT_SCHEMA_VERSION,
        "bundle_sha256": digest,
        "ruleset_candidate_id": snapshot.ruleset_candidate_id,
        "items": item_payloads,
        "required_count": required_count,
        "satisfied_count": satisfied_count,
        "coverage_percentage": snapshot.coverage_percentage,
        "required_ratio": ratio,
        "required": {
            "total": required_count,
            "satisfied": satisfied_count,
            "ratio": ratio,
            "percentage": snapshot.coverage_percentage,
        },
        "blocking_requirement_ids": sorted(snapshot.blocking_requirement_ids),
        "blocking_items": [_item_payload(item) for item in blocking_items],
        "passed": not blocking_items,
    }
    return CoverageArtifacts(coverage=_strict_json_bytes(payload))


# Descriptive aliases keep callers independent from the artifact container's
# singular file name while preserving one serialization path.
render_coverage_report = render_coverage_artifacts
render_coverage_json = render_coverage_artifacts


__all__ = [
    "COVERAGE_ARTIFACT_SCHEMA_VERSION",
    "CoverageArtifactError",
    "CoverageArtifacts",
    "render_coverage_artifacts",
    "render_coverage_json",
    "render_coverage_report",
]
