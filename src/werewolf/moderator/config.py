"""Strict player configuration used by the moderator start boundary.

The YAML file is an operator input, so it is deliberately kept separate from
the authoritative game state.  This module validates only the runtime
selection and the seat roster.  It does not assign roles, start runtimes, or
write a session directory.  The caller supplies the already loaded,
published :class:`BoardDefinition` and the game root; session paths are then
derived from those trusted values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Self

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from werewolf.knowledge.board import BoardDefinition
from werewolf.runtime.player_runtime import RuntimeConfig

_GAME_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
_VALUE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", re.ASCII)

NonEmptyConfigValue = StringConstraints(min_length=1, max_length=256, strict=True)
RuntimeName = Literal["pi", "scripted"]
ReasoningLevel = Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class ModeratorConfigError(ValueError):
    """A safe, user-facing error in the moderator configuration boundary."""


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class PlayerConfig(_StrictConfigModel):
    """One player entry as written in the YAML configuration.

    ``provider`` and ``model`` are required for Pi.  Scripted runtimes are
    accepted only when the parser is explicitly put in test mode, and may
    omit those Pi-only fields.  Extra fields are rejected so credentials,
    arbitrary environment variables, and user-selected session directories
    cannot silently enter the runtime boundary.
    """

    seat: int = Field(ge=1, le=64, strict=True)
    runtime: RuntimeName
    provider: str | None = None
    model: str | None = None
    reasoning: ReasoningLevel = "medium"

    @model_validator(mode="after")
    def validate_runtime_values(self) -> Self:
        if self.runtime == "pi" and (self.provider is None or self.model is None):
            raise ValueError("pi players require non-empty provider and model")
        for field_name in ("provider", "model"):
            value = getattr(self, field_name)
            if value is not None and _VALUE_PATTERN.fullmatch(value) is None:
                raise ValueError(
                    f"{field_name} must be a non-empty provider/model identifier without spaces"
                )
        return self


class PiRuntimeConfig(_StrictConfigModel):
    """Global Pi process settings from the playable configuration envelope."""

    executable: str | None = None
    compatible_version: str | None = None
    auto_compaction: bool = True
    auto_retry: bool = False

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        for name in ("executable", "compatible_version"):
            value = getattr(self, name)
            if value is not None and (not value.strip() or "\x00" in value):
                raise ValueError(f"{name} must be a non-empty string without NUL")
        return self


class PlayerSessionConfig(_StrictConfigModel):
    """A validated player entry with a host-derived, seat-scoped session."""

    seat: int = Field(ge=1, le=64, strict=True)
    runtime: RuntimeName
    provider: str | None = None
    model: str | None = None
    reasoning: ReasoningLevel = "medium"
    executable: str | None = None
    compatible_version: str | None = None
    auto_compaction: bool = True
    auto_retry: bool = False
    session_id: str = Field(min_length=1, max_length=256, strict=True)
    session_dir: Path

    def to_runtime_config(self) -> RuntimeConfig:
        """Build the runtime-neutral config consumed by ``PlayerRuntime``."""

        return RuntimeConfig(
            session_id=self.session_id,
            session_dir=self.session_dir,
            provider=self.provider,
            model=self.model,
            reasoning=self.reasoning,
            executable=self.executable,
            compatible_version=self.compatible_version,
            auto_compaction=self.auto_compaction,
            auto_retry=self.auto_retry,
        )


class PlayerConfiguration(_StrictConfigModel):
    """The complete validated roster for one game start operation."""

    game_id: str = Field(min_length=1, max_length=64, strict=True)
    game_root: Path
    players: tuple[PlayerSessionConfig, ...]

    @property
    def by_seat(self) -> dict[int, PlayerSessionConfig]:
        """Return a fresh seat index for callers that need per-seat startup."""

        return {player.seat: player for player in self.players}

    def for_seat(self, seat: int) -> PlayerSessionConfig:
        """Return one seat entry, raising a clear error for an unknown seat."""

        try:
            return self.by_seat[seat]
        except KeyError as exc:
            raise ModeratorConfigError(f"players has no configuration for seat {seat}") from exc


def _validate_game_id(value: object) -> str:
    if not isinstance(value, str) or _GAME_ID_PATTERN.fullmatch(value) is None:
        raise ModeratorConfigError(
            "configuration.game.game_id must contain only lowercase letters, digits, '-' or '_'"
        )
    return value


def _game_id_from_config(raw: Mapping[str, object], game_id: str | None) -> str:
    if game_id is not None:
        return _validate_game_id(game_id)
    game = raw.get("game")
    if not isinstance(game, Mapping):
        raise ModeratorConfigError("configuration.game is required to derive player sessions")
    return _validate_game_id(game.get("game_id"))


def _players_value(raw: Mapping[str, object]) -> Sequence[object]:
    value = raw.get("players")
    if value is None:
        raise ModeratorConfigError("configuration.players is required")
    if not isinstance(value, list):
        raise ModeratorConfigError("configuration.players must be a YAML list")
    return value


def _pi_runtime_config(raw: Mapping[str, object]) -> PiRuntimeConfig:
    """Read the optional global Pi settings without accepting hidden fields."""

    runtime = raw.get("runtime")
    if runtime is None:
        return PiRuntimeConfig()
    if not isinstance(runtime, Mapping):
        raise ModeratorConfigError("configuration.runtime must be a mapping")
    pi = runtime.get("pi", {})
    if not isinstance(pi, Mapping):
        raise ModeratorConfigError("configuration.runtime.pi must be a mapping")
    try:
        return PiRuntimeConfig.model_validate(dict(pi))
    except Exception as exc:
        raise ModeratorConfigError(f"configuration.runtime.pi is invalid: {exc}") from exc


def _safe_session_dir(game_root: Path, game_id: str, seat: int) -> Path:
    root = game_root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ModeratorConfigError("game_root must be a directory")
    candidate = (root / ".runtime" / "players" / game_id / f"seat_{seat:02d}").resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # defensive if the path layout is changed later
        raise ModeratorConfigError("derived player session directory escapes game_root") from exc
    return candidate


def parse_player_configuration(
    raw: Mapping[str, object],
    *,
    board: BoardDefinition,
    game_root: str | Path,
    game_id: str | None = None,
    allow_scripted: bool = False,
) -> PlayerConfiguration:
    """Validate player YAML and derive one isolated session path per seat.

    ``raw`` is the complete game YAML mapping.  ``board`` must be the already
    verified published board, so this function never accepts a role assignment
    from the operator and never invents one.  ``allow_scripted`` is an
    explicit test-only switch; production callers should leave it false.
    """

    if not isinstance(raw, Mapping):
        raise ModeratorConfigError("configuration root must be a mapping")
    if not isinstance(board, BoardDefinition):
        raise ModeratorConfigError("board must be a published BoardDefinition")
    resolved_game_id = _game_id_from_config(raw, game_id)
    pi_runtime = _pi_runtime_config(raw)
    values = _players_value(raw)
    if len(values) != board.seat_count:
        raise ModeratorConfigError(
            "configuration.players count "
            f"({len(values)}) must equal board.seat_count ({board.seat_count})"
        )

    try:
        root = Path(game_root).expanduser().resolve()
    except (OSError, RuntimeError, TypeError) as exc:
        raise ModeratorConfigError("game_root is not a valid path") from exc

    parsed: list[PlayerConfig] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise ModeratorConfigError(f"configuration.players[{index}] must be a mapping")
        try:
            player = PlayerConfig.model_validate(dict(value))
        except Exception as exc:
            raise ModeratorConfigError(f"configuration.players[{index}] is invalid: {exc}") from exc
        if player.runtime == "scripted" and not allow_scripted:
            raise ModeratorConfigError(
                f"configuration.players[{index}].runtime=scripted is test-only; "
                "pass allow_scripted=True explicitly"
            )
        parsed.append(player)

    seats = [player.seat for player in parsed]
    if len(set(seats)) != len(seats):
        raise ModeratorConfigError("configuration.players seat values must be unique")
    expected_seats = set(range(1, board.seat_count + 1))
    if set(seats) != expected_seats:
        raise ModeratorConfigError(
            "configuration.players seats must be exactly "
            f"1 through {board.seat_count} to match the board"
        )

    sessions = tuple(
        PlayerSessionConfig(
            seat=player.seat,
            runtime=player.runtime,
            provider=player.provider,
            model=player.model,
            reasoning=player.reasoning,
            executable=pi_runtime.executable,
            compatible_version=pi_runtime.compatible_version,
            auto_compaction=pi_runtime.auto_compaction,
            auto_retry=pi_runtime.auto_retry,
            session_id=f"{resolved_game_id}-seat-{player.seat:02d}",
            session_dir=_safe_session_dir(root, resolved_game_id, player.seat),
        )
        for player in sorted(parsed, key=lambda item: item.seat)
    )
    return PlayerConfiguration(game_id=resolved_game_id, game_root=root, players=sessions)


def load_player_configuration(
    path: str | Path,
    *,
    board: BoardDefinition,
    game_root: str | Path,
    game_id: str | None = None,
    allow_scripted: bool = False,
) -> PlayerConfiguration:
    """Read a bounded YAML file and delegate to ``parse_player_configuration``."""

    source = Path(path).expanduser().resolve()
    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise ModeratorConfigError(f"configuration could not be read: {source}") from exc
    if len(raw_bytes) > 1024 * 1024:
        raise ModeratorConfigError("configuration exceeds the 1 MiB limit")
    try:
        decoded = raw_bytes.decode("utf-8")
        value = yaml.safe_load(decoded)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ModeratorConfigError("configuration YAML is invalid") from exc
    if not isinstance(value, Mapping):
        raise ModeratorConfigError("configuration root must be a mapping")
    return parse_player_configuration(
        value,
        board=board,
        game_root=game_root,
        game_id=game_id,
        allow_scripted=allow_scripted,
    )


__all__ = [
    "ModeratorConfigError",
    "PlayerConfig",
    "PlayerConfiguration",
    "PlayerSessionConfig",
    "PiRuntimeConfig",
    "load_player_configuration",
    "parse_player_configuration",
]
