"""Boundary tests for board-specific knowledge models."""

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.models import BoardRoleBinding, KnowledgeRef, ReadingPlan
from werewolf.knowledge.refs import VersionedRef


def _topic(value: str) -> str:
    return value


def _reading_plan() -> ReadingPlan:
    return ReadingPlan(
        board_ref=VersionedRef(id="classic-board", version="1.0.0"),
        bootstrap_topics=[_topic("board:overview"), _topic("mechanic:game_cycle")],
        role_required_topics={
            "witch": [
                _topic("role:witch"),
                _topic("interaction:witch-wolf-kill"),
            ],
        },
        phase_topics={
            "VOTE": [_topic("mechanic:voting"), _topic("mechanic:tie_and_pk")],
            GamePhase.NIGHT_ACTION: [_topic("mechanic:night_resolution")],
        },
        high_risk_topics=[_topic("interaction:hunter-poison")],
        suggested_queries=["猎人吃毒能否开枪", "平票后的投票资格"],
    )


def test_knowledge_ref_supports_strict_compact_navigation_reference() -> None:
    reference = KnowledgeRef.parse("mechanic:tie_and_pk")

    assert reference.kind == "mechanic"
    assert reference.id == "tie_and_pk"
    assert reference.format() == "mechanic:tie_and_pk"
    assert KnowledgeRef.model_validate({"kind": "board", "id": "overview"}) == KnowledgeRef(
        kind="board",
        id="overview",
    )


@pytest.mark.parametrize(
    "value",
    [
        "../board:overview",
        "board:/overview",
        "board:latest",
        "board:overview@1.0.0",
        "board:overview:extra",
        "unknown:overview",
        {"kind": "board", "id": ""},
        {"kind": "board", "id": "Overview"},
        {"kind": "board", "id": "overview", "extra": True},
    ],
)
def test_knowledge_ref_rejects_unpinned_or_malformed_references(value: object) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        KnowledgeRef.model_validate(value)


def test_board_role_binding_requires_claim_provenance_for_overrides() -> None:
    binding = BoardRoleBinding(
        role_ref=VersionedRef(id="witch", version="1.0.0"),
        count=1,
        effective_rules={"can_self_heal": False, "can_use_both_potions_same_night": True},
        override_claim_refs=["claim-witch-self-heal"],
    )

    assert binding.count == 1
    assert binding.effective_rules["can_self_heal"] is False

    empty_override = BoardRoleBinding(
        role_ref=VersionedRef(id="villager", version="1.0.0"),
        count=3,
        effective_rules={},
        override_claim_refs=[],
    )
    assert empty_override.override_claim_refs == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"count": 0},
        {"count": -1},
        {"count": "1"},
        {"effective_rules": {"can_self_heal": False}, "override_claim_refs": []},
        {"effective_rules": {"can_self_heal": False}, "override_claim_refs": ["claim", "claim"]},
        {"effective_rules": {}, "override_claim_refs": ["../claim"]},
        {"effective_rules": {"": True}, "override_claim_refs": ["claim"]},
    ],
)
def test_board_role_binding_rejects_invalid_counts_or_overrides(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "role_ref": VersionedRef(id="witch", version="1.0.0"),
        "count": 1,
        "effective_rules": {},
        "override_claim_refs": [],
    }
    values.update(kwargs)

    with pytest.raises((TypeError, ValueError, ValidationError)):
        BoardRoleBinding.model_validate(values)


def test_reading_plan_accepts_versioned_board_and_phase_navigation() -> None:
    plan = _reading_plan()

    assert plan.board_ref.format() == "classic-board@1.0.0"
    assert plan.phase_topics[GamePhase.VOTE][0] == KnowledgeRef.parse("mechanic:voting")
    assert plan.role_required_topics["witch"][0].format() == "role:witch"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda values: values["bootstrap_topics"].append("board:overview"),
        lambda values: values["role_required_topics"].update(
            {"": ["role:witch"]},
        ),
        lambda values: values["role_required_topics"].update(
            {"witch": []},
        ),
        lambda values: values["phase_topics"].update(
            {"NOT_A_PHASE": ["mechanic:voting"]},
        ),
        lambda values: values["phase_topics"].update(
            {"VOTE": ["mechanic:voting", "mechanic:voting"]},
        ),
        lambda values: values["high_risk_topics"].append("interaction:hunter-poison"),
        lambda values: values["suggested_queries"].append("  "),
        lambda values: values["suggested_queries"].append("猎人吃毒能否开枪"),
    ],
)
def test_reading_plan_rejects_empty_duplicate_or_invalid_entries(mutator: object) -> None:
    values = {
        "board_ref": {"id": "classic-board", "version": "1.0.0"},
        "bootstrap_topics": ["board:overview", "mechanic:game_cycle"],
        "role_required_topics": {
            "witch": ["role:witch", "interaction:witch-wolf-kill"],
        },
        "phase_topics": {
            "VOTE": ["mechanic:voting", "mechanic:tie_and_pk"],
        },
        "high_risk_topics": ["interaction:hunter-poison"],
        "suggested_queries": ["猎人吃毒能否开枪", "平票后的投票资格"],
    }
    # The parametrized mutators are intentionally tiny fixtures; each receives
    # a fresh mapping so one invalid case cannot affect another.
    assert callable(mutator)
    mutator(values)  # type: ignore[union-attr]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        ReadingPlan.model_validate(values)


def test_reading_plan_rejects_extra_fields() -> None:
    values = _reading_plan().model_dump()
    values["unexpected"] = True

    with pytest.raises(ValidationError):
        ReadingPlan.model_validate(values)
