---
schema_version: 1
kind: role
id: villager
name: 平民
aliases:
  - 村民
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的普通角色，没有夜间主动技能。
private_identity_card: 你是平民。你没有夜间主动技能，通过阅读公屏、发言和投票帮助好人找出狼人。
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
  description: 死亡后不能继续发言或投票。
board_compatibility:
  - classic_12_white_wolf_guard@1.0.0
common_mistakes: []
claim_refs:
  - claim-wwg-board-composition
source_refs:
  - source-wpl-2019-board
---
# 平民

平民属于好人阵营，没有夜间主动技能；通过白天发言、阅读公屏和投票参与胜负。
