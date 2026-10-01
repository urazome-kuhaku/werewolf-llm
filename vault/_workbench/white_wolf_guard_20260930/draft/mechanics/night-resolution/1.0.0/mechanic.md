---
schema_version: 1
kind: mechanic
id: night-resolution
version: 1.0.0
name: 夜间行动与统一结算
aliases:
  - 夜间结算
summary: 收集狼人队伍、守卫及其他角色行动，并在统一夜间结算阶段处理结果。
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
board_refs:
  - classic_12_white_wolf_guard@1.0.0
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
    description: 当前夜间窗口提交的角色行动或 PASS。
    required: true
    source: runtime
outputs:
  - output_id: night_result
    value_type: night_resolution
    description: 夜间统一结算结果。
    visibility: PUBLIC
  - output_id: private_role_result
    value_type: private_resolution
    description: 发送给有权获得该结果的角色信息。
    visibility: PRIVATE
processing_order:
  - step_id: collect_night_actions
    order: 1
    action: collect_night_actions
  - step_id: validate_night_actions
    order: 2
    action: validate_night_actions
  - step_id: resolve_night_actions
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
  - claim-guard-night-protect
  - claim-guard-no-repeat
source_refs:
  - source-guard-strategy
  - source-guard-interaction
---
# 夜间行动与统一结算

夜间依次经过狼人队伍讨论、角色行动和统一结算。守卫保护提交为夜间主动行动；守卫和女巫同时效果的具体结算顺序保留待决。
