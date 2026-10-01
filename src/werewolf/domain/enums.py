"""Stable domain enumerations used by the game state and event protocol."""

from enum import StrEnum, unique


@unique
class GamePhase(StrEnum):
    """Authoritative phase of a game."""

    CREATED = "CREATED"
    RULESET_READY = "RULESET_READY"
    ASSIGNED = "ASSIGNED"
    PLAYER_PREPARE = "PLAYER_PREPARE"
    NIGHT_TEAM_CHAT = "NIGHT_TEAM_CHAT"
    NIGHT_ACTION = "NIGHT_ACTION"
    NIGHT_RESOLVE = "NIGHT_RESOLVE"
    DAY_ANNOUNCE = "DAY_ANNOUNCE"
    SHERIFF_ELECTION_SPEECH = "SHERIFF_ELECTION_SPEECH"
    SHERIFF_ELECTION = "SHERIFF_ELECTION"
    SHERIFF_ELECTION_PK_SPEECH = "SHERIFF_ELECTION_PK_SPEECH"
    SHERIFF_ELECTION_PK = "SHERIFF_ELECTION_PK"
    SHERIFF_TRANSFER = "SHERIFF_TRANSFER"
    DAY_SPEECH = "DAY_SPEECH"
    VOTE = "VOTE"
    VOTE_PK_SPEECH = "VOTE_PK_SPEECH"
    VOTE_PK = "VOTE_PK"
    DAY_RESOLVE = "DAY_RESOLVE"
    TRIGGER_ACTION = "TRIGGER_ACTION"
    VICTORY_CHECK = "VICTORY_CHECK"
    FINISHED = "FINISHED"


@unique
class RunStatus(StrEnum):
    """Operational status orthogonal to the current game phase."""

    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_GM = "WAITING_GM"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


@unique
class Channel(StrEnum):
    """Visibility channel for a game event."""

    PUBLIC = "PUBLIC"
    TEAM = "TEAM"
    PRIVATE = "PRIVATE"
    GM_ONLY = "GM_ONLY"
