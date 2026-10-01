---
schema_version: 1
kind: board
id: classic_12_white_wolf_guard
version: 1.0.0
name: 12人白狼王守卫
aliases:
  - 白狼王守卫
  - 白狼王守卫12人局
locale: zh-CN
status: published
reviewed_by: pending-human-review
reviewed_at: 2026-09-30
summary: 3名狼人、1名白狼王、4名平民，以及预言家、女巫、猎人、守卫各1名的12人候选板子。
seat_count: 12
factions:
  wolf: 4
  good: 8
roles:
  - role_ref: wolf@1.0.0
    count: 3
    effective_rules: {}
    override_claim_refs: []
  - role_ref: white_wolf_king@1.0.0
    count: 1
    effective_rules:
      self_explode_day_window: DAY_SPEECH
      self_explode_target_policy: one_living_other_player
      ignores_guard_and_witch: NEEDS_DECISION
    override_claim_refs:
      - claim-wwg-self-explode
  - role_ref: villager@1.0.0
    count: 4
    effective_rules: {}
    override_claim_refs: []
  - role_ref: seer@1.0.0
    count: 1
    effective_rules: {}
    override_claim_refs: []
  - role_ref: witch@1.0.0
    count: 1
    effective_rules: {}
    override_claim_refs: []
  - role_ref: hunter@1.0.0
    count: 1
    effective_rules: {}
    override_claim_refs: []
  - role_ref: guard@1.0.0
    count: 1
    effective_rules:
      cannot_protect_same_target_consecutively: true
      can_self_protect: NEEDS_DECISION
    override_claim_refs:
      - claim-guard-night-protect
      - claim-guard-no-repeat
victory:
  mode: eliminate_side
  winning_sides:
    - good
    - wolf
  check_phases:
    - NIGHT_RESOLVE
    - DAY_RESOLVE
    - VICTORY_CHECK
  draw_policy: no_winner
  special_conditions:
    - good_wins_when_all_wolves_are_dead
    - wolves_win_when_all_gods_are_dead
    - wolves_win_when_all_villagers_are_dead
    - wolf_side_requires_surviving_wolf
  role_groups:
    wolf: wolf
    white_wolf_king: wolf
    villager: villager
    seer: god
    witch: god
    hunter: god
    guard: god
wolf_team_visibility:
  members_know_each_other: true
  discussion_enabled: true
  identity_visibility: members
knife_rule:
  selection_mode: consensus
  target_visibility: wolf_team
  final_target_required: false
  available_after_window: wolf_team_chat
identity_reveal:
  reveal_on_death: false
  reveal_on_exile: false
  exceptional_triggers: []
night_windows:
  - window_id: wolf_team_chat
    order: 1
    phase: NIGHT_TEAM_CHAT
    depends_on: []
    parallel: false
    visible_to:
      - wolf
      - white_wolf_king
  - window_id: night_actions
    order: 2
    phase: NIGHT_ACTION
    depends_on:
      - wolf_team_chat
    parallel: false
    visible_to: []
  - window_id: night_resolve
    order: 3
    phase: NIGHT_RESOLVE
    depends_on:
      - night_actions
    parallel: false
    visible_to: []
day_flow:
  announce_deaths: true
  speech_phase: DAY_SPEECH
  vote:
    visibility_during_collection: public
    reveal_after_close: ballots_and_totals
    tie_policy: pk_then_no_exile_on_retie
    eligible_voters: alive_with_vote
    allow_abstain: true
  pk:
    enabled: true
    max_candidates: 2
    speech_phase: VOTE_PK_SPEECH
    vote_phase: VOTE_PK
    no_exile_on_retie: true
  last_words:
    enabled: true
    eligible_death_causes:
      - wolf_kill
      - exiled
    before_reveal: true
    night_death_policy: first_night_only
    day_death_policy: every_day
  sheriff:
    enabled: true
    first_day_election: true
    vote_weight: 1.5
    final_speech: true
    tie_policy: pk_then_no_sheriff_on_retie
    pk_enabled: true
    transfer_enabled: null
    transfer_on_death: null
    transfer_on_resignation: null
  resolution_phase: DAY_RESOLVE
mechanics:
  - night-resolution@1.0.0
  - day-vote@1.0.0
  - sheriff-election@1.0.0
interactions: []
reading_plan:
  board_ref: classic_12_white_wolf_guard@1.0.0
  bootstrap_topics:
    - board:overview
    - mechanic:night-resolution
    - mechanic:day-vote
  role_required_topics:
    wolf:
      - role:wolf
    white_wolf_king:
      - role:white_wolf_king
    villager:
      - role:villager
    seer:
      - role:seer
    witch:
      - role:witch
    hunter:
      - role:hunter
    guard:
      - role:guard
  phase_topics:
    NIGHT_ACTION:
      - mechanic:night-resolution
      - role:guard
    NIGHT_RESOLVE:
      - mechanic:night-resolution
    SHERIFF_ELECTION:
      - mechanic:sheriff-election
    DAY_SPEECH:
      - mechanic:sheriff-election
      - role:white_wolf_king
    VOTE:
      - mechanic:day-vote
    DAY_RESOLVE:
      - mechanic:day-vote
  high_risk_topics:
    - role:white_wolf_king
    - role:guard
    - mechanic:night-resolution
  suggested_queries:
    - 白狼王何时可以自爆并带走目标
    - 白狼王自爆是否无视守卫和女巫效果
    - 守卫每晚如何选择保护目标
    - 守卫能否连续保护同一目标或自守
    - 白狼王守卫板子的胜负条件是什么
    - 警长和平票规则是否已由主持版本冻结
claim_refs:
  - claim-wwg-board-composition
  - claim-wwg-self-explode
  - claim-guard-night-protect
  - claim-guard-no-repeat
  - claim-wwg-wolf-victory
source_refs:
  - source-white-wolf-king-guide
  - source-wpl-2019-board
  - source-guard-strategy
  - source-guard-interaction
---
# 12人白狼王守卫

## 候选范围 {#overview}

本文件是白狼王＋守卫 12 人板子的候选机器定义。角色数量来自 WPL 规则页搜索摘要；白狼王白天自爆并带走目标、守卫夜间保护及连续目标限制分别来自记录在 `sources/` 下的搜索摘要。所有来源身份和主持细节仍需人工审核。

## 角色与技能 {#roles}

白狼王单独使用白天主动技能 `SELF_SACRIFICE_TAKE`。该能力不是普通狼人死亡触发技能：主持人打开白狼王的白天技能窗口后，由白狼王决定是否提交有序的 `[发动者, 被带走目标]` 目标对。目标资格和与守卫、女巫效果的交互暂按 `NEEDS_DECISION` 保留。

守卫每晚选择一名保护目标，当前候选禁止连续两晚选择同一目标。守卫自守规则和守卫、女巫同时效果的先后顺序未冻结，运行时不得从本文推断。

## 白天与夜间主持流程 {#flow}

夜间仍使用狼人队伍讨论、角色行动、统一结算三个窗口；白天包含发言、技能窗口和放逐投票。警长和平票字段沿用运行时可联调契约，来源摘要没有冻结这些主持细节，正式发布前需单独审核。

## 待决规则 {#decisions}

白狼王自爆的精确时点、被带走目标是否必须存活、是否触发目标死亡技能、是否无视守卫/解药，以及守卫能否自守，均在 `decision-requests.md` 和 `conflicts.json` 中保留待决状态。
