"""Coverage matrix evaluation tests for the ruleset workbench."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from werewolf.ruleset_workbench.bundles import ResearchBundle
from werewolf.ruleset_workbench.claims import ClaimScope, ClaimStatus
from werewolf.ruleset_workbench.coverage import (
    CoverageRequirement,
    CoverageStatus,
    analyze_coverage,
)
from werewolf.ruleset_workbench.evidence import SourceClass


def _source(source_id: str, content_sha256: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"Source {source_id}",
        "publisher": "Fixture Publisher",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": content_sha256,
        "excerpt": "A fictional source excerpt for coverage tests.",
        "retrieval_method": "fixture",
    }


def _claim(
    claim_id: str,
    *,
    key: str = "board.player_count",
    value: object = 12,
    conditions: dict[str, object] | None = None,
    evidence_ids: list[str] | None = None,
    status: ClaimStatus = ClaimStatus.SUPPORTED,
    candidate: str = "fictional-board",
    scope: ClaimScope = ClaimScope.BOARD,
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": candidate,
        "key": key,
        "value": value,
        "scope": scope,
        "conditions": conditions if conditions is not None else {},
        "evidence_ids": evidence_ids or ["source-a"],
        "confidence": 0.9,
        "extraction_note": "Fixture claim.",
        "status": status,
    }


def _bundle(
    claims: list[dict[str, object]],
    sources: dict[str, str] | None = None,
) -> ResearchBundle:
    source_hashes = sources or {
        evidence_id: ("a" if evidence_id == "source-a" else evidence_id[-1]) * 64
        for claim in claims
        for evidence_id in claim["evidence_ids"]
    }
    return ResearchBundle.model_validate(
        {
            "board_name": "Fictional Board",
            "locale": "zh-CN",
            "sources": [
                _source(source_id, content_sha256)
                for source_id, content_sha256 in source_hashes.items()
            ],
            "claims": claims,
        },
    )


def _requirement(
    requirement_id: str = "player-count",
    *,
    key: str = "board.player_count",
    conditions: dict[str, object] | None = None,
    required: bool = True,
    minimum: int | None = None,
) -> CoverageRequirement:
    kwargs: dict[str, object] = {
        "requirement_id": requirement_id,
        "scope": ClaimScope.BOARD,
        "key": key,
        "conditions": conditions if conditions is not None else {},
        "required": required,
    }
    if minimum is not None:
        kwargs["min_independent_evidence_count"] = minimum
    return CoverageRequirement.model_validate(
        {
            **kwargs,
        },
    )


def test_missing_and_required_gate_are_reported() -> None:
    report = analyze_coverage(
        _bundle([_claim("claim-a")]),
        "fictional-board",
        [
            _requirement(minimum=1),
            _requirement("win-condition", key="board.win_condition", minimum=1),
        ],
    )

    assert [item.status for item in report.items] == [
        CoverageStatus.SATISFIED,
        CoverageStatus.MISSING,
    ]
    assert report.required_count == 2
    assert report.satisfied_count == 1
    assert report.coverage_percentage == 50.0
    assert report.blocking_requirement_ids == ("win-condition",)
    assert report.is_complete is False


def test_one_source_is_pending_and_two_independent_sources_satisfy() -> None:
    claim = _claim("claim-a", evidence_ids=["source-a"])
    bundle = _bundle(
        [claim],
        {"source-a": "a" * 64, "source-b": "b" * 64},
    )
    single = analyze_coverage(bundle, "fictional-board", [_requirement()])
    assert single.items[0].status is CoverageStatus.PENDING
    assert single.items[0].independent_evidence_count == 1

    second_claim = _claim("claim-b", evidence_ids=["source-b"])
    complete = analyze_coverage(
        _bundle([claim, second_claim], {"source-a": "a" * 64, "source-b": "b" * 64}),
        "fictional-board",
        [_requirement()],
    )
    assert complete.items[0].status is CoverageStatus.SATISFIED
    assert complete.items[0].independent_evidence_count == 2


def test_mirrored_sources_do_not_count_as_independent_evidence() -> None:
    bundle = _bundle(
        [_claim("claim-a", evidence_ids=["source-a", "source-b"])],
        {"source-a": "a" * 64, "source-b": "a" * 64},
    )

    item = analyze_coverage(bundle, "fictional-board", [_requirement()]).items[0]

    assert item.status is CoverageStatus.PENDING
    assert item.independent_evidence_count == 1
    assert item.evidence_ids == ("source-a", "source-b")


def test_unverified_and_rejected_claims_cannot_satisfy() -> None:
    pending = analyze_coverage(
        _bundle([_claim("claim-u", status=ClaimStatus.UNVERIFIED)]),
        "fictional-board",
        [_requirement()],
    )
    assert pending.items[0].status is CoverageStatus.PENDING

    rejected = analyze_coverage(
        _bundle([_claim("claim-r", status=ClaimStatus.REJECTED)]),
        "fictional-board",
        [_requirement()],
    )
    assert rejected.items[0].status is CoverageStatus.MISSING
    assert rejected.items[0].matched_claim_ids == ("claim-r",)


def test_explicit_and_implicit_conflicts_block_required_item() -> None:
    bundle = _bundle(
        [
            _claim("claim-a", value=False, evidence_ids=["source-a"]),
            _claim("claim-b", value=True, evidence_ids=["source-b"]),
        ],
        {"source-a": "a" * 64, "source-b": "b" * 64},
    )
    report = analyze_coverage(bundle, "fictional-board", [_requirement()])
    assert report.items[0].status is CoverageStatus.CONFLICTING
    assert report.blocking_requirement_ids == ("player-count",)

    explicit = analyze_coverage(
        _bundle(
            [_claim("claim-c", status=ClaimStatus.CONFLICTING)],
        ),
        "fictional-board",
        [_requirement()],
    )
    assert explicit.items[0].status is CoverageStatus.CONFLICTING


def test_conditions_are_exactly_isolated() -> None:
    bundle = _bundle(
        [
            _claim("claim-night-one", conditions={"night": 1}),
            _claim("claim-night-two", conditions={"night": 2}),
        ],
    )
    report = analyze_coverage(
        bundle,
        "fictional-board",
        [
            _requirement("night-one", conditions={"night": 1}, minimum=1),
            _requirement("night-three", conditions={"night": 3}, minimum=1),
        ],
    )
    assert [item.status for item in report.items] == [
        CoverageStatus.SATISFIED,
        CoverageStatus.MISSING,
    ]


def test_report_is_stable_when_requirements_and_claims_are_reordered() -> None:
    claims = [
        _claim("claim-z", key="role.witch.can_self_heal", scope=ClaimScope.ROLE),
        _claim("claim-a", key="board.player_count"),
    ]
    requirements = [
        _requirement(
            "witch-self-heal",
            key="role.witch.can_self_heal",
            required=False,
            minimum=1,
        ),
        _requirement("player-count", minimum=1),
    ]
    first = analyze_coverage(_bundle(claims), "fictional-board", requirements)
    second = analyze_coverage(
        _bundle(list(reversed(claims))),
        "fictional-board",
        list(reversed(requirements)),
    )

    assert first.model_dump() == second.model_dump()
    assert [item.requirement_id for item in first.items] == [
        "player-count",
        "witch-self-heal",
    ]


def test_duplicate_requirement_ids_and_invalid_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate requirement_id"):
        analyze_coverage(
            _bundle([_claim("claim-a")]),
            "fictional-board",
            [_requirement(), _requirement()],
        )

    with pytest.raises(ValidationError, match="key"):
        CoverageRequirement(
            requirement_id="bad-key",
            scope=ClaimScope.BOARD,
            key="player_count",
        )


def test_empty_and_optional_only_matrices_are_rejected() -> None:
    bundle = _bundle([_claim("claim-a")])

    with pytest.raises(ValueError, match="at least one required requirement"):
        analyze_coverage(bundle, "fictional-board", [])

    with pytest.raises(ValueError, match="optional-only"):
        analyze_coverage(
            bundle,
            "fictional-board",
            [_requirement("optional", key="board.missing", required=False)],
        )


def test_explicit_single_source_threshold_is_supported_but_is_not_default() -> None:
    bundle = _bundle([_claim("claim-a")])
    requirement = _requirement(minimum=1)

    report = analyze_coverage(bundle, "fictional-board", [requirement])

    assert requirement.min_independent_evidence_count == 1
    assert report.items[0].status is CoverageStatus.SATISFIED


def test_uses_one_detached_bundle_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _bundle([_claim("claim-a")])
    original_encode = __import__(
        "werewolf.ruleset_workbench.coverage",
        fromlist=["encode_bundle"],
    ).encode_bundle

    def mutate_after_encoding(value: ResearchBundle) -> bytes:
        encoded = original_encode(value)
        value.claims[0].value = 999
        return encoded

    monkeypatch.setattr(
        "werewolf.ruleset_workbench.coverage.encode_bundle",
        mutate_after_encoding,
    )
    report = analyze_coverage(bundle, "fictional-board", [_requirement(minimum=1)])

    assert report.items[0].status is CoverageStatus.SATISFIED
