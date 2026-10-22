from __future__ import annotations

from types import SimpleNamespace

from werewolf.domain.sheriff_eligibility import (
    first_day_sheriff_participants,
    first_night_death_seats,
    should_mask_unannounced_first_night_death,
)


def _scheduled_first_night_state(*, valid_receipt: bool = True) -> tuple[object, object]:
    package_id = "night-package"
    group_id = "night-group-r0"
    batch_id = "night-batch-r0"
    digest = "a" * 64
    death_fact = SimpleNamespace(
        fact_id="death-fact-3",
        fact_type="DEATH_CONFIRMED",
        source_request_id=None,
        target_seat=3,
        death_cause="wolf_kill",
        data={
            "source_request_ids": ["skill-request-internal"],
            "cause_effect_ids": ["effect-intent-runtime-id"],
            "sources": [
                {
                    "source_request_id": "skill-request-internal",
                    "effect_id": "effect-intent-runtime-id",
                    "source_rule_id": "wolf-kill-rule",
                    "actor_seat": 1,
                }
            ],
        },
    )
    ledger = SimpleNamespace(
        batch_id=batch_id,
        package_id=package_id,
        group_id=group_id,
        timing="NIGHT_ACTION",
        read_revision=3,
        committed_revision=4,
        round_no=0,
        request_ids=("physical-request-3",),
        outcome_digest=digest,
        facts=(death_fact,),
        history_updates=(),
    )
    receipt = SimpleNamespace(
        batch_id=batch_id,
        package_id=package_id,
        group_id=group_id,
        timing="NIGHT_ACTION",
        read_revision=3,
        committed_revision=4,
        request_ids=("physical-request-3",),
        occurrence_ids=(),
        outcome_digest=digest,
    )
    boundary = SimpleNamespace(
        source_batch_id=batch_id,
        source_group_id=group_id,
        death_fact_ids=(death_fact.fact_id,),
        death_seats=(3,),
        is_pending=False,
        return_point=SimpleNamespace(
            phase="DAY_ANNOUNCE",
            day_no=0,
            window_id="physical-window-3",
            logical_window_id=None,
        ),
    )
    window = {
        "window_id": "physical-window-3",
        "game_id": "game-1",
        "phase": "NIGHT_ACTION",
        "settlement_group_id": group_id,
        "logical_window_id": "night-last",
        "next_window_id": None,
        "closed_at": "2026-10-01T00:00:00Z",
        "visible_context": {"night_round": 0},
    }
    state = SimpleNamespace(
        day_no=1,
        round_no=0,
        phase="DAY_ANNOUNCE",
        state_revision=10,
        execution_identity=SimpleNamespace(package_id=package_id),
        players={
            1: SimpleNamespace(alive=True, death_cause=None, can_vote=True),
            2: SimpleNamespace(alive=True, death_cause=None, can_vote=True),
            3: SimpleNamespace(alive=False, death_cause="wolf_kill", can_vote=True),
        },
        events=(),
        rule_boundaries=(boundary,),
        rule_ledger=(ledger,),
        rule_receipts=(receipt,) if valid_receipt else (),
        rule_trigger_queue=(),
        rule_workflow_cursor=None,
        action_windows={"physical-window-3": window},
        action_requests={
            "physical-request-3": {
                "status": "CONFIRMED",
                "rule_receipt_id": batch_id,
                "rule_group_id": group_id,
                "window_id": "physical-window-3",
                "rule_dispositions": [
                    {"skill_request_id": "skill-request-internal"}
                ],
            }
        },
    )
    package = SimpleNamespace(
        package_id=package_id,
        night_boundary_schedule=SimpleNamespace(
            prerequisites=("FIRST_DAY_SHERIFF_ELECTION", "DAY_ANNOUNCEMENT")
        ),
        window_metadata=(
            SimpleNamespace(window_id="night-last", phase="NIGHT_ACTION", order=10),
        ),
    )
    return state, package


def test_scheduled_first_day_masks_only_a_closed_terminal_proven_death() -> None:
    state, package = _scheduled_first_night_state()

    assert first_night_death_seats(state, execution_package=package) == (3,)
    assert first_day_sheriff_participants(state, execution_package=package) == (1, 2, 3)
    assert should_mask_unannounced_first_night_death(
        state, 3, execution_package=package
    )


def test_scheduled_first_day_fails_closed_without_the_unique_matching_receipt() -> None:
    state, package = _scheduled_first_night_state(valid_receipt=False)

    assert first_night_death_seats(state, execution_package=package) == ()
    assert first_day_sheriff_participants(state, execution_package=package) == (1, 2)
    assert not should_mask_unannounced_first_night_death(
        state, 3, execution_package=package
    )


def test_scheduled_first_day_rejects_a_nonterminal_or_foreign_window() -> None:
    state, package = _scheduled_first_night_state()
    state.action_windows["physical-window-3"]["next_window_id"] = "night-later"

    assert first_night_death_seats(state, execution_package=package) == ()

    state, package = _scheduled_first_night_state()
    state.execution_identity = SimpleNamespace(package_id="foreign-package")
    assert first_night_death_seats(state, execution_package=package) == ()
