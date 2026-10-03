"""Non-interactive whole-game runner for the classic playable setup.

The runner is intentionally a thin moderator client.  It drives the same
command vocabulary exposed by :class:`ModeratorShell`, so a run made from
this module exercises the production session launcher and all authoritative
phase boundaries.  It never supplies a fake action or a private result to the
game manager.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from werewolf.domain.enums import GamePhase
from werewolf.game.sheriff_eligibility import first_day_sheriff_participants
from werewolf.game.voting import VoteState
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.preview import experimental_preview
from werewolf.knowledge.refs import VersionedRef
from werewolf.moderator.shell import ModeratorError, ModeratorShell


class PlayRunnerError(RuntimeError):
    """A bounded failure while driving a complete playable game."""


PublicProgress = Callable[[Mapping[str, object]], None]


def _read_json(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise PlayRunnerError(f"{label} exceeds the 1 MiB limit")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlayRunnerError(f"{label} could not be read") from exc
    if not isinstance(value, Mapping):
        raise PlayRunnerError(f"{label} must be a JSON object")
    return value


def _read_config(path: Path) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise PlayRunnerError("configuration exceeds the 1 MiB limit")
        value = yaml.safe_load(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PlayRunnerError("configuration could not be read") from exc
    if not isinstance(value, Mapping):
        raise PlayRunnerError("configuration root must be a mapping")
    return value


def _resolve_config_path(value: object, *, config_dir: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise PlayRunnerError("configuration path must be a non-empty string")
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else config_dir / candidate).resolve()


async def _verify_preview_provenance(config_path: Path, config: Mapping[str, object]) -> None:
    """Verify the generated setup report against its detached compiled store.

    This check deliberately uses only the setup report and the compiled
    package manifest.  It does not infer approval from Markdown or silently
    promote a candidate package.
    """

    setup = config.get("play_setup")
    if not isinstance(setup, Mapping):
        raise PlayRunnerError("--experimental-preview requires a play_setup marker")
    if setup.get("status") != "EXPERIMENTAL_CANDIDATE":
        raise PlayRunnerError("play_setup status is not EXPERIMENTAL_CANDIDATE")
    if setup.get("candidate_status") != "CANDIDATE_PENDING_HUMAN_REVIEW":
        raise PlayRunnerError("play_setup candidate status is not pending human review")
    board = config.get("game")
    if not isinstance(board, Mapping):
        raise PlayRunnerError("configuration.game is required")
    board_ref_value = board.get("board")
    if not isinstance(board_ref_value, Mapping):
        raise PlayRunnerError("configuration.game.board is required")
    board_id = board_ref_value.get("id")
    board_version = board_ref_value.get("version")
    if not isinstance(board_id, str) or not isinstance(board_version, str):
        raise PlayRunnerError("configuration.game.board is malformed")
    board_ref = VersionedRef.parse(f"{board_id}@{board_version}")
    if setup.get("board_ref") != board_ref.format():
        raise PlayRunnerError("play_setup board reference does not match game.board")

    report_path = config_path.parent / "setup-report.json"
    report = _read_json(report_path, label="setup-report.json")
    for key in ("status", "candidate_status", "board_ref", "source_manifest_sha256"):
        if report.get(key) != setup.get(key):
            raise PlayRunnerError(f"setup provenance mismatch: {key}")
    compiled_root_value = None
    paths = config.get("paths")
    if isinstance(paths, Mapping):
        compiled_root_value = paths.get("compiled_root")
    compiled_root = _resolve_config_path(compiled_root_value, config_dir=config_path.parent)
    try:
        package = await CompiledKnowledgeStore(compiled_root).load(
            board_ref, expected_board_ref=board_ref
        )
    except Exception as exc:
        raise PlayRunnerError("preview compiled package could not be verified") from exc
    if report.get("package_identity") != package.package_identity:
        raise PlayRunnerError("setup report package identity does not match compiled package")
    manifest = _plain_json(package.manifest_payload)
    manifest_digest = (
        manifest.get("logical_manifest_sha256") if isinstance(manifest, Mapping) else None
    )
    if report.get("compiled_manifest_sha256") != manifest_digest:
        raise PlayRunnerError("setup report manifest hash does not match compiled package")


def _value_mapping(value: object, key: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PlayRunnerError(f"runner result is missing {key}")
    return value


def _plain_json(value: object) -> object:
    if hasattr(value, "model_dump"):
        return _plain_json(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _phase(result: object) -> GamePhase:
    payload = _value_mapping(result, "status")
    value = payload.get("phase")
    if not isinstance(value, str):
        raise PlayRunnerError("runner received an invalid phase")
    try:
        return GamePhase(value)
    except (TypeError, ValueError) as exc:
        raise PlayRunnerError("runner received an invalid phase") from exc


def _emit_progress(callback: PublicProgress | None, result: object, command: str) -> None:
    if callback is None or not isinstance(result, Mapping):
        return
    phase = result.get("phase")
    event: dict[str, object] = {"event": "progress", "command": command}
    if isinstance(phase, str):
        event["phase"] = phase
    for key in ("game_id", "run_status", "round_no", "day_no", "state_revision"):
        value = result.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            event[key] = value
    callback(event)


class ClassicPlayRunner:
    """Drive one classic game with the normal moderator shell."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        max_rounds: int = 20,
        output: PublicProgress | None = None,
        sheriff_candidates: tuple[int, ...] | None = None,
    ) -> None:
        if type(max_rounds) is not int or max_rounds < 1 or max_rounds > 10_000:
            raise ValueError("max_rounds must be an integer between 1 and 10000")
        self.config_path = Path(config_path).expanduser().resolve()
        self.max_rounds = max_rounds
        self.output = output
        self.sheriff_candidates = sheriff_candidates
        self.shell = ModeratorShell(self.config_path)
        self._public_event_ids: set[int] = set()

    async def command(self, line: str) -> Mapping[str, object]:
        try:
            result = await self.shell.execute(line)
        except ModeratorError as exc:
            raise PlayRunnerError(f"{line}: {exc}") from exc
        if not isinstance(result, Mapping):
            raise PlayRunnerError(f"{line}: moderator returned no status")
        _emit_progress(self.output, result, line)
        self._emit_public_events()
        return result

    def _emit_public_events(self) -> None:
        if self.output is None:
            return
        for event in self.shell.state.events:
            event_id = getattr(event, "event_id", None)
            channel = getattr(event, "channel", None)
            channel_value = getattr(channel, "value", channel)
            if type(event_id) is not int or event_id in self._public_event_ids:
                continue
            if channel_value != "PUBLIC":
                continue
            self._public_event_ids.add(event_id)
            self.output(
                {
                    "event": "public",
                    "event_id": event_id,
                    "event_type": getattr(getattr(event, "event_type", None), "value", None),
                    "phase": getattr(getattr(event, "phase", None), "value", None),
                    "payload": _plain_json(getattr(event, "payload", {})),
                }
            )

    async def run(self, *, experimental_preview_enabled: bool = False) -> dict[str, object]:
        config = _read_config(self.config_path)
        if experimental_preview_enabled:
            await _verify_preview_provenance(self.config_path, config)
        scope = experimental_preview() if experimental_preview_enabled else nullcontext()
        try:
            with scope:
                return await self._run_game()
        except asyncio.CancelledError:
            await self._close_runtime("play run cancelled")
            raise
        except Exception:
            await self._close_runtime("play run failed")
            raise

    async def _run_game(self) -> dict[str, object]:
        await self.command("new")
        await self.command("next")
        await self.command("next")
        await self.command("start")
        await self._prepare()
        await self.command("next")

        while True:
            phase = self.shell.state.phase
            if phase is GamePhase.FINISHED:
                break
            # ``round_no`` advances when entering VICTORY_CHECK.  Let the
            # current cycle perform its explicit victory check and snapshot;
            # enforce the limit only when the runner would start another
            # night cycle.
            if phase is GamePhase.NIGHT_TEAM_CHAT and self.shell.state.round_no >= self.max_rounds:
                raise PlayRunnerError(
                    f"round limit reached before victory: {self.shell.state.round_no}"
                )
            if phase is GamePhase.NIGHT_TEAM_CHAT:
                await self._night_team()
            elif phase is GamePhase.NIGHT_ACTION:
                await self._night_actions()
            elif phase is GamePhase.NIGHT_RESOLVE:
                await self._night_resolve()
            elif phase is GamePhase.DAY_ANNOUNCE:
                await self._day_announce()
            elif phase in {
                GamePhase.SHERIFF_ELECTION_SPEECH,
                GamePhase.SHERIFF_ELECTION,
                GamePhase.SHERIFF_ELECTION_PK_SPEECH,
                GamePhase.SHERIFF_ELECTION_PK,
                GamePhase.SHERIFF_TRANSFER,
            }:
                await self._sheriff()
            elif phase in {
                GamePhase.DAY_SPEECH,
                GamePhase.VOTE,
                GamePhase.VOTE_PK_SPEECH,
                GamePhase.VOTE_PK,
            }:
                await self._day_vote()
            elif phase is GamePhase.DAY_RESOLVE:
                await self._day_resolve()
            elif phase is GamePhase.TRIGGER_ACTION:
                await self._trigger()
            elif phase is GamePhase.VICTORY_CHECK:
                await self.command("victory check")
            else:
                raise PlayRunnerError(f"unsupported phase: {phase.value}")

        finished = await self.command("finish")
        await self._close_runtime("play run complete")
        return {
            "status": "finished",
            "game_id": self.shell.state.game_id,
            "phase": self.shell.state.phase.value,
            "run_status": self.shell.state.run_status.value,
            "round_no": self.shell.state.round_no,
            "day_no": self.shell.state.day_no,
            "winner": _plain_json(self.shell.state.winner),
            "archive_id": finished.get("archive_id"),
            "archive_path": finished.get("path"),
            "snapshot_id": finished.get("snapshot_id"),
        }

    async def _prepare(self) -> None:
        while self.shell.state.phase is GamePhase.PLAYER_PREPARE:
            status = await self.command("prepare status")
            if status.get("ready") is True:
                return
            await self.command("prepare next")

    async def _night_team(self) -> None:
        await self.command("night open")
        while True:
            result = await self.command("night team next")
            night = result.get("night")
            # The authoritative queue is persisted by the serial scheduler.
            # Read it directly after the command so a stale/partial progress
            # projection cannot schedule one extra team turn.
            if self.shell.state.current_queue == () or (
                isinstance(night, Mapping) and night.get("team_queue") == []
            ):
                break
        plan_status = await self.command("night plan status")
        plan = plan_status.get("plan")
        if isinstance(plan, Mapping) and plan.get("enabled") is True:
            await self.command("night plan next")
        await self.command("night advance")

    async def _night_actions(self) -> None:
        await self.command("night open")
        while self.shell.state.phase is GamePhase.NIGHT_ACTION:
            result = await self.command("night action next")
            night = result.get("night")
            window = night.get("window") if isinstance(night, Mapping) else None
            submitted = night.get("submitted_seats") if isinstance(night, Mapping) else None
            allowed = window.get("allowed_seats") if isinstance(window, Mapping) else None
            if (
                isinstance(submitted, list)
                and isinstance(allowed, list)
                and len(submitted) >= len(allowed)
            ):
                break
        await self.command("night advance")

    async def _night_resolve(self) -> None:
        await self.command("night open")
        await self.command("night auto-resolve")

    async def _day_announce(self) -> None:
        # A completed night can already satisfy a unique victory condition.
        # The dedicated boundary snapshots that winner and skips an otherwise
        # meaningless announcement/election; ONGOING returns to normal day.
        await self.command("victory night-check")
        if self.shell.state.phase is GamePhase.FINISHED:
            return
        if self.shell.state.day_no == 1 and self._sheriff_enabled():
            candidates = self.sheriff_candidates
            if candidates is None:
                candidates = first_day_sheriff_participants(self.shell.state)[:2]
            if len(candidates) < 1:
                raise PlayRunnerError("no eligible sheriff candidate is available")
            await self.command("sheriff start " + " ".join(str(seat) for seat in candidates))
        elif self._badge_required():
            # Keep DAY_ANNOUNCE open while publishing the death announcement.
            # The badge window belongs to that same boundary and must be
            # completed before ordinary speech can start.
            await self.command("day announce")
            await self.command("sheriff badge open")
            await self.command("sheriff badge next")
            await self.command("sheriff badge resolve")
            await self.command("sheriff badge finish")
            await self._last_words()
            await self.command("day speech open")
        else:
            await self.command("day announce")
            await self._last_words()
            await self.command("day speech open")

    def _sheriff_enabled(self) -> bool:
        bundle = self.shell.runtime_bundle
        return bool(bundle is not None and bundle.board.day_flow.sheriff.enabled)

    def _alive_seats(self) -> tuple[int, ...]:
        return tuple(
            sorted(seat for seat, player in self.shell.state.players.items() if player.alive)
        )

    def _badge_required(self) -> bool:
        bundle = self.shell.runtime_bundle
        state = self.shell.state
        if bundle is None or state.sheriff_seat is None:
            return False
        sheriff = bundle.board.day_flow.sheriff
        player = state.players.get(state.sheriff_seat)
        if player is None or (player.alive and player.can_vote):
            return False
        if player.alive:
            return sheriff.transfer_enabled is True and sheriff.transfer_on_resignation is True
        return sheriff.transfer_enabled is True and sheriff.transfer_on_death is True

    async def _sheriff(self) -> None:
        phase = self.shell.state.phase
        if phase in {GamePhase.SHERIFF_ELECTION_SPEECH, GamePhase.SHERIFF_ELECTION_PK_SPEECH}:
            await self.command("sheriff speech next")
            progress = await self.command("sheriff status")
            sheriff = progress.get("sheriff")
            speech = sheriff.get("speech") if isinstance(sheriff, Mapping) else None
            queue = speech.get("queue") if isinstance(speech, Mapping) else None
            if queue == []:
                await self.command("sheriff vote open")
            return
        if phase in {GamePhase.SHERIFF_ELECTION, GamePhase.SHERIFF_ELECTION_PK}:
            await self.command("sheriff vote next")
            status = await self.command("sheriff status")
            sheriff = status.get("sheriff")
            vote = sheriff.get("vote") if isinstance(sheriff, Mapping) else None
            if isinstance(vote, Mapping) and vote.get("missing_count") == 0:
                await self.command("sheriff vote collect")
                await self.command("sheriff vote confirm")
            return
        if phase is GamePhase.SHERIFF_TRANSFER:
            await self.command("sheriff transfer")
            # The first-day election completes before the death announcement.
            # Publish it while retaining the DAY_SPEECH boundary, then let a
            # dead elected sheriff complete the badge choice before speeches.
            await self.command("day announce")
            if self._badge_required():
                await self.command("sheriff badge open")
                await self.command("sheriff badge next")
                await self.command("sheriff badge resolve")
                await self.command("sheriff badge finish")
            await self._last_words()
            await self.command("day speech open")
            return
        raise PlayRunnerError(f"unexpected sheriff phase: {phase.value}")

    async def _day_vote(self) -> None:
        phase = self.shell.state.phase
        if phase is GamePhase.DAY_SPEECH:
            await self.command("day speech next")
            progress = await self.command("day status")
            day = progress.get("day")
            speech = day.get("speech") if isinstance(day, Mapping) else None
            if isinstance(speech, Mapping) and speech.get("queue") == []:
                await self.command("day speech close")
            return
        if phase is GamePhase.VOTE_PK_SPEECH:
            await self.command("day pk speech next")
            progress = await self.command("day status")
            day = progress.get("day")
            speech = day.get("speech") if isinstance(day, Mapping) else None
            if isinstance(speech, Mapping) and speech.get("queue") == []:
                await self.command("day pk speech close")
            return
        if phase in {GamePhase.VOTE, GamePhase.VOTE_PK}:
            command_prefix = "day pk vote" if phase is GamePhase.VOTE_PK else "day vote"
            await self.command(f"{command_prefix} open")
            while not self._vote_complete():
                await self.command(f"{command_prefix} next")
            await self.command(f"{command_prefix} collect")
            await self.command(f"{command_prefix} confirm")
            return
        raise PlayRunnerError(f"unexpected day vote phase: {phase.value}")

    def _vote_complete(self) -> bool:
        raw = self.shell.state.vote_state
        if raw is None:
            return False
        try:
            # GameState stores vote_state as frozen JSON containers.  A JSON
            # round trip restores enum strings and integer mapping keys for
            # VoteState's strict validators.
            vote_state = VoteState.model_validate_json(json.dumps(_plain_json(raw)))
        except (TypeError, ValueError):
            return False
        return not vote_state.missing_voters

    async def _day_resolve(self) -> None:
        state = self.shell.state
        result = state.vote_state
        target: int | None = None
        if isinstance(result, Mapping):
            public = result.get("public_result")
            if isinstance(public, Mapping):
                value = public.get("eliminated_seat")
                if type(value) is int:
                    target = value
        await self.command(f"day confirm-exile {target if target is not None else 'none'}")
        if self.shell.state.phase is GamePhase.TRIGGER_ACTION:
            await self._trigger()
            return
        await self._last_words()
        if self._badge_required():
            await self.command("sheriff badge open")
            await self.command("sheriff badge next")
            await self.command("sheriff badge resolve")
            await self.command("sheriff badge finish")
            return
        await self.command("day finish")

    async def _last_words(self) -> None:
        """Drain the board-defined last-words queue when present."""
        status = await self.command("last-words status")
        while True:
            payload = status.get("last_words")
            pending = payload.get("pending_seats") if isinstance(payload, Mapping) else None
            if not pending:
                return
            seat = pending[0]
            if type(seat) is not int:
                raise PlayRunnerError("last-words status returned an invalid seat")
            status = await self.command(f"last-words next {seat}")

    async def _trigger(self) -> None:
        await self.command("trigger open")
        pending = await self.command("trigger pending")
        trigger = pending.get("trigger")
        requests = trigger.get("pending_requests") if isinstance(trigger, Mapping) else None
        if not requests:
            await self.command("trigger next")
        operation = trigger.get("operation") if isinstance(trigger, Mapping) else None
        await self.command("trigger auto-resolve")
        if operation == "DAY_EXILE":
            # A day exile trigger stays inside the daytime resolution edge:
            # last words, then any badge choice, then victory check.
            await self._last_words()
            if self._badge_required():
                await self.command("sheriff badge open")
                await self.command("sheriff badge next")
                await self.command("sheriff badge resolve")
                await self.command("sheriff badge finish")
                return
        elif operation != "NIGHT_RESOLUTION":
            raise PlayRunnerError("trigger result has unknown operation")
        # A night trigger only closes its trigger boundary.  Dawn owns the
        # public announcement, last words, and any sheriff badge choice.
        await self.command("trigger finish")

    async def _close_runtime(self, reason: str) -> None:
        service = self.shell.session_service
        if service is not None:
            try:
                await service.close(reason)
            except Exception:
                # Preserve the original runner error.  Session close is
                # best-effort here, while the service itself closes every
                # seat and the gateway in a bounded finally path.
                pass
            self.shell.session_service = None


async def run_game(
    config_path: str | Path,
    *,
    max_rounds: int = 20,
    experimental_preview: bool = False,
    output: PublicProgress | None = None,
    sheriff_candidates: tuple[int, ...] | None = None,
) -> dict[str, object]:
    """Run one complete game through the production moderator shell."""

    return await ClassicPlayRunner(
        config_path,
        max_rounds=max_rounds,
        output=output,
        sheriff_candidates=sheriff_candidates,
    ).run(experimental_preview_enabled=experimental_preview)


__all__ = ["ClassicPlayRunner", "PlayRunnerError", "run_game"]
