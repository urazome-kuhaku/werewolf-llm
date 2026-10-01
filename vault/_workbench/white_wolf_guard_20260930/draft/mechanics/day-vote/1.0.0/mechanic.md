---
schema_version: 1
kind: mechanic
id: day-vote
version: 1.0.0
name: 白天放逐投票
aliases:
  - 放逐投票
summary: 白天发言和主动技能窗口结束后收集放逐票，并在日间结算阶段处理结果。
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
board_refs:
  - classic_12_white_wolf_guard@1.0.0
applicable_phases:
  - DAY_SPEECH
  - VOTE
  - DAY_RESOLVE
participation:
  - participant_id: voter
    participant_kind: player
    required: true
    conditions: []
inputs:
  - input_id: ballot_target
    value_type: player_id
    description: 投票者选择的一名放逐候选人。
    required: false
    source: player
    allowed_values:
      - ABSTAIN
outputs:
  - output_id: exile_result
    value_type: exile_result
    description: 放逐票结算结果。
    visibility: PUBLIC
  - output_id: vote_totals
    value_type: vote_totals
    description: 关闭投票后公开的票数汇总。
    visibility: PUBLIC
processing_order:
  - step_id: collect_ballots
    order: 1
    action: collect_simultaneous_ballots
  - step_id: close_ballot
    order: 2
    action: close_ballot
  - step_id: resolve_exile
    order: 3
    action: resolve_exile_vote
exception_branches:
  - branch_id: tied_vote
    conditions:
      - subject: vote
        field: tied
        operator: IS_TRUE
    outcome_code: no_exile
    processing_order: []
    output_ids:
      - exile_result
    visibility: PUBLIC
    description: 当前候选沿用 PK 后再次平票不放逐的运行时契约，正式主持规则待审核。
result_visibility:
  - notification_id: public_vote_result
    visibility: PUBLIC
    message_code: announce_vote_totals
examples:
  - example_id: unique_vote_exiles
    given:
      - subject: vote
        field: tied
        operator: IS_FALSE
    when:
      - action_id: resolve
        action: resolve_exile_vote
    then:
      outcome_code: exiled
      status: APPLIED
      effects:
        - effect_id: mark_exiled
          operation: SET
          subject: candidate
          field: status
          value: EXILED
          visibility: PUBLIC
claim_refs:
  - claim-wwg-board-composition
source_refs:
  - source-wpl-2019-board
---
# 白天放逐投票

白天主动技能窗口结束后进入放逐投票。白狼王自爆属于发言阶段的主动窗口，发动后由主持人结束当前白天流程并进行死亡结算；平票细节仍需主持版本确认。
