"""Dependency-free domain primitives for Werewolf Arena."""

from .enums import Channel, GamePhase, RunStatus
from .victory_rules import VictoryProgram

__all__ = ["Channel", "GamePhase", "RunStatus", "VictoryProgram"]
