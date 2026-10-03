---
schema_version: 1
kind: interaction
id: hunter-death-trigger
version: 1.0.0
name: 猎人死亡触发
status: published
reviewed_by: 项目所有者（本会话用户）
reviewed_at: '2026-10-03'
board_refs:
- classic_12_seer_witch_hunter_idiot@1.0.0
subjects:
- hunter
- death_event
situation_key: hunter.death_trigger
preconditions:
- subject: hunter
  field: death_cause
  operator: IN
  value:
  - wolf_kill
  - exiled
ordering:
- step_id: check_trigger
  order: 1
  action: check_hunter_trigger
- step_id: open_shot
  order: 2
  action: open_hunter_shot_window
outcome:
  outcome_code: shot_window_opened
  status: APPLIED
  effects:
  - effect_id: hunter_can_shoot
    operation: SET
    subject: hunter
    field: trigger.can_shoot
    value: true
    visibility: PRIVATE
notifications:
- notification_id: hunter_shot_window
  visibility: PRIVATE
  message_code: hunter_shot_window_opened
  audience: hunter
examples:
- example_id: wolf_kill_opens_window
  given:
  - subject: hunter
    field: death_cause
    operator: EQ
    value: wolf_kill
  when:
  - action_id: trigger
    action: open_hunter_shot_window
  then:
    outcome_code: shot_window_opened
    status: APPLIED
    effects:
    - effect_id: hunter_can_shoot
      operation: SET
      subject: hunter
      field: trigger.can_shoot
      value: true
      visibility: PRIVATE
claim_refs:
- claim-netease-hunter-trigger
source_refs:
- source-netease-12-standard
---
# 猎人死亡触发

## 允许的死亡原因 {#allowed_causes}

猎人被狼人杀害或被投票放逐时打开一次开枪窗口。触发条件由死亡事件的结构化原因决定。

## 禁止的死亡原因 {#blocked_causes}

女巫毒杀不属于本板子的开枪触发原因，因此 `witch-poison-hunter` 交互会关闭开枪资格。
