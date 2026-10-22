---
schema_version: 1
kind: board
id: quasar_echo_lab
version: 1.0.0
name: Quasar Echo Integration Board
aliases: []
locale: en-US
status: published
reviewed_by: stage-a-integration-fixture
reviewed_at: '2026-10-03'
summary: A three-seat fixture for exercising a data-defined two-mode ability.
seat_count: 3
factions:
  wolf: 1
  good: 2
roles:
- role_ref: quasar_oracle@1.0.0
  count: 1
  effective_rules: {}
  override_claim_refs: []
- role_ref: quasar_citizen@1.0.0
  count: 2
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
  role_groups:
    quasar_oracle: wolf
    quasar_citizen: villager
  special_conditions:
  - good_wins_when_all_wolves_are_dead
  - wolves_win_when_all_villagers_are_dead
wolf_team_visibility:
  members_know_each_other: true
  discussion_enabled: true
  identity_visibility: members
knife_rule:
  selection_mode: consensus
  target_visibility: wolf_team
  final_target_required: false
  plan_confirmation_required: false
  available_after_window: wolf_team_chat
identity_reveal:
  reveal_on_death: false
  reveal_on_exile: false
night_windows:
- window_id: wolf_team_chat
  order: 1
  phase: NIGHT_TEAM_CHAT
  visible_to:
  - quasar_oracle
- window_id: night_actions
  order: 2
  phase: NIGHT_ACTION
  depends_on:
  - wolf_team_chat
- window_id: night_resolve
  order: 3
  phase: NIGHT_RESOLVE
  depends_on:
  - night_actions
day_flow:
  vote:
    visibility_during_collection: secret
    reveal_after_close: ballots_and_totals
    tie_policy: no_exile_on_tie
    eligible_voters: alive_with_vote
    allow_abstain: true
  pk:
    enabled: false
  last_words:
    enabled: false
  sheriff:
    enabled: false
mechanics: []
interactions: []
reading_plan:
  board_ref: quasar_echo_lab@1.0.0
  bootstrap_topics:
  - board:overview
  role_required_topics:
    quasar_oracle:
    - role:quasar_oracle
    quasar_citizen:
    - role:quasar_citizen
  phase_topics:
    NIGHT_ACTION:
    - board:overview
    NIGHT_RESOLVE:
    - board:overview
  high_risk_topics:
  - role:quasar_oracle
  suggested_queries:
  - What does Quasar Echo do?
claim_refs:
- claim-quasar-echo-fixture
source_refs:
- source-quasar-echo-fixture
---
# Quasar Echo Integration Board

## Overview {#overview}

Three players test a new ability contract.
