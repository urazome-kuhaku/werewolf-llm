---
schema_version: 1
kind: role
id: seer
name: 预言家
aliases:
- 预言
version: 1.0.0
status: published
reviewed_by: 项目所有者（本会话用户）
reviewed_at: '2026-10-03'
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的查验角色，每晚查验一名玩家的阵营信息。
private_identity_card: 你是预言家。每个夜间行动窗口可查验一名其他玩家，并获得该次查验结果。
abilities:
- ability_id: inspect
  name: 查验
  action_code: 102
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
  input_information: []
  request_effect:
    effect_code: seer_check_request
    description: 提交一名仍存活的其他玩家作为查验目标。
    visibility: PRIVATE
  resolution_effect:
    effect_code: seer_check_result
    description: 返回目标的阵营查验结果。
    visibility: PRIVATE
  result_visibility:
  - PRIVATE
  failure_rules:
  - failure_code: invalid_target
    condition: 目标不是仍存活的其他玩家。
    outcome: 拒绝本次查验请求。
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
  description: 死亡后不能继续查验。
board_compatibility:
- classic_12_seer_witch_hunter_idiot@1.0.0
common_mistakes:
- 查验结果只发送给预言家，不能自动公开给全场。
claim_refs:
- claim-netease-board-composition
source_refs:
- source-netease-12-standard
---
# 预言家

## 阵营与目标 {#faction}

预言家属于好人阵营，目标是协助好人找出全部狼人。

## 查验 {#ability}

在夜间行动窗口，预言家选择一名其他存活玩家进行查验。查验结果属于预言家私有信息，公开与否由玩家在白天发言时自行决定。
