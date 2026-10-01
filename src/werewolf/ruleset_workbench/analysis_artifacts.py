"""Pure, deterministic rendering of analysis results for the workbench.

``AnalysisReport`` is an in-memory result of the variant and conflict passes.
This module turns that result into the two JSON files described by section 9.4
of the design document.  It deliberately has no filesystem or policy side
effects: a caller must persist the returned bytes and resolve any marked
decision separately.

The report contains logical claim and evidence identifiers, context data, and
the values involved in each conflict.  It does not contain source excerpts or
web page bodies, so rendering it cannot accidentally publish those contents.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from .analysis import AnalysisReport

ANALYSIS_ARTIFACT_SCHEMA_VERSION: Literal[1] = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)


class AnalysisArtifactError(ValueError):
    """Raised when an analysis report cannot become a safe JSON artifact."""


@dataclass(frozen=True, slots=True)
class AnalysisArtifacts:
    """Immutable UTF-8 contents for ``variants.json`` and ``conflicts.json``."""

    variants: bytes
    conflicts: bytes

    @property
    def variants_json(self) -> bytes:
        """Return the bytes intended for ``variants.json``."""

        return self.variants

    @property
    def conflicts_json(self) -> bytes:
        """Return the bytes intended for ``conflicts.json``."""

        return self.conflicts

    @property
    def files(self) -> tuple[tuple[str, bytes], ...]:
        """Return deterministic workbench-relative file names and contents."""

        return (("variants.json", self.variants), ("conflicts.json", self.conflicts))


def _validate_bundle_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AnalysisArtifactError("bundle_sha256 must be 64 lowercase hexadecimal characters")
    return value


def _strict_json_bytes(payload: object) -> bytes:
    """Encode one artifact as compact, stable UTF-8 JSON with an LF ending."""

    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise AnalysisArtifactError("analysis artifact is not valid strict JSON") from exc
    return (text.replace("\r\n", "\n").replace("\r", "\n") + "\n").encode("utf-8")


def _report_payload(report: AnalysisReport) -> dict[str, Any]:
    """Take one detached JSON-compatible snapshot of a validated report."""

    try:
        payload = report.model_dump(mode="json", round_trip=True)
    except (TypeError, ValueError) as exc:
        raise AnalysisArtifactError("analysis report cannot be serialized") from exc
    if not isinstance(payload, dict):
        raise AnalysisArtifactError("analysis report must serialize to a JSON object")
    return payload


def _string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AnalysisArtifactError(f"analysis report field {field!r} must be a string array")
    return sorted(value)


def _context_sort_key(context: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the stable identity ordering used by ``analysis.py`` contexts."""

    values = (
        context.get("ruleset_candidate_id"),
        context.get("scope"),
        context.get("key"),
        context.get("canonical_conditions"),
    )
    if any(not isinstance(value, str) for value in values):
        raise AnalysisArtifactError("analysis context has invalid identity fields")
    return cast(tuple[str, str, str, str], values)


def _normalise_context(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisArtifactError("analysis context must be a JSON object")
    context = dict(value)
    return context


def _normalise_variant_context(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisArtifactError("variant context must be a JSON object")
    item = dict(value)
    item["context"] = _normalise_context(item.get("context"))
    item["claim_ids"] = _string_list(item.get("claim_ids"), field="claim_ids")
    item["active_claim_ids"] = _string_list(
        item.get("active_claim_ids"),
        field="active_claim_ids",
    )
    return item


def _normalise_variant(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisArtifactError("candidate variant must be a JSON object")
    variant = dict(value)
    candidate = variant.get("ruleset_candidate_id")
    if not isinstance(candidate, str):
        raise AnalysisArtifactError("candidate variant has an invalid candidate ID")
    variant["claim_ids"] = _string_list(variant.get("claim_ids"), field="claim_ids")
    variant["active_claim_ids"] = _string_list(
        variant.get("active_claim_ids"),
        field="active_claim_ids",
    )
    contexts = variant.get("contexts")
    if not isinstance(contexts, list):
        raise AnalysisArtifactError("candidate variant contexts must be an array")
    normalised_contexts = [_normalise_variant_context(item) for item in contexts]
    normalised_contexts.sort(key=lambda item: _context_sort_key(item["context"]))
    variant["contexts"] = normalised_contexts
    return variant


def _normalise_variants(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AnalysisArtifactError("analysis variants must be an array")
    variants = [_normalise_variant(item) for item in value]
    variants.sort(key=lambda item: cast(str, item["ruleset_candidate_id"]))
    return variants


def _normalise_conflict_value(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisArtifactError("conflict value must be a JSON object")
    item = dict(value)
    canonical_value = item.get("canonical_value")
    if not isinstance(canonical_value, str):
        raise AnalysisArtifactError("conflict value has an invalid canonical value")
    item["claim_ids"] = _string_list(item.get("claim_ids"), field="claim_ids")
    item["evidence_ids"] = _string_list(item.get("evidence_ids"), field="evidence_ids")
    return item


def _normalise_conflict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisArtifactError("rule conflict must be a JSON object")
    conflict = dict(value)
    conflict["context"] = _normalise_context(conflict.get("context"))
    values = conflict.get("values")
    if not isinstance(values, list):
        raise AnalysisArtifactError("rule conflict values must be an array")
    normalised_values = [_normalise_conflict_value(item) for item in values]
    normalised_values.sort(key=lambda item: cast(str, item["canonical_value"]))
    conflict["values"] = normalised_values
    conflict["claim_ids"] = _string_list(conflict.get("claim_ids"), field="claim_ids")
    conflict["evidence_ids"] = _string_list(
        conflict.get("evidence_ids"),
        field="evidence_ids",
    )
    return conflict


def _conflict_sort_key(conflict: dict[str, Any]) -> tuple[Any, ...]:
    context_key = _context_sort_key(conflict["context"])
    values = tuple(cast(str, item["canonical_value"]) for item in conflict["values"])
    return (*context_key, values)


def _normalise_conflicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AnalysisArtifactError("analysis conflicts must be an array")
    conflicts = [_normalise_conflict(item) for item in value]
    conflicts.sort(key=_conflict_sort_key)
    return conflicts


def _normalise_decision_contexts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AnalysisArtifactError("decision contexts must be an array")
    contexts = [_normalise_context(item) for item in value]
    contexts.sort(key=_context_sort_key)
    return contexts


def render_analysis_artifacts(
    report: AnalysisReport,
    bundle_sha256: str,
) -> AnalysisArtifacts:
    """Render one report into deterministic ``variants.json`` and ``conflicts.json``.

    ``bundle_sha256`` is supplied by the caller and copied into both files so
    the artifacts cannot be mistaken for the result of another evidence
    bundle.  The renderer preserves all report fields, including unresolved
    conflict markers and claim/evidence references; it never chooses a value
    or changes a decision state.
    """

    if not isinstance(report, AnalysisReport):
        raise TypeError("render_analysis_artifacts() expects an AnalysisReport")
    digest = _validate_bundle_sha256(bundle_sha256)
    report_data = _report_payload(report)

    variants = _normalise_variants(report_data.get("variants"))
    conflicts = _normalise_conflicts(report_data.get("conflicts"))
    unresolved_conflicts = _normalise_conflicts(report_data.get("unresolved_conflicts"))
    keys_needing_decision = _string_list(
        report_data.get("keys_needing_decision"),
        field="keys_needing_decision",
    )
    decision_contexts = _normalise_decision_contexts(report_data.get("decision_contexts"))

    variants_payload = {
        "schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "bundle_sha256": digest,
        "variants": variants,
    }
    conflicts_payload = {
        "schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "bundle_sha256": digest,
        "conflicts": conflicts,
        "unresolved_conflicts": unresolved_conflicts,
        "keys_needing_decision": keys_needing_decision,
        "decision_contexts": decision_contexts,
        "needs_human_decision": bool(unresolved_conflicts),
    }
    return AnalysisArtifacts(
        variants=_strict_json_bytes(variants_payload),
        conflicts=_strict_json_bytes(conflicts_payload),
    )


# Descriptive aliases keep call sites readable without adding another
# serialization path.
render_analysis_report = render_analysis_artifacts
render_variants_and_conflicts = render_analysis_artifacts


__all__ = [
    "ANALYSIS_ARTIFACT_SCHEMA_VERSION",
    "AnalysisArtifactError",
    "AnalysisArtifacts",
    "render_analysis_artifacts",
    "render_analysis_report",
    "render_variants_and_conflicts",
]
