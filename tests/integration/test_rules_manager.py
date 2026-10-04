"""End-to-end manager boundaries for the frozen rules execution package."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionDefinition,
    ActionRegistry,
    ActionRequest,
    GameManager,
    GameState,
    GrantedTriggerAbility,
    PlayerState,
    ResolutionError,
    RulesetRef,
    VoteRequest,
    VoteWindow,
)
from werewolf.game.manager import _stored_action_request
from werewolf.game.state import AbilityInstanceState, RuleLedgerEntry, RuleUseRecord
from werewolf.game.voting import TieAction, TieDecision
from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.role import (
    TargetKind,
    TargetRule,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
)
from werewolf.rules.models import ExecutionPackage, LiteralExpr

PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


async def _classic() -> tuple[ExecutionPackage, BoardDefinition, ActionRegistry, str]:
    package = await KnowledgePackageLoader(PUBLISHED_ROOT).load(BOARD_REF)
    compiled = KnowledgePackageCompiler().compile(package)
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    board = BoardDefinition.model_validate(compiled.package_payload["board_definition"])
    return compiled.execution, board, compiled.action_registry, compiled.manifest_sha256


def _ruleset_ref(manifest_sha256: str) -> RulesetRef:
    return RulesetRef(
        board_id="classic_12_seer_witch_hunter_idiot",
        version="1.0.0",
        snapshot_id=f"ruleset-{manifest_sha256}",
        manifest_sha256=manifest_sha256,
    )


def _hunter_trigger() -> GrantedTriggerAbility:
    return GrantedTriggerAbility(
        ability_id="hunter_shoot",
        action_code=105,
        trigger=TriggerRule(
            event=TriggerEvent.DEATH_CONFIRMED,
            allowed_death_causes=["exiled"],
            mode=TriggerMode.PLAYER_CHOICE,
            effects=[TriggerEffect.OPEN_PLAYER_ACTION],
            allow_pass=True,
        ),
        target_rule=TargetRule(
            kind=TargetKind.PLAYER,
            min_targets=1,
            max_targets=1,
            allow_self=False,
        ),
    )


def _idiot_trigger(*, ability_id: str = "reveal_on_exile") -> GrantedTriggerAbility:
    return GrantedTriggerAbility(
        ability_id=ability_id,
        action_code=0,
        trigger=TriggerRule(
            event=TriggerEvent.EXILE_SELECTED,
            mode=TriggerMode.AUTOMATIC,
            effects=[
                TriggerEffect.REVEAL_ROLE,
                TriggerEffect.SURVIVE_TRIGGER,
                TriggerEffect.REMOVE_VOTE_RIGHT,
            ],
        ),
        target_rule=TargetRule(kind=TargetKind.NONE, min_targets=0, max_targets=0),
    )


def _manager(
    execution: ExecutionPackage,
    registry: ActionRegistry,
    manifest_sha256: str,
    *,
    target_role: str = "villager",
) -> GameManager:
    target_faction = "wolf" if target_role == "wolf" else "good"
    target_triggers: tuple[GrantedTriggerAbility, ...] = ()
    if target_role == "hunter":
        target_triggers = (_hunter_trigger(),)
    elif target_role == "idiot":
        target_triggers = (_idiot_trigger(),)
    state = GameState(
        game_id="rules-manager-game",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.DAY_RESOLVE,
        round_no=1,
        day_no=1,
        ruleset=_ruleset_ref(manifest_sha256),
        players={
            1: PlayerState(
                seat=1,
                role_id=target_role,
                faction_id=target_faction,
                session_epoch=3,
                granted_trigger_abilities=target_triggers,
            ),
            2: PlayerState(seat=2, role_id="wolf", faction_id="wolf", session_epoch=3),
            3: PlayerState(seat=3, role_id="villager", faction_id="good", session_epoch=3),
        },
    )
    return GameManager(state, registry=registry, execution_package=execution)


async def _collect_confirmed_vote(
    manager: GameManager,
    board: BoardDefinition,
    *,
    target_seat: int | None,
    window_id: str = "public-vote-r1",
) -> GameState:
    state = manager.state
    seats = tuple(sorted(state.players))
    window = VoteWindow(
        window_id=window_id,
        game_id=state.game_id,
        session_epoch=3,
        observation_revision=state.state_revision,
        eligible_voters=seats,
        candidate_seats=seats,
        vote_weights={seat: 1.0 for seat in seats},
        expected_request_ids={seat: f"vote-{seat}" for seat in seats},
        allow_abstain=True,
    )
    await manager.open_vote_window(window, now=NOW)
    targets = (
        {seat: target_seat for seat in seats}
        if target_seat is not None
        else {seat: None for seat in seats}
    )
    if target_seat is not None:
        targets[target_seat] = None
    for seat in seats:
        current = manager.state
        await manager.submit_vote(
            VoteRequest(
                request_id=f"vote-{seat}",
                game_id=state.game_id,
                window_id=window_id,
                seat=seat,
                session_epoch=3,
                observation_revision=window.observation_revision,
                target_seat=targets[seat],
            ),
            expected_revision=current.state_revision,
            now=NOW,
        )
    current = manager.state
    finalized = await manager.finalize_vote(
        expected_revision=current.state_revision,
        tie_resolver=(
            lambda window, tally: TieDecision(
                action=TieAction.NO_EXILE,
                candidates=tuple(tally.top_candidates),
            )
        )
        if target_seat is None
        else None,
        now=NOW,
    )
    return await manager.confirm_vote_tally(
        board,
        expected_revision=finalized.state_revision,
        now=NOW,
    )


async def _vote_and_commit_exile(
    manager: GameManager,
    board: BoardDefinition,
    *,
    target_seat: int | None,
    window_id: str = "public-vote-r1",
) -> GameState:
    confirmed = await _collect_confirmed_vote(
        manager,
        board,
        target_seat=target_seat,
        window_id=window_id,
    )
    return await manager.commit_confirmed_vote_exile(
        target_seat=target_seat,
        vote_window_id=window_id,
        expected_revision=confirmed.state_revision,
        now=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_role", "expected_alive", "expected_can_vote", "expected_cause", "expected_phase"),
    [
        ("hunter", False, True, "exiled", GamePhase.TRIGGER_ACTION),
        ("idiot", True, False, None, GamePhase.DAY_RESOLVE),
        ("villager", False, True, "exiled", GamePhase.DAY_RESOLVE),
    ],
)
async def test_confirmed_vote_exile_uses_host_rules_for_roleless_and_legacy_triggers(
    target_role: str,
    expected_alive: bool,
    expected_can_vote: bool,
    expected_cause: str | None,
    expected_phase: GamePhase,
) -> None:
    execution, board, registry, manifest_sha256 = await _classic()
    manager = _manager(
        execution,
        registry,
        manifest_sha256,
        target_role=target_role,
    )
    if target_role == "idiot":
        manager = GameManager(
            manager.state.model_copy(
                update={
                    "ability_instances": (
                        AbilityInstanceState(
                            ability_instance_id="idiot-reveal-instance",
                            skill_id="idiot_reveal",
                            grant_id="reveal_on_exile",
                            action_code=0,
                            actor_seat=1,
                            grant_kind="TRIGGER",
                        ),
                        AbilityInstanceState(
                            ability_instance_id="idiot-other-trigger-instance",
                            skill_id="idiot_other_trigger",
                            grant_id="other_idiot_trigger",
                            action_code=0,
                            actor_seat=1,
                            grant_kind="TRIGGER",
                        ),
                    )
                }
            ),
            registry=registry,
            execution_package=execution,
        )

    committed = await _vote_and_commit_exile(manager, board, target_seat=1)

    target = committed.players[1]
    assert target.alive is expected_alive
    assert target.can_vote is expected_can_vote
    assert target.death_cause == expected_cause
    assert committed.phase is expected_phase
    assert len(committed.rule_ledger) == 1
    if target_role == "idiot":
        assert target.granted_trigger_abilities[0].ability_id == "reveal_on_exile"
        assert target.granted_trigger_abilities[0].consumed is True
        consumed_instances = {item.grant_id: item.consumed for item in committed.ability_instances}
        assert consumed_instances == {
            "reveal_on_exile": True,
            "other_idiot_trigger": False,
        }
        assert any(
            fact.fact_type == "idiot_revealed" and fact.target_seat == 1
            for fact in committed.rule_ledger[0].facts
        )

    restored = GameState.model_validate_json(committed.model_dump_json())
    restored_manager = GameManager(restored, registry=registry, execution_package=execution)
    assert restored_manager.state.rule_ledger[0].created_at == NOW
    ledger_restored = RuleLedgerEntry.model_validate(
        committed.rule_ledger[0].model_dump(mode="python")
    )
    assert ledger_restored.facts == committed.rule_ledger[0].facts


@pytest.mark.asyncio
async def test_confirmed_vote_without_exile_and_exile_replays_are_atomic_and_idempotent() -> None:
    execution, board, registry, manifest_sha256 = await _classic()
    no_exile_manager = _manager(execution, registry, manifest_sha256)

    no_exile = await _vote_and_commit_exile(no_exile_manager, board, target_seat=None)

    assert no_exile.players[1].alive is True
    assert no_exile.rule_ledger == ()

    manager = _manager(execution, registry, manifest_sha256)
    confirmed = await _collect_confirmed_vote(manager, board, target_seat=1)
    first = await manager.commit_confirmed_vote_exile(
        target_seat=1,
        vote_window_id="public-vote-r1",
        expected_revision=confirmed.state_revision,
        now=NOW,
    )
    replay = await manager.commit_confirmed_vote_exile(
        target_seat=1,
        vote_window_id="public-vote-r1",
        now=NOW,
    )
    assert replay is first
    with pytest.raises(ResolutionError, match="IDEMPOTENCY_CONFLICT"):
        await manager.commit_confirmed_vote_exile(
            target_seat=3,
            vote_window_id="public-vote-r1",
            now=NOW,
        )
    assert manager.state is first


@pytest.mark.asyncio
async def test_unknown_consume_ability_id_rejects_without_partial_exile_commit() -> None:
    execution, board, registry, manifest_sha256 = await _classic()
    interactions = []
    for interaction in execution.interactions:
        if interaction.interaction_id != "classic_idiot_exile_replacement":
            interactions.append(interaction)
            continue
        effects = tuple(
            effect.model_copy(update={"value": LiteralExpr(value="unknown_trigger_id")})
            if effect.effect_type == "CONSUME_ABILITY"
            else effect
            for effect in interaction.effects
        )
        interactions.append(interaction.model_copy(update={"effects": effects}))
    changed_execution = execution.model_copy(update={"interactions": tuple(interactions)})
    manager = _manager(
        changed_execution,
        registry,
        manifest_sha256,
        target_role="idiot",
    )
    confirmed = await _collect_confirmed_vote(manager, board, target_seat=1)
    before = manager.state

    with pytest.raises(ResolutionError, match="exactly one granted trigger ability"):
        await manager.commit_confirmed_vote_exile(
            target_seat=1,
            vote_window_id="public-vote-r1",
            expected_revision=confirmed.state_revision,
            now=NOW,
        )

    assert manager.state is before
    assert manager.state.players[1].alive is True
    assert manager.state.players[1].can_vote is True
    assert manager.state.players[1].granted_trigger_abilities[0].consumed is False
    assert manager.state.rule_ledger == ()


@pytest.mark.asyncio
async def test_restore_rejects_same_board_package_with_different_action_registry() -> None:
    execution, _board, registry, manifest_sha256 = await _classic()
    manager = _manager(execution, registry, manifest_sha256)
    additional_action = ActionDefinition(
        action_code=298,
        action_name="UNUSED_TEST_ACTION",
        target_policy="none",
        target_count=0,
    )
    other_registry = ActionRegistry(actions=(*registry.actions, additional_action))

    restored = GameState.model_validate_json(manager.state.model_dump_json())
    with pytest.raises(ValueError, match="pinned execution identity"):
        GameManager(restored, registry=other_registry, execution_package=execution)


def _night_request(
    *,
    request_id: str,
    seat: int,
    action_code: int,
    targets: tuple[int, ...] = (),
) -> dict[str, object]:
    request = ActionRequest(
        request_id=request_id,
        game_id="rules-manager-game",
        window_id="night-r1",
        seat=seat,
        session_epoch=3,
        phase=GamePhase.NIGHT_ACTION,
        actions=(Action(action_code=action_code, targets=targets),),
    )
    return {**request.model_dump(mode="json"), "status": "PENDING"}


def _night_manager(
    execution: ExecutionPackage,
    registry: ActionRegistry,
    manifest_sha256: str,
    requests: dict[str, dict[str, object]],
    *,
    seats: tuple[int, ...],
) -> GameManager:
    state = GameState(
        game_id="rules-manager-game",
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_ACTION,
        round_no=1,
        ruleset=_ruleset_ref(manifest_sha256),
        players={
            1: PlayerState(
                seat=1,
                role_id="wolf",
                faction_id="wolf",
                chat_group_ids=("wolf",),
                session_epoch=3,
            ),
            2: PlayerState(
                seat=2,
                role_id="witch",
                faction_id="good",
                session_epoch=3,
                skill_resources={"witch_heal": 1, "witch_poison": 1},
            ),
            3: PlayerState(seat=3, role_id="villager", faction_id="good", session_epoch=3),
        },
        action_windows={
            "night-r1": {
                "window_id": "night-r1",
                "game_id": "rules-manager-game",
                "session_epoch": 3,
                "phase": GamePhase.NIGHT_ACTION.value,
                "allowed_seats": list(seats),
                "allowed_action_codes": [101, 102, 103, 104, 299],
                "min_actions": 1,
                "max_actions": 1,
                "allow_pass": True,
                "allow_concurrent": True,
                "opened_at": NOW.isoformat(),
                "visible_context": {"candidate_seats": [1, 2, 3]},
            }
        },
        action_requests=requests,
    )
    return GameManager(state, registry=registry, execution_package=execution)


def _night_rule_batch(
    manager: GameManager,
    request_ids: tuple[str, ...],
) -> tuple[object, tuple[object, ...], tuple[object, ...], str]:
    state = manager.state
    requests, bindings, group_id = manager._rule_group_requests(
        state,
        request_ids,
        timing=GamePhase.NIGHT_ACTION.value,
    )
    assert manager._rules is not None
    batch = manager._rules.plan(
        state,
        requests,
        group_id=group_id,
        timing=GamePhase.NIGHT_ACTION.value,
    )
    return batch, requests, bindings, group_id


@pytest.mark.asyncio
async def test_global_pass_binds_each_wolf_or_witch_skill_with_no_cost_on_pass() -> None:
    execution, _board, registry, manifest_sha256 = await _classic()
    wolf_pass_id = "wolf-pass"
    wolf_manager = _night_manager(
        execution,
        registry,
        manifest_sha256,
        {wolf_pass_id: _night_request(request_id=wolf_pass_id, seat=1, action_code=299)},
        seats=(1, 2),
    )
    wolf_batch, wolf_requests, wolf_bindings, wolf_group = _night_rule_batch(
        wolf_manager,
        (wolf_pass_id,),
    )
    assert len(wolf_requests) == 1
    assert wolf_requests[0].skill_id == "wolf_kill"
    assert wolf_requests[0].passed is True
    wolf_committed = wolf_manager._apply_rule_batch(
        wolf_manager.state,
        wolf_batch,
        wolf_requests,
        wolf_bindings,
        (),
        group_id=wolf_group,
        timing=GamePhase.NIGHT_ACTION.value,
        timestamp=NOW,
    )
    assert wolf_batch.cost_updates == ()
    assert wolf_batch.facts == ()
    assert wolf_batch.effects == ()
    wolf_request_payload = wolf_committed.action_requests[wolf_pass_id]
    assert wolf_request_payload["status"] == "CONFIRMED"
    assert wolf_request_payload["rule_disposition"] == "PASSED"
    assert wolf_request_payload["rule_receipt_id"] == wolf_batch.batch_id
    assert wolf_request_payload["rule_group_id"] == wolf_group
    assert [item["status"] for item in wolf_request_payload["rule_dispositions"]] == ["PASSED"]
    wolf_receipt = next(
        item for item in wolf_committed.rule_receipts if item.batch_id == wolf_batch.batch_id
    )
    assert wolf_receipt.group_id == wolf_group
    assert wolf_receipt.request_ids == (wolf_pass_id,)

    wolf_attack_id = "w" * 60
    witch_pass_id = "i" * 60
    night_requests = {
        wolf_attack_id: _night_request(
            request_id=wolf_attack_id,
            seat=1,
            action_code=101,
            targets=(3,),
        ),
        witch_pass_id: _night_request(request_id=witch_pass_id, seat=2, action_code=299),
    }
    witch_manager = _night_manager(
        execution,
        registry,
        manifest_sha256,
        night_requests,
        seats=(1, 2),
    )
    witch_batch, witch_requests, witch_bindings, witch_group = _night_rule_batch(
        witch_manager,
        (wolf_attack_id, witch_pass_id),
    )
    witch_passes = [item for item in witch_requests if item.passed]
    assert {item.skill_id for item in witch_passes} == {"witch_heal", "witch_poison"}
    assert all(item.action_code in {103, 104} for item in witch_passes)
    assert witch_group.startswith("night_action:1:")
    assert len(witch_group) < 128
    assert witch_batch.cost_updates == ()
    assert all(
        not item.accepted
        for item in witch_batch.use_updates
        if item.skill_id
        in {
            "witch_heal",
            "witch_poison",
        }
    )
    witch_committed = witch_manager._apply_rule_batch(
        witch_manager.state,
        witch_batch,
        witch_requests,
        witch_bindings,
        (),
        group_id=witch_group,
        timing=GamePhase.NIGHT_ACTION.value,
        timestamp=NOW,
    )
    assert witch_committed.players[2].skill_resources == {
        "witch_heal": 1,
        "witch_poison": 1,
    }
    witch_request_payload = witch_committed.action_requests[witch_pass_id]
    assert witch_request_payload["status"] == "CONFIRMED"
    assert witch_request_payload["rule_disposition"] == "PASSED"
    assert witch_request_payload["rule_receipt_id"] == witch_batch.batch_id
    assert witch_request_payload["rule_group_id"] == witch_group
    assert [item["status"] for item in witch_request_payload["rule_dispositions"]] == [
        "PASSED",
        "PASSED",
    ]
    witch_receipt = next(
        item for item in witch_committed.rule_receipts if item.batch_id == witch_batch.batch_id
    )
    assert witch_receipt.group_id == witch_group
    assert witch_receipt.request_ids == tuple(sorted((wolf_attack_id, witch_pass_id)))
    restored = GameState.model_validate_json(witch_committed.model_dump_json())
    restored_witch_request = _stored_action_request(restored, witch_pass_id)
    assert restored_witch_request.actions[0].action_code == 299
    assert restored.action_requests[witch_pass_id]["rule_receipt_id"] == witch_batch.batch_id


@pytest.mark.asyncio
async def test_round_usage_uses_current_round_ledger_not_cumulative_instance_counter() -> None:
    execution, _board, registry, manifest_sha256 = await _classic()
    manager = _night_manager(execution, registry, manifest_sha256, {}, seats=(1, 2))
    instance = next(
        item for item in manager.state.ability_instances if item.skill_id == "wolf_kill"
    )
    used = instance.model_copy(update={"uses_consumed": 1})
    use = RuleUseRecord(
        record_id="round-one-wolf-use",
        request_id="round-one-wolf-request",
        ability_instance_id=instance.ability_instance_id,
        skill_id="wolf_kill",
        action_code=101,
        actor_seat=1,
        round_number=1,
        targets=(3,),
    )
    ledger = RuleLedgerEntry(
        batch_id="round-one-wolf-batch",
        package_id=execution.package_id,
        group_id="round-one-wolf-group",
        timing=GamePhase.NIGHT_ACTION.value,
        read_revision=0,
        committed_revision=1,
        round_no=1,
        request_ids=("round-one-wolf-request",),
        actor_seats=(1,),
        skill_ids=("wolf_kill",),
        action_codes=(101,),
        history_updates=(use,),
        outcome_digest="a" * 64,
        created_at=NOW,
    )
    round_one = manager.state.model_copy(
        update={
            "ability_instances": tuple(
                used if item is instance else item for item in manager.state.ability_instances
            ),
            "rule_ledger": (ledger,),
        }
    )
    assert (
        manager._rule_skill_instances(
            round_one,
            1,
            GamePhase.NIGHT_ACTION.value,
            allowed_codes={101},
        )
        == ()
    )
    round_two = round_one.model_copy(update={"round_no": 2})
    assert manager._rule_skill_instances(
        round_two,
        1,
        GamePhase.NIGHT_ACTION.value,
        allowed_codes={101},
    ) == ((used, next(skill for skill in execution.skills if skill.skill_id == "wolf_kill")),)


@pytest.mark.asyncio
async def test_ledger_json_restore_keeps_aware_timestamp_strict() -> None:
    execution, _board, registry, manifest_sha256 = await _classic()
    manager = _manager(execution, registry, manifest_sha256)
    data = json.loads(manager.state.model_dump_json())
    data["rule_ledger"] = [
        {
            "schema_version": 1,
            "batch_id": "timestamp-ledger",
            "package_id": execution.package_id,
            "group_id": "timestamp-group",
            "timing": "DAY_RESOLVE",
            "read_revision": 0,
            "committed_revision": 1,
            "round_no": 1,
            "request_ids": [],
            "actor_seats": [],
            "skill_ids": [],
            "action_codes": [],
            "history_updates": [],
            "facts": [],
            "outcome_digest": "b" * 64,
            "created_at": "2026-10-04T12:00:00",
        }
    ]
    with pytest.raises(ValidationError, match="timestamp must include a timezone"):
        GameState.model_validate_json(json.dumps(data))
