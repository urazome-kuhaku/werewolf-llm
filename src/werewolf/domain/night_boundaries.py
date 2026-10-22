"""Finite, typed prerequisites for completing a night boundary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

NightBoundaryPrerequisite = Literal["FIRST_DAY_SHERIFF_ELECTION", "DAY_ANNOUNCEMENT"]
NightBoundaryContinuation = Literal["RESUME_FIRST_DAY_FLOW"]


class NightBoundarySchedule(BaseModel):
    """Frozen host obligations that must finish before daytime resumes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prerequisites: tuple[NightBoundaryPrerequisite, ...] = ("DAY_ANNOUNCEMENT",)
    continuation: NightBoundaryContinuation = "RESUME_FIRST_DAY_FLOW"

    @field_validator("prerequisites", mode="before")
    @classmethod
    def accept_json_prerequisites(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_prerequisites(self) -> Self:
        if len(set(self.prerequisites)) != len(self.prerequisites):
            raise ValueError("night-boundary prerequisites must be unique")
        if "DAY_ANNOUNCEMENT" not in self.prerequisites:
            raise ValueError("DAY_ANNOUNCEMENT is required for every scheduled night")
        if set(self.prerequisites) - {
            "FIRST_DAY_SHERIFF_ELECTION",
            "DAY_ANNOUNCEMENT",
        }:
            raise ValueError("night-boundary schedule contains an unsupported prerequisite")
        return self

    def required_for_round(self, round_no: int) -> tuple[NightBoundaryPrerequisite, ...]:
        """Return obligations for this night; the sheriff election is round-zero only."""

        if type(round_no) is not int or round_no < 0:
            raise ValueError("round_no must be a non-negative integer")
        return tuple(
            prerequisite
            for prerequisite in self.prerequisites
            if prerequisite != "FIRST_DAY_SHERIFF_ELECTION" or round_no == 0
        )


__all__ = [
    "NightBoundaryContinuation",
    "NightBoundaryPrerequisite",
    "NightBoundarySchedule",
]
