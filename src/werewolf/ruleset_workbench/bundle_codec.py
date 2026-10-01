"""Deterministic JSON encoding for offline research bundles.

The workbench keeps a research bundle in memory as validated Pydantic models.
This module defines the bytes used when that bundle crosses an offline
boundary.  Encoding always reconstructs and validates the current model data;
that matters because a ``RuleClaim`` contains mutable lists and JSON values
even though the containing ``ResearchBundle`` is frozen.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .bundles import ResearchBundle

DEFAULT_MAX_BUNDLE_BYTES = 4 * 1024 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting repeated member names."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    """Reject the non-standard JSON constants accepted by ``json.loads``."""

    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _walk_decoded_json(value: object, *, path: str = "$") -> None:
    """Reject finite overflow and unsupported values in parsed JSON data."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _walk_decoded_json(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk_decoded_json(child, path=f"{path}[{index}]")


def _walk_json_value(value: object, *, path: str) -> None:
    """Validate a current in-memory ``JsonValue`` without coercion."""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk_json_value(child, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key at {path} must be a string")
            _walk_json_value(child, path=f"{path}.{key}")
        return
    raise ValueError(f"unsupported JSON value at {path}: {type(value).__name__}")


def _record_id(record: object, *, collection: str) -> str:
    """Return a validated logical ID from a dumped collection record."""

    if not isinstance(record, dict):
        raise ValueError(f"{collection} must contain JSON objects")
    value = record.get("source_id" if collection == "sources" else "claim_id")
    if not isinstance(value, str):
        raise ValueError(f"{collection} records must contain string logical IDs")
    return value


def _canonical_payload(bundle: ResearchBundle) -> dict[str, Any]:
    """Revalidate current data and prepare sorted JSON-compatible payload."""

    if not isinstance(bundle, ResearchBundle):
        raise TypeError("encode_bundle() expects a ResearchBundle")

    # ``RuleClaim`` is intentionally not frozen.  Inspect its dynamic JSON
    # fields before Pydantic's serializers can coerce an invalid value.
    for index, claim in enumerate(bundle.claims):
        _walk_json_value(claim.value, path=f"$.claims[{index}].value")
        _walk_json_value(claim.conditions, path=f"$.claims[{index}].conditions")

    try:
        current_data = bundle.model_dump(mode="python", round_trip=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("research bundle cannot be serialized") from exc

    # Validate the dump rather than the model instance.  ``model_validate``
    # may otherwise reuse an already-created instance and skip nested checks.
    validated = ResearchBundle.model_validate(current_data, strict=True)
    dumped = validated.model_dump(mode="json", round_trip=True)
    if not isinstance(dumped, dict):
        raise ValueError("research bundle JSON payload must be an object")

    sources = dumped.get("sources")
    claims = dumped.get("claims")
    if not isinstance(sources, list) or not isinstance(claims, list):
        raise ValueError("research bundle collections must be JSON arrays")
    dumped["sources"] = sorted(
        sources,
        key=lambda record: _record_id(record, collection="sources"),
    )
    dumped["claims"] = sorted(
        claims,
        key=lambda record: _record_id(record, collection="claims"),
    )
    return dumped


def _canonical_json(payload: dict[str, Any]) -> bytes:
    """Serialize a payload using the bundle's canonical JSON rules."""

    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return text.encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("research bundle cannot be encoded as canonical UTF-8 JSON") from exc


def encode_bundle(bundle: ResearchBundle) -> bytes:
    """Return canonical UTF-8 JSON bytes for the current validated bundle.

    Sources and claims are sorted by their logical IDs.  Object keys are
    sorted recursively, and compact JSON avoids platform-specific whitespace
    or newline conventions.  The resulting bytes contain no build metadata.
    """

    payload = _canonical_payload(bundle)
    encoded = _canonical_json(payload)
    # This second validation exercises Pydantic's JSON mode on the exact bytes
    # that will be hashed or persisted.
    ResearchBundle.model_validate_json(encoded)
    return encoded


def decode_bundle(
    data: bytes,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
) -> ResearchBundle:
    """Decode and strictly validate one canonical-or-equivalent JSON bundle."""

    if not isinstance(data, bytes):
        raise TypeError("decode_bundle() expects bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an integer")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if len(data) > max_bytes:
        raise ValueError(f"bundle JSON exceeds max_bytes ({max_bytes})")

    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("bundle JSON must be valid UTF-8") from exc

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("bundle JSON is invalid") from exc
    except ValueError:
        # Preserve duplicate-key and non-finite-number diagnostics.
        raise
    _walk_decoded_json(parsed)

    # Keep the final schema check in Pydantic JSON mode so date, URL, enum,
    # tuple, and strict-field behavior match the persisted representation.
    return ResearchBundle.model_validate_json(text)


def bundle_sha256(bundle: ResearchBundle) -> str:
    """Return the SHA-256 digest of a bundle's canonical JSON bytes."""

    return hashlib.sha256(encode_bundle(bundle)).hexdigest()


__all__ = [
    "DEFAULT_MAX_BUNDLE_BYTES",
    "bundle_sha256",
    "decode_bundle",
    "encode_bundle",
]
