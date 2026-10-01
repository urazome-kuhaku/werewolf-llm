---
schema_version: 1
kind: mechanic
id: day-vote
version: 1.0.0
name: 白天放逐投票
aliases:
  - 放逐投票
summary: 白天发言后由仍有投票权的存活玩家按法官指令同时举票，并在日间结算阶段处理放逐结果。
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-28
board_refs:
  - classic_12_seer_witch_hunter_idiot@1.0.0
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
    description: 票型结算后的放逐结果。
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
    description: 平票进入 PK；PK 再次平票时为平安日。该细节来自补充来源，不是网易主来源明文。
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
  - claim-netease-day-vote
  - claim-community-day-vote-pk
source_refs:
  - source-netease-12-standard
  - source-langrensha-community
---
# 白天放逐投票

## 投票资格 {#eligibility}

白天发言后按法官指令同时举票，允许弃票。仍存活且拥有投票权的玩家可以投票；白痴翻牌后继续发言但失去投票权。

## 公开与结算 {#resolution}

同时举票结束后公开选票和票数汇总，并在 `DAY_RESOLVE` 结算放逐。白痴的投票放逐进入 `idiot-exile` 交互。

## 平票待决 {#tie}

`pk_then_no_exile_on_retie` 来自补充规则页：平票进入 PK，PK 再次平票为平安日。该条款低于网易主来源，且警长票型冲突时仍以网易 1.5 票为准。
