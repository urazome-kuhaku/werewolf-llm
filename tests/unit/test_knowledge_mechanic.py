"""Boundary tests for the formal public-mechanic knowledge model."""

from datetime import date

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import Channel, GamePhase
from werewolf.knowledge.mechanic import (
    ExceptionBranch,
    MechanicDefinition,
    MechanicInput,
    MechanicOutput,
    ParticipantEligibility,
)
from werewolf.knowledge.refs import VersionedRef


def _predicate(subject: str = "actor") -> dict[str, object]:
    return {
        "subject": subject,
        "field": "alive",
        "operator": "EQ",
        "value": True,
    }


def _example(example_id: str = "echo_example") -> dict[str, object]:
    return {
        "example_id": example_id,
        "given": [_predicate()],
        "when": [
            {
                "action_id": "submit_request",
                "action": "submit",
                "subject": "echo_mechanic",
            },
        ],
        "then": {
            "outcome_code": "echo_applied",
            "status": "APPLIED",
            "effects": [
                {
                    "effect_id": "record_result",
                    "operation": "SET",
                    "subject": "mechanic",
                    "field": "result",
                    "value": "ready",
                    "visibility": "PUBLIC",
                },
            ],
        },
    }


def _mechanic(**overrides: object) -> MechanicDefinition:
    values: dict[str, object] = {
        "schema_version": 1,
        "kind": "mechanic",
        "id": "echo-resolution",
        "version": "1.0.0",
        "name": "回声结算",
        "aliases": ["回声流程"],
        "summary": "一个虚构的公共请求与结算机制。",
        "status": "published",
        "reviewed_by": "reviewer-1",
        "reviewed_at": date(2026, 9, 27),
        "board_refs": ["fictional-board@1.0.0"],
        "applicable_phases": [GamePhase.NIGHT_ACTION, "NIGHT_RESOLVE"],
        "participation": [
            {
                "participant_id": "actor",
                "participant_kind": "player",
                "required": True,
                "conditions": [_predicate()],
            },
            {
                "participant_id": "moderator",
                "participant_kind": "system",
                "required": True,
            },
        ],
        "inputs": [
            {
                "input_id": "target",
                "value_type": "player_id",
                "description": "请求作用的目标。",
                "required": True,
            },
        ],
        "outputs": [
            {
                "output_id": "resolution",
                "value_type": "resolution_code",
                "description": "本次请求的结算结果。",
                "visibility": Channel.PUBLIC,
            },
        ],
        "processing_order": [
            {
                "step_id": "validate_request",
                "order": 1,
                "action": "validate",
                "subject": "actor",
            },
            {
                "step_id": "resolve_request",
                "order": 2,
                "action": "resolve",
                "subject": "moderator",
            },
        ],
        "exception_branches": [
            {
                "branch_id": "dead_actor",
                "conditions": [
                    {
                        "subject": "actor",
                        "field": "alive",
                        "operator": "IS_FALSE",
                    },
                ],
                "outcome_code": "request_rejected",
                "output_ids": [],
                "visibility": Channel.PRIVATE,
            },
        ],
        "result_visibility": [
            {
                "notification_id": "public_result",
                "visibility": Channel.PUBLIC,
                "message_code": "echo_resolved",
            },
        ],
        "examples": [_example()],
        "claim_refs": ["claim-echo-001"],
        "source_refs": ["source-echo-handbook"],
    }
    values.update(overrides)
    return MechanicDefinition.model_validate(values)


def test_mechanic_definition_parses_formal_published_record() -> None:
    mechanic = _mechanic()

    assert mechanic.kind == "mechanic"
    assert mechanic.board_refs == [VersionedRef(id="fictional-board", version="1.0.0")]
    assert mechanic.applicable_phases == [GamePhase.NIGHT_ACTION, GamePhase.NIGHT_RESOLVE]
    assert mechanic.inputs[0].input_id == "target"
    assert mechanic.outputs[0].visibility is Channel.PUBLIC
    assert mechanic.processing_order[1].order == 2
    assert mechanic.exception_branches[0].branch_id == "dead_actor"
    assert mechanic.id_version == VersionedRef(id="echo-resolution", version="1.0.0")


def test_mechanic_accepts_document_aliases_and_exposes_compatibility_properties() -> None:
    values = _mechanic().model_dump()
    values["applicable_boards"] = values.pop("board_refs")
    values["phases"] = values.pop("applicable_phases")
    values["eligibility"] = values.pop("participation")
    values["ordering"] = values.pop("processing_order")
    values["visibility"] = values.pop("result_visibility")

    mechanic = MechanicDefinition.model_validate(values)

    assert mechanic.eligibility == mechanic.participation
    assert mechanic.ordering == mechanic.processing_order
    assert mechanic.visibility == mechanic.result_visibility


def test_nested_contracts_are_strict_and_structured() -> None:
    participant = ParticipantEligibility.model_validate(
        {
            "participant": "actor",
            "kind": "player",
            "conditions": [_predicate()],
        }
    )
    input_field = MechanicInput.model_validate(
        {
            "field_id": "target",
            "value_type": "player_id",
            "description": "目标。",
        }
    )
    output_field = MechanicOutput.model_validate(
        {
            "output_code": "result",
            "value_type": "resolution_code",
            "description": "结果。",
            "visibility": "PUBLIC",
        }
    )
    branch = ExceptionBranch.model_validate(
        {
            "exception_code": "invalid",
            "when": [_predicate()],
            "outcome": "rejected",
            "visibility": "PRIVATE",
        }
    )

    assert participant.participant_id == "actor"
    assert input_field.input_id == "target"
    assert output_field.output_id == "result"
    assert branch.outcome_code == "rejected"

    values = _mechanic().model_dump()
    values["unexpected"] = True
    with pytest.raises(ValidationError):
        MechanicDefinition.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("kind", "board"),
        ("status", "draft"),
        ("id", "Echo Resolution"),
        ("version", "1.0"),
        ("reviewed_by", "   "),
        ("reviewed_at", "2026-09-27T00:00:00"),
        ("reviewed_at", "2026-02-30"),
    ],
)
def test_mechanic_rejects_invalid_identity_and_review_metadata(
    field: str,
    value: object,
) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _mechanic(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["schema_version", "kind", "status", "reviewed_by", "reviewed_at"],
)
def test_mechanic_requires_published_review_metadata(field: str) -> None:
    values = _mechanic().model_dump()
    values.pop(field)

    with pytest.raises(ValidationError):
        MechanicDefinition.model_validate(values)


@pytest.mark.parametrize(
    "reference",
    [
        "fictional-board@1.0",
        "fictional-board@latest",
        "latest@1.0.0",
        "../fictional-board@1.0.0",
    ],
)
def test_mechanic_rejects_unpinned_board_references(reference: str) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _mechanic(board_refs=[reference])


@pytest.mark.parametrize(
    "mutator",
    [
        lambda values: values["aliases"].append("回声流程"),
        lambda values: values["applicable_phases"].append("NIGHT_ACTION"),
        lambda values: values["participation"].append(values["participation"][0]),
        lambda values: values["inputs"].append(values["inputs"][0]),
        lambda values: values["outputs"].append(values["outputs"][0]),
        lambda values: values["processing_order"].__setitem__(
            1,
            {**values["processing_order"][1], "order": 3},
        ),
        lambda values: values["exception_branches"].append(values["exception_branches"][0]),
        lambda values: values["examples"].append(values["examples"][0]),
        lambda values: values["claim_refs"].append("claim-echo-001"),
        lambda values: values["source_refs"].append("source-echo-handbook"),
    ],
)
def test_mechanic_rejects_duplicate_items_or_non_contiguous_order(mutator: object) -> None:
    values = _mechanic().model_dump()
    assert callable(mutator)
    mutator(values)  # type: ignore[union-attr]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        MechanicDefinition.model_validate(values)


def test_mechanic_rejects_invalid_phase_and_unknown_exception_output() -> None:
    values = _mechanic().model_dump()
    values["applicable_phases"] = ["NOT_A_PHASE"]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        MechanicDefinition.model_validate(values)

    values = _mechanic().model_dump()
    values["exception_branches"][0]["output_ids"] = ["not_declared"]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        MechanicDefinition.model_validate(values)


def test_mechanic_rejects_extra_nested_machine_fields() -> None:
    values = _mechanic().model_dump()
    values["inputs"][0]["arbitrary_rule"] = True
    with pytest.raises(ValidationError):
        MechanicDefinition.model_validate(values)
