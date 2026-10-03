---
schema_version: 1
kind: board
id: classic_12_seer_witch_hunter_idiot
version: 1.0.0
name: 12人标准场（预女猎白）
aliases:
- 预女猎白
- 经典12人局
locale: zh-CN
status: published
reviewed_by: 项目所有者（本会话用户）
reviewed_at: '2026-10-03'
summary: 暗牌、带警长；4名狼人、4名平民、预言家、女巫、猎人和白痴各1名。狼人屠边，好人消灭全部狼人。
seat_count: 12
factions:
  wolf: 4
  good: 8
roles:
- role_ref: wolf@1.0.0
  count: 4
  effective_rules: {}
  override_claim_refs: []
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
  effective_rules:
    can_self_heal: false
    potions_per_night: 1
    heal_potion_count: 1
    poison_potion_count: 1
    knows_wolf_target: true
  override_claim_refs:
  - claim-netease-witch-no-self-heal
  - claim-netease-witch-one-potion-per-night
  - claim-community-witch-target-information
- role_ref: hunter@1.0.0
  count: 1
  effective_rules: {}
  override_claim_refs: []
- role_ref: idiot@1.0.0
  count: 1
  effective_rules: {}
  override_claim_refs: []
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
    villager: villager
    seer: god
    witch: god
    hunter: god
    idiot: god
wolf_team_visibility:
  members_know_each_other: true
  discussion_enabled: true
  identity_visibility: members
knife_rule:
  selection_mode: consensus
  target_visibility: wolf_team
  final_target_required: false
  plan_confirmation_required: true
  available_after_window: wolf_team_chat
identity_reveal:
  reveal_on_death: false
  reveal_on_exile: false
  exceptional_triggers:
  - idiot_exile_reveal
night_windows:
- window_id: wolf_team_chat
  order: 1
  phase: NIGHT_TEAM_CHAT
  depends_on: []
  parallel: false
  visible_to:
  - wolf
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
    visibility_during_collection: secret
    reveal_after_close: ballots_and_totals
    tie_policy: pk_then_no_exile_on_retie
    eligible_voters: alive_with_vote
    allow_abstain: true
  pk:
    enabled: true
    max_candidates: 12
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
    transfer_enabled: true
    transfer_on_death: true
    transfer_on_resignation: true
  resolution_phase: DAY_RESOLVE
mechanics:
- night-resolution@1.0.0
- day-vote@1.0.0
- sheriff-election@1.0.0
interactions:
- hunter-death-trigger@1.0.0
- idiot-exile@1.0.0
- witch-poison-hunter@1.0.0
reading_plan:
  board_ref: classic_12_seer_witch_hunter_idiot@1.0.0
  bootstrap_topics:
  - board:overview
  - mechanic:night-resolution
  - mechanic:day-vote
  role_required_topics:
    wolf:
    - role:wolf
    villager:
    - role:villager
    seer:
    - role:seer
    witch:
    - role:witch
    hunter:
    - role:hunter
    idiot:
    - role:idiot
  phase_topics:
    NIGHT_ACTION:
    - mechanic:night-resolution
    NIGHT_RESOLVE:
    - mechanic:night-resolution
    - interaction:witch-poison-hunter
    SHERIFF_ELECTION:
    - mechanic:sheriff-election
    DAY_SPEECH:
    - mechanic:sheriff-election
    VOTE:
    - mechanic:day-vote
    - interaction:idiot-exile
    DAY_RESOLVE:
    - mechanic:day-vote
    TRIGGER_ACTION:
    - interaction:hunter-death-trigger
  high_risk_topics:
  - interaction:hunter-death-trigger
  - interaction:witch-poison-hunter
  - interaction:idiot-exile
  suggested_queries:
  - 女巫每晚最多使用几瓶药
  - 女巫能否自救
  - 猎人被女巫毒杀能否开枪
  - 白痴被投票放逐后是否继续存活
  - 警长如何产生以及投票权重是多少
  - 平票后是否放逐
claim_refs:
- claim-netease-board-composition
- claim-netease-dark-board
- claim-netease-sheriff
- claim-netease-sheriff-final-speech
- claim-netease-last-words
- claim-community-sheriff-election-tie
- claim-netease-victory
- claim-community-wolf-consensus
- claim-community-day-vote-pk
- claim-community-sheriff-transfer
source_refs:
- source-netease-12-standard
- source-lanke-rulebook-secondary
- source-langrensha-community
---
# 12人标准场（预女猎白）

## 概览 {#overview}

本板子采用网易《狼人杀·官方正版》规则页的 12 人标准场：暗牌、带警长，牌型为 4 名狼人、4 名平民、预言家、女巫、猎人、白痴各 1 名。正式规则的机器字段、角色数量和信息公开策略以本文件 front matter 为准。

主来源：网易《狼人杀·官方正版》12 人标准场规则（官方规则页）。工作台保留来源摘要和主张 ID；本文件不把网页未说明的主持细节推断成官方条款。

## 组成与信息 {#composition}

- 牌面为暗牌；普通死亡或放逐不公开身份。
- 首日进行警长竞选；警长发言顺序和投票权重按本板子机器字段处理。首次警长竞选平票进入 PK，PK 再次平票时警徽流失，本局没有警长。
- 狼人属于同一队伍，夜间可进行队伍讨论并形成一名最终刀口。
- 夜间行动统一在 `NIGHT_RESOLVE` 结算，行动请求本身不直接改变生死状态。

## 胜负条件 {#victory}

好人阵营在结算时消灭全部狼人即获胜。主来源明确狼人以全部神职或全部平民死亡为屠边条件；“至少一名狼人存活”是运行时防止全狼已死仍判狼胜的安全约束，双方条件同时成立时的优先顺序仍需主持版本决议。技术实现应在 `VICTORY_CHECK` 根据存活阵营和本局快照判断，不能从 Markdown 正文临时推断。

## 夜间流程 {#night_flow}

夜间依次经过狼人队伍讨论、角色行动和统一结算。预言家的查验、女巫的药剂选择以及狼人刀口属于夜间行动输入；猎人只有在满足死亡触发条件时进入 `TRIGGER_ACTION`。

网易官方规则页没有明确女巫看到刀口的具体时序。补充来源“领域圈”转载规则页称：女巫解药尚未使用时由法官告知刀口；本版本将其记录为低优先级补充主张并显式标注来源层级，后续更高质量来源可覆盖。

## 白天流程 {#day_flow}

白天先公布夜间死亡，再进行发言、投票和日间结算。首夜死亡者以及白天死亡者可以发表遗言；遗言发生在身份信息处理前。普通死亡与放逐不翻牌，白痴因投票放逐触发其特殊翻牌分支。

普通投票平票、弃票和警徽转移不在网易主来源中完整说明；可复算的领域圈社区 HTML 补充了普通投票 PK、再次平票平安日、允许弃票以及警徽转移/撕徽。该页面也补充警长竞选首次平票进入 PK、再次平票时本局无警长，但其警长票型为2票，与网易主来源的1.5票冲突；本版本保留冲突记录，不能把补充字段解释为网易官方明文。

## 来源与冲突 {#sources}

主来源是网易官方规则页，决定本板子的组成、暗牌、警长 1.5 票、屠边胜负、女巫资源限制、猎人触发限制和白痴放逐分支。领域圈社区 HTML、其他社区摘要和烂柯《狼人杀法典》只作为补充来源，不能覆盖主来源；可复算的领域圈 HTML 支持部分投票与警长平票细节，但其2票口径与主来源冲突，警长票型仍以网易 1.5 票为准。
