"""Pure victory evaluation against a published, frozen board definition.

The game manager owns state transitions.  This module only answers whether a
state contains one or more *victory candidates*.  In particular, it never
chooses an order for simultaneous candidates when the board does not publish
one.  The caller must turn a single unambiguous candidate into ``state.winner``
and must ask the moderator to resolve an ambiguous result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.preview import experimental_preview_enabled

from .state import GameState, PlayerState

VictoryStatus = Literal["ONGOING", "WINNER", "PENDING_MODERATOR"]


class VictoryEvaluationError(ValueError):
    """Base error for an unusable board or an inconsistent game state."""


class UnpublishedBoardError(VictoryEvaluationError):
    """Raised when victory is evaluated against a non-published board."""


class UnreviewedBoardError(VictoryEvaluationError):
    """Raised when the board still carries a review placeholder."""


class RulesetMismatchError(VictoryEvaluationError):
    """Raised when the board is not the frozen ruleset for the game."""


class UnsupportedVictoryModeError(VictoryEvaluationError):
    """Raised when the published mode is not implemented by this runtime."""


class InvalidVictoryDefinitionError(VictoryEvaluationError):
    """Raised when the board omits required machine-readable conditions."""


@dataclass(frozen=True, slots=True)
class VictoryCandidate:
    """One independently satisfied, board-declared winning condition."""

    side: str
    condition: str


@dataclass(frozen=True, slots=True)
class VictoryEvaluation:
    """The result of a pure victory check.

    ``winner`` is populated only for one unambiguous candidate.  A result with
    two candidates deliberately has ``winner is None`` and
    ``requires_moderator=True``; this prevents a Python implementation from
    silently inventing a priority absent from the board snapshot.
    """

    status: VictoryStatus
    candidates: tuple[VictoryCandidate, ...] = ()
    winner: str | None = None
    reasons: tuple[str, ...] = ()
    requires_moderator: bool = False

    @property
    def winner_side(self) -> str | None:
        """Compatibility spelling for callers using the domain terminology."""

        return self.winner

    @property
    def candidate_sides(self) -> tuple[str, ...]:
        """Return distinct candidate side IDs in stable board order."""

        return tuple(candidate.side for candidate in self.candidates)

    @property
    def is_terminal(self) -> bool:
        """Whether this result has a single winner that may be committed."""

        return self.status == "WINNER"


# The IDs below describe the semantic vocabulary used by the published board
# contract.  They are aliases only; role counts and role names are never
# hard-coded here.
_WOLF_SIDE_IDS = frozenset({"wolf", "wolves", "werewolf", "werewolves"})
_GOOD_SIDE_IDS = frozenset({"good", "village", "town", "villagers", "human"})
_GOD_GROUP_IDS = frozenset({"god", "gods", "神职", "神民"})
_VILLAGER_GROUP_IDS = frozenset({"villager", "villagers", "civilian", "民", "平民"})
_WOLF_GROUP_IDS = _WOLF_SIDE_IDS

_CONDITION_ALIASES: dict[str, tuple[str, str]] = {
    "good_wins_when_all_wolves_are_dead": ("good", "all_wolves_dead"),
    "good_when_all_wolves_are_dead": ("good", "all_wolves_dead"),
    "all_wolves_are_dead": ("good", "all_wolves_dead"),
    "wolves_win_when_all_gods_are_dead": ("wolf", "all_gods_dead"),
    "wolf_wins_when_all_gods_are_dead": ("wolf", "all_gods_dead"),
    "all_gods_are_dead": ("wolf", "all_gods_dead"),
    "wolves_win_when_all_villagers_are_dead": ("wolf", "all_villagers_dead"),
    "wolf_wins_when_all_villagers_are_dead": ("wolf", "all_villagers_dead"),
    "all_villagers_are_dead": ("wolf", "all_villagers_dead"),
    "wolf_side_requires_surviving_wolf": ("wolf", "requires_surviving_wolf"),
}
_SURVIVING_WOLF_REQUIREMENT = "requires_surviving_wolf"
_REVIEWER_PLACEHOLDERS = frozenset(
    {
        "anonymous",
        "auto",
        "automated",
        "none",
        "null",
        "n-a",
        "na",
        "pending",
        "pending-human-review",
        "pending-review",
        "system",
        "tbd",
        "todo",
        "unknown",
        "unspecified",
    }
)


def _normalise(value: object) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _published_board(board: BoardDefinition) -> BoardDefinition:
    """Validate the narrow board contract used by this pure evaluator."""

    if not isinstance(board, BoardDefinition):
        raise TypeError("board must be a frozen BoardDefinition")
    # ``model_construct`` is useful for corrupt-snapshot tests, so retain this
    # explicit check instead of trusting the Literal validation alone.
    if board.status != "published":
        raise UnpublishedBoardError("victory evaluation requires a published board")
    reviewed_by = getattr(board, "reviewed_by", None)
    reviewer = (
        "-".join(reviewed_by.split()).casefold().replace("_", "-")
        if isinstance(reviewed_by, str)
        else ""
    )
    if reviewer in _REVIEWER_PLACEHOLDERS and not experimental_preview_enabled():
        raise UnreviewedBoardError("victory evaluation requires a human-reviewed published board")
    return board


def _bound_board(state: GameState, board: BoardDefinition) -> None:
    ruleset = state.ruleset
    if ruleset is None:
        raise RulesetMismatchError("victory evaluation requires a frozen game ruleset")
    if ruleset.board_id != board.board_id or ruleset.version != board.version:
        raise RulesetMismatchError("board does not match the game's frozen ruleset")


def _side_id(board: BoardDefinition, semantic_side: str) -> str | None:
    aliases = _WOLF_SIDE_IDS if semantic_side == "wolf" else _GOOD_SIDE_IDS
    configured = tuple(board.victory.winning_sides) + tuple(board.factions)
    for candidate in configured:
        if _normalise(candidate) in aliases:
            return candidate
    # A configured winning side may have a board-specific ID.  It is safe to
    # use it only when there is exactly one candidate with a known semantic
    # alias; otherwise the board is underspecified and must go to the GM.
    return None


def _condition_name(raw: str) -> tuple[str, str] | None:
    key = _normalise(raw)
    if key in _CONDITION_ALIASES:
        return _CONDITION_ALIASES[key]
    return None


def _role_group_map(
    board: BoardDefinition,
    explicit: Mapping[str, str] | None,
) -> Mapping[str, str]:
    # A non-empty mapping in the frozen victory definition is authoritative.
    # The optional argument remains for legacy boards that predate this field
    # and for callers that have not yet migrated their board snapshot.
    board_groups = getattr(board.victory, "role_groups", None)
    if isinstance(board_groups, Mapping) and board_groups:
        return board_groups
    if explicit is not None:
        return explicit
    return {}


def _semantic_group(
    player: PlayerState,
    role_groups: Mapping[str, str],
) -> str | None:
    mapped = role_groups.get(player.role_id)
    if mapped is not None:
        value = _normalise(mapped)
        if value in _WOLF_GROUP_IDS:
            return "wolf"
        if value in _GOD_GROUP_IDS:
            return "god"
        if value in _VILLAGER_GROUP_IDS:
            return "villager"
        if value in _GOOD_SIDE_IDS:
            return "good"
        return None

    faction = _normalise(player.faction_id)
    if faction in _WOLF_GROUP_IDS:
        return "wolf"
    if faction in _GOD_GROUP_IDS:
        return "god"
    if faction in _VILLAGER_GROUP_IDS:
        return "villager"
    if faction in _GOOD_SIDE_IDS:
        # A broad ``good`` faction does not tell us whether this player is a
        # god or a villager.  Keep it broad and let the caller provide the
        # board's role-group mapping rather than guessing from role names.
        return "good"
    return None


def evaluate_victory(
    state: GameState,
    board: BoardDefinition,
    *,
    role_groups: Mapping[str, str] | None = None,
) -> VictoryEvaluation:
    """Compute board-declared victory candidates without mutating ``state``.

    ``role_groups`` is an optional immutable mapping from role ID to one of
    ``wolf``, ``god``, ``villager`` or ``good``.  It is useful for boards whose
    player state stores one broad good faction while their victory rule uses
    the two sides of the good team.  If that information is unavailable, the
    function returns a moderator-pending result instead of guessing.
    """

    if not isinstance(state, GameState):
        raise TypeError("state must be a GameState")
    board = _published_board(board)
    _bound_board(state, board)
    if board.victory.mode != "eliminate_side":
        raise UnsupportedVictoryModeError(f"unsupported victory mode: {board.victory.mode!r}")

    parsed: list[tuple[str, str]] = []
    unknown: list[str] = []
    for raw in board.victory.special_conditions:
        parsed_condition = _condition_name(raw)
        if parsed_condition is None:
            unknown.append(raw)
        else:
            parsed.append(parsed_condition)
    terminal_conditions = tuple(item for item in parsed if item[1] != _SURVIVING_WOLF_REQUIREMENT)
    if not terminal_conditions:
        raise InvalidVictoryDefinitionError(
            "eliminate_side requires explicit machine-readable special_conditions"
        )

    players = tuple(state.players.values())
    role_groups_map = _role_group_map(board, role_groups)
    grouped = tuple((player, _semantic_group(player, role_groups_map)) for player in players)
    wolves = tuple(player for player, group in grouped if group == "wolf")
    wolf_side_eligible = any(player.alive for player in wolves)
    missing_groups = {group for _, group in grouped if group is None}
    candidate_conditions: dict[str, list[str]] = {}
    reasons: list[str] = []

    for semantic_side, condition in parsed:
        if condition == _SURVIVING_WOLF_REQUIREMENT:
            continue
        side = _side_id(board, semantic_side)
        if side is None:
            reasons.append(f"MISSING_{semantic_side.upper()}_SIDE_ID")
            continue
        if condition == "all_wolves_dead":
            if not wolves:
                reasons.append("NO_WOLF_PLAYERS")
            elif all(not player.alive for player in wolves):
                candidate_conditions.setdefault(side, []).append(condition)
            continue

        if not wolves:
            reasons.append("NO_WOLF_PLAYERS")
            continue
        if not wolf_side_eligible:
            # A dead wolf team cannot satisfy either of the wolf-side
            # conditions, so an unclassified good role cannot create a false
            # moderator conflict with a good-side victory.
            continue
        required_group = "god" if condition == "all_gods_dead" else "villager"
        members = tuple(player for player, group in grouped if group == required_group)
        if not members:
            reasons.append(f"MISSING_{required_group.upper()}_ROLE_GROUP")
        elif any(player.alive for player in wolves) and all(not player.alive for player in members):
            candidate_conditions.setdefault(side, []).append(condition)

    if unknown:
        reasons.append("UNSUPPORTED_SPECIAL_CONDITIONS")
    requires_good_group = any(
        semantic_side == "wolf" and condition in {"all_gods_dead", "all_villagers_dead"}
        for semantic_side, condition in terminal_conditions
    )
    unclassified_good = any(group == "good" for _, group in grouped)
    if (missing_groups or unclassified_good) and requires_good_group and wolf_side_eligible:
        reasons.append("UNCLASSIFIED_PLAYER_GROUP")

    # Preserve the first occurrence order from the frozen board while merging
    # multiple satisfied conditions belonging to the same winning side.
    unique_candidates = tuple(
        VictoryCandidate(side, conditions[0]) for side, conditions in candidate_conditions.items()
    )
    if len(unique_candidates) > 1:
        return VictoryEvaluation(
            status="PENDING_MODERATOR",
            candidates=unique_candidates,
            reasons=tuple(dict.fromkeys([*reasons, "SIMULTANEOUS_WIN_CONDITIONS"])),
            requires_moderator=True,
        )
    if len(unique_candidates) == 1 and not reasons:
        return VictoryEvaluation(
            status="WINNER",
            candidates=unique_candidates,
            winner=unique_candidates[0].side,
        )
    if unique_candidates:
        return VictoryEvaluation(
            status="PENDING_MODERATOR",
            candidates=unique_candidates,
            reasons=tuple(dict.fromkeys(reasons)),
            requires_moderator=True,
        )
    if reasons:
        return VictoryEvaluation(
            status="PENDING_MODERATOR",
            reasons=tuple(dict.fromkeys(reasons)),
            requires_moderator=True,
        )
    return VictoryEvaluation(status="ONGOING")


def compute_victory_candidates(
    state: GameState,
    board: BoardDefinition,
    *,
    role_groups: Mapping[str, str] | None = None,
) -> VictoryEvaluation:
    """Named entry point for callers that treat victory as candidate finding."""

    return evaluate_victory(state, board, role_groups=role_groups)


calculate_victory = evaluate_victory


__all__ = [
    "VictoryCandidate",
    "VictoryEvaluation",
    "VictoryEvaluationError",
    "UnpublishedBoardError",
    "UnreviewedBoardError",
    "RulesetMismatchError",
    "UnsupportedVictoryModeError",
    "InvalidVictoryDefinitionError",
    "calculate_victory",
    "compute_victory_candidates",
    "evaluate_victory",
]
