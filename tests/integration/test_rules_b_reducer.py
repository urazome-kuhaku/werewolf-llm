"""Manager-level acceptance tests for B's typed rules reducer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game import (
    Action,
    ActionDefinition,
    ActionRegistry,
    ActionRequest,
    ActionValidationContext,
    ActionWindow,
    GameManager,
    GameState,
    PlayerState,
    ResolutionError,
    RulesetRef,
)
from werewolf.rules.compiler import validate_execution_package
from werewolf.rules.models import (
    AbilityGrant,
    ActionSpec,
    CompareExpr,
    CostSpec,
    EffectSpec,
    ExecutionPackage,
    LiteralExpr,
    PlayerFieldValues,
    RefExpr,
    RelationDeclaration,
    ResourceDeclaration,
    SelectorExpr,
    SkillSpec,
    StateDeclaration,
    TargetPolicy,
    UsagePolicy,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
GAME_ID = "generic-reducer-game"
BOARD_ID = "generic_reducer_board"
BOARD_VERSION = "1.0.0"
SESSION_EPOCH = 7


def _literal(value: Any, *, value_type: str | None = None) -> LiteralExpr:
    return LiteralExpr(value=value, value_type=value_type)  # type: ignore[arg-type]


def _ref(source: str, name: str) -> RefExpr:
    return RefExpr(source=source, name=name)  # type: ignore[arg-type]


def _all_seats() -> SelectorExpr:
    return SelectorExpr(source="players", map=_ref("item", "seat"))


def _role_seats(role_id: str) -> SelectorExpr:
    return SelectorExpr(
        source="players",
        where=CompareExpr(
            op="eq",
            left=_ref("item", "role_id"),
            right=_literal(role_id),
        ),
        map=_ref("item", "seat"),
    )


def _skill(
    skill_id: str,
    action_code: int,
    *,
    effects: tuple[EffectSpec, ...] = (),
    grant_id: str | None = None,
    grant_selector: SelectorExpr | None = None,
    condition: CompareExpr | None = None,
    usage: UsagePolicy | None = None,
    target_count: int = 0,
) -> SkillSpec:
    return SkillSpec(
        skill_id=skill_id,
        action_code=action_code,
        grants=(
            AbilityGrant(
                grant_id=grant_id or f"{skill_id}_grant",
                actor_selector=grant_selector or _all_seats(),
            ),
        ),
        timing=(GamePhase.NIGHT_ACTION.value,),
        condition=condition,
        targets=TargetPolicy(
            min_targets=target_count,
            max_targets=target_count,
            selector=_all_seats(),
        ),
        usage=usage or UsagePolicy(),
        effects=effects,
    )


def _package(
    skills: tuple[SkillSpec, ...],
    *,
    field_values: PlayerFieldValues | None = None,
    resources: tuple[ResourceDeclaration, ...] = (),
    relations: tuple[str, ...] = (),
    states: tuple[StateDeclaration, ...] = (),
) -> ExecutionPackage:
    package = ExecutionPackage(
        board_id=BOARD_ID,
        board_version=BOARD_VERSION,
        actions=tuple(
            ActionSpec(action_code=skill.action_code, action_id=f"TEST_ACTION_{skill.action_code}")
            for skill in skills
        ),
        skills=skills,
        player_field_values=field_values,
        resource_declarations=resources,
        relation_declarations=tuple(
            RelationDeclaration(relation_type=relation_type) for relation_type in relations
        ),
        state_declarations=states,
    )
    package_target_codes = tuple(
        skill.action_code for skill in skills if skill.targets.max_targets > 0
    )
    validate_execution_package(
        package,
        _registry(skills, target_codes=package_target_codes),
    )
    return package


def _registry(
    skills: tuple[SkillSpec, ...], *, target_codes: tuple[int, ...] = ()
) -> ActionRegistry:
    return ActionRegistry(
        actions=tuple(
            ActionDefinition(
                action_code=skill.action_code,
                action_name=f"TEST_ACTION_{skill.action_code}",
                target_policy="candidate" if skill.action_code in target_codes else "none",
                target_count=1 if skill.action_code in target_codes else 0,
            )
            for skill in skills
        )
    )


def _manager(
    package: ExecutionPackage,
    *,
    roles: dict[int, str] | None = None,
    resources: dict[int, dict[str, int]] | None = None,
    target_codes: tuple[int, ...] = (),
) -> GameManager:
    roles = roles or {1: "controller", 2: "villager"}
    resources = resources or {}
    state = GameState(
        game_id=GAME_ID,
        created_at=NOW,
        updated_at=NOW,
        phase=GamePhase.NIGHT_ACTION,
        round_no=1,
        day_no=1,
        ruleset=RulesetRef(
            board_id=BOARD_ID,
            version=BOARD_VERSION,
            snapshot_id="generic-reducer-snapshot",
            manifest_sha256="a" * 64,
        ),
        players={
            seat: PlayerState(
                seat=seat,
                role_id=role_id,
                faction_id="good",
                session_epoch=SESSION_EPOCH,
                skill_resources=resources.get(seat, {}),
            )
            for seat, role_id in roles.items()
        },
    )
    return GameManager(
        state,
        registry=_registry(package.skills, target_codes=target_codes),
        execution_package=package,
    )


async def _collect_request(
    manager: GameManager,
    *,
    action_code: int,
    group_id: str,
    request_id: str,
    seat: int = 1,
    targets: tuple[int, ...] = (),
) -> str:
    window_id = f"window-{request_id}"
    window = ActionWindow(
        window_id=window_id,
        game_id=GAME_ID,
        session_epoch=SESSION_EPOCH,
        phase=GamePhase.NIGHT_ACTION,
        allowed_seats=(seat,),
        allowed_action_codes=(action_code,),
        min_actions=1,
        max_actions=1,
        opened_at=NOW,
        settlement_group_id=group_id,
    )
    await manager.commit_action_window(window, now=NOW)
    await manager.begin_action_turn(
        seat,
        SESSION_EPOCH,
        window_id=window_id,
        request_id=request_id,
        now=NOW,
    )
    request = ActionRequest(
        request_id=request_id,
        game_id=GAME_ID,
        window_id=window_id,
        seat=seat,
        session_epoch=SESSION_EPOCH,
        phase=GamePhase.NIGHT_ACTION,
        actions=(Action(action_code=action_code, targets=targets),),
    )
    await manager.commit_action_request(
        request,
        ActionValidationContext(
            game_id=GAME_ID,
            session_epoch=SESSION_EPOCH,
            active_request_id=request_id,
            authorized_action_codes=(action_code,),
            eligible_targets_by_action={action_code: targets} if targets else {},
        ),
        now=NOW,
    )
    await manager.complete_rule_window(
        window_id,
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    return request_id


async def _run_action(
    manager: GameManager,
    *,
    action_code: int,
    group_id: str,
    request_id: str,
    seat: int = 1,
    targets: tuple[int, ...] = (),
) -> GameState:
    await _collect_request(
        manager,
        action_code=action_code,
        group_id=group_id,
        request_id=request_id,
        seat=seat,
        targets=targets,
    )
    return await manager.commit_rule_group(
        group_id,
        request_ids=(request_id,),
        expected_revision=manager.state.state_revision,
        now=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("player_field", "value", "allowed_values"),
    [
        ("role_id", "new_role", {"role_ids": ("villager", "new_role")}),
        ("faction_id", "new_faction", {"faction_ids": ("good", "new_faction")}),
        (
            "victory_group_id",
            "new_victory",
            {"victory_group_ids": ("new_victory",)},
        ),
        (
            "chat_group_ids",
            ["new_chat"],
            {"chat_group_ids": ("new_chat",)},
        ),
    ],
)
async def test_player_field_set_writes_each_declared_field_through_manager(
    player_field: str,
    value: object,
    allowed_values: dict[str, tuple[str, ...]],
) -> None:
    skill = _skill(
        "field_writer",
        401,
        effects=(
            EffectSpec(
                effect_id="set_field",
                effect_type="PLAYER_FIELD_SET",
                player_field=player_field,  # type: ignore[arg-type]
                value=_literal(value),
            ),
        ),
    )
    fields = PlayerFieldValues(**allowed_values)
    manager = _manager(_package((skill,), field_values=fields))

    committed = await _run_action(
        manager,
        action_code=401,
        group_id=f"field-{player_field}",
        request_id=f"field-request-{player_field}",
    )

    player = committed.players[1]
    assert getattr(player, player_field) == (
        tuple(value) if player_field == "chat_group_ids" else value
    )
    assert player.role_id == ("new_role" if player_field == "role_id" else "controller")
    assert player.faction_id == ("new_faction" if player_field == "faction_id" else "good")
    assert player.victory_group_id == (
        "new_victory" if player_field == "victory_group_id" else None
    )
    assert player.chat_group_ids == (("new_chat",) if player_field == "chat_group_ids" else ())


@pytest.mark.asyncio
async def test_resource_effect_and_cost_share_one_net_boundary_and_retry_once() -> None:
    skill = _skill(
        "net_resource_writer",
        401,
        effects=(
            EffectSpec(
                effect_id="resource_refund",
                effect_type="RESOURCE_DELTA",
                resource_id="charge",
                delta=_literal(3),
            ),
        ),
        usage=UsagePolicy(costs=(CostSpec(resource_id="charge", amount=2),)),
    )
    package = _package(
        (skill,), resources=(ResourceDeclaration(resource_id="charge", min_value=0, max_value=5),)
    )
    manager = _manager(package, resources={1: {"charge": 4}})
    await _collect_request(
        manager,
        action_code=401,
        group_id="resource-net",
        request_id="resource-net-request",
    )

    committed = await manager.commit_rule_group(
        "resource-net",
        request_ids=("resource-net-request",),
        expected_revision=manager.state.state_revision,
        now=NOW,
    )
    retried = await manager.commit_rule_group(
        "resource-net",
        request_ids=("resource-net-request",),
        expected_revision=manager.state.state_revision,
        now=NOW,
    )

    assert committed.players[1].skill_resources["charge"] == 5
    assert retried == committed
    assert retried.state_revision == committed.state_revision
    assert len(retried.rule_ledger) == 1
    assert len(retried.rule_receipts) == 1


@pytest.mark.asyncio
async def test_candidate_batch_is_replanned_and_a_forged_candidate_is_atomic_rejection() -> None:
    skill = _skill(
        "candidate_writer",
        401,
        effects=(
            EffectSpec(
                effect_id="set_role",
                effect_type="PLAYER_FIELD_SET",
                player_field="role_id",
                value=_literal("new_role"),
            ),
        ),
    )
    package = _package(
        (skill,),
        field_values=PlayerFieldValues(role_ids=("controller", "new_role")),
    )
    manager = _manager(package)
    request_id = await _collect_request(
        manager,
        action_code=401,
        group_id="candidate-group",
        request_id="candidate-request",
    )
    state_before = manager.state
    skill_requests, _bindings, _group_id = manager._rule_group_requests(
        state_before,
        (request_id,),
        timing=GamePhase.NIGHT_ACTION.value,
        group_id_override="candidate-group",
    )
    authentic = manager._rules.plan(
        state_before,
        skill_requests,
        group_id="candidate-group",
        timing=GamePhase.NIGHT_ACTION.value,
    )
    forged = authentic.model_copy(update={"batch_id": "caller-forged-batch"})

    with pytest.raises(ResolutionError, match="RULE_BATCH_INVALID"):
        await manager.commit_rule_group(
            "candidate-group",
            request_ids=(request_id,),
            candidate_batch=forged,
            expected_revision=manager.state.state_revision,
            now=NOW,
        )

    assert manager.state == state_before
    assert manager.state.players[1].role_id == "controller"
    assert manager.state.action_requests[request_id]["status"] == "PENDING"
    assert manager.state.rule_ledger == ()


@pytest.mark.asyncio
async def test_relation_add_and_remove_are_committed_as_typed_manager_updates() -> None:
    relation_type = "generic_link"
    add_skill = _skill(
        "relation_add",
        401,
        effects=(
            EffectSpec(
                effect_id="add_link",
                effect_type="RELATION_ADD",
                relation_type=relation_type,
                relation_source=_ref("actor", "seat"),
                relation_target=_ref("target", "seat"),
                relation_expiry_policy="NEVER",
            ),
        ),
        target_count=1,
    )
    remove_skill = _skill(
        "relation_remove",
        402,
        effects=(
            EffectSpec(
                effect_id="remove_link",
                effect_type="RELATION_REMOVE",
                relation_type=relation_type,
                relation_source=_ref("actor", "seat"),
                relation_target=_ref("target", "seat"),
            ),
        ),
        target_count=1,
    )
    manager = _manager(
        _package((add_skill, remove_skill), relations=(relation_type,)),
        target_codes=(401, 402),
    )

    added = await _run_action(
        manager,
        action_code=401,
        group_id="relation-add-group",
        request_id="relation-add-request",
        targets=(2,),
    )
    assert len(added.rule_relations) == 1
    relation_id = added.rule_relations[0].relation_id
    assert (added.rule_relations[0].source_seat, added.rule_relations[0].target_seat) == (1, 2)

    removed = await _run_action(
        manager,
        action_code=402,
        group_id="relation-remove-group",
        request_id="relation-remove-request",
        targets=(2,),
    )

    assert removed.rule_relations == ()
    assert relation_id not in {
        item.relation_id
        for item in manager._rules.observation(
            removed, group_id="post-remove", timing=GamePhase.NIGHT_ACTION.value
        ).relations
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_kind", "expiry_policy", "present_at_victory_check"),
    [
        ("relation", "ROUND_END", False),
        ("state", "ROUND_END", False),
        ("relation", "NEXT_NIGHT_START", True),
        ("state", "NEXT_NIGHT_START", True),
    ],
)
async def test_expiry_uses_round_and_night_phase_boundaries(
    record_kind: str,
    expiry_policy: str,
    present_at_victory_check: bool,
) -> None:
    if record_kind == "relation":
        declaration = {"relations": ("expiring_link",)}
        effect = EffectSpec(
            effect_id="add_expiring_link",
            effect_type="RELATION_ADD",
            relation_type="expiring_link",
            relation_source=_ref("actor", "seat"),
            relation_target=_ref("target", "seat"),
            relation_expiry_policy=expiry_policy,  # type: ignore[arg-type]
        )
        state_declarations: tuple[StateDeclaration, ...] = ()
    else:
        declaration = {}
        effect = EffectSpec(
            effect_id="write_expiring_state",
            effect_type="STATE_SET",
            state_key="ephemeral_marker",
            value=_literal("present"),
        )
        state_declarations = (
            StateDeclaration(
                skill_id="expiry_writer",
                key="ephemeral_marker",
                value_type="str",
                scope="GAME",
                expiry_policy=expiry_policy,  # type: ignore[arg-type]
                initial="initial",
            ),
        )
    skill = _skill(
        "expiry_writer",
        401,
        effects=(effect,),
        target_count=1 if record_kind == "relation" else 0,
    )
    package = _package((skill,), states=state_declarations, **declaration)
    manager = _manager(
        package,
        target_codes=(401,) if record_kind == "relation" else (),
    )
    await _run_action(
        manager,
        action_code=401,
        group_id=f"expiry-{record_kind}-{expiry_policy}",
        request_id=f"expiry-request-{record_kind}-{expiry_policy}",
        targets=(2,) if record_kind == "relation" else (),
    )

    before = manager._rules.observation(
        manager.state, group_id="before-boundary", timing=GamePhase.NIGHT_ACTION.value
    )
    assert bool(before.relations if record_kind == "relation" else before.state_values)
    for phase in (
        GamePhase.NIGHT_RESOLVE,
        GamePhase.DAY_ANNOUNCE,
        GamePhase.DAY_SPEECH,
        GamePhase.VOTE,
        GamePhase.DAY_RESOLVE,
        GamePhase.VICTORY_CHECK,
    ):
        await manager.commit_phase_transition(phase, now=NOW)
    assert manager.state.round_no == 2
    at_victory_check = manager._rules.observation(
        manager.state,
        group_id="at-victory-check",
        timing=GamePhase.VICTORY_CHECK.value,
    )
    visible_at_victory_check = bool(
        at_victory_check.relations if record_kind == "relation" else at_victory_check.state_values
    )
    assert visible_at_victory_check is present_at_victory_check

    await manager.commit_phase_transition(GamePhase.NIGHT_TEAM_CHAT, now=NOW)
    at_next_night = manager._rules.observation(
        manager.state,
        group_id="at-next-night",
        timing=GamePhase.NIGHT_TEAM_CHAT.value,
    )
    assert at_next_night.relations == ()
    assert not any(item.key == "ephemeral_marker" for item in at_next_night.state_values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected_scope_id"),
    [("GAME", None), ("SEAT", "seat-1"), ("ABILITY", "instance")],
)
async def test_game_seat_and_ability_state_survive_snapshot_restore(
    scope: str,
    expected_scope_id: str | None,
) -> None:
    effect = EffectSpec(
        effect_id="persist_value",
        effect_type="STATE_SET",
        target=None,
        state_key="persisted_marker",
        value=_literal("written"),
    )
    skill = _skill("state_writer", 401, effects=(effect,))
    package = _package(
        (skill,),
        states=(
            StateDeclaration(
                skill_id="state_writer",
                key="persisted_marker",
                value_type="str",
                scope=scope,  # type: ignore[arg-type]
                initial="initial",
            ),
        ),
    )
    manager = _manager(package)

    committed = await _run_action(
        manager,
        action_code=401,
        group_id=f"state-{scope}",
        request_id=f"state-request-{scope}",
    )
    expected_persisted_scope_id = expected_scope_id
    if expected_scope_id == "instance":
        expected_persisted_scope_id = next(
            instance.ability_instance_id
            for instance in committed.ability_instances
            if instance.actor_seat == 1 and instance.skill_id == "state_writer"
        )
    cell = next(
        item
        for item in committed.rule_state
        if item.skill_id == "state_writer"
        and item.key == "persisted_marker"
        and item.scope == scope
        and (expected_persisted_scope_id is None or item.scope_id == expected_persisted_scope_id)
    )
    assert cell.value == "written"
    restored = GameState.model_validate_json(committed.model_dump_json())
    restored_manager = GameManager(
        restored,
        registry=_registry(package.skills),
        execution_package=package,
    )
    restored_cell = next(
        item
        for item in restored_manager.state.rule_state
        if item.skill_id == "state_writer"
        and item.key == "persisted_marker"
        and item.scope == scope
        and (expected_persisted_scope_id is None or item.scope_id == expected_persisted_scope_id)
    )
    observation = restored_manager._rules.observation(
        restored_manager.state,
        group_id="restored-state",
        timing=GamePhase.NIGHT_ACTION.value,
    )

    assert restored_cell == cell
    assert any(
        item.key == "persisted_marker" and item.scope == scope and item.value == "written"
        for item in observation.state_values
    )
    if expected_scope_id == "instance":
        assert restored_cell.scope_id is not None
        assert restored_cell.scope_id in {
            item.ability_instance_id
            for item in restored_manager.state.ability_instances
            if item.actor_seat == 1 and item.skill_id == "state_writer"
        }


@pytest.mark.asyncio
async def test_revoke_is_exact_preserves_ledger_and_new_grant_rechecks_role_condition() -> None:
    old_skill = _skill(
        "old_action",
        450,
        grant_id="old_grant",
        grant_selector=_role_seats("blocked"),
        condition=CompareExpr(op="eq", left=_ref("actor", "role_id"), right=_literal("eligible")),
        effects=(EffectSpec(effect_id="old_fact", effect_type="FACT", fact_type="old_skill_ran"),),
    )
    other_skill = _skill(
        "other_action",
        451,
        grant_id="other_grant",
        grant_selector=_role_seats("blocked"),
    )
    new_skill = _skill(
        "new_action",
        452,
        grant_id="new_grant",
        grant_selector=_role_seats("eligible"),
        condition=CompareExpr(op="eq", left=_ref("actor", "role_id"), right=_literal("eligible")),
        effects=(EffectSpec(effect_id="new_fact", effect_type="FACT", fact_type="new_skill_ran"),),
    )
    revoke_skill = _skill(
        "revoke_action",
        401,
        effects=(
            EffectSpec(
                effect_id="revoke_old",
                effect_type="ABILITY_REVOKE",
                target=_ref("target", "seat"),
                grant_skill_id="old_action",
                grant_id="old_grant",
            ),
        ),
        target_count=1,
    )
    grant_skill = _skill(
        "grant_action",
        402,
        effects=(
            EffectSpec(
                effect_id="grant_new",
                effect_type="ABILITY_GRANT",
                target=_ref("target", "seat"),
                grant_skill_id="new_action",
                grant_id="new_grant",
            ),
        ),
        target_count=1,
    )
    promote_skill = _skill(
        "promote_action",
        403,
        effects=(
            EffectSpec(
                effect_id="set_eligible_role",
                effect_type="PLAYER_FIELD_SET",
                target=_ref("target", "seat"),
                player_field="role_id",
                value=_literal("eligible"),
            ),
        ),
        target_count=1,
    )
    package = _package(
        (revoke_skill, grant_skill, promote_skill, old_skill, other_skill, new_skill),
        field_values=PlayerFieldValues(role_ids=("blocked", "eligible", "controller")),
    )
    manager = _manager(
        package,
        roles={1: "controller", 2: "blocked"},
        target_codes=(401, 402, 403),
    )

    before = await _run_action(
        manager,
        action_code=401,
        group_id="exact-revoke",
        request_id="exact-revoke-request",
        targets=(2,),
    )
    old_instance = next(
        item
        for item in before.ability_instances
        if item.actor_seat == 2 and item.skill_id == "old_action"
    )
    other_instance = next(
        item
        for item in before.ability_instances
        if item.actor_seat == 2 and item.skill_id == "other_action"
    )
    revoked = before
    assert revoked.players[2].role_id == "blocked"
    assert (
        next(
            item
            for item in revoked.ability_instances
            if item.ability_instance_id == old_instance.ability_instance_id
        ).enabled
        is False
    )
    assert (
        next(
            item
            for item in revoked.ability_instances
            if item.ability_instance_id == other_instance.ability_instance_id
        ).enabled
        is True
    )

    granted = await _run_action(
        manager,
        action_code=402,
        group_id="dynamic-grant",
        request_id="dynamic-grant-request",
        targets=(2,),
    )
    new_instance = next(
        item
        for item in granted.ability_instances
        if item.actor_seat == 2 and item.skill_id == "new_action" and item.enabled
    )
    assert new_instance.grant_id == "new_grant"
    assert any(
        item.ability_instance_id == old_instance.ability_instance_id and not item.enabled
        for item in granted.ability_instances
    )
    assert any(
        item.ability_instance_id == other_instance.ability_instance_id and item.enabled
        for item in granted.ability_instances
    )

    await _collect_request(
        manager,
        action_code=452,
        group_id="blocked-new-skill",
        request_id="blocked-new-skill-request",
        seat=2,
    )
    blocked_before_commit = manager.state
    with pytest.raises(ResolutionError, match="RULE_REQUEST_REJECTED"):
        await manager.commit_rule_group(
            "blocked-new-skill",
            request_ids=("blocked-new-skill-request",),
            expected_revision=manager.state.state_revision,
            now=NOW,
        )
    assert manager.state == blocked_before_commit
    assert manager.state.players[2].role_id == "blocked"
    assert manager.state.action_requests["blocked-new-skill-request"]["status"] == "PENDING"
    assert len(manager.state.rule_ledger) == len(granted.rule_ledger)

    promoted_manager = GameManager(
        granted,
        registry=_registry(package.skills, target_codes=(401, 402, 403)),
        execution_package=package,
    )
    promoted = await _run_action(
        promoted_manager,
        action_code=403,
        group_id="promote-target",
        request_id="promote-target-request",
        targets=(2,),
    )
    assert promoted.players[2].role_id == "eligible"
    activated = await _run_action(
        promoted_manager,
        action_code=452,
        group_id="eligible-new-skill",
        request_id="eligible-new-skill-request",
        seat=2,
    )

    assert any(
        fact.fact_type == "new_skill_ran" for entry in activated.rule_ledger for fact in entry.facts
    )
    assert any(
        item.ability_instance_id == old_instance.ability_instance_id and not item.enabled
        for item in activated.ability_instances
    )
    assert any(
        item.ability_instance_id == other_instance.ability_instance_id and item.enabled
        for item in activated.ability_instances
    )
