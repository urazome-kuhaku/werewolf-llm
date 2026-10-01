---
schema_version: 1
kind: role
id: idiot
name: 白痴
aliases:
  - 白痴牌
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-28
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的翻牌角色，被投票放逐时翻牌存活但失去投票权。
private_identity_card: 你是白痴。若被投票放逐，你翻牌并继续存活和发言，但之后失去投票权；因其他原因死亡时正常死亡。
abilities:
  - ability_id: reveal_on_exile
    name: 放逐翻牌
    action_code: 0
    timing: DAY_RESOLVE
    allowed_phases:
      - DAY_RESOLVE
    trigger_type: PASSIVE
    target_rule:
      kind: NONE
      min_targets: 0
      max_targets: 0
      allow_self: false
      allow_dead: false
    usage_limit:
      max_uses: 1
    input_information: []
    request_effect:
      effect_code: idiot_exile_trigger
      description: 记录白痴被选为放逐对象的触发事件。
      visibility: GM_ONLY
    resolution_effect:
      effect_code: idiot_reveal
      description: 翻牌并保留存活和发言资格，同时失去投票权。
      visibility: PUBLIC
    result_visibility:
      - PUBLIC
    failure_rules: []
    trigger:
      event: EXILE_SELECTED
      mode: AUTOMATIC
      effects:
        - REVEAL_ROLE
        - SURVIVE_TRIGGER
        - REMOVE_VOTE_RIGHT
        - RETAIN_SPEECH
      allow_pass: false
      once: true
knowledge_at_start: []
team_visibility:
  channel: PRIVATE
  share_identity: false
  shared_knowledge: []
death_behavior:
  active_abilities_allowed: false
  passive_abilities_continue: false
  death_trigger_fires: false
  description: 白痴因投票放逐以外的死亡原因正常死亡；翻牌存活不等同于死亡。
board_compatibility:
  - classic_12_seer_witch_hunter_idiot@1.0.0
common_mistakes:
  - 白痴只有被投票放逐时触发翻牌存活。
  - 翻牌后仍可发言但不能投票。
  - 被狼人杀害或被女巫毒杀等其他原因命中时正常死亡。
claim_refs:
  - claim-netease-idiot-exile
source_refs:
  - source-netease-12-standard
---
# 白痴

## 阵营与触发 {#faction}

白痴属于好人阵营。仅当白痴被白天投票放逐时，才触发翻牌分支。

## 翻牌后的状态 {#reveal}

白痴翻牌后继续存活并可以发言，但失去投票权。被狼人杀害、被女巫毒杀或以其他方式死亡时不会触发该分支，直接按死亡处理。
