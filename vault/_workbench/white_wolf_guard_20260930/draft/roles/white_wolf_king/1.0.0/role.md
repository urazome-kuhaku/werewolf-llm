---
schema_version: 1
kind: role
id: white_wolf_king
name: 白狼王
aliases:
  - 白狼王
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
faction: WEREWOLF
team: wolf
victory_goal: eliminate_good
public_summary: 狼人阵营的白天主动技能角色，可以在主持人开放的窗口自爆并带走目标。
private_identity_card: 你是白狼王。你知道同队狼人；白天技能窗口开放时，可以选择发动自爆并提交有序的发动者与被带走目标。
abilities:
  - ability_id: self_explode_take
    name: 自爆带人
    action_code: 107
    timing: DAY_SPEECH
    allowed_phases:
      - DAY_SPEECH
    trigger_type: ACTIVE
    target_rule:
      kind: PLAYERS
      min_targets: 2
      max_targets: 2
      allow_self: true
      allow_dead: false
    input_information:
      - field_id: ordered_actor_victim
        description: 有序目标对；第一个位置必须是发动者自身，第二个位置是被带走目标。
        value_type: ordered_player_pair
        required: true
    request_effect:
      effect_code: white_wolf_king_self_explode_request
      description: 白狼王提交自爆并选择一名仍存活的其他玩家作为被带走目标。
      visibility: PRIVATE
    resolution_effect:
      effect_code: white_wolf_king_self_explode_result
      description: 由主持人确认合法性后，白狼王与目标进入白天死亡结算。
      visibility: PUBLIC
    result_visibility:
      - PUBLIC
      - PRIVATE
    failure_rules:
      - failure_code: wrong_actor_order
        condition: 有序目标对的第一项不是发动白狼王自身。
        outcome: 拒绝自爆带人请求。
        visibility: PRIVATE
      - failure_code: invalid_target
        condition: 第二项不是仍存活的其他玩家，或窗口未开放。
        outcome: 拒绝自爆带人请求。
        visibility: PRIVATE
knowledge_at_start: []
team_visibility:
  channel: TEAM
  share_identity: true
  shared_knowledge: []
death_behavior:
  active_abilities_allowed: false
  passive_abilities_continue: false
  death_trigger_fires: false
  description: 白狼王死亡后不能继续发动白天主动技能。
board_compatibility:
  - classic_12_white_wolf_guard@1.0.0
common_mistakes:
  - 白狼王自爆是白天主动技能，不是猎人式死亡触发技能。
  - 自爆目标窗口、目标死亡技能和无视守卫/解药的交互仍待主持版本确认。
claim_refs:
  - claim-wwg-self-explode
source_refs:
  - source-white-wolf-king-guide
  - source-guard-interaction
---
# 白狼王

白狼王属于狼人阵营，与普通狼人共享队伍身份信息。主持人开放白天技能窗口后，白狼王可以选择提交有序的 `[发动者, 被带走目标]` 目标对并自爆。

该技能使用 `SELF_SACRIFICE_TAKE` action code，属于主动技能，不复用猎人的死亡触发分支。目标资格、发动后是否触发目标的死亡技能，以及是否无视守卫或女巫效果，保持人工待决。
