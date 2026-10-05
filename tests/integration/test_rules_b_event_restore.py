"""Typed event restoration through a real B-workflow retry."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_rules_b_authority import (
    GAME_ID,
    HOOK_ACTION,
    NOW,
    _compiled_classic,
    _hook_execution,
    _manager,
    _speech_runtime,
)

from werewolf.domain.enums import Channel, GamePhase
from werewolf.game import GameEvent, GameManager, GameState
from werewolf.game.day import DayCoordinator
from werewolf.game.events import (
    EventType,
    PrivateNoticePayload,
    PublicAnnouncementPayload,
)
from werewolf.game.manager import EventCommitError
from werewolf.knowledge.board import BoardDefinition
from werewolf.moderator import ModeratorTriggerError, ModeratorTriggerFlow
from werewolf.runtime.player_runtime import Action as RuntimeAction
from werewolf.runtime.player_runtime import ActionResponse, InitialContext, RuntimeConfig
from werewolf.runtime.scripted_runtime import ScriptedRuntime


def _choice_runtime() -> ScriptedRuntime:
    def response(*, targets: list[int]):
        def build(request: Any) -> ActionResponse:
            return ActionResponse(
                request_id=request.request_id,
                actions=[RuntimeAction(action_code=HOOK_ACTION, targets=targets)],
            )

        return build

    return ScriptedRuntime((response(targets=[1]), response(targets=[])))


@pytest.mark.asyncio
async def test_restored_json_events_survive_rule_retry_and_keep_visibility() -> None:
    compiled = await _compiled_classic()
    board = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    execution = _hook_execution(board, with_charge_cost=True)
    manager = _manager(compiled, board, execution, resources={2: {"charge": 1}})
    speech = await _speech_runtime(1)
    choice = _choice_runtime()
    await choice.start(
        RuntimeConfig(session_id="b-event-restore-choice"),
        InitialContext(game_id=GAME_ID, seat=2, session_epoch=0, role_id="villager"),
    )

    try:
        day = DayCoordinator(manager, board, {1: speech})
        await day.announce(now=NOW)
        await day.open_speech((1, 2, 3, 4), now=NOW)
        spoken = await day.run_next_speech()
        assert spoken.event.payload.speaker_seat == 1

        first_extra_id = manager.state.events[-1].event_id + 1
        revision = manager.state.state_revision + 1
        custom_event = GameEvent.public(
            event_id=first_extra_id,
            game_id=GAME_ID,
            state_revision=revision,
            round_no=manager.state.round_no,
            phase=GamePhase.DAY_SPEECH,
            created_at=NOW,
            event_type=EventType("custom_restore_notice"),
            eligible_seats=(1, 2, 3, 4),
            actor_seat=1,
            payload=PublicAnnouncementPayload(content="custom event survives restore"),
        )
        private_event = GameEvent.private(
            event_id=first_extra_id + 1,
            game_id=GAME_ID,
            state_revision=revision,
            round_no=manager.state.round_no,
            phase=GamePhase.DAY_SPEECH,
            created_at=NOW,
            event_type=EventType.PRIVATE_NOTICE,
            seat=2,
            actor_seat=2,
            payload=PrivateNoticePayload(content="only seat two sees this"),
        )
        await manager.commit_events((custom_event, private_event), now=NOW)

        triggers = ModeratorTriggerFlow(manager, board, {2: choice}, clock=lambda: NOW)
        hook = await triggers.poll_hook("DAY_SPEECH_AFTER")
        assert hook["status"] == "choice_pending"
        occurrence_id = hook["occurrence_id"]

        with pytest.raises(ModeratorTriggerError, match="TARGET_COUNT"):
            await triggers.next(2)
        before_retry = manager.state
        assert before_retry.players[2].skill_resources["charge"] == 1
        ability = next(item for item in before_retry.ability_instances if item.actor_seat == 2)
        assert ability.uses_consumed == 0
        assert not any(
            occurrence_id in receipt.occurrence_ids for receipt in before_retry.rule_receipts
        )

        restored_state = GameState.model_validate_json(before_retry.model_dump_json())
        assert all(isinstance(event, dict) for event in restored_state.events)
        assert json.dumps(restored_state.events[-1])
        manager = GameManager(
            restored_state,
            registry=manager.registry,
            execution_package=execution,
        )
        triggers = ModeratorTriggerFlow(manager, board, {2: choice}, clock=lambda: NOW)

        seat_one_events = await manager.peek_delivery(1)
        seat_two_events = await manager.peek_delivery(2)
        assert spoken.event in seat_one_events
        assert custom_event in seat_one_events
        assert private_event not in seat_one_events
        assert private_event in seat_two_events
        assert custom_event in seat_two_events
        seat_two_ids = tuple(event.event_id for event in seat_two_events)
        await manager.begin_delivery(
            2,
            0,
            request_id="restored-event-delivery",
            event_ids=seat_two_ids,
            now=NOW,
        )
        delivered = await manager.commit_delivery_ack(
            2,
            0,
            request_id="restored-event-delivery",
            event_ids=seat_two_ids,
            now=NOW,
        )
        assert delivered.delivery_cursors[2].committed_event_id == seat_two_ids[-1]
        repeated_ack = await manager.commit_delivery_ack(
            2,
            0,
            request_id="restored-event-delivery",
            event_ids=seat_two_ids,
            now=NOW,
        )
        assert repeated_ack.state_revision == delivered.state_revision
        assert await manager.peek_delivery(2) == ()

        accepted = await triggers.retry(2)
        assert accepted["status"] == "accepted"
        assert manager.state.phase is GamePhase.DAY_SPEECH
        assert manager.state.players[2].skill_resources["charge"] == 0
        ability = next(item for item in manager.state.ability_instances if item.actor_seat == 2)
        assert ability.uses_consumed == 1
        receipts = [
            receipt
            for receipt in manager.state.rule_receipts
            if occurrence_id in receipt.occurrence_ids
        ]
        assert len(receipts) == 1

        revision_before_replay = manager.state.state_revision
        replayed = await manager.commit_rule_group(
            receipts[0].group_id,
            request_ids=receipts[0].request_ids,
            occurrence_ids=receipts[0].occurrence_ids,
            expected_revision=revision_before_replay,
            now=NOW,
        )
        assert replayed.state_revision == revision_before_replay
        assert replayed.players[2].skill_resources["charge"] == 0
        assert (
            len(
                [
                    receipt
                    for receipt in replayed.rule_receipts
                    if occurrence_id in receipt.occurrence_ids
                ]
            )
            == 1
        )

        transitioned = await manager.commit_phase_transition(
            GamePhase.VOTE,
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
        assert transitioned.phase is GamePhase.VOTE
        assert all(isinstance(event, dict) for event in transitioned.events)

        forged_data = json.loads(replayed.model_dump_json())
        forged_private = next(
            event for event in forged_data["events"] if event["event_id"] == private_event.event_id
        )
        forged_private["channel"] = Channel.PUBLIC.value
        forged_private["audience"] = [1, 2, 3, 4]
        forged_state = GameState.model_validate_json(json.dumps(forged_data))
        forged_manager = GameManager(
            forged_state,
            registry=manager.registry,
            execution_package=execution,
        )
        with pytest.raises(EventCommitError, match="typed event log"):
            await forged_manager.peek_delivery(1)
    finally:
        await speech.close("event restore test complete")
        await choice.close("event restore test complete")
