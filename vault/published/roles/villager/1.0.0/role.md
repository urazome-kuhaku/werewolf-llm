---
schema_version: 1
kind: role
id: villager
name: 平民
aliases:
- 村民
version: 1.0.0
status: published
reviewed_by: 项目所有者（本会话用户）
reviewed_at: '2026-10-03'
faction: GOOD
team: good
victory_goal: eliminate_wolf
public_summary: 好人阵营的无主动技能角色，依靠发言和投票找出狼人。
private_identity_card: 你是平民。你没有夜间主动技能，需要通过白天发言和投票帮助好人找出狼人。
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
- classic_12_seer_witch_hunter_idiot@1.0.0
common_mistakes:
- 平民没有预言家、女巫或猎人的夜间技能。
claim_refs:
- claim-netease-board-composition
source_refs:
- source-netease-12-standard
---
# 平民

## 阵营与目标 {#faction}

平民属于好人阵营。平民没有夜间主动技能，通过公开讨论、听取信息并参与白天投票来帮助好人淘汰狼人。

## 信息边界 {#information}

本板子为暗牌局。平民开局不知道其他玩家身份，不能把玩家的角色牌当作公开信息。
