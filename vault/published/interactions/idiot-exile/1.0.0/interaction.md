---
schema_version: 1
kind: interaction
id: idiot-exile
version: 1.0.0
name: 白痴投票放逐翻牌
status: published
reviewed_by: 项目所有者（本会话用户）
reviewed_at: '2026-10-03'
board_refs:
- classic_12_seer_witch_hunter_idiot@1.0.0
subjects:
- idiot
- day_vote
situation_key: idiot.exile_reveal
preconditions:
- subject: idiot
  field: role
  operator: EQ
  value: idiot
- subject: day_vote
  field: result
  operator: EQ
  value: EXILED
ordering:
- step_id: reveal_card
  order: 1
  action: reveal_idiot_card
- step_id: preserve_life
  order: 2
  action: cancel_exile_death
- step_id: remove_vote
  order: 3
  action: remove_vote_right
outcome:
  outcome_code: idiot_revealed
  status: APPLIED
  effects:
  - effect_id: reveal_idiot
    operation: SET
    subject: idiot
    field: status
    value: REVEALED_ALIVE
    visibility: PUBLIC
  - effect_id: remove_idiot_vote
    operation: SET
    subject: idiot
    field: can_vote
    value: false
    visibility: PUBLIC
notifications:
- notification_id: idiot_reveal_notice
  visibility: PUBLIC
  message_code: idiot_revealed
examples:
- example_id: exile_reveals_idiot
  given:
  - subject: day_vote
    field: result
    operator: EQ
    value: EXILED
  when:
  - action_id: reveal
    action: reveal_idiot_card
  then:
    outcome_code: idiot_revealed
    status: APPLIED
    effects:
    - effect_id: reveal_idiot
      operation: SET
      subject: idiot
      field: status
      value: REVEALED_ALIVE
      visibility: PUBLIC
claim_refs:
- claim-netease-idiot-exile
source_refs:
- source-netease-12-standard
---
# 白痴投票放逐翻牌

## 触发 {#trigger}

只有白痴被白天投票放逐时触发翻牌。狼人杀害、女巫毒杀及其他死亡原因不进入本交互。

## 结果 {#outcome}

白痴公开翻牌并继续存活和发言，但失去投票权。该交互产生的是 `REVEALED_ALIVE` 状态，不是普通死亡结果。
