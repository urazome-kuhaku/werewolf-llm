"""Deterministic variant and conflict analysis tests."""

from datetime import UTC, datetime

import pytest

import werewolf.ruleset_workbench.analysis as analysis_module
from werewolf.ruleset_workbench.analysis import analyze_bundle
from werewolf.ruleset_workbench.bundles import ResearchBundle
from werewolf.ruleset_workbench.claims import ClaimScope, ClaimStatus
from werewolf.ruleset_workbench.evidence import SourceClass


def _source(source_id: str, *, content_sha256: str | None = None) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": f"https://fictional.example/{source_id}",
        "title": f"Source {source_id}",
        "publisher": "Fixture Publisher",
        "source_class": SourceClass.OTHER,
        "published_at": None,
        "fetched_at": datetime(2026, 9, 27, 12, tzinfo=UTC),
        "content_sha256": content_sha256
        or (source_id[-1] if source_id and source_id[-1] in "0123456789abcdef" else "a") * 64,
        "excerpt": "A fictional source excerpt for deterministic analysis tests.",
        "retrieval_method": "fixture",
    }


def _claim(
    claim_id: str,
    *,
    candidate: str = "classic-12",
    value: object = False,
    conditions: dict[str, object] | None = None,
    scope: ClaimScope = ClaimScope.ROLE,
    key: str = "witch.can_self_heal",
    evidence_ids: list[str] | None = None,
    status: ClaimStatus = ClaimStatus.SUPPORTED,
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "ruleset_candidate_id": candidate,
        "key": key,
        "value": value,
        "scope": scope,
        "conditions": conditions if conditions is not None else {"night": 1},
        "evidence_ids": evidence_ids or ["source-a"],
        "confidence": 0.9,
        "extraction_note": "Fixture claim.",
        "status": status,
    }


def _bundle(claims: list[dict[str, object]], source_ids: list[str] | None = None) -> ResearchBundle:
    ids = source_ids or sorted(
        {evidence_id for claim in claims for evidence_id in claim["evidence_ids"]},
    )
    return ResearchBundle.model_validate(
        {
            "board_name": "Fixture Board",
            "locale": "zh-CN",
            "sources": [_source(source_id) for source_id in ids],
            "claims": claims,
        },
    )


def test_same_context_different_values_is_a_blocking_conflict() -> None:
    bundle = _bundle(
        [
            _claim("claim-false", value=False, evidence_ids=["source-a"]),
            _claim("claim-true", value=True, evidence_ids=["source-b"]),
        ],
        ["source-a", "source-b"],
    )

    report = analyze_bundle(bundle)

    assert len(report.variants) == 1
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.unresolved is True
    assert conflict.needs_human_decision is True
    assert conflict.resolution == "NEEDS_DECISION"
    assert conflict.claim_ids == ("claim-false", "claim-true")
    assert conflict.evidence_ids == ("source-a", "source-b")
    assert [item.canonical_value for item in conflict.values] == ["false", "true"]
    assert report.unresolved_conflicts == report.conflicts
    assert report.keys_needing_decision == ("witch.can_self_heal",)


def test_independent_evidence_count_deduplicates_mirrors_but_keeps_ids() -> None:
    bundle = _bundle(
        [
            _claim(
                "claim-false",
                value=False,
                evidence_ids=["source-a", "source-b"],
            ),
            _claim(
                "claim-true",
                value=True,
                evidence_ids=["source-c", "source-d"],
            ),
        ],
        ["source-a", "source-b", "source-c", "source-d"],
    )
    mirrored_source = bundle.sources[1]
    bundle = bundle.model_copy(
        update={
            "sources": (
                bundle.sources[0],
                mirrored_source.model_copy(update={"content_sha256": "a" * 64}),
                bundle.sources[2],
                bundle.sources[3],
            ),
        },
    )

    report = analyze_bundle(bundle)

    values = {item.canonical_value: item for item in report.conflicts[0].values}
    assert values["false"].evidence_ids == ("source-a", "source-b")
    assert values["false"].independent_evidence_count == 1
    assert values["true"].evidence_ids == ("source-c", "source-d")
    assert values["true"].independent_evidence_count == 2


def test_tampered_nested_evidence_reference_is_rejected() -> None:
    bundle = _bundle([_claim("claim-a")])
    bundle.claims[0].evidence_ids.append("source-missing")

    with pytest.raises(ValueError, match="absent from sources"):
        analyze_bundle(bundle)


def test_analysis_uses_one_canonical_snapshot_for_both_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(
        [
            _claim("claim-a", value=False, evidence_ids=["source-a"]),
            _claim("claim-b", value=False, evidence_ids=["source-b"]),
        ],
        ["source-a", "source-b"],
    )
    original_build_variants = analysis_module._build_variants

    def mutate_original_and_build(snapshot: ResearchBundle):
        bundle.claims[1].value = True
        return original_build_variants(snapshot)

    monkeypatch.setattr(
        analysis_module,
        "_build_variants",
        mutate_original_and_build,
    )

    report = analyze_bundle(bundle)

    assert report.conflicts == ()


def test_same_value_is_not_a_conflict_and_duplicate_sources_count_once() -> None:
    bundle = _bundle(
        [
            _claim("claim-a", value=False, evidence_ids=["source-a"]),
            _claim("claim-b", value=False, evidence_ids=["source-a"]),
        ],
    )

    report = analyze_bundle(bundle)

    assert report.conflicts == ()
    context = report.variants[0].contexts[0]
    assert context.claim_ids == ("claim-a", "claim-b")
    assert context.active_claim_ids == ("claim-a", "claim-b")


def test_rejected_claim_does_not_create_or_join_an_active_conflict() -> None:
    bundle = _bundle(
        [
            _claim("claim-accepted", value=False),
            _claim(
                "claim-rejected",
                value=True,
                evidence_ids=["source-b"],
                status=ClaimStatus.REJECTED,
            ),
        ],
        ["source-a", "source-b"],
    )

    report = analyze_bundle(bundle)

    assert report.conflicts == ()
    assert report.variants[0].claim_ids == ("claim-accepted", "claim-rejected")
    assert report.variants[0].active_claim_ids == ("claim-accepted",)


def test_candidates_and_conditions_are_isolated() -> None:
    bundle = _bundle(
        [
            _claim("claim-classic-false", value=False, candidate="classic-12"),
            _claim("claim-event-true", value=True, candidate="event-12"),
            _claim("claim-night-two", value=True, conditions={"night": 2}),
        ],
        ["source-a"],
    )

    report = analyze_bundle(bundle)

    assert [variant.ruleset_candidate_id for variant in report.variants] == [
        "classic-12",
        "event-12",
    ]
    assert report.conflicts == ()
    assert len(report.variants[0].contexts) == 2


def test_output_is_stable_when_claim_and_condition_order_changes() -> None:
    claims = [
        _claim(
            "claim-z",
            value={"b": 2, "a": 1},
            conditions={"z": [2, 1], "a": {"y": 2, "x": 1}},
            evidence_ids=["source-b"],
        ),
        _claim(
            "claim-a",
            value={"a": 1, "b": 2},
            conditions={"a": {"x": 1, "y": 2}, "z": [2, 1]},
            evidence_ids=["source-a"],
        ),
    ]
    first = analyze_bundle(_bundle(claims, ["source-a", "source-b"]))
    second = analyze_bundle(_bundle(list(reversed(claims)), ["source-a", "source-b"]))

    assert first.model_dump() == second.model_dump()
    assert first.variants[0].contexts[0].context.canonical_conditions == (
        '{"a":{"x":1,"y":2},"z":[2,1]}'
    )
