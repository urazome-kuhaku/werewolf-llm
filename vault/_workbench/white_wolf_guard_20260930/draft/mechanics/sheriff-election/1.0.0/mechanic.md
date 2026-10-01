---
schema_version: 1
kind: mechanic
id: sheriff-election
version: 1.0.0
name: 首日警长竞选
aliases:
  - 警长竞选
summary: 首日由玩家竞选警长；本候选保留1.5票和最后发言字段，平票细节待主持版本确认。
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
board_refs:
  - classic_12_white_wolf_guard@1.0.0
applicable_phases:
  - SHERIFF_ELECTION_SPEECH
  - SHERIFF_ELECTION
  - DAY_SPEECH
participation:
  - participant_id: sheriff_candidate
    participant_kind: player
    required: false
    conditions: []
  - participant_id: sheriff_voter
    participant_kind: player
    required: true
    conditions: []
inputs:
  - input_id: candidate_id
    value_type: player_id
    description: 竞选玩家或选举投票目标。
    required: true
    source: player
outputs:
  - output_id: sheriff_result
    value_type: sheriff_result
    description: 警长竞选公开结果。
    visibility: PUBLIC
processing_order:
  - step_id: collect_campaign_speeches
    order: 1
    action: collect_sheriff_speeches
  - step_id: resolve_sheriff_vote
    order: 2
    action: resolve_sheriff_vote
  - step_id: resolve_sheriff_tie
    order: 3
    action: resolve_sheriff_tie
exception_branches: []
result_visibility:
  - notification_id: sheriff_announced
    visibility: PUBLIC
    message_code: announce_sheriff
examples:
  - example_id: sheriff_elected
    given:
      - subject: game
        field: phase
        operator: EQ
        value: SHERIFF_ELECTION
    when:
      - action_id: elect
        action: resolve_sheriff_vote
    then:
      outcome_code: sheriff_elected
      status: APPLIED
      effects:
        - effect_id: assign_sheriff
          operation: SET
          subject: candidate
          field: office
          value: SHERIFF
          visibility: PUBLIC
claim_refs:
  - claim-wwg-board-composition
source_refs:
  - source-wpl-2019-board
---
# 首日警长竞选

首日警长竞选作为独立主持阶段保留。警长票型、平票和警徽传递不由白狼王守卫来源摘要冻结，当前字段服务于联调契约，正式发布前仍需审核。
