"""Tests for deterministic, ruleset-backed initial role assignment."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.game.setup import (
    PlayerAssignmentPlan,
    RoleAssignmentError,
    build_role_assignment_plan,
)
from werewolf.knowledge.board import BoardDefinition, BoardRoleBinding
from werewolf.knowledge.compiler import (
    CompiledKnowledgePackage,
    EffectiveRoleProfile,
)
from werewolf.knowledge.indexes import KnowledgeIndex
from werewolf.knowledge.preview import experimental_preview
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.role import (
    AbilityDefinition,
    Faction,
    ResourceDefinition,
    RoleDefinition,
    TargetKind,
    TargetRule,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
    TriggerType,
    UsageLimit,
)
from werewolf.knowledge.sections import MarkdownDocument

BOARD_REF = VersionedRef(id="test_board", version="1.0.0")


def _role(
    role_id: str,
    *,
    team: str,
    faction: Faction = Faction.GOOD,
    max_uses: int | None = None,
    trigger: TriggerRule | None = None,
    ability_id: str | None = None,
    reviewed_by: str = "human-reviewer",
) -> RoleDefinition:
    ability = None
    if max_uses is not None or trigger is not None:
        ability = AbilityDefinition.model_construct(
            ability_id=ability_id or f"{role_id}_ability",
            action_code=105,
            target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
            resource=ResourceDefinition(
                resource_id=f"{role_id}_resource",
                initial_amount=99,
                cost_per_use=1,
            ),
            usage_limit=UsageLimit(max_uses=max_uses),
            trigger=trigger,
        )
    return RoleDefinition.model_construct(
        role_id=role_id,
        name=role_id,
        version="1.0.0",
        status="published",
        reviewed_by=reviewed_by,
        reviewed_at=date(2026, 9, 29),
        faction=faction,
        team=team,
        abilities=[ability] if ability is not None else [],
        board_compatibility=[BOARD_REF],
    )


def _binding(role_id: str, count: int) -> BoardRoleBinding:
    return BoardRoleBinding(
        role_ref=VersionedRef(id=role_id, version="1.0.0"),
        count=count,
        effective_rules={},
        override_claim_refs=[],
    )


def _board(
    *,
    bindings: list[BoardRoleBinding] | None = None,
    factions: dict[str, int] | None = None,
    reviewed_by: str = "human-reviewer",
    status: str = "published",
) -> BoardDefinition:
    selected_bindings = bindings or [_binding("villager", 2), _binding("wolf", 2)]
    return BoardDefinition.model_construct(
        board_id=BOARD_REF.id,
        version=BOARD_REF.version,
        status=status,
        reviewed_by=reviewed_by,
        reviewed_at=date(2026, 9, 29),
        seat_count=sum(binding.count for binding in selected_bindings),
        factions=factions or {"good": 2, "wolf": 2},
        role_bindings=selected_bindings,
    )


def _package(board: BoardDefinition, roles: dict[str, RoleDefinition]) -> CompiledKnowledgePackage:
    profiles = {
        role_id: EffectiveRoleProfile(
            board_ref=BOARD_REF,
            role_ref=VersionedRef(id=role_id, version="1.0.0"),
            count=next(
                binding.count for binding in board.role_bindings if binding.role_ref.id == role_id
            ),
            base_role=role,
            effective_rules={},
            override_claim_refs=(),
            sections=MarkdownDocument(()),
        )
        for role_id, role in roles.items()
    }
    return CompiledKnowledgePackage(
        board_ref=BOARD_REF,
        documents=(),
        index=KnowledgeIndex.build(()),
        sections={},
        effective_roles=profiles,
        document_digests={},
        package_payload={},
        manifest_payload={},
        canonical_package_json="{}",
        canonical_manifest_json="{}",
        package_identity="package",
        manifest_sha256="manifest",
    )


def _valid_inputs() -> tuple[BoardDefinition, CompiledKnowledgePackage]:
    board = _board()
    package = _package(
        board,
        {
            "villager": _role("villager", team="good"),
            "wolf": _role("wolf", team="wolf", faction=Faction.WEREWOLF),
        },
    )
    return board, package


def _fingerprint(plan: PlayerAssignmentPlan) -> tuple[tuple[int, str, str, dict[str, int]], ...]:
    return tuple(
        (
            seat,
            player.role_id,
            player.faction_id,
            dict(player.skill_resources),
        )
        for seat, player in plan.players.items()
    )


def test_assignment_is_reproducible_and_uses_board_counts() -> None:
    board, package = _valid_inputs()

    first = build_role_assignment_plan(board, package, 42, [1, 2, 3, 4])
    second = build_role_assignment_plan(board, package, 42, [4, 3, 2, 1])

    assert _fingerprint(first) == _fingerprint(second)
    assert len(first.players) == 4
    assert sum(player.faction_id == "wolf" for player in first.players.values()) == 2
    assert sum(player.role_id == "villager" for player in first.players.values()) == 2
    assert all(player.alive for player in first.players.values())


def test_resources_require_explicit_max_uses_and_plan_has_no_events() -> None:
    board = _board(
        bindings=[_binding("seer", 1), _binding("wolf", 1)], factions={"good": 1, "wolf": 1}
    )
    package = _package(
        board,
        {
            "seer": _role("seer", team="good", max_uses=1),
            "wolf": _role("wolf", team="wolf", faction=Faction.WEREWOLF),
        },
    )

    plan = build_role_assignment_plan(board, package, 7, (1, 2))
    seer = next(player for player in plan.players.values() if player.role_id == "seer")

    assert seer.skill_resources == {"seer_resource": 1}
    assert all(
        player.skill_resources == {} for player in plan.players.values() if player.role_id == "wolf"
    )
    assert not hasattr(plan, "events")


def test_assignment_copies_active_ability_authorization_contract() -> None:
    board = _board(
        bindings=[_binding("seer", 1), _binding("wolf", 1)],
        factions={"good": 1, "wolf": 1},
    )
    active = AbilityDefinition.model_construct(
        ability_id="inspect",
        action_code=102,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=[GamePhase.NIGHT_ACTION],
        trigger_type=TriggerType.ACTIVE,
        target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
        usage_limit=UsageLimit(max_uses=2, uses_per_round=1),
        resource=ResourceDefinition(
            resource_id="inspect_charge",
            initial_amount=2,
            cost_per_use=1,
        ),
    )
    seer = _role("seer", team="good").model_copy(update={"abilities": [active]})
    package = _package(
        board,
        {"seer": seer, "wolf": _role("wolf", team="wolf", faction=Faction.WEREWOLF)},
    )

    plan = build_role_assignment_plan(board, package, 13, (1, 2))
    granted = next(
        player.granted_abilities[0] for player in plan.players.values() if player.role_id == "seer"
    )

    assert granted.ability_id == "inspect"
    assert granted.action_code == 102
    assert granted.timing is GamePhase.NIGHT_ACTION
    assert granted.allowed_phases == (GamePhase.NIGHT_ACTION,)
    assert granted.target_rule.kind is TargetKind.PLAYER
    assert granted.usage_limit == UsageLimit(max_uses=2, uses_per_round=1)
    assert granted.resource is not None
    assert granted.resource.resource_id == "inspect_charge"
    assert granted.uses_consumed == 0


def test_assignment_rejects_duplicate_ability_ids_across_grants() -> None:
    board = _board(
        bindings=[_binding("seer", 1), _binding("wolf", 1)],
        factions={"good": 1, "wolf": 1},
    )
    duplicate = AbilityDefinition.model_construct(
        ability_id="shared",
        action_code=102,
        timing=GamePhase.NIGHT_ACTION,
        allowed_phases=[GamePhase.NIGHT_ACTION],
        trigger_type=TriggerType.ACTIVE,
        target_rule=TargetRule(kind=TargetKind.PLAYER, min_targets=1, max_targets=1),
    )
    role = _role("seer", team="good").model_copy(update={"abilities": [duplicate, duplicate]})
    package = _package(
        board,
        {"seer": role, "wolf": _role("wolf", team="wolf", faction=Faction.WEREWOLF)},
    )

    with pytest.raises(RoleAssignmentError, match="duplicate ability IDs"):
        build_role_assignment_plan(board, package, 13, (1, 2))


def test_assignment_grants_trigger_contract_without_role_id_inference() -> None:
    board = _board(
        bindings=[_binding("first_role", 1), _binding("second_role", 1)],
        factions={"good": 2},
    )
    trigger = TriggerRule(
        event=TriggerEvent.DEATH_CONFIRMED,
        allowed_death_causes=["wolf_kill"],
        mode=TriggerMode.PLAYER_CHOICE,
        effects=[TriggerEffect.OPEN_PLAYER_ACTION],
        allow_pass=True,
        once=True,
    )
    package = _package(
        board,
        {
            "first_role": _role(
                "first_role", team="good", trigger=trigger, ability_id="shared_trigger"
            ),
            "second_role": _role(
                "second_role", team="good", trigger=trigger, ability_id="shared_trigger"
            ),
        },
    )

    plan = build_role_assignment_plan(board, package, 11, (1, 2))

    assert all(len(player.granted_trigger_abilities) == 1 for player in plan.players.values())
    granted = [player.granted_trigger_abilities[0] for player in plan.players.values()]
    assert {item.ability_id for item in granted} == {"shared_trigger"}
    assert all(item.action_code == 105 for item in granted)
    assert all(item.trigger.allow_pass and not item.consumed for item in granted)
    assert all(item.target_rule.kind is TargetKind.PLAYER for item in granted)


def test_assignment_omits_non_trigger_abilities_from_granted_state() -> None:
    board, package = _valid_inputs()

    plan = build_role_assignment_plan(board, package, 3, (1, 2, 3, 4))

    assert all(player.granted_trigger_abilities == () for player in plan.players.values())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda board: board.model_copy(update={"status": "draft"}), "board must be published"),
        (
            lambda board: board.model_copy(update={"reviewed_by": "pending-human-review"}),
            "board must have a human reviewer",
        ),
    ],
)
def test_unpublished_or_unreviewed_board_is_rejected(mutate, message: str) -> None:
    board, package = _valid_inputs()

    with pytest.raises(RoleAssignmentError, match=message):
        build_role_assignment_plan(mutate(board), package, 1, (1, 2, 3, 4))


def test_preview_scope_allows_pending_review_board_for_experimental_assignment() -> None:
    board, package = _valid_inputs()
    pending = board.model_copy(update={"reviewed_by": "pending-human-review"})

    with experimental_preview():
        plan = build_role_assignment_plan(pending, package, 1, (1, 2, 3, 4))

    assert plan.board_ref == pending.board_ref


def test_duplicate_seats_and_package_mismatch_are_rejected() -> None:
    board, package = _valid_inputs()

    with pytest.raises(RoleAssignmentError, match="seats must be unique"):
        build_role_assignment_plan(board, package, 1, (1, 1, 2, 3))

    wrong_package = replace(package, board_ref=VersionedRef(id="other_board", version="1.0.0"))
    with pytest.raises(RoleAssignmentError, match="does not match the board reference"):
        build_role_assignment_plan(board, wrong_package, 1, (1, 2, 3, 4))


def test_role_faction_and_board_faction_counts_are_verified() -> None:
    board = _board()
    package = _package(
        board,
        {
            "villager": _role("villager", team="missing-faction"),
            "wolf": _role("wolf", team="wolf", faction=Faction.WEREWOLF),
        },
    )

    with pytest.raises(RoleAssignmentError, match="is not a board faction"):
        build_role_assignment_plan(board, package, 1, (1, 2, 3, 4))
