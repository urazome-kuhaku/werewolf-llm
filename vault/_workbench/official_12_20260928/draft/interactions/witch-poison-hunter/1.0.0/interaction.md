---
schema_version: 1
kind: interaction
id: witch-poison-hunter
version: 1.0.0
name: 女巫毒杀猎人
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-28
board_refs:
  - classic_12_seer_witch_hunter_idiot@1.0.0
subjects:
  - witch_poison
  - hunter
situation_key: witch.poison_hunter
preconditions:
  - subject: death_event
    field: cause
    operator: EQ
    value: witch_poison
  - subject: death_event
    field: target_role
    operator: EQ
    value: hunter
ordering:
  - step_id: mark_poison_death
    order: 1
    action: resolve_poison_death
  - step_id: close_shot
    order: 2
    action: close_hunter_shot_window
outcome:
  outcome_code: hunter_poisoned
  status: APPLIED
  effects:
    - effect_id: disable_hunter_shot
      operation: SET
      subject: hunter
      field: trigger.can_shoot
      value: false
      visibility: PRIVATE
notifications:
  - notification_id: hunter_poisoned_notice
    visibility: PRIVATE
    message_code: hunter_cannot_shoot_after_poison
    audience: hunter
examples:
  - example_id: poisoned_hunter_cannot_shoot
    given:
      - subject: death_event
        field: cause
        operator: EQ
        value: witch_poison
    when:
      - action_id: close_shot
        action: close_hunter_shot_window
    then:
      outcome_code: hunter_poisoned
      status: APPLIED
      effects:
        - effect_id: disable_hunter_shot
          operation: SET
          subject: hunter
          field: trigger.can_shoot
          value: false
          visibility: PRIVATE
claim_refs:
  - claim-netease-hunter-poison-block
source_refs:
  - source-netease-12-standard
---
# 女巫毒杀猎人

## 结算 {#resolution}

当死亡事件的结构化原因是女巫毒杀且目标角色为猎人时，猎人直接按毒杀死亡处理。

## 开枪资格 {#trigger}

本交互关闭猎人的开枪触发窗口。猎人被毒杀不能开枪；该限制与狼人杀害或投票放逐的触发条件相互独立。
