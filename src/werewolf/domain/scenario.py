"""Closed, data-only scenario definitions for experimental rule gates.

Scenario files describe bounded player responses, moderator commands, and
post-run assertions. They never name Python code, imports, callbacks, or game
state patches.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, TypeAlias, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, TypeAdapter

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.runtime.player_runtime import ResponseKind

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$", re.ASCII)
_BOARD_REF_PATTERN = re.compile(
    r"^(?P<board>[a-z][a-z0-9_-]{0,63})@(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))$",
    re.ASCII,
)
SeatNo = Annotated[int, Field(ge=1, le=64, strict=True)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2_000, strict=True)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, strict=True)]


class ScenarioError(ValueError):
    """A malformed or unsupported declarative scenario."""


class ScenarioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class ScenarioMode(StrEnum):
    FULL_GAME = "FULL_GAME"
    COMMAND = "COMMAND"


class HostCommandName(StrEnum):
    NEW = "NEW"
    NEXT = "NEXT"
    START = "START"
    STATUS = "STATUS"
    HELP = "HELP"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    PREPARE_STATUS = "PREPARE_STATUS"
    PREPARE_NEXT = "PREPARE_NEXT"
    PREPARE_RETRY = "PREPARE_RETRY"
    SAVE = "SAVE"
    FINISH = "FINISH"
    DAY_STATUS = "DAY_STATUS"
    DAY_ANNOUNCE = "DAY_ANNOUNCE"
    DAY_SPEECH_OPEN = "DAY_SPEECH_OPEN"
    DAY_SPEECH_NEXT = "DAY_SPEECH_NEXT"
    DAY_SPEECH_RETRY = "DAY_SPEECH_RETRY"
    DAY_SPEECH_CLOSE = "DAY_SPEECH_CLOSE"
    DAY_PK_SPEECH_OPEN = "DAY_PK_SPEECH_OPEN"
    DAY_PK_SPEECH_NEXT = "DAY_PK_SPEECH_NEXT"
    DAY_PK_SPEECH_RETRY = "DAY_PK_SPEECH_RETRY"
    DAY_PK_SPEECH_CLOSE = "DAY_PK_SPEECH_CLOSE"
    DAY_VOTE_OPEN = "DAY_VOTE_OPEN"
    DAY_VOTE_NEXT = "DAY_VOTE_NEXT"
    DAY_VOTE_RETRY = "DAY_VOTE_RETRY"
    DAY_VOTE_COLLECT = "DAY_VOTE_COLLECT"
    DAY_VOTE_CONFIRM = "DAY_VOTE_CONFIRM"
    DAY_PK_VOTE_OPEN = "DAY_PK_VOTE_OPEN"
    DAY_PK_VOTE_NEXT = "DAY_PK_VOTE_NEXT"
    DAY_PK_VOTE_RETRY = "DAY_PK_VOTE_RETRY"
    DAY_PK_VOTE_COLLECT = "DAY_PK_VOTE_COLLECT"
    DAY_PK_VOTE_CONFIRM = "DAY_PK_VOTE_CONFIRM"
    DAY_CONFIRM_EXILE = "DAY_CONFIRM_EXILE"
    DAY_FINISH = "DAY_FINISH"
    LAST_WORDS_STATUS = "LAST_WORDS_STATUS"
    LAST_WORDS_NEXT = "LAST_WORDS_NEXT"
    LAST_WORDS_RETRY = "LAST_WORDS_RETRY"
    NIGHT_STATUS = "NIGHT_STATUS"
    NIGHT_OPEN = "NIGHT_OPEN"
    NIGHT_ADVANCE = "NIGHT_ADVANCE"
    NIGHT_TEAM_NEXT = "NIGHT_TEAM_NEXT"
    NIGHT_TEAM_RETRY = "NIGHT_TEAM_RETRY"
    NIGHT_TEAM_AGAIN = "NIGHT_TEAM_AGAIN"
    NIGHT_PLAN_STATUS = "NIGHT_PLAN_STATUS"
    NIGHT_PLAN_NEXT = "NIGHT_PLAN_NEXT"
    NIGHT_PLAN_RETRY = "NIGHT_PLAN_RETRY"
    NIGHT_ACTION_NEXT = "NIGHT_ACTION_NEXT"
    NIGHT_ACTION_RETRY = "NIGHT_ACTION_RETRY"
    NIGHT_PENDING = "NIGHT_PENDING"
    NIGHT_AUTO_RESOLVE = "NIGHT_AUTO_RESOLVE"
    TRIGGER_STATUS = "TRIGGER_STATUS"
    TRIGGER_OPEN = "TRIGGER_OPEN"
    TRIGGER_NEXT = "TRIGGER_NEXT"
    TRIGGER_RETRY = "TRIGGER_RETRY"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    TRIGGER_AUTO_RESOLVE = "TRIGGER_AUTO_RESOLVE"
    TRIGGER_FINISH = "TRIGGER_FINISH"
    SHERIFF_STATUS = "SHERIFF_STATUS"
    SHERIFF_START = "SHERIFF_START"
    SHERIFF_SPEECH_NEXT = "SHERIFF_SPEECH_NEXT"
    SHERIFF_SPEECH_RETRY = "SHERIFF_SPEECH_RETRY"
    SHERIFF_VOTE_OPEN = "SHERIFF_VOTE_OPEN"
    SHERIFF_VOTE_NEXT = "SHERIFF_VOTE_NEXT"
    SHERIFF_VOTE_RETRY = "SHERIFF_VOTE_RETRY"
    SHERIFF_VOTE_COLLECT = "SHERIFF_VOTE_COLLECT"
    SHERIFF_VOTE_CONFIRM = "SHERIFF_VOTE_CONFIRM"
    SHERIFF_TRANSFER = "SHERIFF_TRANSFER"
    SHERIFF_BADGE_STATUS = "SHERIFF_BADGE_STATUS"
    SHERIFF_BADGE_OPEN = "SHERIFF_BADGE_OPEN"
    SHERIFF_BADGE_NEXT = "SHERIFF_BADGE_NEXT"
    SHERIFF_BADGE_RETRY = "SHERIFF_BADGE_RETRY"
    SHERIFF_BADGE_RESOLVE = "SHERIFF_BADGE_RESOLVE"
    SHERIFF_BADGE_FINISH = "SHERIFF_BADGE_FINISH"
    VICTORY_STATUS = "VICTORY_STATUS"
    VICTORY_CHECK = "VICTORY_CHECK"
    VICTORY_NIGHT_CHECK = "VICTORY_NIGHT_CHECK"


class ScriptAction(ScenarioModel):
    """One fixed action proposal; target seats and parameters are literal data."""

    action_code: Annotated[int, Field(gt=0, le=65_535, strict=True)]
    targets: tuple[SeatNo, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    reason_public: Annotated[str, StringConstraints(max_length=2_000, strict=True)] | None = None


class TurnMatch(ScenarioModel):
    """A selector over only the current seat's request and authorized projection."""

    seat: SeatNo
    role_id: Identifier | None = None
    phase: GamePhase | None = None
    round_no: Annotated[int, Field(ge=0, le=10_000, strict=True)] | None = None
    day_no: Annotated[int, Field(ge=0, le=10_000, strict=True)] | None = None
    authorized_skill_ids: tuple[Identifier, ...] = ()

    def model_post_init(self, __context: object) -> None:
        if len(set(self.authorized_skill_ids)) != len(self.authorized_skill_ids):
            raise ValueError("authorized_skill_ids must not contain duplicates")


class PlayerTurnStep(ScenarioModel):
    kind: Literal["PLAYER_TURN"]
    step_id: Identifier
    required: bool = True
    match: TurnMatch
    expected_kind: ResponseKind | None = None
    actions: tuple[ScriptAction, ...] = ()
    speech: (
        Annotated[str, StringConstraints(min_length=1, max_length=20_000, strict=True)] | None
    ) = None

    def model_post_init(self, __context: object) -> None:
        if self.actions and self.speech is not None:
            raise ValueError("a player step cannot prescribe both actions and speech")
        if self.actions and self.expected_kind not in {None, ResponseKind.ACTION}:
            raise ValueError("action script requires an ACTION response")
        if self.speech is not None and self.expected_kind not in {None, ResponseKind.SPEECH}:
            raise ValueError("speech script requires a SPEECH response")


class HostCommandStep(ScenarioModel):
    kind: Literal["HOST_COMMAND"]
    step_id: Identifier
    required: bool = True
    command: HostCommandName
    seat: SeatNo | None = None
    seats: tuple[SeatNo, ...] = ()
    target: SeatNo | Literal["NONE"] | None = None
    content: Annotated[str, StringConstraints(max_length=2_000, strict=True)] | None = None
    force: bool = False

    def model_post_init(self, __context: object) -> None:
        required = _HOST_ARGUMENTS.get(self.command, frozenset())
        supplied = {
            name
            for name in ("seat", "seats", "target", "content")
            if getattr(self, name) is not None and getattr(self, name) != ()
        }
        allowed = required | _HOST_OPTIONAL_ARGUMENTS.get(self.command, frozenset())
        if required - supplied:
            raise ValueError("host command is missing typed arguments")
        if supplied - allowed:
            raise ValueError("host command contains arguments not supported by that command")
        if self.force and self.command not in {
            HostCommandName.DAY_VOTE_COLLECT,
            HostCommandName.DAY_PK_VOTE_COLLECT,
            HostCommandName.SHERIFF_VOTE_COLLECT,
        }:
            raise ValueError("force is supported only by vote collect commands")
        if self.seats and len(set(self.seats)) != len(self.seats):
            raise ValueError("host command seats must be unique")


class AssertionField(StrEnum):
    PHASE = "phase"
    RUN_STATUS = "run_status"
    ROUND_NO = "round_no"
    DAY_NO = "day_no"
    WINNER_SIDE = "winner_side"
    ARCHIVE_VERIFIED = "archive_verified"
    SNAPSHOT_VERIFIED = "snapshot_verified"
    ALIVE = "alive"
    DEATH_CAUSE = "death_cause"
    SKILL_USE_COUNT = "skill_use_count"
    GAME_STATE_VALUE = "game_state_value"
    RESOURCE_BALANCE = "resource_balance"
    RELATION_COUNT = "relation_count"


class StateAssertionStep(ScenarioModel):
    kind: Literal["ASSERT"]
    step_id: Identifier
    required: bool = True
    field: AssertionField
    operator: Literal["EQ", "NE", "IN", "GE", "LE"]
    expected: JsonValue
    seat: SeatNo | None = None
    skill_id: Identifier | None = None
    state_key: Identifier | None = None
    resource_id: Identifier | None = None
    relation_type: Identifier | None = None

    def model_post_init(self, __context: object) -> None:
        expected_values: tuple[JsonValue, ...]
        if self.operator == "IN":
            if not isinstance(self.expected, list | tuple):
                raise ValueError("IN assertions require a collection of expected values")
            expected_values = tuple(self.expected)
        else:
            expected_values = (self.expected,)

        if self.field in {
            AssertionField.ALIVE,
            AssertionField.ARCHIVE_VERIFIED,
            AssertionField.SNAPSHOT_VERIFIED,
        } and any(type(value) is not bool for value in expected_values):
            raise ValueError("boolean assertions require boolean expected values")
        if self.field is AssertionField.PHASE and any(
            type(value) is not str or value not in GamePhase._value2member_map_
            for value in expected_values
        ):
            raise ValueError("PHASE assertions require valid phase values")
        if self.field is AssertionField.RUN_STATUS and any(
            type(value) is not str or value not in RunStatus._value2member_map_
            for value in expected_values
        ):
            raise ValueError("RUN_STATUS assertions require valid run-status values")
        if self.field in {AssertionField.ROUND_NO, AssertionField.DAY_NO} and any(
            type(value) is not int or value < 0 for value in expected_values
        ):
            raise ValueError("round and day assertions require non-negative integer values")
        if self.field in {AssertionField.DEATH_CAUSE, AssertionField.WINNER_SIDE} and any(
            value is not None and (type(value) is not str or not value) for value in expected_values
        ):
            raise ValueError("death cause and winner-side assertions require strings or null")

        expected_selectors: dict[AssertionField, frozenset[str]] = {
            AssertionField.ALIVE: frozenset({"seat"}),
            AssertionField.DEATH_CAUSE: frozenset({"seat"}),
            AssertionField.SKILL_USE_COUNT: frozenset({"skill_id"}),
            AssertionField.GAME_STATE_VALUE: frozenset({"state_key"}),
            AssertionField.RESOURCE_BALANCE: frozenset({"seat", "resource_id"}),
            AssertionField.RELATION_COUNT: frozenset({"relation_type"}),
        }
        selectors = {
            name
            for name in ("seat", "skill_id", "state_key", "resource_id", "relation_type")
            if getattr(self, name) is not None
        }
        required = expected_selectors.get(self.field, frozenset())
        if selectors != required:
            raise ValueError("assertion selectors do not match the selected typed field")
        if self.field is AssertionField.GAME_STATE_VALUE and type(self.expected) is not bool:
            raise ValueError("GAME_STATE_VALUE assertions require a boolean expected value")
        if (
            self.field
            in {
                AssertionField.SKILL_USE_COUNT,
                AssertionField.RESOURCE_BALANCE,
                AssertionField.RELATION_COUNT,
            }
            and type(self.expected) is not int
        ):
            raise ValueError("count and resource assertions require an integer expected value")


ScenarioStep: TypeAlias = Annotated[
    PlayerTurnStep | HostCommandStep | StateAssertionStep,
    Field(discriminator="kind"),
]
_STEP_ADAPTER: TypeAdapter[ScenarioStep] = TypeAdapter(ScenarioStep)


class ScenarioBudget(ScenarioModel):
    max_commands: Annotated[int, Field(ge=1, le=100_000, strict=True)] = 10_000
    max_turns: Annotated[int, Field(ge=1, le=100_000, strict=True)] = 10_000
    max_rounds: Annotated[int, Field(ge=1, le=10_000, strict=True)] = 100


class ScenarioDefinition(ScenarioModel):
    schema_version: Literal[1] = 1
    scenario_id: Identifier
    board_ref: Annotated[str, StringConstraints(min_length=7, max_length=100, strict=True)]
    mode: ScenarioMode
    budget: ScenarioBudget = Field(default_factory=ScenarioBudget)
    required_skill_ids: tuple[Identifier, ...] = ()
    required_host_skill_ids: tuple[Identifier, ...] = ()
    steps: tuple[ScenarioStep, ...] = ()

    def model_post_init(self, __context: object) -> None:
        if not _ID_PATTERN.fullmatch(self.scenario_id):
            raise ValueError("scenario_id must be a lowercase logical identifier")
        if _BOARD_REF_PATTERN.fullmatch(self.board_ref) is None:
            raise ValueError("board_ref must be pinned as board_id@X.Y.Z")
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("scenario step IDs must be unique")
        if len(set(self.required_skill_ids)) != len(self.required_skill_ids):
            raise ValueError("required_skill_ids must not contain duplicates")
        if len(set(self.required_host_skill_ids)) != len(self.required_host_skill_ids):
            raise ValueError("required_host_skill_ids must not contain duplicates")
        if self.mode is ScenarioMode.FULL_GAME and any(
            isinstance(step, HostCommandStep)
            and step.command in {HostCommandName.NEW, HostCommandName.START, HostCommandName.FINISH}
            for step in self.steps
        ):
            raise ValueError("FULL_GAME lifecycle commands are owned by the standard runner")


_HOST_ARGUMENTS: dict[HostCommandName, frozenset[str]] = {
    HostCommandName.DAY_CONFIRM_EXILE: frozenset({"target"}),
    HostCommandName.DAY_ANNOUNCE: frozenset({"content"}),
    HostCommandName.DAY_SPEECH_OPEN: frozenset({"seats"}),
    HostCommandName.DAY_PK_SPEECH_OPEN: frozenset({"seats"}),
    HostCommandName.SHERIFF_START: frozenset({"seats"}),
}
_HOST_OPTIONAL_ARGUMENTS: dict[HostCommandName, frozenset[str]] = {
    name: frozenset({"seat"})
    for name in {
        HostCommandName.DAY_VOTE_NEXT,
        HostCommandName.DAY_VOTE_RETRY,
        HostCommandName.DAY_PK_VOTE_NEXT,
        HostCommandName.DAY_PK_VOTE_RETRY,
        HostCommandName.NIGHT_ACTION_NEXT,
        HostCommandName.NIGHT_ACTION_RETRY,
        HostCommandName.TRIGGER_NEXT,
        HostCommandName.TRIGGER_RETRY,
        HostCommandName.LAST_WORDS_NEXT,
        HostCommandName.LAST_WORDS_RETRY,
        HostCommandName.SHERIFF_VOTE_NEXT,
        HostCommandName.SHERIFF_VOTE_RETRY,
    }
}


def load_scenario(path: str | Path) -> ScenarioDefinition:
    """Load JSON or safe YAML and reject duplicate keys and unknown fields."""

    source = Path(path).expanduser().resolve()
    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise ScenarioError("scenario file could not be read") from exc
    return load_scenario_bytes(raw_bytes, source_name=source.name)


def load_scenario_bytes(raw_bytes: bytes, *, source_name: str) -> ScenarioDefinition:
    """Validate scenario bytes directly so identity and staged bytes cannot diverge."""

    if not isinstance(raw_bytes, bytes):
        raise ScenarioError("scenario contents must be bytes")
    if len(raw_bytes) > 1_048_576:
        raise ScenarioError("scenario file exceeds the 1 MiB limit")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScenarioError("scenario file must be UTF-8") from exc
    try:
        if Path(source_name).suffix.lower() == ".json":
            raw: object = json.loads(
                text,
                object_pairs_hook=_json_unique_pairs,
                parse_constant=_reject_json_constant,
            )
        else:
            raw = yaml.load(text, Loader=_UniqueKeySafeLoader)
        if not isinstance(raw, Mapping):
            raise ScenarioError("scenario document must be an object")
        return ScenarioDefinition.model_validate(_materialize_wire_types(raw), strict=True)
    except ScenarioError:
        raise
    except Exception as exc:
        raise ScenarioError(f"scenario schema is invalid: {str(exc)[:300]}") from exc


def scenario_sha256(scenario: ScenarioDefinition) -> str:
    """Return the stable identity digest of canonical scenario data."""

    raw = json.dumps(
        scenario.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _json_unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ScenarioError(f"scenario JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ScenarioError(f"scenario JSON contains unsupported numeric constant {value}")


def _materialize_wire_types(raw: Mapping[str, object]) -> dict[str, object]:
    """Convert only JSON/YAML list and enum wire forms before strict validation."""

    data = dict(raw)
    if isinstance(data.get("mode"), str):
        data["mode"] = ScenarioMode(cast(str, data["mode"]))
    required_skill_ids = data.get("required_skill_ids")
    if isinstance(required_skill_ids, list):
        data["required_skill_ids"] = tuple(required_skill_ids)
    required_host_skill_ids = data.get("required_host_skill_ids")
    if isinstance(required_host_skill_ids, list):
        data["required_host_skill_ids"] = tuple(required_host_skill_ids)
    steps = data.get("steps")
    if isinstance(steps, list):
        data["steps"] = tuple(_materialize_step(step) for step in steps)
    return data


def _materialize_step(raw: object) -> object:
    if not isinstance(raw, Mapping):
        return raw
    data = dict(raw)
    kind = data.get("kind")
    if kind == "PLAYER_TURN":
        matcher = data.get("match")
        if isinstance(matcher, Mapping):
            match_data = dict(matcher)
            if isinstance(match_data.get("phase"), str):
                match_data["phase"] = GamePhase(cast(str, match_data["phase"]))
            skill_ids = match_data.get("authorized_skill_ids")
            if isinstance(skill_ids, list):
                match_data["authorized_skill_ids"] = tuple(skill_ids)
            data["match"] = match_data
        actions = data.get("actions")
        if isinstance(actions, list):
            data["actions"] = tuple(
                {
                    **dict(action),
                    **(
                        {"targets": tuple(action.get("targets", ()))}
                        if isinstance(action, Mapping)
                        and isinstance(action.get("targets", ()), list)
                        else {}
                    ),
                }
                if isinstance(action, Mapping)
                else action
                for action in actions
            )
        if isinstance(data.get("expected_kind"), str):
            data["expected_kind"] = ResponseKind(cast(str, data["expected_kind"]))
    elif kind == "HOST_COMMAND":
        if isinstance(data.get("command"), str):
            data["command"] = HostCommandName(cast(str, data["command"]))
        seats = data.get("seats")
        if isinstance(seats, list):
            data["seats"] = tuple(seats)
    elif kind == "ASSERT" and isinstance(data.get("field"), str):
        data["field"] = AssertionField(cast(str, data["field"]))
    return data


def build_host_command(step: HostCommandStep) -> str:
    """Render a typed moderator command using only the existing shell grammar."""

    command = step.command
    fixed: dict[HostCommandName, str] = {
        HostCommandName.NEW: "new",
        HostCommandName.NEXT: "next",
        HostCommandName.START: "start",
        HostCommandName.STATUS: "status",
        HostCommandName.HELP: "help",
        HostCommandName.PAUSE: "pause",
        HostCommandName.RESUME: "resume",
        HostCommandName.PREPARE_STATUS: "prepare status",
        HostCommandName.PREPARE_NEXT: "prepare next",
        HostCommandName.PREPARE_RETRY: "prepare retry",
        HostCommandName.SAVE: "save",
        HostCommandName.FINISH: "finish",
        HostCommandName.DAY_STATUS: "day status",
        HostCommandName.DAY_FINISH: "day finish",
        HostCommandName.DAY_SPEECH_NEXT: "day speech next",
        HostCommandName.DAY_SPEECH_RETRY: "day speech retry",
        HostCommandName.DAY_SPEECH_CLOSE: "day speech close",
        HostCommandName.DAY_PK_SPEECH_NEXT: "day pk speech next",
        HostCommandName.DAY_PK_SPEECH_RETRY: "day pk speech retry",
        HostCommandName.DAY_PK_SPEECH_CLOSE: "day pk speech close",
        HostCommandName.DAY_VOTE_OPEN: "day vote open",
        HostCommandName.DAY_VOTE_COLLECT: "day vote collect",
        HostCommandName.DAY_VOTE_CONFIRM: "day vote confirm",
        HostCommandName.DAY_PK_VOTE_OPEN: "day pk vote open",
        HostCommandName.DAY_PK_VOTE_COLLECT: "day pk vote collect",
        HostCommandName.DAY_PK_VOTE_CONFIRM: "day pk vote confirm",
        HostCommandName.NIGHT_STATUS: "night status",
        HostCommandName.NIGHT_OPEN: "night open",
        HostCommandName.NIGHT_ADVANCE: "night advance",
        HostCommandName.NIGHT_TEAM_NEXT: "night team next",
        HostCommandName.NIGHT_TEAM_RETRY: "night team retry",
        HostCommandName.NIGHT_TEAM_AGAIN: "night team again",
        HostCommandName.NIGHT_PLAN_STATUS: "night plan status",
        HostCommandName.NIGHT_PLAN_NEXT: "night plan next",
        HostCommandName.NIGHT_PLAN_RETRY: "night plan retry",
        HostCommandName.NIGHT_PENDING: "night pending",
        HostCommandName.NIGHT_AUTO_RESOLVE: "night auto-resolve",
        HostCommandName.TRIGGER_STATUS: "trigger status",
        HostCommandName.TRIGGER_OPEN: "trigger open",
        HostCommandName.TRIGGER_PENDING: "trigger pending",
        HostCommandName.TRIGGER_AUTO_RESOLVE: "trigger auto-resolve",
        HostCommandName.TRIGGER_FINISH: "trigger finish",
        HostCommandName.SHERIFF_STATUS: "sheriff status",
        HostCommandName.SHERIFF_SPEECH_NEXT: "sheriff speech next",
        HostCommandName.SHERIFF_SPEECH_RETRY: "sheriff speech retry",
        HostCommandName.SHERIFF_VOTE_OPEN: "sheriff vote open",
        HostCommandName.SHERIFF_VOTE_COLLECT: "sheriff vote collect",
        HostCommandName.SHERIFF_VOTE_CONFIRM: "sheriff vote confirm",
        HostCommandName.SHERIFF_TRANSFER: "sheriff transfer",
        HostCommandName.SHERIFF_BADGE_STATUS: "sheriff badge status",
        HostCommandName.SHERIFF_BADGE_OPEN: "sheriff badge open",
        HostCommandName.SHERIFF_BADGE_NEXT: "sheriff badge next",
        HostCommandName.SHERIFF_BADGE_RETRY: "sheriff badge retry",
        HostCommandName.SHERIFF_BADGE_RESOLVE: "sheriff badge resolve",
        HostCommandName.SHERIFF_BADGE_FINISH: "sheriff badge finish",
        HostCommandName.VICTORY_STATUS: "victory status",
        HostCommandName.VICTORY_CHECK: "victory check",
        HostCommandName.VICTORY_NIGHT_CHECK: "victory night-check",
    }
    if command is HostCommandName.DAY_ANNOUNCE:
        assert step.content is not None
        return f"day announce {step.content}"
    if command in {HostCommandName.DAY_SPEECH_OPEN, HostCommandName.DAY_PK_SPEECH_OPEN}:
        prefix = (
            "day speech open"
            if command is HostCommandName.DAY_SPEECH_OPEN
            else "day pk speech open"
        )
        return f"{prefix} {' '.join(str(seat) for seat in step.seats)}"
    if command is HostCommandName.DAY_CONFIRM_EXILE:
        return f"day confirm-exile {step.target.lower() if step.target == 'NONE' else step.target}"
    if command is HostCommandName.SHERIFF_START:
        return "sheriff start " + " ".join(str(seat) for seat in step.seats)
    seat_commands: dict[HostCommandName, str] = {
        HostCommandName.DAY_VOTE_NEXT: "day vote next",
        HostCommandName.DAY_VOTE_RETRY: "day vote retry",
        HostCommandName.DAY_PK_VOTE_NEXT: "day pk vote next",
        HostCommandName.DAY_PK_VOTE_RETRY: "day pk vote retry",
        HostCommandName.NIGHT_ACTION_NEXT: "night action next",
        HostCommandName.NIGHT_ACTION_RETRY: "night action retry",
        HostCommandName.TRIGGER_NEXT: "trigger next",
        HostCommandName.TRIGGER_RETRY: "trigger retry",
        HostCommandName.LAST_WORDS_NEXT: "last-words next",
        HostCommandName.LAST_WORDS_RETRY: "last-words retry",
        HostCommandName.SHERIFF_VOTE_NEXT: "sheriff vote next",
        HostCommandName.SHERIFF_VOTE_RETRY: "sheriff vote retry",
    }
    if command in seat_commands:
        return seat_commands[command] + (f" {step.seat}" if step.seat is not None else "")
    if (
        command
        in {
            HostCommandName.DAY_VOTE_COLLECT,
            HostCommandName.DAY_PK_VOTE_COLLECT,
            HostCommandName.SHERIFF_VOTE_COLLECT,
        }
        and step.force
    ):
        return fixed[command] + " --force"
    try:
        return fixed[command]
    except KeyError as exc:
        raise ScenarioError(f"unsupported host command: {command.value}") from exc


def host_command_matches(step: HostCommandStep, line: str) -> bool:
    """Compare a script step with one command observed at the shell boundary."""

    try:
        return build_host_command(step) == line.strip()
    except ScenarioError:
        return False


class _UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    def construct_mapping(self, node: object, deep: bool = False) -> dict[object, object]:
        if not isinstance(node, yaml.MappingNode):
            return cast(dict[object, object], super().construct_mapping(node, deep=deep))
        self.flatten_mapping(node)
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a scenario",
                    node.start_mark,
                    "mapping keys must be hashable",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a scenario",
                    node.start_mark,
                    f"duplicate mapping key {key!r}",
                    key_node.start_mark,
                )
        return cast(dict[object, object], super().construct_mapping(node, deep=deep))


__all__ = [
    "AssertionField",
    "HostCommandName",
    "HostCommandStep",
    "PlayerTurnStep",
    "ScenarioBudget",
    "ScenarioDefinition",
    "ScenarioError",
    "ScenarioMode",
    "ScenarioStep",
    "ScriptAction",
    "StateAssertionStep",
    "TurnMatch",
    "build_host_command",
    "host_command_matches",
    "load_scenario",
    "load_scenario_bytes",
    "scenario_sha256",
]
