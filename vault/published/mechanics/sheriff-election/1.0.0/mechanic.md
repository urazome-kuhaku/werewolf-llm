---
schema_version: 1
kind: mechanic
id: sheriff-election
version: 1.0.0
name: 首日警长竞选
aliases:
- 警长竞选
summary: 首日由玩家竞选警长；首次平票进入PK，PK再次平票时本局不产生警长；警长拥有本板子规定的发言顺序和投票权重。
status: published
reviewed_by: 项目所有者（本会话用户）
reviewed_at: '2026-10-03'
board_refs:
- classic_12_seer_witch_hunter_idiot@1.0.0
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
  description: 竞选玩家或警长选举投票的目标。
  required: true
  source: player
outputs:
- output_id: sheriff_result
  value_type: sheriff_result
  description: 警长竞选的公开结果。
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
  parameters:
    tie_policy: pk_then_no_sheriff_on_retie
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
- example_id: sheriff_tie_enters_pk
  given:
  - subject: election
    field: tie_round
    operator: EQ
    value: FIRST
  when:
  - action_id: resolve_tie
    action: resolve_sheriff_tie
  then:
    outcome_code: sheriff_pk_required
    status: APPLIED
    effects:
    - effect_id: enter_sheriff_pk
      operation: SET
      subject: game
      field: sheriff_result
      value: pk_required
      visibility: PUBLIC
- example_id: sheriff_retie_no_office
  given:
  - subject: election
    field: tie_round
    operator: EQ
    value: SECOND
  when:
  - action_id: resolve_tie
    action: resolve_sheriff_tie
  then:
    outcome_code: sheriff_badge_lost
    status: APPLIED
    effects:
    - effect_id: remove_sheriff_office
      operation: CLEAR
      subject: game
      field: sheriff
      visibility: PUBLIC
claim_refs:
- claim-netease-sheriff
- claim-netease-sheriff-vote-weight
- claim-netease-sheriff-final-speech
- claim-community-sheriff-election-tie
source_refs:
- source-netease-12-standard
- source-langrensha-community
---
# 首日警长竞选

## 竞选时机 {#timing}

本板子首日进行警长竞选。竞选发言和投票使用独立阶段，选举结果在公开通知后进入日间流程。

## 平票处理 {#tie}

首轮警长竞选出现两名或以上候选人平票时，平票候选人进入 PK 发言和 PK 投票；首轮未参与 PK 的合资格玩家按本板子主持流程参与 PK 投票。PK 后得票最高者当选。PK 阶段再次平票时警徽流失，本局不产生警长。

该平票路径由可复算的领域圈社区 HTML 补充支持，低于网易主来源，不能写成网易官方明文；该页面同时给出2票面杀口径，与网易主来源的1.5票冲突，本板子只采用网易票型。

## 警长权限 {#office}

警长按本板子字段拥有 1.5 票投票权重，并在白天发言顺序中最后发言。警长死亡或退选时的警徽处理继续引用补充来源字段；该字段与竞选平票规则分开记录。
