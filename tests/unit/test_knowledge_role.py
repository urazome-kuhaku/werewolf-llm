"""Boundary tests for versioned role and ability knowledge models."""

from datetime import date

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import Channel, GamePhase
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.role import (
    AbilityDefinition,
    DeathBehavior,
    EffectDefinition,
    Faction,
    FailureRule,
    InputInformation,
    KnowledgeItem,
    ResourceDefinition,
    RoleDefinition,
    TargetKind,
    TargetRule,
    TeamVisibility,
    TriggerEffect,
    TriggerEvent,
    TriggerMode,
    TriggerRule,
    TriggerType,
    UsageLimit,
)


def _ability(*, ability_id: str = "echo_scan") -> dict[str, object]:
    return {
        "ability_id": ability_id,
        "name": "回声扫描",
        "action_code": 701,
        "timing": GamePhase.NIGHT_ACTION,
        "allowed_phases": [GamePhase.NIGHT_ACTION],
        "trigger_type": TriggerType.ACTIVE,
        "target_rule": {
            "kind": TargetKind.PLAYER,
            "min_targets": 1,
            "max_targets": 1,
            "allow_self": False,
            "allow_dead": False,
        },
        "usage_limit": {"max_uses": 2, "uses_per_round": 1},
        "resource": {
            "resource_id": "echo_charge",
            "initial_amount": 2,
            "cost_per_use": 1,
        },
        "input_information": [
            {
                "field_id": "target_player",
                "description": "要查询的玩家座位",
                "value_type": "player_id",
                "required": True,
            },
        ],
        "request_effect": {
            "effect_code": "scan_requested",
            "description": "记录一次扫描请求",
            "visibility": Channel.PRIVATE,
        },
        "resolution_effect": {
            "effect_code": "scan_resolved",
            "description": "向角色返回结构化扫描结果",
            "visibility": Channel.PRIVATE,
        },
        "result_visibility": [Channel.PRIVATE],
        "failure_rules": [
            {
                "failure_code": "target_not_eligible",
                "condition": "目标不满足当前窗口条件",
                "outcome": "请求被拒绝且不消耗次数",
                "visibility": Channel.PRIVATE,
            },
        ],
    }


def _role(**overrides: object) -> RoleDefinition:
    values: dict[str, object] = {
        "schema_version": 1,
        "kind": "role",
        "role_id": "echo_scout",
        "name": "回声侦察者",
        "aliases": ["回声者"],
        "version": "1.0.0",
        "status": "published",
        "reviewed_by": "GM",
        "reviewed_at": date(2026, 9, 27),
        "faction": Faction.GOOD,
        "team": "town",
        "victory_goal": "town_survival",
        "public_summary": "一个需要在夜间提交查询的虚构角色。",
        "private_identity_card": "你是回声侦察者，按规则窗口提交扫描请求。",
        "abilities": [_ability()],
        "knowledge_at_start": [
            {
                "knowledge_id": "own_role",
                "description": "知道自己的角色身份",
                "visibility": Channel.PRIVATE,
            },
        ],
        "team_visibility": {
            "channel": Channel.TEAM,
            "share_identity": False,
            "shared_knowledge": [],
        },
        "death_behavior": {
            "active_abilities_allowed": False,
            "passive_abilities_continue": False,
            "death_trigger_fires": True,
            "description": "死亡时按已记录的触发规则处理。",
        },
        "board_compatibility": [
            {"id": "fictional_board", "version": "1.0.0"},
        ],
        "common_mistakes": ["把请求提交误认为结算成功"],
        "claim_refs": ["claim-echo-001"],
        "source_refs": ["source-echo-handbook"],
    }
    values.update(overrides)
    if "applicable_boards" in overrides:
        values.pop("board_compatibility", None)
    return RoleDefinition.model_validate(values)


def test_role_definition_parses_typed_execution_contract() -> None:
    role = _role()

    assert role.role_id == "echo_scout"
    assert role.abilities[0].target_rule.max_targets == 1
    assert role.abilities[0].request_effect.visibility is Channel.PRIVATE
    assert role.applicable_boards[0] == VersionedRef(id="fictional_board", version="1.0.0")


def test_trigger_rule_parses_closed_machine_contract() -> None:
    trigger = TriggerRule.model_validate(
        {
            "event": "DEATH_CONFIRMED",
            "allowed_death_causes": ["wolf_kill", "exiled"],
            "mode": "PLAYER_CHOICE",
            "effects": ["OPEN_PLAYER_ACTION"],
            "allow_pass": True,
            "once": True,
        }
    )

    assert trigger.event is TriggerEvent.DEATH_CONFIRMED
    assert trigger.mode is TriggerMode.PLAYER_CHOICE
    assert trigger.effects == [TriggerEffect.OPEN_PLAYER_ACTION]
    assert trigger.allowed_death_causes == ["wolf_kill", "exiled"]


def test_trigger_rule_rejects_reusable_trigger_until_usage_counter_exists() -> None:
    with pytest.raises(ValidationError, match="once=false"):
        TriggerRule.model_validate(
            {
                "event": "DEATH_CONFIRMED",
                "allowed_death_causes": ["wolf_kill"],
                "mode": "PLAYER_CHOICE",
                "effects": ["OPEN_PLAYER_ACTION"],
                "once": False,
            }
        )


def test_trigger_ability_rejects_multi_use_limit_with_boolean_consumption() -> None:
    values = _ability()
    values.update(
        {
            "ability_id": "shoot",
            "trigger_type": TriggerType.DEATH_TRIGGER,
            "timing": GamePhase.TRIGGER_ACTION,
            "allowed_phases": [GamePhase.TRIGGER_ACTION],
            "usage_limit": {"max_uses": 2},
            "trigger": {
                "event": "DEATH_CONFIRMED",
                "allowed_death_causes": ["wolf_kill"],
                "mode": "PLAYER_CHOICE",
                "effects": ["OPEN_PLAYER_ACTION"],
                "once": True,
            },
        }
    )

    with pytest.raises(ValidationError, match="one total use"):
        AbilityDefinition.model_validate(values)


def test_death_trigger_ability_requires_and_preserves_trigger_rule() -> None:
    values = _ability()
    values.update(
        {
            "ability_id": "shoot",
            "trigger_type": TriggerType.DEATH_TRIGGER,
            "timing": GamePhase.TRIGGER_ACTION,
            "allowed_phases": [GamePhase.TRIGGER_ACTION],
            "usage_limit": {"max_uses": 1},
            "trigger": {
                "event": "DEATH_CONFIRMED",
                "allowed_death_causes": ["wolf_kill", "exiled"],
                "mode": "PLAYER_CHOICE",
                "effects": ["OPEN_PLAYER_ACTION"],
                "allow_pass": True,
                "once": True,
            },
        }
    )

    ability = AbilityDefinition.model_validate(values)

    assert ability.trigger is not None
    assert ability.trigger.once is True
    assert ability.trigger.allow_pass is True


def test_passive_automatic_trigger_uses_zero_action_and_no_target() -> None:
    values = _ability()
    values.update(
        {
            "ability_id": "reveal_on_exile",
            "action_code": 0,
            "timing": GamePhase.DAY_RESOLVE,
            "allowed_phases": [GamePhase.DAY_RESOLVE],
            "trigger_type": TriggerType.PASSIVE,
            "target_rule": {
                "kind": TargetKind.NONE,
                "min_targets": 0,
                "max_targets": 0,
            },
            "usage_limit": {"max_uses": 1},
            "trigger": {
                "event": "EXILE_SELECTED",
                "mode": "AUTOMATIC",
                "effects": ["REVEAL_ROLE", "SURVIVE_TRIGGER"],
                "allow_pass": False,
                "once": True,
            },
        }
    )

    ability = AbilityDefinition.model_validate(values)

    assert ability.trigger_type is TriggerType.PASSIVE
    assert ability.action_code == 0
    assert ability.trigger is not None
    assert ability.trigger.mode is TriggerMode.AUTOMATIC


@pytest.mark.parametrize(
    "mutator",
    [
        lambda values: values["abilities"][0].update(
            {
                "trigger_type": TriggerType.PASSIVE,
                "trigger": {
                    "event": "EXILE_SELECTED",
                    "mode": "PLAYER_CHOICE",
                    "effects": ["OPEN_PLAYER_ACTION"],
                },
            }
        ),
        lambda values: values["abilities"][0].update(
            {
                "trigger_type": TriggerType.PASSIVE,
                "action_code": 0,
                "target_rule": {"kind": "PLAYER", "min_targets": 1, "max_targets": 1},
                "trigger": {
                    "event": "EXILE_SELECTED",
                    "mode": "AUTOMATIC",
                    "effects": ["REVEAL_ROLE"],
                },
            }
        ),
        lambda values: values["abilities"][0].update(
            {
                "trigger_type": TriggerType.PASSIVE,
                "action_code": 1,
                "target_rule": {"kind": "NONE", "min_targets": 0, "max_targets": 0},
                "trigger": {
                    "event": "EXILE_SELECTED",
                    "mode": "AUTOMATIC",
                    "effects": ["REVEAL_ROLE"],
                },
            }
        ),
        lambda values: values["abilities"][0].update(
            {
                "trigger_type": TriggerType.DEATH_TRIGGER,
                "trigger": {
                    "event": "EXILE_SELECTED",
                    "mode": "PLAYER_CHOICE",
                    "effects": ["OPEN_PLAYER_ACTION"],
                },
            }
        ),
        lambda values: values["abilities"][0].update(
            {
                "trigger_type": TriggerType.DEATH_TRIGGER,
                "trigger": {
                    "event": "DEATH_CONFIRMED",
                    "allowed_death_causes": ["wolf_kill"],
                    "mode": "AUTOMATIC",
                    "effects": ["REVEAL_ROLE"],
                },
            }
        ),
    ],
)
def test_trigger_type_and_invocation_shape_cannot_be_mixed(
    mutator: object,
) -> None:
    values = _role().model_dump()
    assert callable(mutator)
    mutator(values)  # type: ignore[union-attr]

    with pytest.raises(ValidationError):
        RoleDefinition.model_validate(values)


def test_active_ability_cannot_declare_trigger_or_zero_action() -> None:
    values = _role().model_dump()
    values["abilities"][0].update({"action_code": 0})

    with pytest.raises(ValidationError):
        RoleDefinition.model_validate(values)

    values = _role().model_dump()
    values["abilities"][0].update(
        {
            "trigger": {
                "event": "DEATH_CONFIRMED",
                "allowed_death_causes": ["wolf_kill"],
                "mode": "PLAYER_CHOICE",
                "effects": ["OPEN_PLAYER_ACTION"],
            }
        }
    )

    with pytest.raises(ValidationError):
        RoleDefinition.model_validate(values)


@pytest.mark.parametrize(
    "trigger",
    [
        {
            "event": "DEATH_CONFIRMED",
            "mode": "PLAYER_CHOICE",
            "effects": ["OPEN_PLAYER_ACTION"],
        },
        {
            "event": "EXILE_SELECTED",
            "allowed_death_causes": ["exiled"],
            "mode": "AUTOMATIC",
            "effects": ["REVEAL_ROLE"],
        },
        {
            "event": "EXILE_SELECTED",
            "mode": "AUTOMATIC",
            "effects": ["OPEN_PLAYER_ACTION"],
        },
        {
            "event": "DEATH_CONFIRMED",
            "allowed_death_causes": ["wolf_kill"],
            "mode": "AUTOMATIC",
            "effects": ["REVEAL_ROLE"],
            "allow_pass": True,
        },
        {
            "event": "DEATH_CONFIRMED",
            "allowed_death_causes": ["wolf_kill"],
            "mode": "PLAYER_CHOICE",
            "effects": ["NOT_A_REAL_EFFECT"],
        },
    ],
)
def test_trigger_rule_rejects_ambiguous_or_unbounded_execution_fields(
    trigger: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TriggerRule.model_validate(trigger)


def test_role_definition_accepts_frontmatter_board_alias() -> None:
    role = _role(
        applicable_boards=["other_board@2.0.0"],
    )

    assert role.board_compatibility[0].format() == "other_board@2.0.0"


def test_role_definition_accepts_formal_frontmatter_shape() -> None:
    values = _role().model_dump()
    values["id"] = values.pop("role_id")
    values["faction"] = "good"
    values["applicable_boards"] = ["fictional_board@1.0.0"]
    values.pop("board_compatibility")

    role = RoleDefinition.model_validate(values)

    assert role.schema_version == 1
    assert role.kind == "role"
    assert role.role_id == "echo_scout"
    assert role.status == "published"
    assert role.reviewed_by == "GM"
    assert role.reviewed_at == date(2026, 9, 27)
    assert role.applicable_boards == [VersionedRef(id="fictional_board", version="1.0.0")]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("kind", "board"),
        ("status", "draft"),
        ("reviewed_by", "   "),
        ("reviewed_at", "2026-02-30"),
        ("reviewed_at", "2026-09-27T00:00:00"),
    ],
)
def test_role_definition_rejects_non_published_or_invalid_review_metadata(
    field: str,
    value: object,
) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _role(**{field: value})


@pytest.mark.parametrize(
    "field", ["schema_version", "kind", "status", "reviewed_by", "reviewed_at"]
)
def test_role_definition_requires_formal_frontmatter_metadata(field: str) -> None:
    values = _role().model_dump()
    values.pop(field)

    with pytest.raises(ValidationError):
        RoleDefinition.model_validate(values)


@pytest.mark.parametrize(
    "reference",
    [
        "latest@1.0.0",
        "fictional_board@latest",
        "../fictional_board@1.0.0",
        "fictional_board/child@1.0.0",
        "fictional_board@1.0",
        "fictional_board@1.0.0@extra",
    ],
)
def test_role_definition_rejects_unpinned_or_path_like_board_references(
    reference: str,
) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _role(applicable_boards=[reference])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role_id", "Echo Scout"),
        ("version", "1.0"),
        ("team", "town/team"),
        ("claim_refs", ["claim", "claim"]),
        ("source_refs", ["../source"]),
        ("abilities", [_ability(), _ability()]),
    ],
)
def test_role_definition_rejects_invalid_ids_versions_and_duplicate_abilities(
    field: str,
    value: object,
) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        _role(**{field: value})


def test_role_definition_rejects_extra_fields_at_every_boundary() -> None:
    values = _role().model_dump()
    values["unexpected"] = True
    with pytest.raises(ValidationError):
        RoleDefinition.model_validate(values)

    bad_ability = _ability()
    bad_ability["unexpected"] = True
    with pytest.raises(ValidationError):
        AbilityDefinition.model_validate(bad_ability)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda values: values["abilities"][0].update({"timing": GamePhase.DAY_SPEECH}),
        lambda values: values["abilities"][0].update({"allowed_phases": []}),
        lambda values: values["abilities"][0]["target_rule"].update(
            {"min_targets": 2, "max_targets": 1}
        ),
        lambda values: values["abilities"][0]["usage_limit"].update({"max_uses": -1}),
        lambda values: values["abilities"][0]["result_visibility"].append(Channel.PRIVATE),
    ],
)
def test_ability_contract_rejects_invalid_bounds_or_duplicates(mutator: object) -> None:
    values = _role().model_dump()
    assert callable(mutator)
    mutator(values)  # type: ignore[union-attr]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        RoleDefinition.model_validate(values)


def test_typed_submodels_reject_unbounded_machine_maps() -> None:
    with pytest.raises(ValidationError):
        ResourceDefinition(
            resource_id="charge",
            initial_amount=1,
            cost_per_use=1,
            extra_rules={"anything": True},  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        TeamVisibility(
            channel=Channel.TEAM,
            share_identity=False,
            shared_knowledge=[
                KnowledgeItem(
                    knowledge_id="fact",
                    description="一个事实",
                    visibility=Channel.TEAM,
                ),
                KnowledgeItem(
                    knowledge_id="fact",
                    description="重复事实",
                    visibility=Channel.TEAM,
                ),
            ],
        )


def test_imported_contract_types_are_real_models() -> None:
    assert isinstance(_ability()["target_rule"], dict)
    assert isinstance(TargetRule.model_validate({"kind": "NONE"}), TargetRule)
    assert TargetRule.model_validate({"kind": "NONE"}).max_targets == 0
    assert TargetRule(kind="NONE").max_targets == 0
    assert TargetRule(kind=TargetKind.NONE, max_targets=None).max_targets == 0
    assert TargetRule.model_validate(
        {"kind": "SELF", "min_targets": 1, "max_targets": 1, "allow_self": True}
    ).allow_self
    assert isinstance(
        InputInformation.model_validate(
            {
                "field_id": "choice",
                "description": "选择",
                "value_type": "player_id",
            }
        ),
        InputInformation,
    )
    assert isinstance(
        EffectDefinition.model_validate(
            {
                "effect_code": "requested",
                "description": "已请求",
                "visibility": "PRIVATE",
            }
        ),
        EffectDefinition,
    )
    assert isinstance(
        FailureRule.model_validate(
            {
                "failure_code": "invalid",
                "condition": "输入非法",
                "outcome": "拒绝",
                "visibility": "PRIVATE",
            }
        ),
        FailureRule,
    )
    assert isinstance(
        DeathBehavior.model_validate(
            {
                "active_abilities_allowed": False,
                "passive_abilities_continue": True,
                "death_trigger_fires": False,
                "description": "无额外行为",
            }
        ),
        DeathBehavior,
    )
    assert isinstance(UsageLimit.model_validate({"max_uses": 0}), UsageLimit)


@pytest.mark.parametrize(
    "rule",
    [
        {"kind": "SELF", "min_targets": 1, "max_targets": 1, "allow_self": False},
        {"kind": "SELF", "min_targets": 0, "max_targets": 1, "allow_self": True},
        {"kind": "SELF", "min_targets": 1, "max_targets": 2, "allow_self": True},
        {"kind": "NONE", "min_targets": 1, "max_targets": 1},
        {"kind": "NONE", "min_targets": 0, "max_targets": 1},
    ],
)
def test_target_rule_rejects_contradictory_target_shapes(rule: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TargetRule.model_validate(rule)
