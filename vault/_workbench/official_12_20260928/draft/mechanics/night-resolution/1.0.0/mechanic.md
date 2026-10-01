---
schema_version: 1
kind: mechanic
id: night-resolution
version: 1.0.0
name: 夜间行动与统一结算
aliases:
  - 夜间结算
summary: 收集狼人队伍和神职行动，并在统一夜间结算阶段确认刀口、药剂和查验结果。
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-28
board_refs:
  - classic_12_seer_witch_hunter_idiot@1.0.0
applicable_phases:
  - NIGHT_TEAM_CHAT
  - NIGHT_ACTION
  - NIGHT_RESOLVE
participation:
  - participant_id: night_actor
    participant_kind: role
    required: true
    conditions: []
inputs:
  - input_id: action_bundle
    value_type: action_bundle
    description: 当前夜间窗口提交的单项行动或 PASS。
    required: true
    source: runtime
outputs:
  - output_id: night_result
    value_type: night_resolution
    description: 夜间统一结算后的行动结果。
    visibility: PUBLIC
  - output_id: private_role_result
    value_type: private_resolution
    description: 只发送给有权获得该结果的角色信息。
    visibility: PRIVATE
processing_order:
  - step_id: collect_team_action
    order: 1
    action: collect_night_actions
  - step_id: validate_actions
    order: 2
    action: validate_night_actions
  - step_id: resolve_night
    order: 3
    action: resolve_night_actions
exception_branches: []
result_visibility:
  - notification_id: night_death_announcement
    visibility: PUBLIC
    message_code: announce_night_deaths
  - notification_id: private_action_result
    visibility: PRIVATE
    message_code: deliver_private_night_result
examples:
  - example_id: night_action_resolves
    given:
      - subject: game
        field: phase
        operator: EQ
        value: NIGHT_ACTION
    when:
      - action_id: submit_action
        action: submit_night_action
    then:
      outcome_code: resolved
      status: APPLIED
      effects:
        - effect_id: mark_night_resolved
          operation: SET
          subject: game
          field: night.resolved
          value: true
          visibility: GM_ONLY
claim_refs:
  - claim-netease-witch-one-potion-per-night
source_refs:
  - source-netease-12-standard
---
# 夜间行动与统一结算

## 参与者与窗口 {#participants}

狼人队伍先在夜间队伍频道讨论并形成刀口，预言家和女巫在夜间行动窗口提交各自行动。行动窗口由板子快照按顺序打开，未提交时也必须提交显式 PASS。

## 结算顺序 {#resolution}

服务器先收集行动，再统一验证并在 `NIGHT_RESOLVE` 确认结果。请求阶段不直接写入生死状态；主持人确认后才广播允许公开的死亡信息，并向角色发送其有权获得的私有结果。

## 未决信息 {#uncertain_information}

主来源没有明确女巫获知刀口的时序。本机制只保证行动与结算边界，不把刀口信息自动加入女巫输入。
