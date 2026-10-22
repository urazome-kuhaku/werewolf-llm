"""Run bounded declarative scenarios through the production game boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path

from werewolf.domain.scenario import (
    AssertionField,
    HostCommandStep,
    ScenarioDefinition,
    ScenarioError,
    ScenarioMode,
    StateAssertionStep,
    build_host_command,
    load_scenario,
    scenario_sha256,
)
from werewolf.game.state import GameState
from werewolf.knowledge.preview import experimental_preview
from werewolf.moderator.config import PlayerSessionConfig
from werewolf.moderator.play_runner import (
    ClassicPlayRunner,
    PlayRunnerError,
    _read_config,
    _read_json,
    _verify_preview_provenance,
)
from werewolf.moderator.sessions import RuntimeFactory
from werewolf.persistence import resolve_contained_path
from werewolf.persistence.archive import GameArchiveStore
from werewolf.persistence.snapshot import GameSnapshotStore
from werewolf.runtime.declarative_runtime import (
    DeclarativeRuntime,
    ScenarioRunEvidence,
    ScenarioRuntimeError,
)


class ScenarioRunError(RuntimeError):
    """A scenario could not be run or did not pass its evidence gate."""


class _ScenarioPlayRunner(ClassicPlayRunner):
    def __init__(
        self,
        config_path: str | Path,
        *,
        evidence: ScenarioRunEvidence,
        runtime_factory: RuntimeFactory,
        output: Callable[[Mapping[str, object]], None] | None,
    ) -> None:
        super().__init__(
            config_path,
            max_rounds=evidence.scenario.budget.max_rounds,
            output=output,
            runtime_factory=runtime_factory,
            command_budget=evidence.scenario.budget.max_commands,
        )
        self._scenario_evidence = evidence

    async def command(self, line: str) -> Mapping[str, object]:
        pre_state = _state_summary(self.shell.state) if self.shell.manager is not None else None
        result = await super().command(line)
        post_state = _state_summary(self.shell.state) if self.shell.manager is not None else None
        self._scenario_evidence.observe_host_command(
            line,
            result,
            pre_state=pre_state,
            post_state=post_state,
        )
        return result


async def run_scenario(
    config_path: str | Path,
    scenario_path: str | Path,
    output_path: str | Path,
    *,
    experimental_preview_enabled: bool = False,
    expected_package_identity: str | None = None,
    enforce_mandatory_coverage: bool = False,
    output: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, object]:
    """Execute one scenario and write its evidence report, returning PASS/FAIL."""

    config_file = Path(config_path).expanduser().resolve()
    scenario_file = Path(scenario_path).expanduser().resolve()
    report_target = _report_target(output_path)
    scenario = load_scenario(scenario_file)
    config = _read_config(config_file)
    _assert_board_reference(config, scenario)
    setup_path = config_file.parent / "setup-report.json"
    setup_report: Mapping[str, object] = {}
    if setup_path.is_file():
        setup_report = _read_json(setup_path, label="setup-report.json")
    prepared_identity = setup_report.get("package_identity")
    if expected_package_identity is not None and prepared_identity != expected_package_identity:
        raise ScenarioRunError("play setup package identity does not match the expected digest")
    if not isinstance(prepared_identity, str) or len(prepared_identity) != 64:
        raise ScenarioRunError("play setup report must contain a compiled package identity")

    evidence = ScenarioRunEvidence(scenario=scenario, turn_budget=scenario.budget.max_turns)
    def make_runtime(
        _player: PlayerSessionConfig, gateway_url: str, token: str
    ) -> DeclarativeRuntime:
        return DeclarativeRuntime(gateway_url, token, evidence=evidence)

    runtime_factory: RuntimeFactory = make_runtime
    runner = _ScenarioPlayRunner(
        config_file,
        evidence=evidence,
        runtime_factory=runtime_factory,
        output=output,
    )
    failures: list[str] = []
    game_result: dict[str, object] | None = None
    archive_verified = False
    snapshot_verified = False
    archive_evidence: dict[str, object] | None = None
    archived_state: GameState | None = None
    assertion_results: list[dict[str, object]] = []
    try:
        if scenario.mode is ScenarioMode.FULL_GAME:
            game_result = await runner.run(
                experimental_preview_enabled=experimental_preview_enabled
            )
            archive_evidence, archived_state = await _verify_finished_archive(game_result)
            archive_verified = True
            snapshot_verified = True
            assertion_results = _evaluate_assertions(
                scenario,
                state=archived_state,
                archive_verified=archive_verified,
                snapshot_verified=snapshot_verified,
                evidence=evidence,
            )
        else:
            if experimental_preview_enabled:
                await _verify_preview_provenance(config_file, config)
            scope = experimental_preview() if experimental_preview_enabled else nullcontext()
            with scope:
                for step in scenario.steps:
                    if isinstance(step, HostCommandStep):
                        await runner.command(build_host_command(step))
                    elif isinstance(step, StateAssertionStep):
                        assertion_results.append(
                            _evaluate_assertion_step(
                                step,
                                state=(
                                    runner.shell.state
                                    if runner.shell.manager is not None
                                    else None
                                ),
                                archive_verified=False,
                                snapshot_verified=False,
                                evidence=evidence,
                            )
                        )
            game_result = {
                "status": "commands-complete",
                "game_id": runner.shell.state.game_id if runner.shell.manager is not None else None,
                "phase": runner.shell.state.phase.value
                if runner.shell.manager is not None
                else None,
                "run_status": runner.shell.state.run_status.value
                if runner.shell.manager is not None
                else None,
            }
            if runner.shell.manager is None:
                failures.append("COMMAND scenario did not create an authoritative game")
    except asyncio.CancelledError:
        if runner.shell.manager is not None:
            await runner._close_runtime("scenario run cancelled")
        raise
    except Exception as exc:
        failures.append(_safe_failure(exc))
        if runner.shell.manager is not None:
            await runner._close_runtime("scenario run failed")
    finally:
        if scenario.mode is ScenarioMode.COMMAND and runner.shell.manager is not None:
            await runner._close_runtime("scenario command complete")

    manager = runner.shell.manager
    live_final_state = runner.shell.state if runner.shell.manager is not None else None
    state = live_final_state or archived_state
    package = (
        runner.shell.runtime_bundle.package if runner.shell.runtime_bundle is not None else None
    )
    if state is None or manager is None or package is None or manager.execution_package is None:
        failures.append("scenario did not bind a frozen executable package")
    else:
        actual_package_identity = package.package_identity
        if actual_package_identity != prepared_identity:
            failures.append("frozen game package identity differs from prepared setup")
        if (
            expected_package_identity is not None
            and actual_package_identity != expected_package_identity
        ):
            failures.append("frozen game package identity differs from catalog candidate")
        if scenario.mode is ScenarioMode.FULL_GAME and (
            state.phase.value != "FINISHED" or state.run_status.value != "CLOSED"
        ):
            failures.append("FULL_GAME did not reach FINISHED and CLOSED")
        if scenario.mode is ScenarioMode.FULL_GAME and archived_state is not None:
            archived_digest = (
                archive_evidence.get("final_state_sha256")
                if archive_evidence is not None
                else None
            )
            if (
                live_final_state is None
                or archived_state.game_id != live_final_state.game_id
                or archived_state.state_revision != live_final_state.state_revision
                or archived_state.execution_identity != live_final_state.execution_identity
                or archived_state.phase != live_final_state.phase
                or archived_state.run_status != live_final_state.run_status
                or archived_state.winner != live_final_state.winner
                or archived_digest != _state_digest(live_final_state)
            ):
                failures.append(
                    "archived final snapshot does not match the live final state digest"
                )

    if scenario.mode is ScenarioMode.FULL_GAME:
        # FULL_GAME assertions run against the verified final archive snapshot.
        # COMMAND assertions were evaluated in place while the command stream ran.
        assertion_results = _evaluate_assertions(
            scenario,
            state=archived_state,
            archive_verified=archive_verified,
            snapshot_verified=snapshot_verified,
            evidence=evidence,
        )
    if state is not None:
        evidence.confirm_player_steps(state)
    for result in assertion_results:
        assertion_failure = result.get("failure")
        if isinstance(assertion_failure, str):
            failures.append(assertion_failure)
    unconsumed = evidence.required_unconsumed()
    if unconsumed:
        failures.append("required scenario steps were not consumed: " + ", ".join(unconsumed))

    (
        player_skill_uses,
        host_skill_uses,
        automatic_skill_uses,
        unclassified_skill_uses,
        skill_coverage,
        coverage_failures,
    ) = _skill_use_evidence(
        scenario,
        state=state,
        execution_package=manager.execution_package if manager is not None else None,
        player_turns=evidence.player_turns,
        enforce_mandatory_coverage=enforce_mandatory_coverage,
    )
    failures.extend(coverage_failures)
    status = "PASS" if not failures else "FAIL"
    frozen_identity = None
    if state is not None and state.execution_identity is not None:
        frozen_identity = state.execution_identity.model_dump(mode="json")
    report: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "scenario_id": scenario.scenario_id,
        "mode": scenario.mode.value,
        "board_ref": scenario.board_ref,
        "scenario_sha256": scenario_sha256(scenario),
        "prepared_source_digest": setup_report.get("source_digest"),
        "compiled_package_identity": prepared_identity,
        "frozen_execution_identity": frozen_identity,
        "execution_digest": (
            state.execution_identity.execution_digest
            if state is not None and state.execution_identity is not None
            else None
        ),
        "game_result": game_result,
        "budget": {
            "max_commands": scenario.budget.max_commands,
            "commands_used": runner.command_count,
            "max_turns": scenario.budget.max_turns,
            "turns_used": evidence.turn_count,
            "max_rounds": scenario.budget.max_rounds,
        },
        "steps": evidence.steps_report(),
        "assertions": assertion_results,
        "player_casts": player_skill_uses,
        "host_ledger_uses": host_skill_uses,
        "automatic_ledger_uses": automatic_skill_uses,
        "unclassified_ledger_uses": unclassified_skill_uses,
        "skill_coverage": skill_coverage,
        "gateway_reads": evidence.gateway_reads,
        "private_skill_query_counts": {
            str(seat): count for seat, count in sorted(evidence.skill_queries.items())
        },
        "player_turns": evidence.player_turns,
        "host_commands": evidence.host_commands,
        "archive": archive_evidence,
        "proof_summary": {
            "required_steps_total": sum(1 for item in scenario.steps if item.required),
            "required_steps_consumed": sum(
                1
                for item in scenario.steps
                if item.required and item.step_id in evidence.used_step_ids
            ),
            "successful_player_casts": len(player_skill_uses),
            "successful_host_uses": len(host_skill_uses),
            "successful_automatic_uses": len(automatic_skill_uses),
            "ledger_batches": len(state.rule_ledger) if state is not None else 0,
            "gateway_reads": len(evidence.gateway_reads),
            "private_skill_queries": sum(evidence.skill_queries.values()),
        },
        "failures": failures,
    }
    _write_report(report_target, report)
    return report


async def _verify_finished_archive(
    game_result: Mapping[str, object],
) -> tuple[dict[str, object], GameState]:
    archive_value = game_result.get("archive_path")
    if not isinstance(archive_value, str):
        raise ScenarioRunError("FULL_GAME result does not contain an archive path")
    archive_path = Path(archive_value).resolve()
    archive_store = GameArchiveStore(archive_path.parent.parent)
    manifest = await archive_store.verify(archive_path)
    final_path = resolve_contained_path(archive_path, manifest.final_snapshot_path)
    snapshot_manifest = await GameSnapshotStore(archive_path).verify(final_path)
    if (
        snapshot_manifest.game_id != manifest.game_id
        or snapshot_manifest.snapshot_id != manifest.final_snapshot_id
        or snapshot_manifest.state_revision != manifest.final_state_revision
        or snapshot_manifest.manifest_sha256 != manifest.final_snapshot_manifest_sha256
    ):
        raise ScenarioRunError("archive final snapshot does not match its manifest")
    try:
        archived_state = GameState.model_validate_json((final_path / "state.json").read_bytes())
    except Exception as exc:
        raise ScenarioRunError("archived final snapshot state could not be loaded") from exc
    if archived_state.phase.value != "FINISHED" or archived_state.run_status.value != "CLOSED":
        raise ScenarioRunError("archived snapshot is not FINISHED and CLOSED")
    state_file = next(
        (item for item in snapshot_manifest.files if item.relative_path == "state.json"), None
    )
    if state_file is None or state_file.sha256 != _state_digest(archived_state):
        raise ScenarioRunError("archived final state digest does not match the snapshot manifest")
    return (
        {
            "path": str(archive_path),
            "archive_id": manifest.archive_id,
            "game_id": manifest.game_id,
            "final_snapshot_path": manifest.final_snapshot_path,
            "final_snapshot_id": manifest.final_snapshot_id,
            "final_snapshot_manifest_sha256": snapshot_manifest.manifest_sha256,
            "final_state_sha256": state_file.sha256,
            "archive_verified": True,
            "snapshot_verified": True,
        },
        archived_state,
    )


def _skill_use_evidence(
    scenario: ScenarioDefinition,
    *,
    state: GameState | None,
    execution_package: object | None,
    player_turns: list[dict[str, object]],
    enforce_mandatory_coverage: bool,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
    list[str],
]:
    if state is None or execution_package is None:
        return [], [], [], [], {}, ["skill coverage requires the frozen package and game ledger"]
    skills = tuple(getattr(execution_package, "skills", ()))
    skill_modes = {
        str(getattr(skill, "skill_id")): str(getattr(skill, "mode", "PLAYER"))
        for skill in skills
        if isinstance(getattr(skill, "skill_id", None), str)
    }
    committed_casts: dict[str, dict[str, object]] = {}
    for turn in player_turns:
        outer_id = turn.get("request_id")
        if turn.get("expected_kind") != "action" or not isinstance(outer_id, str):
            continue
        outer = state.action_requests.get(outer_id)
        if not isinstance(outer, Mapping) or outer.get("status") != "CONFIRMED":
            continue
        outer_actions = outer.get("actions")
        dispositions = outer.get("rule_dispositions")
        if not isinstance(outer_actions, list | tuple) or not isinstance(
            dispositions, list | tuple
        ):
            continue
        receipt_id = outer.get("rule_receipt_id")
        matching_batches = {
            entry.batch_id
            for entry in state.rule_ledger
            if entry.batch_id == receipt_id and outer_id in entry.request_ids
        }
        if not matching_batches:
            continue
        for disposition in dispositions:
            if not isinstance(disposition, Mapping):
                continue
            index = disposition.get("action_index")
            internal_id = disposition.get("skill_request_id")
            if type(index) is not int or not 0 <= index < len(outer_actions):
                continue
            action = outer_actions[index]
            if not isinstance(action, Mapping):
                continue
            if (
                not isinstance(internal_id, str)
                or disposition.get("status") != "ACCEPTED"
                or disposition.get("action_code") != action.get("action_code")
                or disposition.get("skill_id") is None
                or disposition.get("ability_instance_id") is None
            ):
                continue
            committed_casts[internal_id] = {
                "outer_request_id": outer_id,
                "batch_id": receipt_id,
                "action_index": index,
                "action_code": action.get("action_code"),
                "skill_id": disposition.get("skill_id"),
                "ability_instance_id": disposition.get("ability_instance_id"),
                "actor_seat": outer.get("seat"),
                "status": disposition.get("status"),
            }
    player_uses: list[dict[str, object]] = []
    host_uses: list[dict[str, object]] = []
    automatic_uses: list[dict[str, object]] = []
    unclassified_uses: list[dict[str, object]] = []
    for entry in state.rule_ledger:
        for use in entry.history_updates:
            if (
                use.action_code == 299
                or use.passed
                or not use.successful
                or use.disposition != "ACCEPTED"
            ):
                continue
            item = {
                "request_id": use.request_id,
                "batch_id": entry.batch_id,
                "skill_id": use.skill_id,
                "action_code": use.action_code,
                "actor_seat": use.actor_seat,
                "round_number": use.round_number,
                "successful": True,
                "passed": False,
            }
            cast_proof = committed_casts.get(use.request_id)
            if cast_proof is not None:
                if (
                    cast_proof["batch_id"] != entry.batch_id
                    or cast_proof["skill_id"] != use.skill_id
                    or cast_proof["action_code"] != use.action_code
                    or cast_proof["ability_instance_id"] != use.ability_instance_id
                    or cast_proof["actor_seat"] != use.actor_seat
                ):
                    continue
                player_uses.append(
                    {**item, **cast_proof, "evidence_scope": "CONFIRMED_PLAYER_ACTION"}
                )
            elif skill_modes.get(use.skill_id) == "HOST":
                host_uses.append({**item, "evidence_scope": "HOST_LEDGER"})
            elif skill_modes.get(use.skill_id) == "AUTOMATIC":
                automatic_uses.append({**item, "evidence_scope": "AUTOMATIC_LEDGER"})
            else:
                unclassified_uses.append({**item, "evidence_scope": "UNCLASSIFIED_LEDGER"})
    actual_player_skills = {str(item["skill_id"]) for item in player_uses}
    actual_automatic_skills = {str(item["skill_id"]) for item in automatic_uses}
    required_player = {
        skill for skill in scenario.required_skill_ids if skill_modes.get(skill) == "PLAYER"
    }
    required_automatic = {
        skill for skill in scenario.required_skill_ids if skill_modes.get(skill) == "AUTOMATIC"
    }
    if enforce_mandatory_coverage:
        # Runtime ability instances include both the initial role grants and
        # any dynamic ABILITY_GRANT instances produced during this execution.
        actually_granted = {item.skill_id for item in state.ability_instances}
        required_player.update(
            skill_id
            for skill_id, mode in skill_modes.items()
            if mode == "PLAYER" and skill_id in actually_granted
        )
        required_automatic.update(
            skill_id for skill_id, mode in skill_modes.items() if mode == "AUTOMATIC"
        )
    required_host = set(scenario.required_host_skill_ids)
    invalid_explicit_player = sorted(
        skill
        for skill in scenario.required_skill_ids
        if skill_modes.get(skill) not in {"PLAYER", "AUTOMATIC"}
    )
    invalid_explicit_host = sorted(
        skill for skill in required_host if skill_modes.get(skill) != "HOST"
    )
    missing_player = sorted(required_player - actual_player_skills)
    missing_automatic = sorted(required_automatic - actual_automatic_skills)
    actual_host_skills = {str(item["skill_id"]) for item in host_uses}
    missing_host = sorted(required_host - actual_host_skills)
    failures: list[str] = []
    if invalid_explicit_player:
        failures.append(
            "required_skill_ids must refer to PLAYER or AUTOMATIC skills: "
            + ", ".join(invalid_explicit_player)
        )
    if invalid_explicit_host:
        failures.append(
            "required_host_skill_ids must refer to HOST skills: " + ", ".join(invalid_explicit_host)
        )
    if missing_player:
        failures.append(
            "required PLAYER skills had no confirmed successful cast: " + ", ".join(missing_player)
        )
    if missing_automatic:
        failures.append(
            "AUTOMATIC skills had no successful ledger use: " + ", ".join(missing_automatic)
        )
    if missing_host:
        failures.append(
            "required HOST skills had no successful ledger use: " + ", ".join(missing_host)
        )
    coverage: dict[str, object] = {
        "actually_granted_player_skill_ids": sorted(
            {
                item.skill_id
                for item in state.ability_instances
                if skill_modes.get(item.skill_id) == "PLAYER"
            }
        ),
        "required_player_skill_ids": sorted(required_player),
        "covered_player_skill_ids": sorted(required_player & actual_player_skills),
        "missing_player_skill_ids": missing_player,
        "required_automatic_skill_ids": sorted(required_automatic),
        "covered_automatic_skill_ids": sorted(required_automatic & actual_automatic_skills),
        "missing_automatic_skill_ids": missing_automatic,
        "required_host_skill_ids": sorted(required_host),
        "covered_host_skill_ids": sorted(required_host & actual_host_skills),
        "missing_host_skill_ids": missing_host,
    }
    return player_uses, host_uses, automatic_uses, unclassified_uses, coverage, failures


def _evaluate_assertions(
    scenario: ScenarioDefinition,
    *,
    state: GameState | None,
    archive_verified: bool,
    snapshot_verified: bool,
    evidence: ScenarioRunEvidence,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for step in scenario.steps:
        if not isinstance(step, StateAssertionStep):
            continue
        results.append(
            _evaluate_assertion_step(
                step,
                state=state,
                archive_verified=archive_verified,
                snapshot_verified=snapshot_verified,
                evidence=evidence,
            )
        )
    return results


def _evaluate_assertion_step(
    step: StateAssertionStep,
    *,
    state: GameState | None,
    archive_verified: bool,
    snapshot_verified: bool,
    evidence: ScenarioRunEvidence,
) -> dict[str, object]:
    actual = _assertion_value(
            step,
            state=state,
            archive_verified=archive_verified,
            snapshot_verified=snapshot_verified,
        )
    passed = _compare(step, actual)
    if passed:
        evidence.used_step_ids.add(step.step_id)
    return {
        "step_id": step.step_id,
        "field": step.field.value,
        "selectors": {
            key: value
            for key, value in {
                "seat": step.seat,
                "skill_id": step.skill_id,
                "state_key": step.state_key,
                "resource_id": step.resource_id,
                "relation_type": step.relation_type,
            }.items()
            if value is not None
        },
        "operator": step.operator,
        "expected": step.expected,
        "actual": actual,
        "passed": passed,
        **(
            {}
            if passed or not step.required
            else {"failure": f"assertion failed: {step.step_id}"}
        )
    }


def _assertion_value(
    step: StateAssertionStep,
    *,
    state: GameState | None,
    archive_verified: bool,
    snapshot_verified: bool,
) -> object:
    field = step.field
    if field is AssertionField.ARCHIVE_VERIFIED:
        return archive_verified
    if field is AssertionField.SNAPSHOT_VERIFIED:
        return snapshot_verified
    if state is None:
        return None
    if field is AssertionField.PHASE:
        return state.phase.value
    if field is AssertionField.RUN_STATUS:
        return state.run_status.value
    if field is AssertionField.ROUND_NO:
        return state.round_no
    if field is AssertionField.DAY_NO:
        return state.day_no
    if field is AssertionField.WINNER_SIDE:
        return _winner_side(state)
    if field is AssertionField.ALIVE:
        player = state.players.get(step.seat) if step.seat is not None else None
        return player.alive if player is not None else None
    if field is AssertionField.DEATH_CAUSE:
        player = state.players.get(step.seat) if step.seat is not None else None
        return player.death_cause if player is not None else None
    if field is AssertionField.SKILL_USE_COUNT:
        return sum(
            1
            for entry in state.rule_ledger
            for use in entry.history_updates
            if use.skill_id == step.skill_id
            and use.action_code != 299
            and not use.passed
            and use.successful
            and use.disposition == "ACCEPTED"
        )
    if field is AssertionField.GAME_STATE_VALUE:
        matching = [
            item
            for item in state.rule_state
            if item.scope == "GAME" and item.key == step.state_key
        ]
        if matching and type(matching[-1].value) is bool:
            return matching[-1].value
        return None
    if field is AssertionField.RESOURCE_BALANCE:
        if step.resource_id is None:
            return None
        player = state.players.get(step.seat) if step.seat is not None else None
        return player.skill_resources.get(step.resource_id) if player is not None else None
    if field is AssertionField.RELATION_COUNT:
        return sum(item.relation_type == step.relation_type for item in state.rule_relations)
    return None


def _compare(step: StateAssertionStep, actual: object) -> bool:
    expected = step.expected
    if step.operator == "EQ":
        return actual == expected
    if step.operator == "NE":
        return actual != expected
    if step.operator == "IN":
        return isinstance(expected, list | tuple) and actual in expected
    if step.operator in {"GE", "LE"}:
        if type(actual) is not int or type(expected) is not int:
            return False
        return actual >= expected if step.operator == "GE" else actual <= expected
    return False


def _winner_side(state: GameState) -> object:
    winner = state.winner
    if isinstance(winner, Mapping):
        return winner.get("side")
    return None


def _state_summary(state: GameState) -> dict[str, object]:
    return {
        "game_id": state.game_id,
        "state_revision": state.state_revision,
        "phase": state.phase.value,
        "run_status": state.run_status.value,
        "round_no": state.round_no,
        "day_no": state.day_no,
    }


def _state_digest(state: GameState) -> str:
    raw = (
        json.dumps(
            state.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _assert_board_reference(config: Mapping[str, object], scenario: ScenarioDefinition) -> None:
    game = config.get("game")
    board = game.get("board") if isinstance(game, Mapping) else None
    if not isinstance(board, Mapping):
        raise ScenarioRunError("configuration.game.board is required")
    board_id = board.get("id")
    version = board.get("version")
    if f"{board_id}@{version}" != scenario.board_ref:
        raise ScenarioRunError("scenario board reference does not match game configuration")


def _report_target(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser().resolve()
    if path.suffix.lower() == ".json":
        return path
    return path / "scenario-report.json"


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _safe_failure(exc: BaseException) -> str:
    if isinstance(exc, (PlayRunnerError, ScenarioRuntimeError, ScenarioRunError, ScenarioError)):
        return f"{type(exc).__name__}: {str(exc)[:300]}"
    return f"{type(exc).__name__}: {str(exc)[:300]}"


__all__ = ["ScenarioRunError", "run_scenario"]
