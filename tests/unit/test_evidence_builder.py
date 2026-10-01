"""Unit tests for bounded conversion of fetched pages into source evidence."""

from datetime import UTC, datetime
from hashlib import sha256

import pytest

from werewolf.ruleset_workbench.evidence import EXCERPT_MAX_LENGTH, SourceClass
from werewolf.ruleset_workbench.evidence_builder import (
    EvidenceBoundsError,
    FocusMarkerNotFoundError,
    build_source_evidence,
)
from werewolf.ruleset_workbench.research_provider import FetchedDocument

FETCHED_AT = datetime(2026, 9, 28, 1, 2, 3, tzinfo=UTC)
FOCUS_MARKER = "App\u914d\u7f6e\u540d\u79f0\uff1a12\u4eba\u6807\u51c6\u573a"


def _document(body: str, **overrides: object) -> FetchedDocument:
    values: dict[str, object] = {
        "url": "https://rules.example.test/boards",
        "body": body,
        "title": "Published board rules",
        "published": "2026-09-01",
    }
    values.update(overrides)
    return FetchedDocument.model_validate(values)


def _build(document: FetchedDocument, **overrides: object):
    values: dict[str, object] = {
        "source_class": SourceClass.PLATFORM_RULES,
        "publisher": "Rules Platform",
        "retrieval_method": "agent-reach/https",
        "fetched_at": FETCHED_AT,
    }
    values.update(overrides)
    return build_source_evidence(document, **values)


def test_source_id_and_full_body_hash_are_deterministic() -> None:
    document = _document(
        "prefix\n" + FOCUS_MARKER + "\n\u72fc\u961f\u884c\u52a8\u987a\u5e8f\nsuffix"
    )

    first = _build(document, focus_marker=FOCUS_MARKER)
    second = _build(document, focus_marker=FOCUS_MARKER)

    assert first.source_id == second.source_id
    assert first.content_sha256 == sha256(document.body.encode("utf-8")).hexdigest()
    assert first.fetched_at == FETCHED_AT
    assert first.source_id.startswith("source-")
    assert len(first.source_id) <= 64


def test_focus_selects_a_bounded_contiguous_window() -> None:
    body = "opening\n" + ("before.\u3000" * 30) + FOCUS_MARKER + ("after.\u3000" * 30)
    document = _document(body)

    evidence = _build(
        document,
        focus_marker=FOCUS_MARKER,
        max_excerpt_length=80,
    )

    assert len(evidence.excerpt) <= 80
    assert FOCUS_MARKER in evidence.excerpt
    assert evidence.excerpt in body


def test_missing_focus_marker_is_rejected_without_using_other_page_text() -> None:
    document = _document(
        "App\u914d\u7f6e\u540d\u79f0\uff1a10\u4eba\u6807\u51c6\u573a\n\u53e6\u4e00\u5957\u89c4\u5219"
    )

    with pytest.raises(FocusMarkerNotFoundError):
        _build(document, focus_marker=FOCUS_MARKER)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_excerpt_length": 0}, "max_excerpt_length"),
        ({"max_excerpt_length": EXCERPT_MAX_LENGTH + 1}, "max_excerpt_length"),
        ({"max_excerpt_length": 8, "focus_marker": "marker is too long"}, "focus_marker"),
        ({"focus_marker": " marker"}, "focus_marker"),
        ({"retrieval_method": "x" * 65}, "retrieval_method"),
    ],
)
def test_length_and_boundary_inputs_are_rejected(kwargs: dict[str, object], message: str) -> None:
    document = _document("safe body marker")

    with pytest.raises(EvidenceBoundsError, match=message):
        _build(document, **kwargs)


def test_excerpt_does_not_expose_leading_or_trailing_page_padding() -> None:
    document = _document("\n\nA bounded rule excerpt\n\n")

    evidence = _build(document, max_excerpt_length=100)

    assert evidence.excerpt == "A bounded rule excerpt"
    assert evidence.excerpt in document.body


def test_non_utc_fetched_at_is_rejected() -> None:
    document = _document("a rule")

    with pytest.raises(EvidenceBoundsError, match="fetched_at"):
        _build(document, fetched_at=datetime(2026, 9, 28, 9))
