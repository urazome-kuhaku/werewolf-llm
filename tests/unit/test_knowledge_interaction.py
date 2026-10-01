"""Boundary tests for the formal special-interaction knowledge model."""

from datetime import date

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import Channel
from werewolf.knowledge.interaction import (
    InteractionDefinition,
    OutcomeDefinition,
    Predicate,
    PredicateOperator,
    ResolutionStep,
    ScenarioExample,
    StateChange,
    StateOperation,
    VisibilityRule,
)
from werewolf.knowledge.refs import VersionedRef


def _effect(effect_id: str = "mark_resolved") -> dict[str, object]:
    return {
        "effect_id": effect_id,
        "operation": "SET",
        "subject": "phantom",
        "field": "alive",
        "value": False,
        "visibility": "PUBLIC",
    }


def _outcome() -> dict[str, object]:
    return {
        "outcome_code": "interaction_applied",
        "status": "APPLIED",
        "effects": [_effect()],
        "summary": "交互结算完成。",
    }


def _example(example_id: str = "phantom_tide_example") -> dict[str, object]:
    return {
        "example_id": example_id,
        "given": [
            {
                "subject": "phantom",
                "field": "alive",
                "operator": "EQ",
                "value": True,
            },
        ],
        "when": [
            {
                "action_id": "submit_tide",
                "action": "resolve_tide",
                "subject": "tide_mechanic",
                "target": "phantom",
                "parameters": {"strength": 1},
            },
        ],
        "then": _outcome(),
    }


def _interaction(**overrides: object) -> InteractionDefinition:
    values: dict[str, object] = {
        "schema_version": 1,
        "kind": "interaction",
        "id": "phantom-tide-resolution",
        "version": "1.0.0",
        "name": "幻影与潮汐的结算",
        "status": "published",
        "reviewed_by": "reviewer-1",
        "reviewed_at": date(2026, 9, 27),
        "board_refs": ["fictional-board@1.0.0"],
        "subjects": ["phantom", "tide_mechanic"],
        "situation_key": "phantom.dies_by.tide",
        "preconditions": [
            {
                "subject": "phantom",
                "field": "alive",
                "operator": "EQ",
                "value": True,
            },
            {
                "subject": "tide_mechanic",
                "field": "window.open",
                "operator": "IS_TRUE",
            },
        ],
        "ordering": [
            {
                "step_id": "check_eligibility",
                "order": 1,
                "action": "check_target",
                "subject": "phantom",
            },
            {
                "step_id": "apply_effect",
                "order": 2,
                "action": "apply_state_change",
                "subject": "phantom",
                "parameters": {"cause": "tide"},
            },
        ],
        "outcome": _outcome(),
        "notifications": [
            {
                "notification_id": "public_resolution",
                "visibility": Channel.PUBLIC,
                "message_code": "phantom_resolved",
            },
            {
                "notification_id": "private_audit",
                "visibility": Channel.GM_ONLY,
                "message_code": "phantom_resolution_audit",
                "audience": "moderator",
            },
        ],
        "examples": [_example()],
        "claim_refs": ["claim-phantom-001"],
        "source_refs": ["source-tide-handbook"],
    }
    values.update(overrides)
    return InteractionDefinition.model_validate(values)


def test_interaction_definition_accepts_typed_published_record() -> None:
    interaction = _interaction(situation_key="  Phantom.Dies-By.Tide  ")

    assert interaction.kind == "interaction"
    assert interaction.board_refs == [VersionedRef(id="fictional-board", version="1.0.0")]
    assert interaction.situation_key == "phantom.dies_by.tide"
    assert len(interaction.subjects) == 2
    assert interaction.ordering[1].order == 2
    assert interaction.ordering[0].action == "check_target"
    assert interaction.notifications[0].visibility is Channel.PUBLIC
    assert interaction.outcome.effects[0].operation.value == "SET"


def test_nested_models_keep_machine_fields_structured_and_strict() -> None:
    predicate = Predicate(
        subject="phantom",
        field="alive",
        operator=PredicateOperator.EQ,
        value=True,
    )
    step = ResolutionStep(
        step_id="apply_effect",
        order=1,
        action="apply_state_change",
        subject="phantom",
        parameters={"cause": "tide"},
    )
    outcome = OutcomeDefinition.model_validate(_outcome())
    example = ScenarioExample.model_validate(_example())
    notification = VisibilityRule(
        notification_id="public_resolution",
        channel=Channel.PUBLIC,
        message_code="resolved",
    )

    assert predicate.operator is PredicateOperator.EQ
    assert step.parameters["cause"] == "tide"
    assert isinstance(outcome.effects[0], StateChange)
    assert example.then.outcome_code == "interaction_applied"
    assert notification.visibility is Channel.PUBLIC

    values = _interaction().model_dump()
    values["unexpected"] = True
    with pytest.raises(ValidationError):
        InteractionDefinition.model_validate(values)


@pytest.mark.parametrize("status", ["REJECTED", "NO_EFFECT"])
def test_rejected_or_no_effect_outcomes_allow_empty_effects(status: str) -> None:
    outcome = OutcomeDefinition.model_validate(
        {
            "outcome_code": "interaction_skipped",
            "status": status,
            "effects": [],
        },
    )

    assert outcome.effects == []


@pytest.mark.parametrize("status", ["APPLIED", "PARTIAL"])
def test_applied_or_partial_outcomes_require_effects(status: str) -> None:
    with pytest.raises(ValidationError):
        OutcomeDefinition.model_validate(
            {
                "outcome_code": "interaction_applied",
                "status": status,
                "effects": [],
            },
        )


@pytest.mark.parametrize("operation", ["SET", "ADD", "APPEND", "REMOVE"])
def test_value_operations_require_an_explicit_value(operation: str) -> None:
    effect = _effect()
    effect.pop("value")
    effect["operation"] = operation

    with pytest.raises(ValidationError):
        OutcomeDefinition.model_validate(
            {
                "outcome_code": "interaction_applied",
                "status": "APPLIED",
                "effects": [effect],
            },
        )


def test_clear_must_not_include_value() -> None:
    effect = _effect()
    effect["operation"] = StateOperation.CLEAR

    with pytest.raises(ValidationError):
        OutcomeDefinition.model_validate(
            {
                "outcome_code": "interaction_applied",
                "status": "APPLIED",
                "effects": [effect],
            },
        )


def test_clear_without_value_is_valid() -> None:
    effect = _effect()
    effect["operation"] = StateOperation.CLEAR
    effect.pop("value")

    outcome = OutcomeDefinition.model_validate(
        {
            "outcome_code": "interaction_applied",
            "status": "APPLIED",
            "effects": [effect],
        },
    )

    assert outcome.effects[0].operation is StateOperation.CLEAR


@pytest.mark.parametrize(
    "reference",
    [
        "fictional-board@1.0",
        "fictional-board@latest",
        "latest@1.0.0",
        "../fictional-board@1.0.0",
    ],
)
def test_interaction_rejects_unpinned_or_malformed_board_versions(reference: str) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _interaction(board_refs=[reference])


def test_interaction_rejects_duplicate_board_refs_subjects_and_claims() -> None:
    with pytest.raises(ValidationError):
        _interaction(board_refs=["fictional-board@1.0.0", "fictional-board@1.0.0"])

    with pytest.raises(ValidationError):
        _interaction(subjects=["phantom", "phantom"])

    with pytest.raises(ValidationError):
        _interaction(claim_refs=["claim-phantom-001", "claim-phantom-001"])


@pytest.mark.parametrize(
    "ordering",
    [
        [
            {
                "step_id": "same_step",
                "order": 1,
                "action": "check_target",
            },
            {
                "step_id": "same_step",
                "order": 2,
                "action": "apply_state_change",
            },
        ],
        [
            {"step_id": "first", "order": 2, "action": "check_target"},
            {"step_id": "second", "order": 1, "action": "apply_state_change"},
        ],
        [
            {"step_id": "first", "order": 1, "action": "check_target"},
            {"step_id": "second", "order": 3, "action": "apply_state_change"},
        ],
    ],
)
def test_interaction_rejects_duplicate_or_unordered_steps(
    ordering: list[dict[str, object]],
) -> None:
    with pytest.raises(ValidationError):
        _interaction(ordering=ordering)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "draft"),
        ("kind", "role"),
        ("schema_version", 2),
        ("reviewed_by", "   "),
        ("reviewed_at", "2026-09-27T00:00:00"),
        ("reviewed_at", "2026-02-30"),
    ],
)
def test_interaction_rejects_invalid_status_or_review_metadata(
    field: str,
    value: object,
) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _interaction(**{field: value})


@pytest.mark.parametrize(
    "field", ["schema_version", "kind", "status", "reviewed_by", "reviewed_at"]
)
def test_interaction_requires_review_metadata(field: str) -> None:
    values = _interaction().model_dump()
    values.pop(field)

    with pytest.raises(ValidationError):
        InteractionDefinition.model_validate(values)


def test_interaction_rejects_invalid_visibility() -> None:
    notifications = _interaction().model_dump()["notifications"]
    assert isinstance(notifications, list)
    notifications[0]["visibility"] = "SECRET"

    with pytest.raises(ValidationError):
        _interaction(notifications=notifications)


@pytest.mark.parametrize(
    "predicate",
    [
        {
            "subject": "phantom",
            "field": "alive",
            "operator": "IS_TRUE",
            "value": True,
        },
        {
            "subject": "phantom",
            "field": "alive",
            "operator": "EQ",
        },
        {
            "subject": "phantom",
            "field": "alive",
            "operator": "IN",
            "value": [],
        },
    ],
)
def test_predicate_requires_operator_appropriate_value(predicate: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Predicate.model_validate(predicate)


def test_interaction_requires_multiple_distinct_subjects() -> None:
    with pytest.raises(ValidationError):
        _interaction(subjects=["phantom"])
