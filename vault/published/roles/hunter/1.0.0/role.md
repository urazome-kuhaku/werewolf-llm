---
schema_version: 1
kind: role
id: hunter
name: 猎人
aliases:
- 枪手
version: 1.0.0
status: published
reviewed_by: 项目所有者（本会话用户）
reviewed_at: '2026-10-03'
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的触发角色，在被狼人杀害或被投票放逐时可以开枪带走一名玩家。
private_identity_card: 你是猎人。被狼人杀害或被投票放逐时进入开枪窗口；被女巫毒杀时不能开枪。
abilities:
- ability_id: shoot
  name: 开枪
  action_code: 105
  timing: TRIGGER_ACTION
  allowed_phases:
  - TRIGGER_ACTION
  trigger_type: DEATH_TRIGGER
  target_rule:
    kind: PLAYER
    min_targets: 1
    max_targets: 1
    allow_self: false
    allow_dead: false
  usage_limit:
    max_uses: 1
  input_information: []
  request_effect:
    effect_code: hunter_shoot_request
    description: 选择一名仍存活的其他玩家作为开枪目标。
    visibility: PRIVATE
  resolution_effect:
    effect_code: hunter_shoot_result
    description: 在允许的死亡触发窗口带走目标。
    visibility: PUBLIC
  result_visibility:
  - PUBLIC
  - PRIVATE
  failure_rules:
  - failure_code: poison_forbidden
    condition: 猎人的死亡原因为女巫毒杀。
    outcome: 不开启开枪窗口。
    visibility: PRIVATE
  - failure_code: trigger_not_allowed
    condition: 死亡原因不属于狼人杀害或投票放逐。
    outcome: 不开启开枪窗口。
    visibility: PRIVATE
  trigger:
    event: DEATH_CONFIRMED
    allowed_death_causes:
    - wolf_kill
    - exiled
    mode: PLAYER_CHOICE
    effects:
    - OPEN_PLAYER_ACTION
    allow_pass: true
    once: true
knowledge_at_start: []
team_visibility:
  channel: PRIVATE
  share_identity: false
  shared_knowledge: []
death_behavior:
  active_abilities_allowed: false
  passive_abilities_continue: false
  death_trigger_fires: true
  description: 只有允许的死亡原因会触发一次开枪窗口。
board_compatibility:
- classic_12_seer_witch_hunter_idiot@1.0.0
common_mistakes:
- 被女巫毒杀不能开枪。
- 被狼人杀害或被投票放逐才属于本板子的开枪触发原因。
claim_refs:
- claim-netease-hunter-trigger
- claim-netease-hunter-poison-block
source_refs:
- source-netease-12-standard
---
# 猎人

## 阵营与目标 {#faction}

猎人属于好人阵营，目标是帮助好人消灭全部狼人。

## 开枪触发 {#trigger}

猎人被狼人杀害或被投票放逐时，进入一次开枪窗口，可以选择一名仍存活的其他玩家。猎人被女巫毒杀时不能开枪；其他死亡原因也不会自动获得开枪资格。
