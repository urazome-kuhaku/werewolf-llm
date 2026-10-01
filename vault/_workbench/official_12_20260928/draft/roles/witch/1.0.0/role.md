---
schema_version: 1
kind: role
id: witch
name: 女巫
aliases:
  - 药师
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-28
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的药剂角色，拥有一瓶解药和一瓶毒药。
private_identity_card: 你是女巫。你拥有一瓶解药和一瓶毒药；当前板子每晚最多使用一瓶药，且不能用解药自救。
abilities:
  - ability_id: heal
    name: 解药
    action_code: 104
    timing: NIGHT_ACTION
    allowed_phases:
      - NIGHT_ACTION
    trigger_type: ACTIVE
    target_rule:
      kind: PLAYER
      min_targets: 1
      max_targets: 1
      allow_self: false
      allow_dead: false
    usage_limit:
      max_uses: 1
    resource:
      resource_id: witch_heal
      initial_amount: 1
      cost_per_use: 1
    input_information: []
    request_effect:
      effect_code: witch_heal_request
      description: 选择当夜允许救治的目标。
      visibility: PRIVATE
    resolution_effect:
      effect_code: witch_heal_result
      description: 消耗解药并在夜间结算中救治目标。
      visibility: PRIVATE
    result_visibility:
      - PRIVATE
    failure_rules:
      - failure_code: potion_spent
        condition: 解药已使用。
        outcome: 拒绝解药请求。
        visibility: PRIVATE
      - failure_code: self_heal_forbidden
        condition: 目标是女巫自己。
        outcome: 本板子不允许女巫自救。
        visibility: PRIVATE
  - ability_id: poison
    name: 毒药
    action_code: 103
    timing: NIGHT_ACTION
    allowed_phases:
      - NIGHT_ACTION
    trigger_type: ACTIVE
    target_rule:
      kind: PLAYER
      min_targets: 1
      max_targets: 1
      allow_self: false
      allow_dead: false
    usage_limit:
      max_uses: 1
    resource:
      resource_id: witch_poison
      initial_amount: 1
      cost_per_use: 1
    input_information: []
    request_effect:
      effect_code: witch_poison_request
      description: 选择一名仍存活的其他玩家作为毒杀目标。
      visibility: PRIVATE
    resolution_effect:
      effect_code: witch_poison_result
      description: 消耗毒药并在夜间结算中毒杀目标。
      visibility: PRIVATE
    result_visibility:
      - PRIVATE
    failure_rules:
      - failure_code: potion_spent
        condition: 毒药已使用。
        outcome: 拒绝毒药请求。
        visibility: PRIVATE
knowledge_at_start: []
team_visibility:
  channel: PRIVATE
  share_identity: false
  shared_knowledge: []
death_behavior:
  active_abilities_allowed: false
  passive_abilities_continue: false
  death_trigger_fires: false
  description: 死亡后不能继续使用药剂。
board_compatibility:
  - classic_12_seer_witch_hunter_idiot@1.0.0
common_mistakes:
  - 本板子每晚最多使用一瓶药，解药和毒药不能在同一夜同时提交。
  - 本板子不允许女巫用解药自救。
  - 网易主来源未明确女巫获知刀口的时序；本版本仅按低优先级补充来源记录“解药未用时由法官告知刀口”。
claim_refs:
  - claim-netease-witch-no-self-heal
  - claim-netease-witch-one-potion-per-night
  - claim-community-witch-target-information
source_refs:
  - source-netease-12-standard
  - source-langrensha-community
---
# 女巫

## 阵营与资源 {#faction}

女巫属于好人阵营，起始拥有一瓶解药和一瓶毒药。两种药剂分别是一次性资源。

## 本板子用药限制 {#potion_rules}

本板子每晚最多使用一瓶药；解药和毒药不能在同一夜同时提交。女巫不能使用解药救自己。药剂只有在夜间统一结算确认后才消耗并产生结果。

## 未决信息 {#uncertain_information}

网易官方规则页没有明确女巫何时、以何种方式获知狼人的刀口。当前版本采用领域圈转载规则页的补充口径：解药尚未使用时由法官告知刀口。该字段的来源层级低于网易官方，后续更高质量证据可以覆盖它。
