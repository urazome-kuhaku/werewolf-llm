"""Tests for stable versioned knowledge references."""

import pytest
from pydantic import ValidationError

from werewolf.knowledge import VersionedRef


def test_reference_round_trips_through_canonical_string() -> None:
    reference = VersionedRef(id="classic_12-seer", version="1.0.0")

    assert reference.format() == "classic_12-seer@1.0.0"
    assert str(reference) == reference.format()
    assert VersionedRef.parse(reference.format()) == reference


def test_reference_is_strict_and_immutable() -> None:
    reference = VersionedRef.model_validate(
        {"id": "board_v1", "version": "2.10.3"},
    )

    with pytest.raises(ValidationError):
        reference.id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        VersionedRef.model_validate(
            {"id": "board_v1", "version": "2.10.3", "extra": True},
        )
    with pytest.raises(ValidationError):
        VersionedRef.model_validate({"id": 123, "version": "2.10.3"})


@pytest.mark.parametrize(
    "value",
    [
        "../board@1.0.0",
        "board/name@1.0.0",
        "Board@1.0.0",
        "board name@1.0.0",
        "board@latest",
        "board@v1.0.0",
        "board@1.0",
        "board@1.0.0-beta.1",
        "board@01.0.0",
        "board@1.0.0 @",
        "board@1.0.0@2.0.0",
    ],
)
def test_invalid_references_are_rejected(value: str) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        VersionedRef.parse(value)


def test_id_length_limit_is_enforced() -> None:
    with pytest.raises(ValidationError):
        VersionedRef(id="a" * 65, version="1.0.0")
