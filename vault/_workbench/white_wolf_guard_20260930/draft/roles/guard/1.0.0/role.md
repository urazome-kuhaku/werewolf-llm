---
schema_version: 1
kind: role
id: guard
name: 守卫
aliases:
  - 守护者
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的夜间保护角色，每晚选择一名存活玩家进行保护。
private_identity_card: 你是守卫。每个夜间行动窗口可以选择一名其他存活玩家保护；当前候选禁止自守和连续两晚保护同一目标。
abilities:
  - ability_id: protect
    name: 守护
    action_code: 106
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
    usage_limit:
      uses_per_round: 1
    input_information: []
    request_effect:
      effect_code: guard_protect_request
      description: 选择一名仍存活的玩家作为本夜保护目标。
      visibility: PRIVATE
    resolution_effect:
      effect_code: guard_protect_result
      description: 在夜间统一结算中记录保护效果；连续目标限制由板子状态校验。
      visibility: GM_ONLY
    result_visibility:
      - PRIVATE
    failure_rules:
      - failure_code: same_target_consecutively
        condition: 本夜目标与守卫上一夜保护目标相同。
        outcome: 拒绝本次守护请求。
        visibility: PRIVATE
      - failure_code: invalid_target
        condition: 目标不是仍存活的板上玩家。
        outcome: 拒绝本次守护请求。
        visibility: PRIVATE
knowledge_at_start: []
team_visibility:
  channel: PRIVATE
  share_identity: false
  shared_knowledge: []
death_behavior:
  active_abilities_allowed: false
  passive_abilities_continue: false
  death_trigger_fires: false
  description: 守卫死亡后不能继续保护。
board_compatibility:
  - classic_12_white_wolf_guard@1.0.0
common_mistakes:
  - 当前候选禁止连续两晚保护同一目标。
  - 当前候选运行配置禁止自守；正式规则是否允许自守以及与女巫解药同时生效的顺序待人工决策。
claim_refs:
  - claim-guard-night-protect
  - claim-guard-no-repeat
source_refs:
  - source-guard-strategy
  - source-guard-interaction
---
# 守卫

守卫属于好人阵营。每夜在 `NIGHT_ACTION` 窗口选择一名其他仍存活的玩家保护；当前候选运行配置禁止自守，状态校验也不允许连续两夜保护同一目标。

自守和守卫与女巫同时效果的结算规则没有可靠统一证据，当前只作为主持人待决项保留。
