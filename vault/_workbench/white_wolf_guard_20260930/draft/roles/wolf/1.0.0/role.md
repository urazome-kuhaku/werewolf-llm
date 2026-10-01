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
reviewed_at: 2026-09-30
faction: WEREWOLF
team: wolf
victory_goal: eliminate_good
public_summary: 狼人阵营成员，夜间与同队狼人协作选择刀口。
private_identity_card: 你是狼人。你知道同队狼人，并在夜间队伍讨论后参与选择一名刀口。
abilities: []
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
  - classic_12_white_wolf_guard@1.0.0
common_mistakes:
  - 狼人队伍的最终刀口必须经过本板子夜间窗口确认。
claim_refs:
  - claim-wwg-board-composition
source_refs:
  - source-wpl-2019-board
---
# 狼人

狼人属于狼人阵营，夜间与白狼王协作讨论并形成最终刀口。
