"""Gateway-backed player runtime driven by a closed scenario document."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from werewolf.domain.scenario import (
    HostCommandStep,
    PlayerTurnStep,
    ScenarioDefinition,
    host_command_matches,
)
from werewolf.runtime.demo_runtime import DemoRuntime
from werewolf.runtime.player_runtime import (
    Action,
    ActionResponse,
    ResponseKind,
    RuntimeTurnResult,
    Speech,
    SpeechResponse,
    TurnRequest,
)


class ScenarioRuntimeError(RuntimeError):
    """A declarative scenario could not satisfy its runtime boundary."""


@dataclass(slots=True)
class ScenarioRunEvidence:
    """Shared, host-owned evidence collector for one real scenario execution."""

    scenario: ScenarioDefinition
    turn_budget: int
    used_step_ids: set[str] = field(default_factory=set)
    player_turns: list[dict[str, object]] = field(default_factory=list)
    host_commands: list[dict[str, object]] = field(default_factory=list)
    gateway_reads: list[dict[str, object]] = field(default_factory=list)
    skill_queries: dict[int, int] = field(default_factory=dict)
    runtime_stats: dict[int, dict[str, object]] = field(default_factory=dict)
    _turn_count: int = 0
    _host_cursor: int = 0
    _reserved_step_ids: dict[str, str] = field(default_factory=dict)

    def begin_turn(self) -> None:
        if self._turn_count >= self.turn_budget:
            raise ScenarioRuntimeError("scenario turn budget exhausted")
        self._turn_count += 1

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def add_gateway_read(self, seat: int, path: str, receipt_id: str) -> None:
        self.gateway_reads.append({"seat": seat, "path": path, "receipt_id": receipt_id})

    def add_skill_query(self, seat: int) -> None:
        self.skill_queries[seat] = self.skill_queries.get(seat, 0) + 1

    def select_player_step(
        self,
        request: TurnRequest,
        *,
        seat: int,
        role_id: str | None,
        skill_ids: frozenset[str],
    ) -> PlayerTurnStep | None:
        payload = request.observation.payload
        requested_seat = payload.get("seat")
        if requested_seat is not None and requested_seat != seat:
            raise ScenarioRuntimeError("turn observation seat does not match its runtime binding")
        round_no = _nonnegative_int(payload.get("round_no"))
        day_no = _nonnegative_int(payload.get("day_no"))
        for candidate in self.scenario.steps:
            if (
                not isinstance(candidate, PlayerTurnStep)
                or candidate.step_id in self.used_step_ids
                or candidate.step_id in self._reserved_step_ids
            ):
                continue
            matcher = candidate.match
            if matcher.seat != seat:
                continue
            if matcher.role_id is not None and matcher.role_id != role_id:
                continue
            if matcher.phase is not None and matcher.phase is not request.phase:
                continue
            if matcher.round_no is not None and matcher.round_no != round_no:
                continue
            if matcher.day_no is not None and matcher.day_no != day_no:
                continue
            if not set(matcher.authorized_skill_ids).issubset(skill_ids):
                continue
            if (
                candidate.expected_kind is not None
                and candidate.expected_kind is not request.expected_kind
            ):
                continue
            if candidate.actions and request.expected_kind is not ResponseKind.ACTION:
                continue
            if candidate.speech is not None and request.expected_kind is not ResponseKind.SPEECH:
                continue
            if candidate.actions and request.action_window is not None:
                allowed = set(request.action_window.allowed_action_codes)
                if any(action.action_code not in allowed for action in candidate.actions):
                    continue
            self._reserved_step_ids[candidate.step_id] = request.request_id
            return candidate
        return None

    def abandon_turn_selection(self, request_id: str) -> None:
        for step_id, reserved_request_id in tuple(self._reserved_step_ids.items()):
            if reserved_request_id == request_id:
                del self._reserved_step_ids[step_id]

    def observe_host_command(
        self,
        line: str,
        result: Mapping[str, object],
        *,
        pre_state: Mapping[str, object] | None,
        post_state: Mapping[str, object] | None,
    ) -> None:
        host_steps = [step for step in self.scenario.steps if isinstance(step, HostCommandStep)]
        matched: HostCommandStep | None = None
        consumed = False
        proof: dict[str, object] | None = None
        while self._host_cursor < len(host_steps):
            candidate = host_steps[self._host_cursor]
            if host_command_matches(candidate, line):
                matched = candidate
                proof = _host_post_state_proof(
                    line.strip(),
                    result,
                    pre_state=pre_state,
                    post_state=post_state,
                )
                consumed = proof is not None
                if consumed:
                    self._host_cursor += 1
                    self.used_step_ids.add(candidate.step_id)
                break
            if candidate.required:
                break
            self._host_cursor += 1
        self.host_commands.append(
            {
                "command": line,
                "status": result.get("status"),
                "matched_step_id": matched.step_id if matched is not None else None,
                "consumed": consumed,
                "post_state_proof": proof,
            }
        )

    def record_turn(
        self,
        *,
        request: TurnRequest,
        result: RuntimeTurnResult,
        seat: int,
        role_id: str,
        step: PlayerTurnStep | None,
    ) -> None:
        self.player_turns.append(
            {
                "request_id": request.request_id,
                "logical_request_id": request.logical_request_id,
                "seat": seat,
                "initial_role_id": role_id,
                "phase": request.phase.value,
                "round_no": _nonnegative_int(request.observation.payload.get("round_no")),
                "day_no": _nonnegative_int(request.observation.payload.get("day_no")),
                "expected_kind": request.expected_kind.value,
                "script_step_id": step.step_id if step is not None else None,
                "script_actions": [
                    {
                        "action_code": action.action_code,
                        "targets": list(action.targets),
                        "parameters": dict(action.parameters),
                        "reason_public": action.reason_public,
                    }
                    for action in step.actions
                ]
                if step is not None
                else [],
                "script_speech": step.speech if step is not None else None,
                "ready_receipts": list(
                    getattr(getattr(result.response, "ready", None), "knowledge_receipts", ())
                    or ()
                ),
                "action_codes": [
                    action.action_code for action in getattr(result.response, "actions", ()) or ()
                ],
            }
        )
        self.abandon_turn_selection(request.request_id)

    def confirm_player_steps(self, state: object) -> None:
        """Mark scripted responses consumed only when the final state proves commit."""

        steps_by_id = {
            step.step_id: step
            for step in self.scenario.steps
            if isinstance(step, PlayerTurnStep)
        }
        action_requests = getattr(state, "action_requests", {})
        players = getattr(state, "players", {})
        events = getattr(state, "events", ())
        for turn in self.player_turns:
            step_id = turn.get("script_step_id")
            request_id = turn.get("request_id")
            if not isinstance(step_id, str) or not isinstance(request_id, str):
                continue
            step = steps_by_id.get(step_id)
            if step is None:
                continue
            kind = turn.get("expected_kind")
            seat = turn.get("seat")
            confirmed = False
            if kind == ResponseKind.ACTION.value:
                raw = (
                    action_requests.get(request_id)
                    if isinstance(action_requests, Mapping)
                    else None
                )
                confirmed = _confirmed_script_action(step, turn, raw)
            elif kind == ResponseKind.SPEECH.value:
                logical_request_id = turn.get("logical_request_id")
                confirmed = isinstance(logical_request_id, str) and any(
                    _speech_event_matches(
                        event,
                        logical_request_id=logical_request_id,
                        seat=seat,
                        phase=turn.get("phase"),
                    )
                    for event in events
                )
            elif kind == ResponseKind.READY.value and type(seat) is int:
                player = players.get(seat) if isinstance(players, Mapping) else None
                player_receipts = getattr(player, "knowledge_receipt_ids", ())
                turn_receipts = turn.get("ready_receipts")
                confirmed = (
                    isinstance(turn_receipts, list)
                    and bool(turn_receipts)
                    and set(turn_receipts).issubset(set(player_receipts))
                )
            if confirmed:
                self.used_step_ids.add(step_id)

    def steps_report(self) -> list[dict[str, object]]:
        return [
            {
                "step_id": step.step_id,
                "kind": step.kind,
                "required": step.required,
                "consumed": step.step_id in self.used_step_ids,
            }
            for step in self.scenario.steps
        ]

    def required_unconsumed(self) -> list[str]:
        return [
            step.step_id
            for step in self.scenario.steps
            if step.required and step.step_id not in self.used_step_ids
        ]


class DeclarativeRuntime(DemoRuntime):
    """A real loopback-Gateway runtime that only sees its own request and grants."""

    def __init__(
        self,
        knowledge_base_url: str,
        knowledge_token: str,
        *,
        evidence: ScenarioRunEvidence,
    ) -> None:
        super().__init__(knowledge_base_url, knowledge_token)
        self._evidence = evidence
        self._selected_steps: dict[str, PlayerTurnStep | None] = {}
        self._last_skill_ids: frozenset[str] = frozenset()

    async def _read_document(self, path: str) -> Mapping[str, Any]:
        payload = await super()._read_document(path)
        context = self._require_context()
        receipt = payload.get("receipt_id")
        if isinstance(receipt, str):
            self._evidence.add_gateway_read(context.seat, path, receipt)
        return payload

    async def _read_skill_status(self) -> Mapping[str, Any]:
        context = self._require_context()
        self._evidence.add_skill_query(context.seat)
        status = await super()._read_skill_status()
        abilities = status.get("abilities")
        if isinstance(abilities, list | tuple):
            self._last_skill_ids = frozenset(
                item["skill_id"]
                for item in abilities
                if isinstance(item, Mapping)
                and isinstance(item.get("skill_id"), str)
                and item.get("enabled", True) is True
            )
        else:
            self._last_skill_ids = frozenset()
        return status

    def _scenario_speech_for(self, request: TurnRequest) -> str:
        context = self._require_context()
        step = self._evidence.select_player_step(
            request,
            seat=context.seat,
            role_id=context.role_id,
            skill_ids=frozenset(),
        )
        self._selected_steps[request.request_id] = step
        if step is not None and step.speech is not None:
            return step.speech
        return DemoRuntime._speech_for(request)

    def _action_for(self, request: TurnRequest, status: Mapping[str, Any]) -> ActionResponse:
        context = self._require_context()
        step = self._evidence.select_player_step(
            request,
            seat=context.seat,
            role_id=context.role_id,
            skill_ids=self._last_skill_ids,
        )
        self._selected_steps[request.request_id] = step
        if step is None or not step.actions:
            return super()._action_for(request, status)
        return ActionResponse(
            request_id=request.request_id,
            actions=[
                Action(
                    action_code=action.action_code,
                    targets=list(action.targets),
                    parameters=dict(action.parameters),
                    reason_public=action.reason_public,
                )
                for action in step.actions
            ],
        )

    async def run_turn(self, request: TurnRequest) -> RuntimeTurnResult:
        """Use the demo runtime for all transport while binding scripted speech."""

        self._evidence.begin_turn()
        context = self._require_context()
        self._selected_steps[request.request_id] = None
        if request.expected_kind is ResponseKind.READY:
            self._selected_steps[request.request_id] = self._evidence.select_player_step(
                request,
                seat=context.seat,
                role_id=context.role_id,
                skill_ids=frozenset(),
            )
        try:
            if request.expected_kind is ResponseKind.SPEECH:
                self._require_open()
                if (
                    request.game_id != context.game_id
                    or request.session_epoch != context.session_epoch
                ):
                    raise RuntimeError("turn request does not match the runtime session")
                if self._active_request_id is not None:
                    raise RuntimeError("runtime already has an active turn")
                self._active_request_id = request.request_id
                self._last_request_id = request.request_id
                self._requests.append(request)
                response = SpeechResponse(
                    request_id=request.request_id,
                    speech=Speech(text=self._scenario_speech_for(request)),
                )
                self._validate_response(request, response)
                result = RuntimeTurnResult(
                    request_id=request.request_id,
                    logical_request_id=request.logical_request_id,
                    attempt_no=request.attempt_no,
                    response=response,
                    elapsed_ms=0,
                )
            else:
                result = await super().run_turn(request)
            self._evidence.record_turn(
                request=request,
                result=result,
                seat=context.seat,
                role_id=context.role_id or "",
                step=self._selected_steps.get(request.request_id),
            )
            return result
        finally:
            self._active_request_id = None
            self._evidence.abandon_turn_selection(request.request_id)
            self._selected_steps.pop(request.request_id, None)


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _confirmed_script_action(
    step: PlayerTurnStep, turn: Mapping[str, object], raw_request: object
) -> bool:
    if not isinstance(raw_request, Mapping) or raw_request.get("status") != "CONFIRMED":
        return False
    submitted = raw_request.get("actions")
    expected = turn.get("script_actions")
    if not isinstance(submitted, list | tuple) or not isinstance(expected, list):
        return False
    if len(submitted) != len(expected) or not expected:
        return False
    for actual, scripted in zip(submitted, expected, strict=True):
        if not isinstance(actual, Mapping) or not isinstance(scripted, Mapping):
            return False
        if (
            actual.get("action_code") != scripted.get("action_code")
            or list(actual.get("targets", ())) != scripted.get("targets")
            or actual.get("parameters", {}) != scripted.get("parameters", {})
        ):
            return False
    dispositions = raw_request.get("rule_dispositions", ())
    if not isinstance(dispositions, list | tuple):
        return False
    by_index = {
        item.get("action_index"): item
        for item in dispositions
        if isinstance(item, Mapping) and type(item.get("action_index")) is int
    }
    for index, action in enumerate(expected):
        if not isinstance(action, Mapping) or action.get("action_code") == 299:
            continue
        disposition = by_index.get(index)
        if not isinstance(disposition, Mapping):
            return False
        if (
            disposition.get("action_code") != action.get("action_code")
            or disposition.get("status") != "ACCEPTED"
        ):
            return False
    return True


def _speech_event_matches(
    event: object,
    *,
    logical_request_id: str,
    seat: object,
    phase: object,
) -> bool:
    event_type = getattr(getattr(event, "event_type", None), "value", None)
    actor_seat = getattr(event, "actor_seat", None)
    correlation_id = getattr(event, "correlation_id", None)
    event_phase = getattr(getattr(event, "phase", None), "value", None)
    return (
        event_type in {"speech", "team_speech"}
        and actor_seat == seat
        and correlation_id == logical_request_id
        and event_phase == phase
    )


def _host_post_state_proof(
    command: str,
    result: Mapping[str, object],
    *,
    pre_state: Mapping[str, object] | None,
    post_state: Mapping[str, object] | None,
) -> dict[str, object] | None:
    status = result.get("status")
    if not isinstance(status, str) or status.lower() in {"error", "failed", "skipped"}:
        return None
    if post_state is None or not isinstance(post_state.get("game_id"), str):
        return None
    if pre_state is not None and pre_state.get("game_id") != post_state.get("game_id"):
        return None
    readonly = {
        "status",
        "help",
        "prepare status",
        "day status",
        "night status",
        "night plan status",
        "trigger status",
        "trigger pending",
        "sheriff status",
        "sheriff badge status",
        "victory status",
    }
    pre_revision = pre_state.get("state_revision") if pre_state is not None else None
    post_revision = post_state.get("state_revision")
    if command not in readonly and command != "save":
        if (
            type(pre_revision) is not int
            or type(post_revision) is not int
            or post_revision <= pre_revision
        ):
            return None
    elif command == "save" and not isinstance(result.get("snapshot_id"), str):
        return None
    return {
        "status": status,
        "game_id": post_state.get("game_id"),
        "state_revision_before": pre_revision,
        "state_revision_after": post_revision,
        "phase_after": post_state.get("phase"),
        "run_status_after": post_state.get("run_status"),
    }


__all__ = ["DeclarativeRuntime", "ScenarioRunEvidence", "ScenarioRuntimeError"]
