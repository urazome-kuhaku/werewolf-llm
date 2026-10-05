"""Typed death-boundary completion proofs and resumable host flows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from test_rules_scripted_runtime import _compiled_classic

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    EventCommitError,
    GameManager,
    GameState,
    PlayerState,
    RulesetRef,
    SheriffElectionError,
)
from werewolf.game.state import RuleBoundary, RuleReturnPoint, RuleWorkflowCursor
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiler import CompiledKnowledgePackage
from werewolf.moderator import (
    LastWordsFlow,
    ModeratorSheriffBadgeFlow,
    ModeratorTriggerError,
    ModeratorTriggerFlow,
)
from werewolf.runtime.player_runtime import Action, ActionResponse, InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
GAME_ID = "b-boundary-completion-game"
SNAPSHOT_ID = "b-boundary-completion-snapshot"
ORDINARY_QUEUE = (1, 4, 5)


@pytest_asyncio.fixture(scope="module")
async def compiled() -> CompiledKnowledgePackage:
    return await _compiled_classic()


def _board(
    compiled: CompiledKnowledgePackage, *, sheriff_transfer: bool = False
) -> BoardDefinition:
    payload = json.loads(json.dumps(compiled.package_payload["board_definition"]))
    payload["day_flow"]["last_words"].update(
        enabled=True,
        eligible_death_causes=["wolf_kill"],
        day_death_policy="every_day",
        night_death_policy="every_night",
    )
    if sheriff_transfer:
        payload["day_flow"]["sheriff"].update(
            enabled=True,
            transfer_enabled=True,
            transfer_on_death=True,
            transfer_on_resignation=True,
        )
    return BoardDefinition.model_validate(payload)


def _manager(
    compiled: CompiledKnowledgePackage,
    board: BoardDefinition,
    *,
    death_seats: tuple[int, ...] = (2, 3),
    last_words_seats: tuple[int, ...] = (2, 3),
    completed_words: tuple[int, ...] = (),
    sheriff_badge_required: bool = False,
    sheriff_badge_completed: bool = False,
    current_queue: tuple[int, ...] = ORDINARY_QUEUE,
    phase: GamePhase = GamePhase.DAY_RESOLVE,
    day_exile_source: bool = False,
) -> tuple[GameManager, RuleBoundary]:
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    boundary_id = "boundary-deaths-1"
    return_point = RuleReturnPoint(phase=GamePhase.DAY_SPEECH, day_no=1)
    pending = (bool(last_words_seats) and completed_words != last_words_seats) or (
        sheriff_badge_required and not sheriff_badge_completed
    )
    boundary = RuleBoundary(
        boundary_id=boundary_id,
        source_group_id="night-settlement-1",
        source_batch_id="death-batch-1",
        death_fact_ids=tuple(f"death-fact-{seat}" for seat in death_seats),
        death_seats=death_seats,
        return_point=return_point,
        last_words_required=bool(last_words_seats),
        last_words_seats=last_words_seats,
        last_words_completed_seats=completed_words,
        sheriff_badge_required=sheriff_badge_required,
        sheriff_badge_completed=sheriff_badge_completed,
        created_at=NOW,
        completed_at=None if pending else NOW,
    )
    audit = (
        (
            {
                "operation": "DAY_EXILE",
                "vote_window_id": "day-vote-1",
                "resolution_id": "day-exile-1",
                "target_seat": death_seats[0],
            },
        )
        if day_exile_source
        else ()
    )
    state = GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        round_no=1,
        day_no=1,
        current_queue=current_queue,
        ruleset=RulesetRef(
            board_id=compiled.board_ref.id,
            version=compiled.board_ref.version,
            snapshot_id=SNAPSHOT_ID,
            manifest_sha256=compiled.manifest_sha256,
        ),
        players={
            seat: PlayerState(
                seat=seat,
                role_id="villager",
                faction_id="town",
                session_epoch=7,
                alive=seat not in death_seats,
                can_vote=seat not in death_seats,
                death_cause="wolf_kill" if seat in death_seats else None,
            )
            for seat in range(1, 6)
        },
        sheriff_seat=death_seats[0] if sheriff_badge_required else None,
        rule_boundaries=(boundary,),
        rule_workflow_cursor=RuleWorkflowCursor(
            status="WAITING_BOUNDARY",
            pending_boundary_id=boundary_id,
            return_point=return_point,
        ),
        vote_state={"window": {"window_id": "day-vote-1"}} if day_exile_source else None,
        moderator_audit=audit,
    )
    manager = GameManager(
        state,
        registry=compiled.action_registry,
        execution_package=compiled.execution,
    )
    return manager, boundary


async def _speech_runtime(seat: int) -> ScriptedRuntime:
    runtime = ScriptedRuntime()
    await runtime.start(
        RuntimeConfig(session_id=f"boundary-speech-{seat}"),
        InitialContext(
            game_id=GAME_ID,
            seat=seat,
            session_epoch=7,
            role_id="villager",
        ),
    )
    return runtime


@pytest.mark.asyncio
async def test_boundary_speech_is_atomic_resumable_and_preserves_the_ordinary_queue(
    compiled: CompiledKnowledgePackage,
) -> None:
    board = _board(compiled)
    manager, boundary = _manager(compiled, board)
    runtime2 = await _speech_runtime(2)
    runtime3 = await _speech_runtime(3)
    try:
        with pytest.raises(EventCommitError, match="BOUNDARY_NOT_PENDING"):
            await manager.begin_serial_speech_turn(
                3,
                7,
                request_id="wrong-boundary-head",
                logical_request_id="wrong-boundary-head",
                phase=GamePhase.DAY_RESOLVE,
                rule_boundary_id=boundary.boundary_id,
                now=NOW,
            )
        with pytest.raises(EventCommitError, match="BOUNDARY_NOT_PENDING"):
            await manager.begin_serial_speech_turn(
                2,
                7,
                request_id="unknown-boundary",
                logical_request_id="unknown-boundary",
                phase=GamePhase.DAY_RESOLVE,
                rule_boundary_id="boundary-from-another-death",
                now=NOW,
            )
        with pytest.raises(EventCommitError, match="SESSION_MISMATCH"):
            await manager.begin_serial_speech_turn(
                2,
                6,
                request_id="stale-boundary-session",
                logical_request_id="stale-boundary-session",
                phase=GamePhase.DAY_RESOLVE,
                rule_boundary_id=boundary.boundary_id,
                now=NOW,
            )

        flow = LastWordsFlow(manager, board, {2: runtime2, 3: runtime3})
        first = await flow.next(2)
        first_boundary = manager.state.rule_boundaries[0]
        assert first_boundary.last_words_completed_seats == (2,)
        assert manager.state.current_queue == ORDINARY_QUEUE
        assert first.event.actor_seat == 2
        proof = next(
            audit
            for audit in manager.state.moderator_audit
            if audit.get("operation") == "RULE_BOUNDARY_LAST_WORDS_SPEECH_COMPLETE"
            and audit.get("seat") == 2
        )
        assert proof["boundary_id"] == boundary.boundary_id
        assert proof["source_group_id"] == boundary.source_group_id
        assert proof["source_batch_id"] == boundary.source_batch_id
        assert tuple(proof["death_fact_ids"]) == boundary.death_fact_ids
        assert proof["session_epoch"] == 7
        assert proof["request_id"] == first.request.request_id
        assert proof["logical_request_id"] == first.request.logical_request_id
        assert proof["attempt_no"] == first.request.attempt_no
        assert proof["speech_event_id"] == first.event.event_id
        assert proof["speech_event_revision"] == first.event.state_revision
        assert proof["committed_revision"] == first.event.state_revision
        assert first.event.correlation_id == first.request.logical_request_id

        restored_state = GameState.model_validate_json(manager.state.model_dump_json())
        restored_manager = GameManager(
            restored_state,
            registry=compiled.action_registry,
            execution_package=compiled.execution,
        )
        resumed_flow = LastWordsFlow(restored_manager, board, {3: runtime3})
        assert resumed_flow.status()["pending_seats"] == [3]
        second = await resumed_flow.next(3)
        final_boundary = restored_manager.state.rule_boundaries[0]
        assert second.event.actor_seat == 3
        assert final_boundary.last_words_completed_seats == (2, 3)
        assert final_boundary.completed_at is not None
        assert restored_manager.state.current_queue == ORDINARY_QUEUE

        speech_proofs = [
            audit
            for audit in restored_manager.state.moderator_audit
            if audit.get("operation") == "RULE_BOUNDARY_LAST_WORDS_SPEECH_COMPLETE"
        ]
        assert [audit["seat"] for audit in speech_proofs] == [2, 3]

        # Rebuild a damaged completion tuple from the immutable event proofs,
        # as a restore/replay path would, while preserving the same source.
        replay_payload = json.loads(restored_manager.state.model_dump_json())
        replay_payload["rule_boundaries"][0]["last_words_completed_seats"] = []
        replay_payload["rule_boundaries"][0]["completed_at"] = None
        replay = GameManager(
            GameState.model_validate_json(json.dumps(replay_payload)),
            registry=compiled.action_registry,
            execution_package=compiled.execution,
        )
        replay_step = await replay.advance_rule_workflow(
            expected_revision=replay.state.state_revision,
            now=NOW,
        )
        replay_cursor = replay.state.rule_workflow_cursor
        assert replay_step.kind == "IDLE"
        assert replay_cursor is not None and replay_cursor.status == "RETURN_READY"
        assert replay_cursor.pending_boundary_id is None
        assert replay.state.rule_boundaries[0].last_words_completed_seats == (2, 3)
        assert replay.state.rule_boundaries[0].completed_at is not None
        assert replay.state.current_queue == ORDINARY_QUEUE
    finally:
        await runtime2.close("boundary speech test complete")
        await runtime3.close("boundary speech test complete")


@pytest.mark.asyncio
async def test_reason_only_completion_audit_cannot_complete_a_typed_boundary(
    compiled: CompiledKnowledgePackage,
) -> None:
    board = _board(compiled)
    manager, boundary = _manager(compiled, board)
    await manager.commit_moderator_operation(
        operation="LAST_WORDS_COMPLETE",
        command="last-words next",
        expected_revision=manager.state.state_revision,
        reason=f"source={boundary.boundary_id};seat=2",
        now=NOW,
    )
    step = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    current = manager.state.rule_boundaries[0]
    assert step.kind == "RETURN"
    assert current.last_words_completed_seats == ()
    assert current.is_pending
    assert manager.state.current_queue == ORDINARY_QUEUE


@pytest.mark.asyncio
async def test_trigger_finish_stops_at_an_uncompleted_boundary(
    compiled: CompiledKnowledgePackage,
) -> None:
    board = _board(compiled)
    manager, boundary = _manager(compiled, board)
    flow = ModeratorTriggerFlow(manager, board, {})

    with pytest.raises(ModeratorTriggerError, match="RULE_BOUNDARY_PENDING"):
        await flow.finish(now=NOW)

    cursor = manager.state.rule_workflow_cursor
    assert cursor is not None and cursor.status == "WAITING_BOUNDARY"
    assert cursor.pending_boundary_id == boundary.boundary_id
    assert manager.state.rule_boundaries[0].is_pending
    assert manager.state.current_queue == ORDINARY_QUEUE


@pytest.mark.asyncio
async def test_unproven_day_announce_return_point_does_not_stage_dawn(
    compiled: CompiledKnowledgePackage,
) -> None:
    board = _board(compiled)
    manager, boundary = _manager(compiled, board)
    forged_return = RuleReturnPoint(
        phase=GamePhase.DAY_ANNOUNCE,
        window_id="forged-terminal-night-window",
        day_no=1,
    )
    forged_boundary = boundary.model_copy(update={"return_point": forged_return})
    cursor = manager.state.rule_workflow_cursor
    assert cursor is not None
    forged_state = manager.state.model_copy(
        update={
            "rule_boundaries": (forged_boundary,),
            "rule_workflow_cursor": cursor.model_copy(update={"return_point": forged_return}),
        }
    )
    unproven = GameManager(
        forged_state,
        registry=compiled.action_registry,
        execution_package=compiled.execution,
    )

    step = await unproven.advance_rule_workflow(
        expected_revision=unproven.state.state_revision,
        now=NOW,
    )

    assert step.kind == "RETURN"
    assert unproven.state.phase is GamePhase.DAY_RESOLVE
    assert unproven.state.day_no == 1
    assert unproven.state.rule_boundaries[0].is_pending


@pytest.mark.parametrize("badge_pending", [False, True])
@pytest.mark.asyncio
async def test_current_boundary_suppresses_legacy_last_words_rediscovery(
    badge_pending: bool,
    compiled: CompiledKnowledgePackage,
) -> None:
    board = _board(compiled)
    manager, _ = _manager(
        compiled,
        board,
        death_seats=(2,),
        last_words_seats=(2,),
        completed_words=(2,),
        sheriff_badge_required=badge_pending,
        sheriff_badge_completed=False,
        day_exile_source=True,
    )
    status = LastWordsFlow(manager, board, {}).status()
    assert status["source"] is None
    assert status["pending_seats"] == []


@pytest.mark.asyncio
async def test_old_completed_boundary_does_not_hide_a_new_legacy_exile_source(
    compiled: CompiledKnowledgePackage,
) -> None:
    board = _board(compiled)
    manager, _ = _manager(
        compiled,
        board,
        death_seats=(2,),
        last_words_seats=(2,),
        completed_words=(2,),
        day_exile_source=True,
    )
    idle_state = manager.state.model_copy(
        update={"rule_workflow_cursor": RuleWorkflowCursor(status="IDLE")}
    )
    idle_manager = GameManager(
        idle_state,
        registry=compiled.action_registry,
        execution_package=compiled.execution,
    )
    status = LastWordsFlow(idle_manager, board, {}).status()
    assert status["source"] == "day-exile-d1-day-exile-1"
    assert status["pending_seats"] == [2]


@pytest.mark.asyncio
async def test_typed_badge_boundary_requires_a_real_decision_request_and_audit(
    compiled: CompiledKnowledgePackage,
) -> None:
    board = _board(compiled, sheriff_transfer=True)
    manager, boundary = _manager(
        compiled,
        board,
        death_seats=(2,),
        last_words_seats=(),
        sheriff_badge_required=True,
    )
    marker = {
        "status": "COMPLETE",
        "source_seat": 2,
        "source_session_epoch": 7,
        "window_id": "missing-badge-window",
        "request_id": "invented-badge-request",
        "action_code": 201,
        "target_seat": 1,
        "rule_boundary_id": boundary.boundary_id,
        "source_group_id": boundary.source_group_id,
        "source_batch_id": boundary.source_batch_id,
        "death_fact_ids": list(boundary.death_fact_ids),
    }
    corrupted = GameManager(
        manager.state.model_copy(update={"sheriff_badge": marker}),
        registry=compiled.action_registry,
        execution_package=compiled.execution,
    )
    with pytest.raises(SheriffElectionError, match="BADGE_BOUNDARY_INVALID"):
        await corrupted.complete_sheriff_badge(
            expected_revision=corrupted.state.state_revision, now=NOW
        )
    assert not corrupted.state.rule_boundaries[0].sheriff_badge_completed


@pytest.mark.asyncio
async def test_real_typed_badge_decision_completes_its_boundary_atomically(
    compiled: CompiledKnowledgePackage,
) -> None:
    board = _board(compiled, sheriff_transfer=True)
    manager, boundary = _manager(
        compiled,
        board,
        death_seats=(2,),
        last_words_seats=(),
        sheriff_badge_required=True,
    )

    def choose_badge_target(request):
        return ActionResponse(
            request_id=request.request_id,
            actions=[Action(action_code=201, targets=[1])],
        )

    runtime = ScriptedRuntime([choose_badge_target])
    await runtime.start(
        RuntimeConfig(session_id="typed-boundary-badge"),
        InitialContext(
            game_id=GAME_ID,
            seat=2,
            session_epoch=7,
            role_id="villager",
        ),
    )
    flow = ModeratorSheriffBadgeFlow(
        manager,
        board,
        {2: runtime},
        clock=lambda: NOW + timedelta(seconds=1),
    )
    try:
        opened = await flow.open()
        assert opened["status"] == "OPEN"
        submitted = await flow.next()
        action = submitted["action"]
        assert isinstance(action, dict)
        request_id = action["request_id"]
        assert isinstance(request_id, str)
        decided = await flow.resolve(request_id=request_id)
        assert not decided.rule_boundaries[0].sheriff_badge_completed
        decision_audit = next(
            audit
            for audit in decided.moderator_audit
            if audit.get("operation") == "SHERIFF_BADGE" and audit.get("request_id") == request_id
        )
        assert decision_audit["rule_boundary_id"] == boundary.boundary_id
        assert decision_audit["source_group_id"] == boundary.source_group_id
        assert decision_audit["source_batch_id"] == boundary.source_batch_id
        assert tuple(decision_audit["death_fact_ids"]) == boundary.death_fact_ids

        finished = await flow.finish()
        completed_boundary = finished.rule_boundaries[0]
        assert completed_boundary.sheriff_badge_completed
        assert completed_boundary.completed_at is not None
        assert completed_boundary.is_pending is False
        completion_audit = next(
            audit
            for audit in finished.moderator_audit
            if audit.get("operation") == "SHERIFF_BADGE_COMPLETE"
        )
        assert completion_audit["rule_boundary_id"] == boundary.boundary_id
        assert completion_audit["source_group_id"] == boundary.source_group_id
        assert completion_audit["source_batch_id"] == boundary.source_batch_id
        assert tuple(completion_audit["death_fact_ids"]) == boundary.death_fact_ids
        assert completion_audit["request_id"] == request_id
        assert completion_audit["source_seat"] == 2
        assert finished.current_queue == ORDINARY_QUEUE
    finally:
        await runtime.close("typed badge boundary test complete")
