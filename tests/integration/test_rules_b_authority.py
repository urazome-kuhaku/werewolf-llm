"""Manager authority boundaries for ordinary hooks and B workflow returns."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
import test_day_flow as day_flow_tests
import test_moderator_sheriff_flow as sheriff_flow_tests
import test_rules_b_workflow as workflow_tests
import test_sheriff_flow as sheriff_tests
from test_rules_scripted_runtime import (
    _compiled_classic,
    _novel_execution,
    _ref,
)

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    ActionDefinition,
    ActionRegistry,
    GameManager,
    GameState,
    PlayerState,
    RulesetRef,
    TieAction,
    TieDecision,
    load_action_registry,
)
from werewolf.game.day import DayCoordinator
from werewolf.game.manager import EventCommitError
from werewolf.game.state import RuleWorkflowCursor, SerialTurnBinding
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiler import CompiledKnowledgePackage
from werewolf.moderator import (
    LastWordsError,
    LastWordsFlow,
    ModeratorSheriffFlow,
    ModeratorTriggerFlow,
)
from werewolf.rules.compiler import execution_windows_from_board, validate_execution_package
from werewolf.rules.models import BoundaryPolicy, ExecutionPackage
from werewolf.runtime.player_runtime import (
    Action as RuntimeAction,
)
from werewolf.runtime.player_runtime import (
    ActionResponse,
    InitialContext,
    RuntimeConfig,
)
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
GAME_ID = "b-authority-hook-game"
SNAPSHOT_ID = "b-authority-hook-snapshot"
HOOK_ACTION = 987


def _boundary_policy(board: BoardDefinition) -> BoundaryPolicy:
    words = board.day_flow.last_words
    sheriff = board.day_flow.sheriff
    return BoundaryPolicy(
        last_words_enabled=words.enabled,
        eligible_death_causes=tuple(words.eligible_death_causes),
        before_reveal=words.before_reveal,
        night_death_policy=words.night_death_policy,
        day_death_policy=words.day_death_policy,
        sheriff_enabled=sheriff.enabled,
        badge_transfer_enabled=sheriff.transfer_enabled,
        badge_transfer_on_death=sheriff.transfer_on_death,
    )


def _hook_registry() -> ActionRegistry:
    base = load_action_registry()
    return ActionRegistry(
        actions=(
            *(item for item in base.actions if item.action_code != HOOK_ACTION),
            ActionDefinition(
                action_code=HOOK_ACTION,
                action_name="QUASAR_ECHO",
                target_policy="none",
                target_count=0,
            ),
        )
    )


def _hook_execution(
    board: BoardDefinition,
    *,
    hook_ids: tuple[str, ...] = ("DAY_SPEECH_AFTER",),
    condition: dict[str, Any] | None = None,
    with_charge_cost: bool = False,
    max_uses: int = 1,
) -> ExecutionPackage:
    raw = json.loads(
        _novel_execution(
            target_count=0,
            modes=[],
            board_id=board.id,
            board_version=board.version,
        ).model_dump_json()
    )
    skill = raw["skills"][0]
    skill["timing"] = ["DAY_SPEECH", "TRIGGER_ACTION"]
    skill["hook_ids"] = list(hook_ids)
    skill["grants"] = [
        {
            "grant_id": "quasar_echo_grant",
            "actor_selector": {
                "op": "select",
                "source": "players",
                "where": {
                    "op": "eq",
                    "left": _ref("item", "seat"),
                    "right": {"op": "literal", "value": 2},
                },
                "map": _ref("item", "seat"),
            },
        }
    ]
    skill["targets"] = {
        "min_targets": 0,
        "max_targets": 0,
        "selector": {"op": "select", "source": "players", "map": _ref("item", "seat")},
        "allow_self": True,
    }
    skill["effects"] = []
    skill["disclosures"] = []
    skill["parameters"] = []
    if condition is not None:
        skill["condition"] = condition
    skill["usage"] = {
        "max_uses": max_uses,
        "scope": "GAME",
        "costs": ([{"resource_id": "charge", "amount": 1}] if with_charge_cost else []),
    }
    raw["resource_declarations"] = (
        [{"resource_id": "charge", "min_value": 0, "max_value": 1}] if with_charge_cost else []
    )
    windows = execution_windows_from_board(board)
    raw["window_metadata"] = [item.model_dump(mode="json") for item in windows]
    raw["window_settlement_groups"] = {}
    raw["boundary_policy"] = _boundary_policy(board).model_dump(mode="json")
    execution = ExecutionPackage.model_validate(raw)
    registry = _hook_registry()
    validate_execution_package(
        execution,
        registry,
        available_windows=windows,
        expected_boundary_policy=_boundary_policy(board),
    )
    return execution


def _manager(
    compiled: CompiledKnowledgePackage,
    board: BoardDefinition,
    execution: ExecutionPackage,
    *,
    game_id: str = GAME_ID,
    resources: dict[int, dict[str, int]] | None = None,
    phase: GamePhase = GamePhase.DAY_ANNOUNCE,
    current_queue: tuple[int, ...] | None = None,
    round_no: int = 0,
) -> GameManager:
    state = GameState(
        game_id=game_id,
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        round_no=round_no,
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
                faction_id="village",
                session_epoch=0,
                skill_resources=(resources or {}).get(seat, {}),
            )
            for seat in range(1, 5)
        },
    )
    return GameManager(state, registry=_hook_registry(), execution_package=execution)


async def _speech_runtime(seat: int, *, game_id: str = GAME_ID) -> ScriptedRuntime:
    runtime = ScriptedRuntime()
    await runtime.start(
        RuntimeConfig(session_id=f"b-authority-speech-{game_id}-{seat}"),
        InitialContext(game_id=game_id, seat=seat, session_epoch=0, role_id="villager"),
    )
    return runtime


async def _choice_runtime(*, game_id: str = GAME_ID) -> ScriptedRuntime:
    def response(request: Any) -> ActionResponse:
        return ActionResponse(
            request_id=request.request_id,
            actions=[RuntimeAction(action_code=HOOK_ACTION)],
        )

    runtime = ScriptedRuntime([response])
    await runtime.start(
        RuntimeConfig(session_id=f"b-authority-choice-{game_id}-2"),
        InitialContext(game_id=game_id, seat=2, session_epoch=0, role_id="villager"),
    )
    return runtime


async def _last_words_runtime(seat: int) -> ScriptedRuntime:
    runtime = ScriptedRuntime()
    await runtime.start(
        RuntimeConfig(session_id=f"b-authority-last-words-{seat}"),
        InitialContext(
            game_id=workflow_tests.GAME_ID,
            seat=seat,
            session_epoch=7,
            role_id="villager",
        ),
    )
    return runtime


async def _complete_hook_choice(
    triggers: ModeratorTriggerFlow,
    step: Any,
) -> None:
    assert step.kind == "PLAYER_CHOICE"
    assert step.occurrence_id is not None
    seat = step.actor_seat
    assert seat == 2
    result = await triggers.next(seat)
    assert result["status"] == "accepted"
    assert triggers.state.phase is GamePhase.DAY_SPEECH


@pytest_asyncio.fixture(scope="module")
async def compiled() -> CompiledKnowledgePackage:
    return await _compiled_classic()


@pytest.mark.parametrize("hook_id", ["DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"])
@pytest.mark.asyncio
async def test_ordinary_hooks_require_real_queue_or_completed_serial_speech(
    hook_id: str,
    compiled: CompiledKnowledgePackage,
) -> None:
    """The manager binds BEFORE to a queue head and AFTER to its real speech event."""

    board = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    execution = _hook_execution(
        board,
        hook_ids=("DAY_SPEECH_BEFORE", "DAY_SPEECH_AFTER"),
        max_uses=2,
    )
    manager = _manager(compiled, board, execution)
    runtime = await _speech_runtime(1)
    choice_runtime = await _choice_runtime()
    day = DayCoordinator(manager, board, {1: runtime})
    triggers = ModeratorTriggerFlow(manager, board, {2: choice_runtime})
    try:
        await day.announce(now=NOW)
        await day.open_speech((1, 2, 3, 4), now=NOW)
        with pytest.raises(EventCommitError, match="RULE_HOOK_NOT_ALLOWED"):
            await manager.advance_rule_workflow(
                expected_revision=manager.state.state_revision,
                now=NOW,
                hook_id="DAY_SPEECH_AFTER",
            )
        if hook_id == "DAY_SPEECH_BEFORE":
            before = await manager.advance_rule_workflow(
                expected_revision=manager.state.state_revision,
                now=NOW,
                hook_id="DAY_SPEECH_BEFORE",
            )
            assert before.kind == "PLAYER_CHOICE"
            assert before.return_point is not None
            assert before.return_point.speaker_seat == 1
            assert manager.state.current_queue == (1, 2, 3, 4)
            await _complete_hook_choice(triggers, before)

        speech = await day.run_next_speech()
        assert speech.event.payload.speaker_seat == 1
        turn = manager.state.last_serial_turn
        assert turn is not None
        assert turn.request_id == speech.request.request_id
        assert speech.event.event_id in turn.event_ids

        after = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=NOW,
            hook_id="DAY_SPEECH_AFTER",
        )
        assert after.kind == "PLAYER_CHOICE"
        assert after.return_point is not None
        assert after.return_point.serial_turn_id == turn.request_id
        assert after.return_point.event_ids == turn.event_ids

        if hook_id == "DAY_SPEECH_AFTER":
            # Polling before the turn is complete has no fabricated source.
            assert after.return_point.hook_id == "DAY_SPEECH_AFTER"
    finally:
        await runtime.close("B authority hook test complete")
        await choice_runtime.close("B authority hook test complete")


@pytest.mark.parametrize(
    ("condition", "with_charge_cost", "resources"),
    [
        ({"op": "literal", "value": False}, False, {}),
        (None, True, {2: {"charge": 0}}),
    ],
)
@pytest.mark.asyncio
async def test_unavailable_hook_skills_are_not_offered(
    condition: dict[str, Any] | None,
    with_charge_cost: bool,
    resources: dict[int, dict[str, int]],
    compiled: CompiledKnowledgePackage,
) -> None:
    """False skill conditions and empty declared resources close the offer."""

    board = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    execution = _hook_execution(
        board,
        hook_ids=("DAY_SPEECH_BEFORE",),
        condition=condition,
        with_charge_cost=with_charge_cost,
    )
    manager = _manager(compiled, board, execution, resources=resources)
    day = DayCoordinator(manager, board, {})
    await day.announce(now=NOW)
    await day.open_speech((1, 2, 3, 4), now=NOW)

    step = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=NOW,
        hook_id="DAY_SPEECH_BEFORE",
    )

    assert step.kind == "IDLE"
    assert not step.queue_pending
    assert manager.state.rule_trigger_queue == ()
    assert manager.state.phase is GamePhase.DAY_SPEECH


@pytest.mark.asyncio
async def test_game_once_speech_hook_is_not_offered_for_a_second_speech_source(
    compiled: CompiledKnowledgePackage,
) -> None:
    """Usage is checked before installing a hook occurrence for the next speaker."""

    board = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    execution = _hook_execution(board, hook_ids=("DAY_SPEECH_AFTER",))
    manager = _manager(compiled, board, execution)
    runtime = await _speech_runtime(1)
    runtime2 = await _speech_runtime(2)
    choice_runtime = await _choice_runtime()
    day = DayCoordinator(manager, board, {1: runtime, 2: runtime2})
    triggers = ModeratorTriggerFlow(manager, board, {2: choice_runtime})
    try:
        await day.announce(now=NOW)
        await day.open_speech((1, 2, 3, 4), now=NOW)
        first_speech = await day.run_next_speech()
        assert first_speech.event.payload.speaker_seat == 1
        first = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=NOW,
            hook_id="DAY_SPEECH_AFTER",
        )
        assert first.kind == "PLAYER_CHOICE"
        ability_instance_id = first.ability_instance_id
        await _complete_hook_choice(triggers, first)
        instance = next(
            item
            for item in manager.state.ability_instances
            if item.ability_instance_id == ability_instance_id
        )
        assert instance.uses_consumed == 1
        assert not instance.consumed

        second_speech = await day.run_next_speech()
        assert second_speech.event.payload.speaker_seat == 2
        second = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=NOW,
            hook_id="DAY_SPEECH_AFTER",
        )

        assert second.kind == "IDLE"
        assert not second.queue_pending
        assert manager.state.rule_trigger_queue[-1].source_fact_id == first.source_fact_id
    finally:
        await runtime.close("B authority usage test complete")
        await runtime2.close("B authority usage test complete")
        await choice_runtime.close("B authority usage test complete")


@pytest.mark.asyncio
async def test_pk_speech_event_cannot_source_ordinary_skill_hook() -> None:
    """A real PK speech turn is not an ordinary DAY_SPEECH hook source."""

    board = day_flow_tests._board(pk_enabled=True)
    execution = _hook_execution(board, hook_ids=("DAY_SPEECH_AFTER",))
    manager = GameManager(
        day_flow_tests._state(),
        registry=_hook_registry(),
        execution_package=execution,
    )
    runtimes = await day_flow_tests._runtimes()

    def choose_pk(_window: Any, tally: Any) -> TieDecision:
        return TieDecision(action=TieAction.PK, candidates=tuple(tally.top_candidates))

    day = DayCoordinator(manager, board, runtimes, tie_resolver=choose_pk)
    try:
        await day.announce(now=NOW)
        await day.open_speech(now=NOW)
        for _ in range(4):
            await day.run_next_speech()
        await day.advance_from_speech(now=NOW)

        progress = await day.open_vote(now=NOW)
        for seat, target in zip(
            progress.window.eligible_voters,
            (3, 4, 3, 4),
            strict=True,
        ):
            await day.submit_vote(
                day_flow_tests._vote_request(progress, seat, target, "authority-pk"),
                now=NOW,
            )
        await day.finalize_vote(now=NOW)
        state = await day.confirm_vote(now=NOW)
        assert state.phase is GamePhase.VOTE_PK_SPEECH

        await day.open_speech(is_pk=True, now=NOW)
        pk_speech = await day.run_next_speech()
        assert pk_speech.event.payload.speaker_seat in {3, 4}
        assert pk_speech.event.phase is GamePhase.VOTE_PK_SPEECH
        assert manager.state.phase is GamePhase.VOTE_PK_SPEECH
        with pytest.raises(EventCommitError, match="RULE_HOOK_NOT_ALLOWED"):
            await manager.advance_rule_workflow(
                expected_revision=manager.state.state_revision,
                now=NOW,
                hook_id="DAY_SPEECH_AFTER",
            )
        assert manager.state.rule_trigger_queue == ()
    finally:
        for runtime in runtimes.values():
            await runtime.close("B authority PK source test complete")


@pytest.mark.asyncio
async def test_sheriff_speech_and_nonordinary_after_source_cannot_open_skill_hook() -> None:
    """Real sheriff serial speech is excluded both in-phase and after transfer."""

    board = sheriff_tests._board()
    execution = _hook_execution(board, hook_ids=("DAY_SPEECH_AFTER",))
    state = GameState(
        game_id="sheriff-manager-game",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.DAY_ANNOUNCE,
        day_no=1,
        ruleset=RulesetRef(
            board_id=board.id,
            version=board.version,
            snapshot_id="sheriff-manager-snapshot",
            manifest_sha256="c" * 64,
        ),
        players={
            seat: PlayerState(
                seat=seat,
                role_id="villager" if seat != 3 else "wolf",
                faction_id="town" if seat != 3 else "wolf",
            )
            for seat in (1, 2, 3)
        },
    )
    manager = GameManager(state, registry=_hook_registry(), execution_package=execution)
    runtimes = await sheriff_flow_tests._runtimes()
    flow = ModeratorSheriffFlow(manager, board, runtimes)
    try:
        started = await flow.start((1, 2))
        assert started.phase is GamePhase.SHERIFF_ELECTION_SPEECH
        first_speech = await flow.speech_next()
        assert first_speech.event.payload.speaker_seat == 1
        with pytest.raises(EventCommitError, match="RULE_HOOK_NOT_ALLOWED"):
            await manager.advance_rule_workflow(
                expected_revision=manager.state.state_revision,
                now=NOW,
                hook_id="DAY_SPEECH_AFTER",
            )
        assert manager.state.rule_trigger_queue == ()

        await flow.speech_next()
        await flow.open_vote()
        for seat in (1, 2, 3):
            await flow.vote_next(seat)
        await flow.collect()
        elected = await flow.confirm()
        assert elected.sheriff_seat == 2
        transferred = await flow.transfer()
        assert transferred.phase is GamePhase.DAY_SPEECH

        # Even if a restored snapshot misbinds that actual sheriff speech as
        # the last turn, the manager checks the event's real phase before it
        # can install an ordinary AFTER occurrence.
        sheriff_event = first_speech.event
        assert sheriff_event.phase is GamePhase.SHERIFF_ELECTION_SPEECH
        assert sheriff_event.actor_seat is not None
        snapshot_data = transferred.model_dump(mode="python", exclude={"sheriff_election"})
        snapshot_data["sheriff_election"] = json.loads(json.dumps(transferred.sheriff_election))
        snapshot_data["last_serial_turn"] = SerialTurnBinding(
            seat=sheriff_event.actor_seat,
            session_epoch=transferred.players[sheriff_event.actor_seat].session_epoch,
            request_id="misbound-sheriff-speech",
            logical_request_id="misbound-sheriff-speech",
            attempt_no=1,
            event_ids=(sheriff_event.event_id,),
        ).model_dump(mode="python")
        restored_state = GameState.model_validate(snapshot_data)
        restored_manager = GameManager(
            restored_state,
            registry=_hook_registry(),
            execution_package=execution,
        )
        with pytest.raises(
            EventCommitError,
            match="AFTER source is not an ordinary speech event",
        ):
            await restored_manager.advance_rule_workflow(
                expected_revision=restored_manager.state.state_revision,
                now=NOW,
                hook_id="DAY_SPEECH_AFTER",
            )
        assert restored_manager.state.rule_trigger_queue == ()
    finally:
        for runtime in runtimes.values():
            await runtime.close("B authority sheriff source test complete")


@pytest.mark.asyncio
async def test_active_serial_turn_and_legacy_night_edges_cannot_skip_authority(
    compiled: CompiledKnowledgePackage,
) -> None:
    """An active speech request and coarse legacy phase edges keep their guards."""

    board = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    execution = _hook_execution(board, hook_ids=("DAY_SPEECH_BEFORE",))
    manager = _manager(compiled, board, execution)
    await DayCoordinator(manager, board, {}).announce(now=NOW)
    await DayCoordinator(manager, board, {}).open_speech((1, 2, 3, 4), now=NOW)
    await manager.begin_serial_speech_turn(
        1,
        0,
        request_id="active-ordinary-speech",
        logical_request_id="active-ordinary-speech",
        phase=GamePhase.DAY_SPEECH,
        now=NOW,
    )
    with pytest.raises(EventCommitError, match="RULE_HOOK_NOT_ALLOWED"):
        await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=NOW,
            hook_id="DAY_SPEECH_BEFORE",
        )

    legacy_state = GameState(
        game_id="b-authority-legacy-night",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        ruleset=RulesetRef(
            board_id="legacy-authority-board",
            version="1.0.0",
            snapshot_id="legacy-authority-snapshot",
            manifest_sha256="b" * 64,
        ),
        players={1: PlayerState(seat=1, role_id="wolf", faction_id="wolf")},
    )
    legacy = GameManager(legacy_state, registry=load_action_registry())
    with pytest.raises(ValueError, match="cannot transition from NIGHT_TEAM_CHAT to NIGHT_RESOLVE"):
        await legacy.commit_phase_transition(
            GamePhase.NIGHT_RESOLVE,
            expected_revision=legacy.state.state_revision,
            now=NOW,
        )

    collecting_state = legacy_state.model_copy(
        update={
            "phase": GamePhase.NIGHT_RESOLVE,
            "rule_workflow_cursor": RuleWorkflowCursor(
                status="COLLECTING",
                settlement_group_id="night:1",
            ),
        }
    )
    collecting = GameManager(collecting_state, registry=load_action_registry())
    with pytest.raises(EventCommitError, match="PHASE_BLOCKED"):
        await collecting.commit_phase_transition(
            GamePhase.DAY_ANNOUNCE,
            expected_revision=collecting.state.state_revision,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_manager_death_boundary_uses_serial_last_words_and_keeps_next_seat_pending() -> None:
    """Manager-confirmed deaths bind last words to the boundary serial source."""

    compiled, source_board = await workflow_tests._compiled_workflow_fixture()
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    board_payload = source_board.model_dump(mode="json")
    board_payload["day_flow"]["last_words"] = {
        "enabled": True,
        "eligible_death_causes": ["generic_chain_death"],
        "night_death_policy": "every_night",
        "day_death_policy": "every_day",
    }
    board = BoardDefinition.model_validate(board_payload)

    execution_payload = json.loads(compiled.execution.model_dump_json())
    execution_payload["skills"] = [
        item for item in execution_payload["skills"] if item.get("trigger") is None
    ]
    policy = compiled.execution.boundary_policy.model_copy(
        update={
            "last_words_enabled": True,
            "eligible_death_causes": ("generic_chain_death",),
            "night_death_policy": "every_night",
            "day_death_policy": "every_day",
        }
    )
    execution_payload["boundary_policy"] = policy.model_dump(mode="json")
    execution = ExecutionPackage.model_validate(execution_payload)
    validate_execution_package(
        execution,
        compiled.action_registry,
        available_windows=execution.window_metadata,
        expected_boundary_policy=policy,
    )

    state = GameState(
        game_id=workflow_tests.GAME_ID,
        created_at=workflow_tests.NOW,
        updated_at=workflow_tests.NOW,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        round_no=1,
        day_no=1,
        current_queue=(),
        ruleset=RulesetRef(
            board_id=compiled.board_ref.id,
            version=compiled.board_ref.version,
            snapshot_id=workflow_tests.SNAPSHOT_ID,
            manifest_sha256=compiled.manifest_sha256,
        ),
        players={
            seat: PlayerState(
                seat=seat,
                role_id="wolf" if seat == 1 else "villager",
                faction_id="wolf" if seat == 1 else "good",
                session_epoch=7,
                skill_resources={"charge": 2},
            )
            for seat in range(1, 7)
        },
        rule_workflow_cursor=RuleWorkflowCursor(budget_limit=512),
    )
    manager = GameManager(
        state,
        registry=compiled.action_registry,
        execution_package=execution,
    )
    committed = await workflow_tests._settle_initial_multi_death(manager)
    assert {seat for seat, player in committed.players.items() if not player.alive} == {2, 3}
    boundary_step = await manager.advance_rule_workflow(
        expected_revision=manager.state.state_revision,
        now=workflow_tests.NOW,
    )
    assert boundary_step.kind == "RETURN"
    assert boundary_step.boundary is not None
    assert boundary_step.boundary.last_words_required
    assert boundary_step.boundary.last_words_seats == (2, 3)
    assert manager.state.phase is GamePhase.DAY_RESOLVE

    with pytest.raises(EventCommitError, match="PHASE_BLOCKED"):
        await manager.commit_phase_transition(
            GamePhase.DAY_SPEECH,
            expected_revision=manager.state.state_revision,
            now=workflow_tests.NOW,
        )
    with pytest.raises(EventCommitError, match="PHASE_BLOCKED"):
        await manager.commit_phase_transition(
            GamePhase.VICTORY_CHECK,
            expected_revision=manager.state.state_revision,
            now=workflow_tests.NOW,
        )

    runtime2 = await _last_words_runtime(2)
    runtime3 = await _last_words_runtime(3)
    flow = LastWordsFlow(manager, board, {2: runtime2, 3: runtime3})
    try:
        with pytest.raises(LastWordsError, match="SEAT_NOT_AT_HEAD"):
            await flow.next(3)
        result = await flow.next(2)
        assert result.request.phase is GamePhase.DAY_RESOLVE
        assert result.request.action_window is None
        first_boundary_progress = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=workflow_tests.NOW,
        )
        assert first_boundary_progress.kind == "RETURN"
        assert first_boundary_progress.boundary is not None
        assert first_boundary_progress.boundary.last_words_completed_seats == (2,)
        assert flow.status()["pending_seats"] == [3]
        assert manager.state.rule_boundaries[0].last_words_completed_seats == (2,)
        with pytest.raises(EventCommitError, match="RULE_HOOK_NOT_ALLOWED"):
            await manager.advance_rule_workflow(
                expected_revision=manager.state.state_revision,
                now=workflow_tests.NOW,
                hook_id="DAY_SPEECH_AFTER",
            )
        assert manager.state.rule_trigger_queue == ()
        assert manager.state.current_queue == ()

        second = await flow.next(3)
        assert second.event.payload.speaker_seat == 3
        final_boundary_progress = await manager.advance_rule_workflow(
            expected_revision=manager.state.state_revision,
            now=workflow_tests.NOW,
        )
        assert final_boundary_progress.kind == "IDLE"
        assert not final_boundary_progress.queue_pending
        assert flow.status()["pending_seats"] == []
        assert manager.state.current_queue == ()
    finally:
        await runtime2.close("B authority boundary test complete")
        await runtime3.close("B authority boundary test complete")
