---
schema_version: 1
kind: role
id: wolf
name: 狼人
aliases:
  - 狼
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-28
faction: WEREWOLF
team: wolf
victory_goal: eliminate_good
public_summary: 狼人阵营成员，夜间与其他狼人协作选择刀口。
private_identity_card: 你是狼人。你知道同队狼人身份，并在夜间队伍讨论后参与选择一名刀口。
abilities:
  - ability_id: kill
    name: 狼刀
    action_code: 101
    timing: NIGHT_ACTION
    allowed_phases:
      - NIGHT_ACTION
    trigger_type: ACTIVE
    target_rule:
      kind: PLAYER
      min_targets: 1
      max_targets: 1
      allow_self: false
      allow_dead: false
    input_information:
      - field_id: team_consensus_receipt
        description: 由板子夜间流程签发的团队协商完成凭证，证明本座位获得当夜最终刀口提交授权。
        value_type: consensus_receipt
        required: true
    request_effect:
      effect_code: wolf_kill_request
      description: 仅由当夜获得最终刀口提交授权的狼人提交团队最终刀口；其他狼人只能在团队频道参与讨论。
      visibility: TEAM
    resolution_effect:
      effect_code: wolf_kill_result
      description: 经夜间结算确认后，对一名仍存活的非狼人目标施加狼人击杀结果。
      visibility: GM_ONLY
    result_visibility:
      - TEAM
    failure_rules:
      - failure_code: final_submitter_unauthorized
        condition: 提交者没有当夜最终刀口提交授权，或缺少有效的团队协商完成凭证。
        outcome: 拒绝本次狼人刀口请求。
        visibility: PRIVATE
      - failure_code: invalid_target
        condition: 目标不是仍存活且不属于狼人阵营的玩家。
        outcome: 拒绝本次狼人刀口请求。
        visibility: PRIVATE
knowledge_at_start:
  - knowledge_id: wolf_team_members
    description: 你知道同局其他狼人身份。
    visibility: TEAM
team_visibility:
  channel: TEAM
  share_identity: true
  shared_knowledge: []
death_behavior:
  active_abilities_allowed: false
  passive_abilities_continue: false
  death_trigger_fires: false
  description: 死亡后不再参与狼人讨论或行动。
board_compatibility:
  - classic_12_seer_witch_hunter_idiot@1.0.0
common_mistakes:
  - 狼人队伍的最终刀口必须经过本板子规定的夜间窗口确认。
claim_refs:
  - claim-netease-board-composition
source_refs:
  - source-netease-12-standard
---
# 狼人

## 阵营与目标 {#faction}

狼人属于狼人阵营。狼人需要协作淘汰好人，并遵循板子的胜负检查。

## 夜间协作 {#night_action}

狼人可在队伍频道中讨论并形成一名最终刀口。具体窗口顺序和结算时点由板子快照控制。
