---
schema_version: 1
kind: role
id: witch
name: 女巫
aliases: []
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的药剂角色；本候选未重新冻结女巫药剂细节。
private_identity_card: 你是女巫。你的药剂状态和使用窗口由当前板子正式角色快照提供。
abilities: []
knowledge_at_start: []
team_visibility:
  channel: PRIVATE
  share_identity: false
  shared_knowledge: []
death_behavior:
  active_abilities_allowed: false
  passive_abilities_continue: false
  death_trigger_fires: false
  description: 死亡后不能继续使用药剂。
board_compatibility:
  - classic_12_white_wolf_guard@1.0.0
common_mistakes:
  - 守卫与女巫同时效果的结算顺序在本候选中保持待决。
claim_refs:
  - claim-wwg-board-composition
source_refs:
  - source-wpl-2019-board
  - source-guard-interaction
---
# 女巫

女巫属于好人阵营。守卫与女巫的同时效果不在本候选中臆定，须由主持版本提供明确规则。
