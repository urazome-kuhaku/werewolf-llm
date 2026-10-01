---
schema_version: 1
kind: role
id: hunter
name: 猎人
aliases: []
version: 1.0.0
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的死亡触发角色；本候选沿用主持版本的猎人技能契约。
private_identity_card: 你是猎人。你的死亡触发技能由当前主持版本的角色快照提供。
abilities: []
knowledge_at_start: []
team_visibility:
  channel: PRIVATE
  share_identity: false
  shared_knowledge: []
death_behavior:
  active_abilities_allowed: false
  passive_abilities_continue: false
  death_trigger_fires: true
  description: 是否进入死亡触发窗口由死亡原因和主持版本角色快照决定。
board_compatibility:
  - classic_12_white_wolf_guard@1.0.0
common_mistakes:
  - 不要把白狼王自爆误判成猎人的死亡触发技能。
claim_refs:
  - claim-wwg-board-composition
source_refs:
  - source-wpl-2019-board
---
# 猎人

猎人属于好人阵营。猎人的具体开枪窗口沿用主持版本角色契约；白狼王自爆是另一种主动技能，不复用猎人分支。
