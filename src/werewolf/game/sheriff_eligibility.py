"""Game-facing exports for the first-day sheriff participation projection."""

from werewolf.domain.sheriff_eligibility import (
    first_day_sheriff_participants,
    first_night_death_seats,
    has_first_day_announcement,
    is_first_day_sheriff_boundary,
    should_mask_unannounced_first_night_death,
)

__all__ = [
    "first_day_sheriff_participants",
    "first_night_death_seats",
    "has_first_day_announcement",
    "is_first_day_sheriff_boundary",
    "should_mask_unannounced_first_night_death",
]
