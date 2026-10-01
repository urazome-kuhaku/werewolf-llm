"""The V1 moderator process and its deliberately small command loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shlex
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import JsonValue

from werewolf.domain.enums import GamePhase, RunStatus
from werewolf.game import (
    ALLOWED_PHASE_TRANSITIONS,
    ActionResolution,
    DayCoordinatorError,
    EventCommitError,
    GameManager,
    GameState,
    RandomStateRef,
    RulesetRef,
    load_action_registry,
)
from werewolf.game.setup import build_role_assignment_plan
from werewolf.game.victory import evaluate_victory
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.runtime_loader import (
    RuntimeKnowledgeBundle,
    load_runtime_knowledge_bundle_from_snapshot,
)
from werewolf.knowledge.snapshot import KnowledgeSnapshot, KnowledgeSnapshotBuilder
from werewolf.moderator.badge_flow import ModeratorBadgeError, ModeratorSheriffBadgeFlow
from werewolf.moderator.config import parse_player_configuration
from werewolf.moderator.day_flow import ModeratorDayFlow
from werewolf.moderator.last_words import LastWordsError, LastWordsFlow
from werewolf.moderator.night_flow import ModeratorNightError, ModeratorNightFlow
from werewolf.moderator.prepare_flow import PlayerPrepareError, PlayerPrepareFlow
from werewolf.moderator.sessions import PlayerSessionError, PlayerSessionService
from werewolf.moderator.sheriff_flow import ModeratorSheriffError, ModeratorSheriffFlow
from werewolf.moderator.trigger_flow import ModeratorTriggerError, ModeratorTriggerFlow
from werewolf.persistence.archive import GameArchiveStore
from werewolf.persistence.snapshot import FrozenRulesetSnapshot, GameSnapshotStore


class ModeratorError(RuntimeError):
    """A safe, user-facing moderator command failure."""


CommandInput = Callable[[], str]
CommandOutput = Callable[[str], None]
Clock = Callable[[], datetime]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _now() -> datetime:
    return datetime.now(UTC)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ModeratorError(f"configuration {name} must be a mapping")
    return value


def _load_config(path: Path) -> dict[str, object]:
    try:
        raw = path.expanduser().resolve().read_bytes()
    except OSError as exc:
        raise ModeratorError(f"configuration could not be read: {path}") from exc
    if len(raw) > 1024 * 1024:
        raise ModeratorError("configuration exceeds the 1 MiB limit")
    try:
        value = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ModeratorError("configuration YAML is invalid") from exc
    config = dict(_mapping(value, "root"))
    if config.get("schema_version") != 1:
        raise ModeratorError("configuration schema_version must be 1")
    game = _mapping(config.get("game"), "game")
    board = _mapping(game.get("board"), "game.board")
    board_id = board.get("id")
    board_version = board.get("version")
    if not isinstance(board_id, str) or not board_id.strip():
        raise ModeratorError("configuration.game.board.id must be a non-empty string")
    if not isinstance(board_version, str) or not board_version.strip():
        raise ModeratorError("configuration.game.board.version must be a non-empty string")
    game_id = game.get("game_id")
    if not isinstance(game_id, str) or not game_id.strip():
        raise ModeratorError("configuration.game.game_id must be a non-empty string")
    config["game"] = dict(game)
    return config


def _path_option(config: Mapping[str, object], *keys: str) -> Path | None:
    """Read optional path knobs without accepting arbitrary nested objects."""

    for parent_name in ("paths", "knowledge", "game"):
        parent = config.get(parent_name)
        if not isinstance(parent, dict):
            continue
        for key in keys:
            value = parent.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value).expanduser()
    return None


def _thaw_state_data(state: GameState) -> dict[str, Any]:
    """Turn frozen JSON extension values into data accepted by Pydantic."""

    data = state.model_dump(mode="python", warnings=False)
    for name in (
        "action_windows",
        "action_requests",
        "pending_resolution",
        "knowledge_receipts",
        "winner",
        "last_snapshot",
        "vote_state",
    ):
        data[name] = json.loads(json.dumps(data[name]))
    for name in ("resolutions", "knowledge_receipts", "moderator_audit"):
        data[name] = tuple(json.loads(json.dumps(data[name])))
    return data


def _active_window(window: object) -> bool:
    if not isinstance(window, Mapping):
        return True
    status = window.get("status")
    if isinstance(status, str) and status.upper() in {
        "OPEN",
        "REQUESTED",
        "SUBMITTING",
        "IN_FLIGHT",
    }:
        return True
    # ActionWindow has no status field in its typed shape.  An opened window
    # without a closed_at timestamp is still an in-flight boundary.
    return window.get("closed_at") is None and window.get("opened_at") is not None


def _active_request(request: object) -> bool:
    if not isinstance(request, Mapping):
        return True
    status = request.get("status")
    return not isinstance(status, str) or status.upper() in {
        "OPEN",
        "REQUESTED",
        "SUBMITTING",
        "IN_FLIGHT",
        "PENDING",
    }


def _timeout_seconds(config: Mapping[str, object] | None, key: str) -> float | None:
    """Read one optional game timeout and reject ambiguous values early."""

    if config is None:
        return None
    game = config.get("game")
    if not isinstance(game, Mapping):
        return None
    timeout = game.get("timeout")
    if timeout is None:
        return None
    if not isinstance(timeout, Mapping):
        raise ModeratorError("configuration.game.timeout must be a mapping")
    value = timeout.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModeratorError(f"configuration.game.timeout.{key} must be a positive number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ModeratorError(f"configuration.game.timeout.{key} must be a positive number")
    return seconds


class ModeratorShell:
    """One process-wide moderator authority and command interpreter."""

    _NOT_IMPLEMENTED = frozenset({"resolve", "retry", "extend", "team"})
    _DAY_HELP = (
        "day status",
        "day announce [content]",
        "day speech open [seat ...]",
        "day speech next",
        "day speech retry",
        "day speech close",
        "day pk speech open [seat ...]",
        "day pk speech next",
        "day pk speech retry",
        "day pk speech close",
        "day vote open",
        "day vote next [seat]",
        "day vote retry [seat]",
        "day vote collect [--force]",
        "day vote confirm",
        "day pk vote open",
        "day pk vote next [seat]",
        "day pk vote retry [seat]",
        "day pk vote collect [--force]",
        "day pk vote confirm",
        "day confirm-exile <seat|none>",
        "day finish",
        "last-words status",
        "last-words next [seat]",
        "last-words retry [seat]",
    )
    _NIGHT_HELP = (
        "night status",
        "night open",
        "night advance",
        "night team next",
        "night team retry",
        "night team again",
        "night action next [seat]",
        "night action retry [seat]",
        "night pending",
        "night resolve <json-file>",
        "night auto-resolve",
    )
    _TRIGGER_HELP = (
        "trigger status",
        "trigger open",
        "trigger next [seat]",
        "trigger retry [seat]",
        "trigger pending",
        "trigger resolve <json-file>",
        "trigger auto-resolve",
        "trigger finish",
    )
    _PREPARE_HELP = (
        "prepare status",
        "prepare next [seat]",
        "prepare retry [seat]",
    )
    _SHERIFF_HELP = (
        "sheriff status",
        "sheriff start <candidate seat ...>",
        "sheriff speech next",
        "sheriff speech retry",
        "sheriff vote open",
        "sheriff vote next [seat]",
        "sheriff vote retry [seat]",
        "sheriff vote collect [--force]",
        "sheriff vote confirm",
        "sheriff transfer",
        "sheriff badge status",
        "sheriff badge open",
        "sheriff badge next",
        "sheriff badge retry",
        "sheriff badge resolve",
        "sheriff badge finish",
    )
    _VICTORY_HELP = (
        "victory status",
        "victory check [candidate-side]",
        "victory night-check",
    )

    def __init__(
        self,
        config_path: str | Path,
        *,
        input_fn: CommandInput | None = None,
        output_fn: CommandOutput | None = None,
        clock: Clock | None = None,
        runtime_factory: Any | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.input_fn = input_fn or input
        self.output_fn = output_fn or print
        self.clock = clock or _now
        self.config: dict[str, object] | None = None
        self.manager: GameManager | None = None
        self.session_refs: dict[int, str] = {}
        self.ruleset_snapshot: KnowledgeSnapshot | None = None
        self.games_root: Path | None = None
        self.active_root: Path | None = None
        self.snapshot_store: GameSnapshotStore | None = None
        self.archive_store: GameArchiveStore | None = None
        self.runtime_factory = runtime_factory
        self.session_service: PlayerSessionService | None = None
        self.runtime_bundle: RuntimeKnowledgeBundle | None = None
        self.assignment_plan: object | None = None
        self._day_flow: ModeratorDayFlow | None = None
        self._night_flow: ModeratorNightFlow | None = None
        self._prepare_flow: PlayerPrepareFlow | None = None
        self._last_words_flow: LastWordsFlow | None = None
        self._sheriff_flow: ModeratorSheriffFlow | None = None
        self._badge_flow: ModeratorSheriffBadgeFlow | None = None
        self._trigger_flow: ModeratorTriggerFlow | None = None
        self._closed = False

    @property
    def state(self) -> GameState:
        if self.manager is None:
            raise ModeratorError("no game is loaded; run new first")
        return self.manager.state

    async def new(self) -> dict[str, object]:
        """Load a published compiled package and create a frozen game ruleset."""

        if self.manager is not None:
            raise ModeratorError("a game is already loaded in this moderator process")
        config = _load_config(self.config_path)
        game = _mapping(config["game"], "game")
        board = _mapping(game["board"], "game.board")
        game_id = str(game["game_id"])
        board_ref = VersionedRef.parse(f"{board['id']}@{board['version']}")
        root = _project_root()
        compiled_root = _path_option(config, "compiled_root", "compiled", "compiled_store")
        games_root = _path_option(config, "games_root", "games")
        compiled = (root / "vault" / "compiled") if compiled_root is None else compiled_root
        games = (root / "games") if games_root is None else games_root
        if not compiled.is_absolute():
            compiled = (self.config_path.parent / compiled).resolve()
        else:
            compiled = compiled.resolve()
        if not games.is_absolute():
            games = (self.config_path.parent / games).resolve()
        else:
            games = games.resolve()
        active = games / "active" / game_id
        if active.exists() or active.is_symlink():
            raise ModeratorError(f"active game directory already exists: {game_id}")

        # Loading verifies the package identity and its complete dependency
        # closure before the immutable per-game snapshot is materialized.
        compiled_store = CompiledKnowledgeStore(compiled)
        try:
            package = await compiled_store.load(board_ref, expected_board_ref=board_ref)
        except Exception as exc:
            raise ModeratorError(
                f"published compiled ruleset is unavailable or invalid: {board_ref.format()}"
            ) from exc
        active.mkdir(parents=True, exist_ok=False)
        (active / "private").mkdir()
        (active / "snapshots").mkdir()
        try:
            builder = KnowledgeSnapshotBuilder.from_game_root(compiled_store, active)
            snapshot = await builder.create(
                game_id,
                package,
                expected_board_ref=board_ref,
                created_at=self.clock(),
            )
        except Exception as exc:
            # No partially initialized game is left when snapshot creation
            # fails.  The directory only contains fresh, empty subdirectories.
            for child in sorted(active.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            active.rmdir()
            raise ModeratorError("could not create the frozen ruleset snapshot") from exc

        ruleset = RulesetRef(
            board_id=board_ref.id,
            version=board_ref.version,
            snapshot_id=snapshot.snapshot_id,
            manifest_sha256=snapshot.manifest_sha256,
        )
        timestamp = self.clock()
        seed_value = game.get("seed", 0)
        if type(seed_value) is not int:
            raise ModeratorError("configuration.game.seed must be an integer")
        audit: dict[str, JsonValue] = {
            "operation": "NEW",
            "moderator_id": "human",
            "command": "new",
            "reason": "game created",
            "base_revision": 0,
            "committed_revision": 1,
            "created_at": timestamp.astimezone(UTC).isoformat(),
        }
        play_setup = config.get("play_setup")
        if isinstance(play_setup, Mapping):
            for key in (
                "status",
                "board_ref",
                "source_manifest_sha256",
                "compiled_manifest_sha256",
                "candidate_manifest_sha256",
            ):
                value = play_setup.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    audit[f"play_setup_{key}"] = value
        state = GameState(
            game_id=game_id,
            created_at=timestamp,
            updated_at=timestamp,
            ruleset=ruleset,
            rng=RandomStateRef(seed=seed_value),
            run_status=RunStatus.READY,
            moderator_audit=(audit,),
            state_revision=1,
        )
        self.config = config
        self.games_root = games
        self.active_root = active
        self.ruleset_snapshot = snapshot
        self.snapshot_store = GameSnapshotStore(active)
        self.archive_store = GameArchiveStore(games)
        self.manager = GameManager(state, registry=load_action_registry())
        return self._status_payload(private=False)

    async def start(self) -> dict[str, object]:
        """Start seat runtimes and atomically enter ``PLAYER_PREPARE``.

        Knowledge and role assignment are derived from the frozen game
        snapshot.  Runtime startup happens before the manager commit; a
        failure therefore leaves the authoritative state in ``ASSIGNED`` and
        makes the command safely retryable with the same seed.
        """

        if self.manager is None or self.config is None or self.active_root is None:
            raise ModeratorError("no game is loaded; run new first")
        if self.state.phase is not GamePhase.ASSIGNED:
            raise ModeratorError("start is not connected until the game is in ASSIGNED phase")
        if self.session_service is not None:
            raise ModeratorError("player sessions are already running")
        snapshot = self.ruleset_snapshot
        if snapshot is None:
            raise ModeratorError("start requires a frozen ruleset snapshot")

        # Validate operator supplied deadlines before any Pi or scripted
        # process is launched.  Soft deadlines are retained for future host
        # steering, while the hard values are consumed by every turn flow.
        for timeout_key in (
            "prepare_soft_seconds",
            "prepare_hard_seconds",
            "turn_soft_seconds",
            "turn_hard_seconds",
        ):
            _timeout_seconds(self.config, timeout_key)

        try:
            bundle = await load_runtime_knowledge_bundle_from_snapshot(snapshot)
            configuration = parse_player_configuration(
                self.config,
                board=bundle.board,
                game_root=self.active_root,
                game_id=self.state.game_id,
                allow_scripted=True,
            )
            if self.state.rng is None:
                raise ModeratorError("start requires a configured random seed")
            assignment_plan = build_role_assignment_plan(
                bundle.board,
                bundle.package,
                seed=self.state.rng.seed,
                seats=tuple(player.seat for player in configuration.players),
            )
        except ModeratorError:
            raise
        except Exception as exc:
            raise ModeratorError(f"start validation failed: {exc}") from exc

        service = PlayerSessionService(
            runtime_factory=self.runtime_factory,
            state_provider=self.manager.snapshot,
        )
        try:
            records = await service.start(bundle, configuration, assignment_plan)
            refs = {seat: record.runtime_ref.session_id for seat, record in records.items()}
            await self.manager.commit_game_start(
                assignment_plan=assignment_plan,
                session_refs=refs,
                expected_revision=self.state.state_revision,
                now=self.clock(),
            )
        except Exception as exc:
            try:
                await service.close("start failed")
            except Exception:
                pass
            if isinstance(exc, ModeratorError):
                raise
            if isinstance(exc, PlayerSessionError):
                raise ModeratorError(f"player session startup failed: {exc}") from exc
            raise ModeratorError(f"start could not be committed: {exc}") from exc

        self.runtime_bundle = bundle
        self.assignment_plan = assignment_plan
        self.session_refs = dict(refs)
        self.session_service = service
        # Bind lazily on the first day command.  This keeps the start
        # transaction focused on session startup and lets the coordinator
        # fail closed without making an already committed start irrecoverable.
        self._day_flow = None
        self._night_flow = None
        self._prepare_flow = None
        self._last_words_flow = None
        self._sheriff_flow = None
        self._badge_flow = None
        self._trigger_flow = None
        return self._status_payload(private=False)

    def _require_prepare_flow(self) -> PlayerPrepareFlow:
        if self.manager is None or self.session_service is None:
            raise ModeratorError("prepare commands require started player sessions")
        if self._prepare_flow is None:
            self._prepare_flow = PlayerPrepareFlow(
                self.manager,
                self.session_service,
                clock=self.clock,
                turn_timeout=timedelta(
                    seconds=_timeout_seconds(self.config, "prepare_hard_seconds") or 120.0
                ),
            )
        return self._prepare_flow

    async def _prepare_command(self, args: list[str]) -> dict[str, object]:
        if args and args[0].lower() in {"help", "?"}:
            if len(args) != 1:
                raise ModeratorError("prepare help accepts no arguments")
            return {"status": "ok", "commands": list(self._PREPARE_HELP)}
        flow = self._require_prepare_flow()
        if not args or args[0].lower() in {"status", "show"}:
            if len(args) > 1:
                raise ModeratorError("prepare status accepts no arguments")
            try:
                return await flow.status()
            except PlayerPrepareError as exc:
                raise ModeratorError(str(exc)) from exc
        action = args[0].lower()
        if action not in {"next", "retry"}:
            raise ModeratorError("prepare syntax: status | next [seat] | retry [seat]")
        if len(args) > 2:
            raise ModeratorError(f"prepare {action} accepts at most one seat")
        seat: int | None = None
        if len(args) == 2:
            try:
                seat = int(args[1], 10)
            except ValueError as exc:
                raise ModeratorError(f"prepare {action} seat must be an integer") from exc
        try:
            result = await (flow.next(seat) if action == "next" else flow.retry(seat))
        except PlayerPrepareError as exc:
            raise ModeratorError(str(exc)) from exc
        payload = self._status_payload(private=False)
        payload["prepare"] = result
        return payload

    def _require_day_flow(self) -> ModeratorDayFlow:
        """Return the runtime-bound day adapter or fail closed."""

        if self.manager is None or self.runtime_bundle is None or self.session_service is None:
            raise ModeratorError("day commands require started player sessions")
        if self._day_flow is None:
            try:
                self._day_flow = ModeratorDayFlow(
                    self.manager,
                    self.runtime_bundle.board,
                    self.session_service.runtimes,
                    snapshot_id=self.state.ruleset.snapshot_id if self.state.ruleset else None,
                    timeout_seconds=_timeout_seconds(self.config, "turn_hard_seconds"),
                )
            except DayCoordinatorError as exc:
                raise ModeratorError(str(exc)) from exc
        return self._day_flow

    def _require_last_words_flow(self) -> LastWordsFlow:
        runtime_bundle = self.runtime_bundle
        if self.manager is None or runtime_bundle is None or self.session_service is None:
            raise ModeratorError("last-words commands require started player sessions")
        board = runtime_bundle.board
        if not isinstance(board, BoardDefinition):
            raise ModeratorError("last-words commands require a frozen runtime board")
        if self._last_words_flow is None:
            self._last_words_flow = LastWordsFlow(
                self.manager,
                board,
                self.session_service.runtimes,
                timeout_seconds=_timeout_seconds(self.config, "turn_hard_seconds"),
            )
        return self._last_words_flow

    def _require_last_words_complete(self) -> None:
        # Lightweight shell tests may replace the runtime bundle with a stub
        # while exercising command forwarding.  Do not construct the strict
        # LastWordsFlow for that substitute; a real runtime always carries a
        # published BoardDefinition and takes the validation path below.
        runtime_bundle = self.runtime_bundle
        if runtime_bundle is None or not isinstance(runtime_bundle.board, BoardDefinition):
            return
        try:
            self._require_last_words_flow().require_complete()
        except LastWordsError as exc:
            raise ModeratorError(str(exc)) from exc

    def _day_payload(self) -> dict[str, object]:
        flow = self._require_day_flow()
        payload = self._status_payload(private=False)
        payload["day"] = flow.progress()
        return payload

    def _require_night_flow(self) -> ModeratorNightFlow:
        """Return the runtime-bound night adapter or fail closed."""

        if self.manager is None or self.runtime_bundle is None or self.session_service is None:
            raise ModeratorError("night commands require started player sessions")
        if self._night_flow is None:
            try:
                self._night_flow = ModeratorNightFlow(
                    self.manager,
                    self.runtime_bundle.board,
                    self.session_service.runtimes,
                    snapshot_id=self.state.ruleset.snapshot_id if self.state.ruleset else None,
                    clock=self.clock,
                    timeout_seconds=_timeout_seconds(self.config, "turn_hard_seconds"),
                )
            except (ModeratorNightError, ValueError, TypeError) as exc:
                raise ModeratorError(str(exc)) from exc
        return self._night_flow

    def _night_payload(self) -> dict[str, object]:
        flow = self._require_night_flow()
        payload = self._status_payload(private=False)
        payload["night"] = flow.progress()
        return payload

    def _require_sheriff_flow(self) -> ModeratorSheriffFlow:
        """Return the runtime-bound sheriff adapter or fail closed."""

        if self.manager is None or self.runtime_bundle is None or self.session_service is None:
            raise ModeratorError("sheriff commands require started player sessions")
        if self._sheriff_flow is None:
            try:
                self._sheriff_flow = ModeratorSheriffFlow(
                    self.manager,
                    self.runtime_bundle.board,
                    self.session_service.runtimes,
                    timeout_seconds=_timeout_seconds(self.config, "turn_hard_seconds"),
                )
            except (ModeratorSheriffError, ValueError, TypeError) as exc:
                raise ModeratorError(str(exc)) from exc
        return self._sheriff_flow

    def _sheriff_payload(self) -> dict[str, object]:
        flow = self._require_sheriff_flow()
        payload = self._status_payload(private=False)
        payload["sheriff"] = flow.progress()
        # Lightweight command-forwarding tests may install a stub bundle;
        # badge orchestration is only meaningful once the frozen board has
        # been loaded and validated.  A real session always satisfies this
        # guard, while the sheriff payload remains usable for those stubs.
        if (
            self.runtime_bundle is not None
            and isinstance(self.runtime_bundle.board, BoardDefinition)
            and self.session_service is not None
        ):
            payload["badge"] = self._require_badge_flow().progress()
        return payload

    def _require_badge_flow(self) -> ModeratorSheriffBadgeFlow:
        """Return the runtime-bound death/resignation badge adapter."""

        if self.manager is None or self.runtime_bundle is None or self.session_service is None:
            raise ModeratorError("sheriff badge commands require started player sessions")
        if self._badge_flow is None:
            try:
                self._badge_flow = ModeratorSheriffBadgeFlow(
                    self.manager,
                    self.runtime_bundle.board,
                    self.session_service.runtimes,
                    clock=self.clock,
                    timeout_seconds=_timeout_seconds(self.config, "turn_hard_seconds"),
                )
            except (ModeratorBadgeError, ValueError, TypeError) as exc:
                raise ModeratorError(str(exc)) from exc
        return self._badge_flow

    def _badge_is_required(self) -> bool:
        if self.manager is None or self.runtime_bundle is None:
            return False
        state = self.state
        if state.sheriff_seat is None:
            return False
        player = state.players.get(state.sheriff_seat)
        if player is None or (player.alive and player.can_vote):
            return False
        policy = self.runtime_bundle.board.day_flow.sheriff
        return bool(
            policy.transfer_enabled is True
            and (
                (player.alive and policy.transfer_on_resignation is True)
                or (not player.alive and policy.transfer_on_death is True)
            )
        )

    def _require_badge_complete(self) -> None:
        if not self._badge_is_required():
            return
        marker = self.state.sheriff_badge
        if not isinstance(marker, dict) or marker.get("status") != "COMPLETE":
            raise ModeratorError(
                "BADGE_PENDING: current sheriff is ineligible; run sheriff badge open, "
                "next, resolve, and finish"
            )

    def _require_trigger_flow(self) -> ModeratorTriggerFlow:
        """Return the generic board-defined trigger adapter or fail closed."""

        if self.manager is None or self.runtime_bundle is None or self.session_service is None:
            raise ModeratorError("trigger commands require started player sessions")
        if self._trigger_flow is None:
            try:
                self._trigger_flow = ModeratorTriggerFlow(
                    self.manager,
                    self.runtime_bundle.board,
                    self.session_service.runtimes,
                    clock=self.clock,
                    timeout_seconds=_timeout_seconds(self.config, "turn_hard_seconds"),
                )
            except (ModeratorTriggerError, ValueError, TypeError) as exc:
                raise ModeratorError(str(exc)) from exc
        return self._trigger_flow

    def _trigger_payload(self) -> dict[str, object]:
        flow = self._require_trigger_flow()
        # Trigger progress contains board-defined action windows, eligible
        # seats, action codes and ability/resource identifiers.  It is a
        # moderator-only projection even when the command itself is a status
        # read, so never expose it through the public status envelope.
        payload = self._status_payload(private=True)
        payload["trigger"] = flow.progress()
        return payload

    @staticmethod
    def _reject_duplicate_json_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        """Reject ambiguous JSON objects before strict model validation."""

        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    @classmethod
    def _load_trigger_resolutions_file(cls, filename: str) -> tuple[ActionResolution, ...]:
        """Load one explicit, strictly typed trigger ruling batch."""

        path = Path(filename).expanduser()
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ModeratorError(f"trigger resolution file cannot be opened: {filename}") from exc
        if not path.is_file():
            raise ModeratorError(f"trigger resolution file is not a regular file: {filename}")
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ModeratorError(f"trigger resolution file cannot be read: {filename}") from exc
        try:
            raw = json.loads(raw_text, object_pairs_hook=cls._reject_duplicate_json_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ModeratorError("trigger resolution file must contain valid JSON") from exc
        if not isinstance(raw, list) or not raw:
            raise ModeratorError("trigger resolution file must contain a non-empty JSON array")
        try:
            return tuple(ActionResolution.model_validate(item) for item in raw)
        except (TypeError, ValueError) as exc:
            raise ModeratorError(
                "trigger resolution file contains an invalid ActionResolution record"
            ) from exc

    def _require_trigger_mutation_allowed(self) -> None:
        state = self.state
        if state.run_status is RunStatus.PAUSED:
            raise ModeratorError("game is paused; run resume first")
        if state.run_status is RunStatus.CLOSED:
            raise ModeratorError("game is closed")
        if state.run_status is RunStatus.FAILED:
            raise ModeratorError("game has failed and cannot run trigger actions")

    @staticmethod
    def _trigger_optional_seat(parts: list[str], *, command: str) -> int | None:
        if not parts:
            return None
        if len(parts) != 1:
            raise ModeratorError(f"{command} accepts at most one seat")
        try:
            seat = int(parts[0], 10)
        except ValueError as exc:
            raise ModeratorError(f"{command} seat must be an integer") from exc
        if seat < 1 or seat > 64:
            raise ModeratorError(f"{command} seat must be between 1 and 64")
        return seat

    async def _trigger_command(self, args: list[str]) -> dict[str, object]:
        if args and args[0].lower() in {"help", "?"}:
            if len(args) != 1:
                raise ModeratorError("trigger help accepts no arguments")
            return {"status": "ok", "commands": list(self._TRIGGER_HELP)}

        flow = self._require_trigger_flow()
        if not args or args[0].lower() in {"status", "show"}:
            if len(args) > 1:
                raise ModeratorError("trigger status accepts no arguments")
            return self._trigger_payload()

        action = args[0].lower()
        if action in {"open", "next", "retry", "resolve", "auto-resolve", "finish"}:
            self._require_trigger_mutation_allowed()
        try:
            if action == "open" and len(args) == 1:
                await flow.open()
            elif action in {"next", "retry"}:
                seat = self._trigger_optional_seat(args[1:], command=f"trigger {action}")
                result = await flow.next(seat) if action == "next" else await flow.retry(seat)
                payload = self._trigger_payload()
                payload["action"] = result
                return payload
            elif action == "pending" and len(args) == 1:
                pending = flow.pending()
                payload = self._status_payload(private=False)
                payload["private"] = True
                payload["sensitive"] = True
                payload["trigger"] = pending
                return payload
            elif action == "resolve" and len(args) == 2:
                resolutions = self._load_trigger_resolutions_file(args[1])
                await flow.resolve(resolutions)
                payload = self._status_payload(private=False)
                payload["private"] = True
                payload["sensitive"] = True
                payload["resolution"] = {"status": "committed", "count": len(resolutions)}
                payload["trigger"] = flow.progress()
                return payload
            elif action == "auto-resolve" and len(args) == 1:
                auto_resolve = getattr(flow, "auto_resolve", None)
                if auto_resolve is None:
                    raise ModeratorError("trigger auto-resolve is unavailable for this board")
                result = await auto_resolve()
                if isinstance(result, dict):
                    payload = self._trigger_payload()
                    payload["action"] = result
                    return payload
            elif action == "finish" and len(args) == 1:
                origin = flow.completion_origin()
                if origin == "DAY_EXILE":
                    self._require_last_words_complete()
                    if self.state.phase is GamePhase.TRIGGER_ACTION and self._badge_is_required():
                        raise ModeratorError(
                            "BADGE_PENDING: complete sheriff badge before finishing the day trigger"
                        )
                await flow.finish()
            else:
                raise ModeratorError(
                    "trigger syntax: status | open | next [seat] | retry [seat] | "
                    "pending | resolve <json-file> | auto-resolve | finish"
                )
        except ModeratorTriggerError as exc:
            raise ModeratorError(str(exc)) from exc
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ModeratorError(str(exc)) from exc
        return self._trigger_payload()

    def _require_first_day_sheriff(self) -> None:
        """Prevent a board-required first-day election from being skipped."""

        if self.manager is None or self.runtime_bundle is None:
            return
        state = self.state
        sheriff = self.runtime_bundle.board.day_flow.sheriff
        if (
            state.phase is GamePhase.DAY_ANNOUNCE
            and state.day_no == 1
            and sheriff.enabled
            and sheriff.first_day_election
            and state.sheriff_election is None
        ):
            raise ModeratorError(
                "FIRST_DAY_SHERIFF_REQUIRED: run sheriff start before continuing the day"
            )

    @staticmethod
    def _sheriff_candidates(parts: list[str]) -> tuple[int, ...]:
        if not parts:
            raise ModeratorError("sheriff start requires at least one candidate seat")
        candidates: list[int] = []
        for part in parts:
            try:
                seat = int(part, 10)
            except ValueError as exc:
                raise ModeratorError(f"sheriff candidate seat must be an integer: {part}") from exc
            if seat < 1 or seat > 64:
                raise ModeratorError("sheriff candidate seats must be between 1 and 64")
            candidates.append(seat)
        return tuple(candidates)

    async def _sheriff_command(self, args: list[str]) -> dict[str, object]:
        if args and args[0].lower() in {"help", "?"}:
            if len(args) != 1:
                raise ModeratorError("sheriff help accepts no arguments")
            return {"status": "ok", "commands": list(self._SHERIFF_HELP)}
        flow = self._require_sheriff_flow()
        if not args or args[0].lower() in {"status", "show"}:
            if len(args) > 1:
                raise ModeratorError("sheriff status accepts no arguments")
            return self._sheriff_payload()

        action = args[0].lower()
        try:
            if action == "badge":
                badge = self._require_badge_flow()
                if len(args) == 1 or args[1].lower() in {"status", "show"}:
                    if len(args) > 2:
                        raise ModeratorError("sheriff badge status accepts no arguments")
                    payload = self._sheriff_payload()
                    payload["badge"] = badge.progress()
                    return payload
                subcommand = args[1].lower()
                if subcommand == "open" and len(args) == 2:
                    await badge.open()
                elif subcommand == "next" and len(args) == 2:
                    result = await badge.next()
                    payload = self._sheriff_payload()
                    payload["badge_action"] = result
                    return payload
                elif subcommand == "retry" and len(args) == 2:
                    result = await badge.retry()
                    payload = self._sheriff_payload()
                    payload["badge_action"] = result
                    return payload
                elif subcommand == "resolve" and len(args) == 2:
                    await badge.resolve()
                elif subcommand == "finish" and len(args) == 2:
                    # The announcement must precede the day speech boundary,
                    # including after a later sheriff's death.
                    if self.state.phase is GamePhase.DAY_ANNOUNCE:
                        await self._require_day_flow().ensure_announcement()
                    await badge.finish()
                else:
                    raise ModeratorError(
                        "sheriff badge syntax: status | open | next | retry | resolve | finish"
                    )
                return self._sheriff_payload()
            if action == "start":
                candidates = self._sheriff_candidates(args[1:])
                # The official first-day boundary starts before the death
                # announcement.  Announcement and last words happen only
                # after the election transfer completes.
                await flow.start(candidates)
            elif action == "speech":
                if len(args) != 2 or args[1].lower() not in {"next", "retry"}:
                    raise ModeratorError("sheriff speech syntax: next | retry")
                if args[1].lower() == "next":
                    speech_result = await flow.speech_next()
                else:
                    speech_result = await flow.speech_retry()
                payload = self._sheriff_payload()
                payload["speech"] = speech_result
                return payload
            elif action == "vote":
                if len(args) < 2:
                    raise ModeratorError(
                        "sheriff vote syntax: open | next [seat] | retry [seat] | "
                        "collect [--force] | confirm"
                    )
                subcommand = args[1].lower()
                if subcommand == "open" and len(args) == 2:
                    await flow.open_vote()
                elif subcommand in {"next", "run"}:
                    seat = self._optional_seat(args[2:], command="sheriff vote next")
                    vote_result = await flow.vote_next(seat)
                    payload = self._sheriff_payload()
                    payload["vote"] = vote_result
                    return payload
                elif subcommand == "retry":
                    seat = self._optional_seat(args[2:], command="sheriff vote retry")
                    vote_result = await flow.vote_retry(seat)
                    payload = self._sheriff_payload()
                    payload["vote"] = vote_result
                    return payload
                elif subcommand == "collect":
                    flags = set(args[2:])
                    if flags - {"--force"}:
                        raise ModeratorError("sheriff vote collect accepts only --force")
                    await flow.collect(force="--force" in flags)
                elif subcommand == "confirm" and len(args) == 2:
                    await flow.confirm()
                else:
                    raise ModeratorError(
                        "sheriff vote syntax: open | next [seat] | retry [seat] | "
                        "collect [--force] | confirm"
                    )
            elif action == "transfer" and len(args) == 1:
                await flow.transfer()
            else:
                raise ModeratorError(
                    "sheriff syntax: status | start <candidate seat ...> | speech next | "
                    "speech retry | vote open | vote next [seat] | vote retry [seat] | "
                    "vote collect [--force] | vote confirm | transfer"
                )
        except ModeratorSheriffError as exc:
            raise ModeratorError(str(exc)) from exc
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ModeratorError(str(exc)) from exc
        return self._sheriff_payload()

    def _require_victory_runtime(self) -> tuple[GameManager, BoardDefinition]:
        """Return the manager and frozen runtime board for victory commands."""

        if self.manager is None:
            raise ModeratorError("no game is loaded; run new first")
        if self.runtime_bundle is None:
            raise ModeratorError("victory commands require started player sessions")
        board = self.runtime_bundle.board
        if not isinstance(board, BoardDefinition):
            raise ModeratorError("victory commands require a frozen runtime board")
        return self.manager, board

    def _victory_role_groups(self) -> Mapping[str, str] | None:
        """Build the semantic role-group map from the frozen board/package."""

        if self.runtime_bundle is None:
            return None
        board_groups = getattr(self.runtime_bundle.board.victory, "role_groups", None)
        if isinstance(board_groups, Mapping):
            normalized = {
                role_id: group
                for role_id, group in board_groups.items()
                if isinstance(role_id, str) and isinstance(group, str)
            }
            if normalized:
                return normalized
        package = getattr(self.runtime_bundle, "package", None)
        profiles = getattr(package, "effective_roles", None)
        if not isinstance(profiles, Mapping):
            return None
        role_groups: dict[str, str] = {}
        for role_id, profile in profiles.items():
            base_role = getattr(profile, "base_role", None)
            team = getattr(base_role, "team", None)
            if isinstance(role_id, str) and isinstance(team, str):
                role_groups[role_id] = team
        return role_groups or None

    @staticmethod
    def _victory_payload(evaluation: object) -> dict[str, object]:
        """Expose board-level victory facts without player identity details."""

        candidates = getattr(evaluation, "candidates", ())
        return {
            "status": getattr(evaluation, "status", "UNKNOWN"),
            "winner": getattr(evaluation, "winner", None),
            "candidate_sides": list(getattr(evaluation, "candidate_sides", ())),
            "candidates": [{"side": item.side, "condition": item.condition} for item in candidates],
            "reasons": list(getattr(evaluation, "reasons", ())),
            "requires_moderator": bool(getattr(evaluation, "requires_moderator", False)),
        }

    def _evaluate_victory(self) -> dict[str, object]:
        manager, board = self._require_victory_runtime()
        try:
            evaluation = evaluate_victory(
                manager.state,
                board,
                role_groups=self._victory_role_groups(),
            )
        except (TypeError, ValueError) as exc:
            raise ModeratorError(str(exc)) from exc
        return self._victory_payload(evaluation)

    async def _write_victory_snapshot(self, state: GameState) -> Mapping[str, JsonValue]:
        """Publish the fully committed victory candidate and return its ref.

        ``GameManager.commit_victory_check`` invokes this callback before it
        installs the candidate in memory.  A failed materialization therefore
        leaves the authoritative phase at VICTORY_CHECK and keeps the command
        retryable.  The callback intentionally receives the candidate state,
        so a pre-check VICTORY_CHECK state can never be mistaken for a
        completed cycle snapshot.
        """

        if self.snapshot_store is None:
            raise ModeratorError("victory snapshot storage is not configured")
        frozen_ruleset = await self._load_frozen_ruleset()
        try:
            snapshot = await self.snapshot_store.create(
                state,
                ruleset=frozen_ruleset,
                created_at=self.clock(),
            )
        except Exception as exc:
            raise ModeratorError("consistent victory snapshot could not be created") from exc
        return {
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_revision": snapshot.manifest.snapshot_revision,
            "state_revision": snapshot.manifest.state_revision,
            "created_at": snapshot.manifest.created_at,
            "manifest_sha256": snapshot.manifest.manifest_sha256,
        }

    async def _victory_command(self, args: list[str]) -> dict[str, object]:
        if args and args[0].lower() in {"help", "?"}:
            if len(args) != 1:
                raise ModeratorError("victory help accepts no arguments")
            return {"status": "ok", "commands": list(self._VICTORY_HELP)}

        if not args or args[0].lower() in {"status", "show"}:
            if len(args) > 1:
                raise ModeratorError("victory status accepts no arguments")
            payload = self._status_payload(private=False)
            payload["victory"] = self._evaluate_victory()
            return payload

        night_check = args[0].lower() in {"night-check", "night_check"}
        if night_check and len(args) != 1:
            raise ModeratorError("victory syntax: status | check [candidate-side] | night-check")
        if not night_check and (args[0].lower() != "check" or len(args) > 2):
            raise ModeratorError("victory syntax: status | check [candidate-side] | night-check")

        manager, board = self._require_victory_runtime()
        state = manager.state
        if state.run_status is RunStatus.PAUSED:
            raise ModeratorError("game is paused; run resume first")
        if state.run_status is RunStatus.CLOSED:
            raise ModeratorError("game is closed")
        if state.run_status is RunStatus.FAILED:
            raise ModeratorError("game has failed and cannot run a victory check")
        if night_check:
            if state.phase is not GamePhase.DAY_ANNOUNCE:
                raise ModeratorError("night victory check requires the DAY_ANNOUNCE phase")
            try:
                committed = await manager.commit_night_victory_check(
                    board,
                    role_groups=self._victory_role_groups(),
                    expected_revision=state.state_revision,
                    now=self.clock(),
                    snapshot_writer=(
                        self._write_victory_snapshot if self.snapshot_store is not None else None
                    ),
                )
            except (EventCommitError, TypeError, ValueError) as exc:
                raise ModeratorError(str(exc)) from exc
            victory = self._evaluate_victory()
            if committed.winner is not None:
                victory["status"] = "WINNER"
                victory["winner"] = committed.winner.get("side")
            payload = self._status_payload(private=False)
            payload["victory"] = victory
            return payload

        if state.phase is not GamePhase.VICTORY_CHECK:
            raise ModeratorError("victory check requires the VICTORY_CHECK phase")

        moderator_winner: str | None = None
        if len(args) == 2:
            moderator_winner = args[1].strip()
            if not moderator_winner:
                raise ModeratorError("victory candidate-side must be non-empty")
        try:
            committed = await manager.commit_victory_check(
                board,
                role_groups=self._victory_role_groups(),
                moderator_winner=moderator_winner,
                reason=(
                    "moderator victory check"
                    if moderator_winner is None
                    else f"moderator selected victory candidate {moderator_winner}"
                ),
                expected_revision=state.state_revision,
                now=self.clock(),
                # Lightweight unit shells may not own a persistence root.  A
                # real moderator created by ``new`` always has a store, and
                # therefore always takes the durable boundary path.
                snapshot_writer=(
                    self._write_victory_snapshot if self.snapshot_store is not None else None
                ),
            )
        except (EventCommitError, TypeError, ValueError) as exc:
            raise ModeratorError(str(exc)) from exc

        victory = self._evaluate_victory()
        if committed.winner is not None:
            # A moderator-resolved PENDING result is now a committed winner;
            # do not expose the stale pure-evaluation status in the command
            # response.
            victory["status"] = "WINNER"
            victory["winner"] = committed.winner.get("side")
        payload = self._status_payload(private=False)
        payload["victory"] = victory
        return payload

    @staticmethod
    def _night_optional_seat(parts: list[str], *, command: str) -> int | None:
        if not parts:
            return None
        if len(parts) != 1:
            raise ModeratorError(f"{command} accepts at most one seat")
        try:
            seat = int(parts[0], 10)
        except ValueError as exc:
            raise ModeratorError(f"{command} seat must be an integer") from exc
        if seat < 1 or seat > 64:
            raise ModeratorError(f"{command} seat must be between 1 and 64")
        return seat

    async def _night_command(self, args: list[str]) -> dict[str, object]:
        if args and args[0] in {"help", "?"}:
            if len(args) != 1:
                raise ModeratorError("night help accepts no arguments")
            return {"status": "ok", "commands": list(self._NIGHT_HELP)}
        flow = self._require_night_flow()
        if not args or args[0].lower() in {"status", "show"}:
            if len(args) > 1:
                raise ModeratorError("night status accepts no arguments")
            return self._night_payload()
        action = args[0].lower()
        try:
            if action == "open" and len(args) == 1:
                await flow.open()
            elif action in {"advance", "next"} and len(args) == 1:
                await flow.advance()
            elif action == "action" and len(args) >= 2:
                subcommand = args[1].lower()
                seat = self._night_optional_seat(args[2:], command=f"night action {subcommand}")
                if subcommand == "next":
                    result = await flow.action_next(seat)
                    payload = self._night_payload()
                    payload["action"] = result
                    return payload
                if subcommand == "retry":
                    result = await flow.action_retry(seat)
                    payload = self._night_payload()
                    payload["action"] = result
                    return payload
                raise ModeratorError("night action requires next or retry")
            elif action == "team":
                if len(args) != 2:
                    raise ModeratorError("night team syntax: next | retry | again")
                subcommand = args[1].lower()
                if subcommand == "next":
                    result = await flow.team_next()
                elif subcommand == "retry":
                    result = await flow.team_retry()
                elif subcommand == "again":
                    result = await flow.team_again()
                else:
                    raise ModeratorError("night team requires next, retry, or again")
                payload = self._night_payload()
                payload["team"] = result
                return payload
            elif action == "resolve":
                await flow.resolve(tuple(args[1:]))
            elif action == "auto-resolve" and len(args) == 1:
                auto_resolve = getattr(flow, "auto_resolve", None)
                if auto_resolve is None:
                    raise ModeratorError("night auto-resolve is unavailable for this board")
                await auto_resolve()
            elif action == "pending" and len(args) == 1:
                pending = flow.pending()
                payload = self._status_payload(private=False)
                payload["private"] = True
                payload["sensitive"] = True
                payload["night"] = pending
                return payload
            else:
                raise ModeratorError(
                    "night syntax: status | open | advance | team next | team retry | "
                    "team again | action next [seat] | action retry [seat] | pending | "
                    "resolve <json-file> | auto-resolve"
                )
        except ModeratorNightError as exc:
            raise ModeratorError(str(exc)) from exc
        return self._night_payload()

    @staticmethod
    def _seat_queue(parts: list[str]) -> tuple[int, ...] | None:
        if not parts:
            return None
        seats: list[int] = []
        for part in parts:
            try:
                seat = int(part, 10)
            except ValueError as exc:
                raise ModeratorError(f"speech queue seat is not an integer: {part}") from exc
            if seat < 1 or seat > 64:
                raise ModeratorError("speech queue seats must be between 1 and 64")
            seats.append(seat)
        return tuple(seats)

    @staticmethod
    def _optional_seat(parts: list[str], *, command: str) -> int | None:
        if not parts:
            return None
        if len(parts) != 1:
            raise ModeratorError(f"{command} accepts at most one seat")
        try:
            seat = int(parts[0], 10)
        except ValueError as exc:
            raise ModeratorError(f"{command} seat must be an integer") from exc
        if seat < 1 or seat > 64:
            raise ModeratorError(f"{command} seat must be between 1 and 64")
        return seat

    async def _last_words_command(self, args: list[str]) -> dict[str, object]:
        if args and args[0].lower() in {"help", "?"}:
            return {
                "status": "ok",
                "commands": [
                    "last-words status",
                    "last-words next [seat]",
                    "last-words retry [seat]",
                ],
            }
        flow = self._require_last_words_flow()
        if not args or args[0].lower() in {"status", "show"}:
            if len(args) > 1:
                raise ModeratorError("last-words status accepts no arguments")
            payload = self._status_payload(private=False)
            payload["last_words"] = flow.status()
            return payload
        action = args[0].lower()
        if action not in {"next", "retry"}:
            raise ModeratorError("last-words syntax: status | next [seat] | retry [seat]")
        seat = self._optional_seat(args[1:], command=f"last-words {action}")
        try:
            result = await (flow.next(seat) if action == "next" else flow.retry(seat))
        except LastWordsError as exc:
            raise ModeratorError(str(exc)) from exc
        payload = self._status_payload(private=False)
        payload["last_words"] = flow.status()
        payload["speech"] = {
            "seat": result.request.logical_request_id.rsplit("-s", 1)[-1],
            "request_id": result.request.request_id,
        }
        return payload

    async def _day_command(self, args: list[str]) -> dict[str, object]:
        """Execute the explicit daytime command vocabulary.

        The shell intentionally exposes lifecycle boundaries, while player
        speech and ballots still arrive through their seat-bound runtimes and
        the authoritative manager validators.
        """

        if args and args[0] in {"help", "?"}:
            if len(args) != 1:
                raise ModeratorError("day help accepts no arguments")
            return {"status": "ok", "commands": list(self._DAY_HELP)}
        flow = self._require_day_flow()
        if not args or args[0] in {"status", "show"}:
            if len(args) > 1:
                raise ModeratorError("day status accepts no arguments")
            return self._day_payload()
        action = args[0].lower()
        self._require_first_day_sheriff()
        # A dead or voting-disabled current sheriff owns a separate player
        # action window.  DAY_RESOLVE and TRIGGER_ACTION are already inside a
        # resolution commit; their dedicated flows must be allowed to finish
        # that boundary before opening the badge window.
        badge_announcement = (
            action in {"announce", "open"}
            and self._badge_is_required()
            and (
                self.state.phase is GamePhase.DAY_ANNOUNCE
                or (
                    self.state.phase is GamePhase.DAY_SPEECH
                    and self.state.sheriff_election is not None
                )
            )
        )
        if (
            self.state.phase
            in {
                GamePhase.DAY_ANNOUNCE,
                GamePhase.DAY_SPEECH,
                GamePhase.VOTE,
                GamePhase.VOTE_PK_SPEECH,
                GamePhase.VOTE_PK,
            }
            and not badge_announcement
        ):
            self._require_badge_complete()
        # The normal ``day speech``/``day vote`` commands also recover the
        # PK branch from the durable phase.  Keep an explicit ``day pk``
        # spelling for hosts that want the command log to show the branch;
        # it is translated into the same flow methods so both forms share
        # all seat and phase guards.
        pk_command = action == "pk"
        if pk_command:
            if len(args) < 2 or args[1].lower() not in {"speech", "speak", "vote", "voting"}:
                raise ModeratorError("day pk requires speech or vote followed by its subcommand")
            action = args[1].lower()
            args = [action, *args[2:]]
        try:
            if action in {"announce", "open"}:
                if len(args) < 1:
                    raise ModeratorError("day announce accepts optional content")
                content = " ".join(args[1:]).strip() or None
                if badge_announcement and self.state.phase is GamePhase.DAY_ANNOUNCE:
                    # Publish the death announcement while retaining the
                    # boundary so the dead sheriff can still choose transfer
                    # or tear before ordinary speech begins.
                    await flow.ensure_announcement(content)
                elif (
                    self.state.phase is GamePhase.DAY_SPEECH
                    and self.state.sheriff_election is not None
                ):
                    # First-day sheriff transfer already enters the ordinary
                    # speech phase.  Commit its announcement event before
                    # opening the speech queue, without a second phase edge.
                    await flow.ensure_announcement(content)
                else:
                    await flow.announce(content)
            elif action in {"speech", "speak"}:
                if len(args) < 2:
                    raise ModeratorError("day speech requires open, next, retry, or close")
                subcommand = args[1].lower()
                if subcommand == "open":
                    await flow.open_speech(
                        self._seat_queue(args[2:]),
                        is_pk=True if pk_command else None,
                    )
                elif subcommand in {"next", "run"} and len(args) == 2:
                    await flow.next_speech()
                elif subcommand == "retry" and len(args) == 2:
                    await flow.retry_speech()
                elif subcommand in {"close", "advance"} and len(args) == 2:
                    await flow.close_speech()
                else:
                    raise ModeratorError(
                        "day speech syntax: open [seat ...] | next | retry | close"
                    )
            elif action in {"next-speech", "speech-next"} and len(args) == 1:
                await flow.next_speech()
            elif action == "next" and len(args) == 1:
                await flow.next_speech()
            elif action in {"retry-speech", "speech-retry"} and len(args) == 1:
                await flow.retry_speech()
            elif action == "retry" and len(args) == 1:
                await flow.retry_speech()
            elif action in {"close-speech", "advance-speech"} and len(args) == 1:
                await flow.close_speech()
            elif action in {"vote", "voting"}:
                if len(args) < 2:
                    raise ModeratorError("day vote requires open, collect, or confirm")
                subcommand = args[1].lower()
                if subcommand == "open" and len(args) == 2:
                    await flow.open_vote(is_pk=True if pk_command else None)
                elif subcommand in {"next", "run"}:
                    seat = self._optional_seat(args[2:], command="day vote next")
                    await flow.next_vote(seat)
                elif subcommand == "retry":
                    seat = self._optional_seat(args[2:], command="day vote retry")
                    await flow.retry_vote(seat)
                elif subcommand in {"collect", "close"}:
                    flags = set(args[2:])
                    if flags - {"--force"}:
                        raise ModeratorError("day vote collect accepts only --force")
                    await flow.collect_vote(force="--force" in flags)
                elif subcommand in {"confirm", "resolve"} and len(args) == 2:
                    await flow.confirm_vote()
                else:
                    raise ModeratorError(
                        "day vote syntax: open | next [seat] | retry [seat] | "
                        "collect [--force] | confirm"
                    )
            elif action in {"open-vote", "vote-open"} and len(args) == 1:
                await flow.open_vote()
            elif action in {"collect-vote", "vote-collect"}:
                flags = set(args[1:])
                if flags - {"--force"}:
                    raise ModeratorError("day collect-vote accepts only --force")
                await flow.collect_vote(force="--force" in flags)
            elif action in {"confirm-vote", "vote-confirm"} and len(args) == 1:
                await flow.confirm_vote()
            elif action in {"exile", "confirm-exile"}:
                if len(args) != 2:
                    raise ModeratorError("day confirm-exile requires a target seat or none")
                if args[1].lower() in {"none", "no-exile", "no_exile", "pass"}:
                    target = None
                else:
                    try:
                        target = int(args[1], 10)
                    except ValueError as exc:
                        raise ModeratorError(
                            "exile target seat must be an integer or none"
                        ) from exc
                await flow.confirm_exile(target)
            elif action in {"finish", "close-day"} and len(args) == 1:
                self._require_last_words_complete()
                await flow.finish()
            else:
                raise ModeratorError(
                    "unknown day command; use day status, announce, speech, vote, "
                    "confirm-exile, or finish"
                )
        except DayCoordinatorError as exc:
            raise ModeratorError(str(exc)) from exc
        except (RuntimeError, TypeError, ValueError) as exc:
            # Runtime protocol failures leave the serialized speech head in
            # place when retry is safe.  Convert them at the interactive
            # boundary while preserving that durable retry state.
            raise ModeratorError(str(exc)) from exc
        return self._day_payload()

    async def _commit_audit(
        self,
        operation: str,
        *,
        command: str,
        reason: str | None = None,
        before_revision: int | None = None,
    ) -> GameState:
        """Append a redacted moderator operation through the manager boundary."""

        if self.manager is None:
            raise ModeratorError("no game is loaded; run new first")
        expected_revision = (
            self.state.state_revision if before_revision is None else before_revision
        )
        try:
            return await self.manager.commit_moderator_operation(
                operation=operation,
                command=command,
                expected_revision=expected_revision,
                reason=reason or "",
                now=self.clock(),
            )
        except (EventCommitError, ValueError, TypeError) as exc:
            raise ModeratorError(str(exc)) from exc

    async def _set_run_status(
        self,
        status: RunStatus,
        *,
        operation: str,
        command: str,
        reason: str = "",
    ) -> GameState:
        """Commit an operational status change and its audit as one state."""

        if self.manager is None:
            raise ModeratorError("no game is loaded; run new first")
        try:
            return await self.manager.commit_moderator_operation(
                operation=operation,
                command=command,
                expected_revision=self.state.state_revision,
                run_status=status,
                reason=reason,
                now=self.clock(),
            )
        except (EventCommitError, ValueError, TypeError) as exc:
            raise ModeratorError(str(exc)) from exc

    def _status_payload(self, *, private: bool) -> dict[str, object]:
        state = self.state
        payload: dict[str, object] = {
            "status": "ok",
            "game_id": state.game_id,
            "phase": state.phase.value,
            "run_status": state.run_status.value,
            "round_no": state.round_no,
            "day_no": state.day_no,
            "state_revision": state.state_revision,
            "ruleset": None
            if state.ruleset is None
            else {
                "board_id": state.ruleset.board_id,
                "version": state.ruleset.version,
                "snapshot_id": state.ruleset.snapshot_id,
            },
            "private": private,
        }
        if private:
            payload["sensitive"] = True
            payload["players"] = [
                {
                    "seat": player.seat,
                    "role_id": player.role_id,
                    "faction_id": player.faction_id,
                    "alive": player.alive,
                    "can_vote": player.can_vote,
                    "skill_resources": dict(player.skill_resources),
                    "session_epoch": player.session_epoch,
                }
                for player in state.players.values()
            ]
            payload["moderator_audit"] = list(state.moderator_audit)
        return payload

    async def status(self, *, private: bool = False) -> dict[str, object]:
        return self._status_payload(private=private)

    @classmethod
    def help(cls) -> dict[str, object]:
        """Return the stable command vocabulary for interactive hosts."""

        return {
            "status": "ok",
            "commands": [
                "new",
                "start",
                *cls._PREPARE_HELP,
                *cls._SHERIFF_HELP,
                *cls._VICTORY_HELP,
                "status [--private]",
                "pause",
                "resume",
                "next",
                *cls._DAY_HELP,
                *cls._NIGHT_HELP,
                *cls._TRIGGER_HELP,
                "save",
                "finish",
                "quit",
            ],
        }

    async def pause(self) -> dict[str, object]:
        state = self.state
        if state.run_status is RunStatus.CLOSED:
            raise ModeratorError("cannot pause a closed game")
        if state.run_status is RunStatus.PAUSED:
            return self._status_payload(private=False)
        await self._set_run_status(
            RunStatus.PAUSED,
            operation="PAUSE",
            command="pause",
            reason="game paused by moderator",
        )
        return self._status_payload(private=False)

    async def resume(self) -> dict[str, object]:
        state = self.state
        if state.run_status is not RunStatus.PAUSED:
            raise ModeratorError("game is not paused")
        await self._set_run_status(
            RunStatus.READY,
            operation="RESUME",
            command="resume",
            reason="game resumed by moderator",
        )
        return self._status_payload(private=False)

    async def next(self) -> dict[str, object]:
        state = self.state
        if state.run_status is RunStatus.PAUSED:
            raise ModeratorError("game is paused; run resume first")
        if state.run_status in {RunStatus.CLOSED, RunStatus.FAILED}:
            raise ModeratorError(f"game is {state.run_status.value.lower()}")
        if state.phase is GamePhase.VICTORY_CHECK:
            raise ModeratorError(
                "VICTORY_CHECK requires an explicit victory check; "
                "use victory check [candidate-side]"
            )
        if state.phase is GamePhase.PLAYER_PREPARE and not state.players:
            raise ModeratorError(
                "next is blocked during PLAYER_PREPARE; run prepare next for each seat first"
            )
        if state.phase is GamePhase.PLAYER_PREPARE and any(
            not player.knowledge_receipt_ids for player in state.players.values()
        ):
            raise ModeratorError(
                "next is blocked during PLAYER_PREPARE; run prepare next for each seat first"
            )
        if state.phase in {
            GamePhase.DAY_ANNOUNCE,
            GamePhase.DAY_RESOLVE,
            GamePhase.TRIGGER_ACTION,
        }:
            self._require_last_words_complete()
        if state.serial_turn is not None or state.pending_resolution is not None:
            raise ModeratorError("next is blocked by an active turn or unconfirmed resolution")
        if any(_active_window(window) for window in state.action_windows.values()):
            raise ModeratorError("next is blocked by an active action window")
        if any(_active_request(request) for request in state.action_requests.values()):
            raise ModeratorError("next is blocked by an active action request")
        targets = sorted(ALLOWED_PHASE_TRANSITIONS[state.phase], key=lambda item: item.value)
        if not targets:
            raise ModeratorError(f"phase {state.phase.value} has no next transition")
        if len(targets) != 1:
            choices = ", ".join(target.value for target in targets)
            raise ModeratorError(
                f"phase {state.phase.value} requires an explicit branch: {choices}"
            )
        target = targets[0]
        assert self.manager is not None
        try:
            await self.manager.commit_moderator_operation(
                operation="NEXT",
                command="next",
                expected_revision=state.state_revision,
                target_phase=target,
                reason=f"phase {state.phase.value} -> {target.value}",
                now=self.clock(),
            )
        except (EventCommitError, ValueError, TypeError) as exc:
            raise ModeratorError(str(exc)) from exc
        return self._status_payload(private=False)

    async def save(self) -> dict[str, object]:
        state = self.state
        if state.phase not in {
            GamePhase.VICTORY_CHECK,
            GamePhase.NIGHT_TEAM_CHAT,
            GamePhase.FINISHED,
        }:
            raise ModeratorError(
                "save requires a VICTORY_CHECK, completed NIGHT_TEAM_CHAT, or "
                "FINISHED cycle boundary"
            )
        if state.serial_turn is not None or state.pending_resolution is not None:
            raise ModeratorError("save requires no active turn or unconfirmed resolution")
        if any(_active_window(window) for window in state.action_windows.values()):
            raise ModeratorError("save requires all action windows to be closed")
        if any(_active_request(request) for request in state.action_requests.values()):
            raise ModeratorError("save requires all action requests to be resolved")
        # A RulesetRef is only an identity.  The durable game snapshot must
        # carry the exact verified files from this game's immutable ruleset
        # directory, never whatever happens to be in the current Vault.
        frozen_ruleset = await self._load_frozen_ruleset()
        # A completed ongoing victory boundary is identified by its final
        # VICTORY_CHECK audit.  Appending a SAVE audit before materialization
        # would destroy that proof and turn a valid retry into an arbitrary
        # night save.  Pre-check and final saves retain the historical audit.
        if state.phase is not GamePhase.NIGHT_TEAM_CHAT:
            await self._commit_audit("SAVE", command="save")
        assert self.snapshot_store is not None
        try:
            snapshot = await self.snapshot_store.create(
                self.state,
                ruleset=frozen_ruleset,
                created_at=self.clock(),
            )
        except Exception as exc:
            raise ModeratorError("consistent snapshot could not be created") from exc
        # Manual saves happen outside the victory commit callback.  Mirror the
        # active projection's reference in memory while requiring the same
        # state revision that was snapshotted.
        if self.manager is not None:
            reference: dict[str, JsonValue] = {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_revision": snapshot.manifest.snapshot_revision,
                "state_revision": snapshot.manifest.state_revision,
                "created_at": snapshot.manifest.created_at,
                "manifest_sha256": snapshot.manifest.manifest_sha256,
            }
            try:
                await self.manager.bind_snapshot_reference(
                    reference,
                    expected_revision=self.state.state_revision,
                )
            except (EventCommitError, ValueError, TypeError) as exc:
                raise ModeratorError("snapshot reference could not be bound") from exc
        return {
            "status": "saved",
            "game_id": self.state.game_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_revision": snapshot.manifest.snapshot_revision,
            "path": str(snapshot.path),
        }

    async def _close_sessions(self, reason: str) -> None:
        """Close the runtime group once, without masking an earlier failure."""

        service = self.session_service
        if service is None:
            return
        self.session_service = None
        self._day_flow = None
        self._last_words_flow = None
        self._night_flow = None
        self._prepare_flow = None
        self._sheriff_flow = None
        self._badge_flow = None
        self._trigger_flow = None
        try:
            await service.close(reason)
        except Exception:
            # Callers such as run_async use this from a finally block.  The
            # original command or input error is more useful than a cleanup
            # exception, while PlayerSessionService itself still attempts all
            # component closes before reporting its first failure.
            return

    async def _load_frozen_ruleset(self) -> FrozenRulesetSnapshot:
        """Verify and detach the per-game ruleset used by a manual save.

        ``KnowledgeSnapshotBuilder.load`` validates the manifest, file set and
        hashes without consulting the compiled store.  We then read the
        verified files with explicit containment checks so
        ``GameSnapshotStore`` can embed their bytes in the durable snapshot.
        """

        state = self.state
        ref = state.ruleset
        active_root = self.active_root
        if ref is None or active_root is None:
            raise ModeratorError("save requires a frozen ruleset snapshot")
        try:
            builder = KnowledgeSnapshotBuilder.from_game_root(
                CompiledKnowledgeStore(active_root.parent),
                active_root,
                game_id=state.game_id,
            )
            verified = await builder.load(state.game_id)
        except Exception as exc:
            raise ModeratorError("frozen ruleset snapshot could not be verified") from exc
        if (
            verified.board_ref.id != ref.board_id
            or verified.board_ref.version != ref.version
            or verified.snapshot_id != ref.snapshot_id
            or verified.manifest_sha256 != ref.manifest_sha256
        ):
            raise ModeratorError("frozen ruleset snapshot does not match game state")

        root = verified.root.resolve()
        files: dict[str, bytes] = {}
        for relative_path, expected_digest in sorted(verified.file_digests.items()):
            candidate = root / relative_path
            if candidate.is_symlink() or not candidate.is_file():
                raise ModeratorError(f"frozen ruleset contains a non-regular file: {relative_path}")
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
                content = resolved.read_bytes()
            except (OSError, ValueError) as exc:
                raise ModeratorError(
                    f"frozen ruleset file escapes its snapshot: {relative_path}"
                ) from exc
            if hashlib.sha256(content).hexdigest() != expected_digest:
                raise ModeratorError(f"frozen ruleset hash mismatch: {relative_path}")
            files[relative_path] = content
        try:
            return FrozenRulesetSnapshot.from_ref(ref, files=files)
        except (TypeError, ValueError) as exc:
            raise ModeratorError("frozen ruleset files could not be detached") from exc

    async def finish(self) -> dict[str, object]:
        if self.state.phase is not GamePhase.FINISHED:
            raise ModeratorError("finish requires the FINISHED phase; use next after victory check")
        if self.state.run_status is RunStatus.CLOSED:
            raise ModeratorError("game is already closed")
        previous_status = self.state.run_status
        await self._set_run_status(
            RunStatus.CLOSED,
            operation="FINISH",
            command="finish",
            reason="final archive requested",
        )
        try:
            # The final snapshot must carry CLOSED, so the status commit is
            # intentionally before save.  If either durable operation fails,
            # restore the pre-finish status through the same serialized path;
            # the command can then be retried against a consistent manager.
            saved = await self.save()
            assert self.archive_store is not None
            assert self.active_root is not None
            snapshot_path = saved.get("path")
            if not isinstance(snapshot_path, str):
                raise ModeratorError("save did not return a snapshot path")
            archive = await self.archive_store.finish(self.active_root, snapshot_path)
        except Exception as exc:
            if self.manager is not None:
                try:
                    await self.manager.commit_moderator_operation(
                        operation="FINISH_ROLLBACK",
                        command="finish",
                        expected_revision=self.state.state_revision,
                        run_status=previous_status,
                        reason="final snapshot or archive failed; finish remains retryable",
                        now=self.clock(),
                    )
                except (EventCommitError, ValueError, TypeError) as rollback_exc:
                    raise ModeratorError(
                        "finish failed and its status rollback could not be committed"
                    ) from rollback_exc
            if isinstance(exc, ModeratorError):
                raise
            raise ModeratorError("final snapshot or archive creation failed") from exc
        await self._close_sessions("game finished")
        return {
            "status": "finished",
            "game_id": self.state.game_id,
            "archive_id": archive.archive_id,
            "path": str(archive.path),
            "snapshot_id": saved["snapshot_id"],
        }

    async def execute(self, line: str) -> dict[str, object] | None:
        try:
            parts = shlex.split(line, posix=False)
        except ValueError as exc:
            raise ModeratorError("command syntax is invalid") from exc
        if not parts:
            return None
        command = parts[0].lower()
        if command == "new" and len(parts) == 1:
            return await self.new()
        if command == "start" and len(parts) == 1:
            return await self.start()
        if command == "prepare":
            return await self._prepare_command(parts[1:])
        if command == "status":
            private = any(part == "--private" for part in parts[1:])
            if any(part != "--private" for part in parts[1:]):
                raise ModeratorError("status accepts only --private")
            return await self.status(private=private)
        if command in {"help", "?"} and len(parts) == 1:
            return self.help()
        if command == "pause" and len(parts) == 1:
            return await self.pause()
        if command == "resume" and len(parts) == 1:
            return await self.resume()
        if command == "next" and len(parts) == 1:
            return await self.next()
        if command == "day":
            return await self._day_command(parts[1:])
        if command in {"last-words", "last_words", "lastwords"}:
            return await self._last_words_command(parts[1:])
        if command == "night":
            return await self._night_command(parts[1:])
        if command == "trigger":
            return await self._trigger_command(parts[1:])
        if command == "sheriff":
            return await self._sheriff_command(parts[1:])
        if command == "victory":
            return await self._victory_command(parts[1:])
        if command == "save" and len(parts) == 1:
            return await self.save()
        if command == "finish" and len(parts) == 1:
            return await self.finish()
        if command == "quit" and len(parts) == 1:
            self._closed = True
            await self._close_sessions("moderator quit")
            if self.manager is None:
                return {"status": "closed", "message": "moderator stopped"}
            return {
                "status": "closed",
                "message": "game remains recoverable only from the latest consistent snapshot",
                "game_id": self.state.game_id,
                "run_status": self.state.run_status.value,
            }
        if command in self._NOT_IMPLEMENTED:
            raise ModeratorError(f"{command} is not connected in this milestone")
        raise ModeratorError(f"unknown moderator command: {parts[0]}")

    async def run_async(self) -> None:
        try:
            while not self._closed:
                try:
                    line = self.input_fn()
                except (EOFError, KeyboardInterrupt):
                    self._closed = True
                    break
                try:
                    result = await self.execute(line)
                except ModeratorError as exc:
                    self.output_fn(_json({"status": "error", "error": str(exc)}))
                    continue
                if result is not None:
                    self.output_fn(_json(result))
        finally:
            await self._close_sessions("moderator loop ended")

    def run(self) -> None:
        asyncio.run(self.run_async())


__all__ = ["ModeratorError", "ModeratorShell"]
