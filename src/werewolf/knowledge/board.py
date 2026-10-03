"""Strict published board knowledge models.

This module describes the machine-readable part of a published board
document.  It intentionally contains no concrete board rules: a board only
declares version-pinned role, mechanic, and interaction documents together
with the ordering and visibility contracts needed by the runtime.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from werewolf.domain.enums import GamePhase

from .models import BoardRoleBinding, ReadingPlan
from .refs import VersionedRef
from .role import BoundedText, LogicalId, SemanticVersion

_LOCALE_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?", re.ASCII)

BoardSummary = Annotated[
    str,
    StringConstraints(min_length=1, max_length=300, strip_whitespace=True, strict=True),
]
ReviewerId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, strip_whitespace=True, strict=True),
]
PositiveCount = Annotated[int, Field(gt=0, strict=True)]


class _StrictBoardModel(BaseModel):
    """Shared immutable and closed configuration for published board records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


def _enum_phase(value: object, *, field_name: str) -> GamePhase:
    """Parse one persisted phase while retaining strict non-string input."""

    if isinstance(value, GamePhase):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a GamePhase or its string value")
    try:
        return GamePhase(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} contains an invalid game phase: {value!r}") from exc


def _unique(values: Sequence[object], field_name: str) -> Sequence[object]:
    """Reject duplicate persisted identifiers without changing input order."""

    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicate values")
    return values


def _parse_versioned_ref(value: object) -> object:
    """Accept the documented compact ``id@version`` spelling for references."""

    if isinstance(value, str):
        return VersionedRef.parse(value)
    return value


def _parse_versioned_ref_list(value: object) -> object:
    if not isinstance(value, list):
        return value
    return [_parse_versioned_ref(item) for item in value]


def _validate_pinned_refs(value: list[VersionedRef], field_name: str) -> list[VersionedRef]:
    """Require distinct, version-pinned references in one dependency list."""

    for reference in value:
        if reference.id == "latest" or reference.version == "latest":
            raise ValueError(f"{field_name} must not use the unpinned 'latest' reference")
    _unique([reference.format() for reference in value], field_name)
    return value


def _validate_logical_refs(
    value: list[str], field_name: str, *, required: bool = False
) -> list[str]:
    if required and not value:
        raise ValueError(f"{field_name} must contain at least one reference")
    _unique(list(value), field_name)
    return value


def _default_night_phase(window_id: str) -> GamePhase:
    """Map the compact plan spelling from the design document to a phase."""

    if window_id in {"wolf_team_chat", "wolf_chat", "team_chat"}:
        return GamePhase.NIGHT_TEAM_CHAT
    if window_id in {"night_resolve", "resolve"}:
        return GamePhase.NIGHT_RESOLVE
    return GamePhase.NIGHT_ACTION


# These are structural relationships understood by the knowledge model.  A
# tie policy outside this set remains representable as a logical ID, but the
# runtime must still reject it unless its support table explicitly enables
# execution.  The knowledge layer must not turn an unknown ID into code.
_TIE_POLICIES_REQUIRING_PK = frozenset({"pk_then_no_exile_on_retie", "revote_until_unique"})
_TIE_POLICIES_REJECTING_PK = frozenset({"no_exile_on_tie"})
_SHERIFF_TIE_POLICIES_REQUIRING_PK = frozenset(
    {"pk_then_revote", "revote_until_unique", "pk_then_no_sheriff_on_retie"}
)
_SHERIFF_TIE_POLICIES_REJECTING_PK = frozenset({"no_sheriff_on_tie", "no_election_on_tie"})


class FactionDefinition(_StrictBoardModel):
    """One named faction entry used by a board's side summary."""

    faction_id: LogicalId = Field(validation_alias=AliasChoices("faction_id", "id"))
    name: BoundedText
    role_ids: list[LogicalId] = Field(default_factory=list)
    victory_side: LogicalId | None = None

    @field_validator("role_ids")
    @classmethod
    def validate_role_ids(cls, value: list[str]) -> list[str]:
        _unique(list(value), "role_ids")
        return value


class VictoryDefinition(_StrictBoardModel):
    """Machine-readable victory and terminal-check contract."""

    mode: LogicalId = Field(validation_alias=AliasChoices("mode", "victory_mode"))
    winning_sides: list[LogicalId] = Field(default_factory=list)
    check_phases: list[GamePhase] = Field(
        default_factory=lambda: [GamePhase.VICTORY_CHECK],
        validation_alias=AliasChoices("check_phases", "check_phase"),
    )
    draw_policy: LogicalId = "no_winner"
    special_conditions: list[BoundedText] = Field(default_factory=list)
    # A non-empty map makes the board's good-side split explicit for
    # eliminate-side rules.  Empty is retained for legacy boards whose
    # victory evaluator still receives an explicit role-group map from the
    # host.  The board integrity validator below requires a non-empty map to
    # cover every bound role exactly once.
    role_groups: dict[LogicalId, Literal["wolf", "god", "villager"]] = Field(default_factory=dict)

    @field_validator("check_phases", mode="before")
    @classmethod
    def normalize_check_phases(cls, value: object) -> object:
        if isinstance(value, (str, GamePhase)):
            return [_enum_phase(value, field_name="check_phase")]
        if not isinstance(value, list):
            return value
        return [_enum_phase(item, field_name="check_phases") for item in value]

    @field_validator("winning_sides")
    @classmethod
    def validate_winning_sides(cls, value: list[str]) -> list[str]:
        _unique(list(value), "winning_sides")
        return value

    @field_validator("check_phases")
    @classmethod
    def validate_check_phases(cls, value: list[GamePhase]) -> list[GamePhase]:
        if not value:
            raise ValueError("check_phases must contain at least one phase")
        _unique(list(value), "check_phases")
        allowed = {
            GamePhase.NIGHT_RESOLVE,
            GamePhase.DAY_RESOLVE,
            GamePhase.TRIGGER_ACTION,
            GamePhase.VICTORY_CHECK,
            GamePhase.FINISHED,
        }
        if any(phase not in allowed for phase in value):
            raise ValueError("check_phases must contain resolution or victory phases")
        return value

    @field_validator("special_conditions")
    @classmethod
    def validate_special_conditions(cls, value: list[str]) -> list[str]:
        _unique(list(value), "special_conditions")
        return value

    @property
    def victory_mode(self) -> str:
        """Compatibility spelling used by the frontmatter example."""

        return self.mode


class VoteDefinition(_StrictBoardModel):
    """Collection, reveal, and tie behavior for a daytime vote.

    ``tie_policy`` is a constrained logical ID instead of a closed literal so
    the knowledge layer can preserve reviewed board variants that the current
    runtime does not know yet.  Runtime execution must gate the ID through its
    supported-policy table before dispatching behavior.
    """

    visibility_during_collection: Literal["secret", "public"]
    reveal_after_close: Literal["ballots_and_totals", "totals_only", "none"]
    tie_policy: LogicalId
    eligible_voters: Literal["alive", "alive_with_vote"] = "alive_with_vote"
    allow_abstain: bool = False

    @field_validator(
        "visibility_during_collection",
        "reveal_after_close",
        "tie_policy",
        "eligible_voters",
        mode="before",
    )
    @classmethod
    def normalize_lowercase_policy(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value


class PkDefinition(_StrictBoardModel):
    """Structured re-vote/PK branch of the daytime flow."""

    enabled: bool = False
    max_candidates: PositiveCount = Field(
        default=2,
        validation_alias=AliasChoices("max_candidates", "candidate_count"),
    )
    speech_phase: Literal[GamePhase.VOTE_PK_SPEECH] = GamePhase.VOTE_PK_SPEECH
    vote_phase: Literal[GamePhase.VOTE_PK] = GamePhase.VOTE_PK
    no_exile_on_retie: bool = True

    @field_validator("speech_phase", "vote_phase", mode="before")
    @classmethod
    def normalize_pk_phase(cls, value: object) -> object:
        return _enum_phase(value, field_name="pk phase")


# Both spellings occur in design discussions; keep one canonical model while
# making the acronym-friendly spelling available to callers.
PKDefinition = PkDefinition


class LastWordsDefinition(_StrictBoardModel):
    """Whether and when a dead player may give last words."""

    enabled: bool = False
    eligible_death_causes: list[LogicalId] = Field(default_factory=list)
    before_reveal: bool = True
    night_death_policy: Literal["none", "first_night_only", "every_night"] = Field(
        default="every_night",
        validation_alias=AliasChoices("night_death_policy", "night_policy", "night_round_policy"),
    )
    day_death_policy: Literal["none", "every_day"] = Field(
        default="every_day",
        validation_alias=AliasChoices("day_death_policy", "day_policy", "day_round_policy"),
    )

    @field_validator("eligible_death_causes")
    @classmethod
    def validate_eligible_causes(cls, value: list[str]) -> list[str]:
        _unique(list(value), "eligible_death_causes")
        return value

    @field_validator("night_death_policy", "day_death_policy", mode="before")
    @classmethod
    def normalize_round_policy(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value


LastWordsPolicy = LastWordsDefinition


class SheriffDefinition(_StrictBoardModel):
    """Optional daytime sheriff office and its board-defined election hooks.

    The official 12-player standard board enables this office, elects it on
    the first day, gives the office a 1.5 vote weight, and lets it speak last.
    Tie, PK, and badge-transfer behavior was not defined by that source, so
    those fields remain optional and preserve ``None`` until another source
    or a host decision supplies a policy.
    """

    enabled: bool = False
    first_day_election: bool = Field(
        default=False,
        validation_alias=AliasChoices("first_day_election", "election_on_first_day", "first_day"),
    )
    vote_weight: float = Field(
        default=1.0,
        gt=0,
        validation_alias=AliasChoices("vote_weight", "vote_multiplier", "election_vote_weight"),
    )
    final_speech: bool = Field(
        default=False,
        validation_alias=AliasChoices("final_speech", "speaks_last", "speaks_last_on_day"),
    )
    tie_policy: LogicalId | None = Field(
        default=None,
        validation_alias=AliasChoices("tie_policy", "election_tie_policy"),
    )
    pk_enabled: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("pk_enabled", "election_pk_enabled", "pk_on_tie"),
    )
    transfer_enabled: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("transfer_enabled", "badge_transfer_enabled"),
    )
    transfer_on_death: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("transfer_on_death", "badge_transfer_on_death"),
    )
    transfer_on_resignation: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("transfer_on_resignation", "badge_transfer_on_resignation"),
    )

    @field_validator("tie_policy", mode="before")
    @classmethod
    def normalize_tie_policy(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_sheriff_contract(self) -> Self:
        if not self.enabled:
            if self.first_day_election:
                raise ValueError("first_day_election requires sheriff.enabled")
            if self.vote_weight != 1.0:
                raise ValueError("vote_weight must be 1.0 when sheriff is disabled")
            if self.final_speech:
                raise ValueError("final_speech requires sheriff.enabled")
            if self.tie_policy is not None:
                raise ValueError("tie_policy requires sheriff.enabled")
            if self.pk_enabled is True:
                raise ValueError("pk_enabled requires sheriff.enabled")
            if self.transfer_enabled is True:
                raise ValueError("transfer_enabled requires sheriff.enabled")
            if self.transfer_on_death is True or self.transfer_on_resignation is True:
                raise ValueError("badge transfer flags require sheriff.enabled")

        if self.transfer_enabled is False and (
            self.transfer_on_death is True or self.transfer_on_resignation is True
        ):
            raise ValueError("specific badge transfer flags require transfer_enabled")

        if self.pk_enabled is True and self.tie_policy is None:
            raise ValueError("tie_policy is required when sheriff PK is enabled")
        if self.tie_policy in _SHERIFF_TIE_POLICIES_REQUIRING_PK and self.pk_enabled is not True:
            raise ValueError("pk_enabled must be true for this sheriff tie_policy")
        if self.tie_policy in _SHERIFF_TIE_POLICIES_REJECTING_PK and self.pk_enabled is True:
            raise ValueError("pk_enabled must be false for this sheriff tie_policy")
        return self

    @property
    def speaks_last(self) -> bool:
        """Compatibility spelling for the official final-speech rule."""

        return self.final_speech


class DayFlow(_StrictBoardModel):
    """Ordered daytime operations, including sheriff, vote, PK, and last words."""

    announce_deaths: bool = True
    speech_phase: Literal[GamePhase.DAY_SPEECH] = GamePhase.DAY_SPEECH
    vote: VoteDefinition
    pk: PkDefinition = Field(default_factory=PkDefinition)
    last_words: LastWordsDefinition = Field(default_factory=LastWordsDefinition)
    sheriff: SheriffDefinition = Field(default_factory=SheriffDefinition)
    resolution_phase: Literal[GamePhase.DAY_RESOLVE] = GamePhase.DAY_RESOLVE

    @field_validator("speech_phase", "resolution_phase", mode="before")
    @classmethod
    def normalize_day_phase(cls, value: object) -> object:
        return _enum_phase(value, field_name="day flow phase")


class NightWindow(_StrictBoardModel):
    """One ordered night action window and its information dependencies."""

    window_id: LogicalId = Field(validation_alias=AliasChoices("window_id", "id", "name"))
    order: PositiveCount = 1
    phase: GamePhase = GamePhase.NIGHT_ACTION
    depends_on: list[LogicalId] = Field(
        default_factory=list,
        validation_alias=AliasChoices("depends_on", "dependencies"),
    )
    parallel: bool = False
    visible_to: list[LogicalId] = Field(default_factory=list)

    @field_validator("phase", mode="before")
    @classmethod
    def normalize_phase(cls, value: object) -> object:
        return _enum_phase(value, field_name="night window phase")

    @field_validator("phase")
    @classmethod
    def validate_night_phase(cls, value: GamePhase) -> GamePhase:
        if value not in {
            GamePhase.NIGHT_TEAM_CHAT,
            GamePhase.NIGHT_ACTION,
            GamePhase.NIGHT_RESOLVE,
        }:
            raise ValueError("night window phase must be a night phase")
        return value

    @field_validator("depends_on", "visible_to")
    @classmethod
    def validate_window_refs(cls, value: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "window references")
        _unique(list(value), field_name)
        return value

    @property
    def id(self) -> str:
        """Compatibility spelling for compact window entries."""

        return self.window_id


class WolfTeamVisibility(_StrictBoardModel):
    """What wolf-team members know and where their team discussion occurs."""

    members_know_each_other: bool
    discussion_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("discussion_enabled", "can_chat"),
    )
    identity_visibility: Literal["members", "faction", "none"] = "members"
    discussion_phase: Literal[GamePhase.NIGHT_TEAM_CHAT] = GamePhase.NIGHT_TEAM_CHAT
    visible_to: list[LogicalId] = Field(default_factory=list)

    @field_validator("discussion_phase", mode="before")
    @classmethod
    def normalize_discussion_phase(cls, value: object) -> object:
        return _enum_phase(value, field_name="discussion_phase")

    @field_validator("visible_to")
    @classmethod
    def validate_visible_to(cls, value: list[str]) -> list[str]:
        _unique(list(value), "visible_to")
        return value


class KnifeRule(_StrictBoardModel):
    """Define wolf target semantics and optional private plan confirmation.

    ``final_target_required`` describes the target/action contract, while
    ``plan_confirmation_required`` describes whether the coordinator must
    summarize the team's discussion before the night can advance.
    """

    selection_mode: Literal["consensus", "designated", "majority"] = Field(
        default="consensus",
        validation_alias=AliasChoices("selection_mode", "mode"),
    )
    target_visibility: Literal["wolf_team", "team", "public", "none"] = Field(
        default="wolf_team",
        validation_alias=AliasChoices("target_visibility", "target_visible_to"),
    )
    final_target_required: bool = True
    plan_confirmation_required: bool = False
    available_after_window: LogicalId = "wolf_team_chat"

    @field_validator("selection_mode", "target_visibility", mode="before")
    @classmethod
    def normalize_knife_policy(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value


KnifeDefinition = KnifeRule


class IdentityRevealDefinition(_StrictBoardModel):
    """Board policy for revealing role cards after ordinary outcomes.

    Existing board documents had no reveal policy and therefore retain the
    historical public default.  A dark board can explicitly disable ordinary
    death and exile reveals while listing separately sourced exceptional
    triggers such as a role-specific flip.
    """

    reveal_on_death: bool = Field(
        default=True,
        validation_alias=AliasChoices("reveal_on_death", "death_reveal"),
    )
    reveal_on_exile: bool = Field(
        default=True,
        validation_alias=AliasChoices("reveal_on_exile", "exile_reveal"),
    )
    exceptional_triggers: list[LogicalId] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "exceptional_triggers", "special_reveal_triggers", "special_reveals"
        ),
    )

    @field_validator("exceptional_triggers")
    @classmethod
    def validate_exceptional_triggers(cls, value: list[str]) -> list[str]:
        _unique(list(value), "exceptional_triggers")
        return value


class BoardDefinition(_StrictBoardModel):
    """A complete, reviewed, immutable board document.

    ``role_bindings``, ``mechanic_refs``, and ``interaction_refs`` are the
    board's dependency closure.  They are all version-pinned; a board cannot
    be published with a ``latest`` alias or duplicate dependency.
    """

    schema_version: Literal[1]
    kind: Literal["board"]
    board_id: LogicalId = Field(validation_alias=AliasChoices("board_id", "id"))
    version: SemanticVersion
    name: BoundedText
    aliases: list[BoundedText] = Field(default_factory=list)
    locale: Annotated[
        str,
        StringConstraints(min_length=2, max_length=16, strip_whitespace=True, strict=True),
    ]
    status: Literal["published"]
    reviewed_by: ReviewerId
    reviewed_at: date
    summary: BoardSummary = Field(validation_alias=AliasChoices("summary", "public_summary"))
    seat_count: PositiveCount
    factions: dict[LogicalId, PositiveCount] = Field(min_length=1)
    role_bindings: list[BoardRoleBinding] = Field(
        min_length=1,
        validation_alias=AliasChoices("role_bindings", "roles"),
    )
    victory: VictoryDefinition
    wolf_team_visibility: WolfTeamVisibility
    knife_rule: KnifeRule
    identity_reveal: IdentityRevealDefinition = Field(
        default_factory=IdentityRevealDefinition,
        validation_alias=AliasChoices("identity_reveal", "identity_reveal_policy", "role_reveal"),
    )
    night_windows: list[NightWindow] = Field(min_length=1)
    day_flow: DayFlow
    mechanic_refs: list[VersionedRef] = Field(
        default_factory=list,
        validation_alias=AliasChoices("mechanic_refs", "mechanics"),
    )
    interaction_refs: list[VersionedRef] = Field(
        default_factory=list,
        validation_alias=AliasChoices("interaction_refs", "interactions"),
    )
    reading_plan: ReadingPlan
    claim_refs: list[LogicalId] = Field(
        min_length=1,
        validation_alias=AliasChoices("claim_refs", "claims"),
    )
    source_refs: list[LogicalId] = Field(
        min_length=1,
        validation_alias=AliasChoices("source_refs", "sources"),
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_document_aliases(cls, value: object) -> object:
        """Normalize the compact frontmatter spelling before strict parsing."""

        if not isinstance(value, Mapping):
            return value
        data = dict(value)

        # The technical plan's examples call these collections ``roles``,
        # ``mechanics``, and ``interactions``.  Keep the formal field names
        # explicit while accepting those frontmatter aliases.
        if "role_bindings" not in data and "roles" in data:
            data["role_bindings"] = data.pop("roles")
        if "mechanic_refs" not in data and "mechanics" in data:
            data["mechanic_refs"] = data.pop("mechanics")
        if "interaction_refs" not in data and "interactions" in data:
            data["interaction_refs"] = data.pop("interactions")

        # A board's short frontmatter form may put vote/PK/last-word entries at
        # the document root.  They are still parsed into one DayFlow model.
        if "day_flow" not in data and "vote" in data:
            day_flow: dict[str, object] = {"vote": data.pop("vote")}
            for key in ("pk", "last_words", "sheriff", "announce_deaths"):
                if key in data:
                    day_flow[key] = data.pop(key)
            data["day_flow"] = day_flow

        # The design also groups the two wolf rules under ``wolf_team``.  The
        # canonical model exposes both contracts as named top-level fields.
        if "wolf_team" in data:
            wolf_team = data.pop("wolf_team")
            if isinstance(wolf_team, Mapping):
                if "wolf_team_visibility" not in data and "visibility" in wolf_team:
                    data["wolf_team_visibility"] = wolf_team["visibility"]
                if "knife_rule" not in data and "knife" in wolf_team:
                    data["knife_rule"] = wolf_team["knife"]

        # Support the source spelling from the minimal YAML example when it is
        # a plain list of logical source IDs.  Structured source records remain
        # intentionally rejected until a dedicated source-reference model is
        # introduced by the publishing layer.
        if "source_refs" not in data and "sources" in data:
            data["source_refs"] = data.pop("sources")

        windows = data.get("night_windows")
        if isinstance(windows, list):
            normalized_windows: list[object] = []
            for index, window in enumerate(windows, start=1):
                if isinstance(window, str):
                    normalized_windows.append(
                        {
                            "window_id": window,
                            "order": index,
                            "phase": _default_night_phase(window),
                        }
                    )
                elif isinstance(window, Mapping):
                    item = dict(window)
                    item.setdefault("order", index)
                    if "phase" not in item:
                        identifier = item.get("window_id", item.get("id", item.get("name")))
                        if isinstance(identifier, str):
                            item["phase"] = _default_night_phase(identifier)
                    normalized_windows.append(item)
                else:
                    normalized_windows.append(window)
            data["night_windows"] = normalized_windows
        return data

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        _unique(list(value), "aliases")
        return value

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: str) -> str:
        if _LOCALE_PATTERN.fullmatch(value) is None:
            raise ValueError("locale must use a compact BCP-47-like language tag")
        return value

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def normalize_reviewed_at(cls, value: object) -> object:
        if type(value) is date:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("reviewed_at must be a valid ISO calendar date") from exc
        raise TypeError("reviewed_at must be a date in YYYY-MM-DD form")

    @field_validator("claim_refs", "source_refs")
    @classmethod
    def validate_claim_source_refs(cls, value: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "knowledge references")
        return _validate_logical_refs(value, field_name, required=True)

    @field_validator("mechanic_refs", "interaction_refs", mode="before")
    @classmethod
    def parse_dependency_refs(cls, value: object) -> object:
        return _parse_versioned_ref_list(value)

    @field_validator("mechanic_refs", "interaction_refs")
    @classmethod
    def validate_dependency_refs(
        cls, value: list[VersionedRef], info: object
    ) -> list[VersionedRef]:
        field_name = getattr(info, "field_name", "dependency references")
        return _validate_pinned_refs(value, field_name)

    @field_validator("night_windows")
    @classmethod
    def validate_night_windows(cls, value: list[NightWindow]) -> list[NightWindow]:
        _unique([window.window_id for window in value], "night_windows window_id")
        orders = [window.order for window in value]
        _unique(orders, "night_windows order")
        if orders != list(range(1, len(value) + 1)):
            raise ValueError("night_windows order must be contiguous starting at 1")
        position = {window.window_id: window.order for window in value}
        for window in value:
            for dependency in window.depends_on:
                if dependency not in position:
                    raise ValueError(
                        f"night window {window.window_id!r} depends on an unknown window"
                    )
                if position[dependency] >= window.order:
                    raise ValueError("night window dependencies must point to an earlier window")
        return value

    @model_validator(mode="after")
    def validate_board_integrity(self) -> Self:
        expected_ref = VersionedRef(id=self.board_id, version=self.version)
        if self.reading_plan.board_ref != expected_ref:
            raise ValueError("reading_plan.board_ref must equal this board's id and version")

        faction_total = sum(self.factions.values())
        if faction_total != self.seat_count:
            raise ValueError("seat_count must equal the sum of faction counts")

        tie_policy = self.day_flow.vote.tie_policy
        if tie_policy in _TIE_POLICIES_REQUIRING_PK and not self.day_flow.pk.enabled:
            raise ValueError("day_flow.pk.enabled must be true for this tie_policy")
        if tie_policy in _TIE_POLICIES_REJECTING_PK and self.day_flow.pk.enabled:
            raise ValueError("day_flow.pk.enabled must be false for this tie_policy")

        if self.day_flow.last_words.enabled and not self.day_flow.last_words.eligible_death_causes:
            raise ValueError("eligible_death_causes must be non-empty when last_words is enabled")

        windows_by_id = {window.window_id: window for window in self.night_windows}
        if self.knife_rule.available_after_window not in windows_by_id:
            raise ValueError("knife_rule.available_after_window must reference a night window")
        if self.wolf_team_visibility.discussion_enabled and not any(
            window.phase == self.wolf_team_visibility.discussion_phase
            for window in self.night_windows
        ):
            raise ValueError("discussion_enabled requires a night window matching discussion_phase")

        role_ids = [binding.role_ref.id for binding in self.role_bindings]
        _unique(role_ids, "role_bindings role IDs")
        role_total = sum(binding.count for binding in self.role_bindings)
        if role_total != self.seat_count:
            raise ValueError("seat_count must equal the sum of role binding counts")

        role_groups = self.victory.role_groups
        if role_groups:
            bound_role_ids = set(role_ids)
            mapped_role_ids = set(role_groups)
            unknown_role_ids = sorted(mapped_role_ids - bound_role_ids)
            missing_role_ids = sorted(bound_role_ids - mapped_role_ids)
            if unknown_role_ids:
                raise ValueError(
                    "victory.role_groups contains unknown role IDs: " + ", ".join(unknown_role_ids)
                )
            if missing_role_ids:
                raise ValueError(
                    "victory.role_groups is missing role IDs: " + ", ".join(missing_role_ids)
                )

        all_dependencies = [binding.role_ref.format() for binding in self.role_bindings]
        all_dependencies.extend(reference.format() for reference in self.mechanic_refs)
        all_dependencies.extend(reference.format() for reference in self.interaction_refs)
        if len(all_dependencies) != len(set(all_dependencies)):
            raise ValueError("board dependency references must be unique")
        # IDs called ``latest`` are rejected above for explicit dependency
        # lists; role bindings need the same guard because they are models of
        # the same immutable dependency closure.
        if any(binding.role_ref.id == "latest" for binding in self.role_bindings):
            raise ValueError("role_bindings must not use the unpinned 'latest' id")
        return self

    @property
    def id(self) -> str:
        """Compatibility spelling used by board frontmatter."""

        return self.board_id

    @property
    def board_ref(self) -> VersionedRef:
        """Return this immutable board's own versioned reference."""

        return VersionedRef(id=self.board_id, version=self.version)

    @property
    def roles(self) -> list[BoardRoleBinding]:
        """Compatibility spelling for the frontmatter role collection."""

        return self.role_bindings

    @property
    def mechanics(self) -> list[VersionedRef]:
        """Compatibility spelling for compact dependency frontmatter."""

        return self.mechanic_refs

    @property
    def interactions(self) -> list[VersionedRef]:
        """Compatibility spelling for compact dependency frontmatter."""

        return self.interaction_refs

    @property
    def public_summary(self) -> str:
        """Compatibility spelling for role-style public summaries."""

        return self.summary

    @property
    def vote(self) -> VoteDefinition:
        """Return the daytime vote contract."""

        return self.day_flow.vote

    @property
    def pk(self) -> PkDefinition:
        """Return the daytime PK contract."""

        return self.day_flow.pk

    @property
    def last_words(self) -> LastWordsDefinition:
        """Return the daytime last-words contract."""

        return self.day_flow.last_words

    @property
    def sheriff(self) -> SheriffDefinition:
        """Return the optional daytime sheriff contract."""

        return self.day_flow.sheriff


__all__ = [
    "BoardDefinition",
    "BoardSummary",
    "DayFlow",
    "FactionDefinition",
    "IdentityRevealDefinition",
    "KnifeDefinition",
    "KnifeRule",
    "LastWordsDefinition",
    "LastWordsPolicy",
    "NightWindow",
    "PKDefinition",
    "PkDefinition",
    "SheriffDefinition",
    "VictoryDefinition",
    "VoteDefinition",
    "WolfTeamVisibility",
]
