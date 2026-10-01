---
schema_version: 1
kind: role
id: seer
name: 预言家
aliases: []
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的查验角色。
private_identity_card: 你是预言家。夜间行动窗口开放时，可以查验一名玩家的阵营信息。
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
  description: 死亡后不能继续查验。
board_compatibility:
  - classic_12_white_wolf_guard@1.0.0
common_mistakes: []
claim_refs:
  - claim-wwg-board-composition
source_refs:
  - source-wpl-2019-board
---
# 预言家

预言家属于好人阵营。具体查验返回格式沿用运行时角色契约；本候选本轮只验证板子角色闭包。
