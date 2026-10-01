"""Boundary tests for the formal published board knowledge model."""

from datetime import date

import pytest
from pydantic import ValidationError

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.board import BoardDefinition


def _reading_plan(board_id: str = "fictional-board", version: str = "1.0.0") -> dict[str, object]:
    return {
        "board_ref": {"id": board_id, "version": version},
        "bootstrap_topics": ["board:overview", "mechanic:game_cycle"],
        "role_required_topics": {"wolf": ["role:wolf"]},
        "phase_topics": {"VOTE": ["mechanic:voting"]},
        "high_risk_topics": ["interaction:fictional-edge"],
        "suggested_queries": ["平票如何处理"],
    }


def _board(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": 1,
        "kind": "board",
        "id": "fictional-board",
        "version": "1.0.0",
        "name": "虚构四人测试板",
        "aliases": ["四人测试局"],
        "locale": "zh-CN",
        "status": "published",
        "reviewed_by": "GM",
        "reviewed_at": date(2026, 9, 27),
        "summary": "用于验证正式板子知识模型的虚构测试板。",
        "seat_count": 4,
        "factions": {"town": 2, "wolf": 2},
        "roles": [
            {
                "role_ref": {"id": "wolf", "version": "1.0.0"},
                "count": 2,
                "effective_rules": {},
                "override_claim_refs": [],
            },
            {
                "role_ref": {"id": "villager", "version": "1.0.0"},
                "count": 2,
                "effective_rules": {},
                "override_claim_refs": [],
            },
        ],
        "victory": {
            "mode": "eliminate_side",
            "winning_sides": ["town", "wolf"],
            "check_phases": ["VICTORY_CHECK"],
            "draw_policy": "no_winner",
        },
        "wolf_team_visibility": {
            "members_know_each_other": True,
            "discussion_enabled": True,
            "identity_visibility": "members",
        },
        "knife_rule": {
            "selection_mode": "consensus",
            "target_visibility": "wolf_team",
        },
        "night_windows": [
            "wolf_team_chat",
            {"window_id": "wolf_kill", "depends_on": ["wolf_team_chat"]},
            {"window_id": "night_resolve", "phase": GamePhase.NIGHT_RESOLVE},
        ],
        "day_flow": {
            "announce_deaths": True,
            "vote": {
                "visibility_during_collection": "secret",
                "reveal_after_close": "ballots_and_totals",
                "tie_policy": "pk_then_no_exile_on_retie",
            },
            "pk": {"enabled": True, "candidate_count": 2},
            "last_words": {
                "enabled": True,
                "eligible_death_causes": ["night_kill", "day_exile"],
            },
        },
        "mechanics": ["voting@1.0.0", "night-resolution@1.0.0"],
        "interactions": ["fictional-edge@1.0.0"],
        "reading_plan": _reading_plan(),
        "claim_refs": ["claim-board-core"],
        "source_refs": ["source-fictional-rules"],
    }
    values.update(overrides)
    return values


def test_board_definition_parses_formal_published_document() -> None:
    board = BoardDefinition.model_validate(_board())

    assert board.board_id == "fictional-board"
    assert board.id == "fictional-board"
    assert board.status == "published"
    assert board.seat_count == 4
    assert board.role_bindings[0].role_ref.format() == "wolf@1.0.0"
    assert board.night_windows[0].phase is GamePhase.NIGHT_TEAM_CHAT
    assert board.night_windows[1].order == 2
    assert board.day_flow.vote.tie_policy == "pk_then_no_exile_on_retie"
    assert board.day_flow.sheriff.enabled is False
    assert board.day_flow.sheriff.vote_weight == 1.0
    assert board.identity_reveal.reveal_on_death is True
    assert board.identity_reveal.reveal_on_exile is True
    assert board.reading_plan.board_ref == board.board_ref


def test_board_definition_exposes_stable_board_reference() -> None:
    board = BoardDefinition.model_validate(_board())

    assert board.board_ref.id == board.board_id
    assert board.board_ref.version == board.version
    assert board.vote == board.day_flow.vote


def test_board_definition_represents_official_sheriff_contract() -> None:
    values = _board()
    values["day_flow"] = {
        **values["day_flow"],  # type: ignore[arg-type]
        "sheriff": {
            "enabled": True,
            "first_day_election": True,
            "vote_weight": 1.5,
            "final_speech": True,
        },
    }
    values["identity_reveal"] = {
        "reveal_on_death": False,
        "reveal_on_exile": False,
        "exceptional_triggers": ["idiot_exile"],
    }
    values["day_flow"] = {
        **values["day_flow"],  # type: ignore[arg-type]
        "last_words": {
            "enabled": True,
            "eligible_death_causes": ["night_kill", "day_exile"],
            "night_death_policy": "first_night_only",
            "day_death_policy": "every_day",
        },
    }

    board = BoardDefinition.model_validate(values)

    assert board.sheriff.enabled is True
    assert board.sheriff.first_day_election is True
    assert board.sheriff.vote_weight == 1.5
    assert board.sheriff.final_speech is True
    assert board.sheriff.speaks_last is True
    assert board.last_words.night_death_policy == "first_night_only"
    assert board.last_words.day_death_policy == "every_day"
    assert board.identity_reveal.reveal_on_death is False
    assert board.identity_reveal.reveal_on_exile is False
    assert board.identity_reveal.exceptional_triggers == ["idiot_exile"]


@pytest.mark.parametrize(
    "sheriff",
    [
        {"first_day_election": True},
        {"vote_weight": 1.5},
        {"final_speech": True},
        {"transfer_on_death": True},
        {"pk_enabled": True},
        {
            "enabled": True,
            "pk_enabled": True,
        },
        {
            "enabled": True,
            "pk_enabled": False,
            "tie_policy": "pk_then_revote",
        },
    ],
)
def test_board_definition_rejects_inconsistent_sheriff_contract(
    sheriff: dict[str, object],
) -> None:
    values = _board()
    values["day_flow"] = {
        **values["day_flow"],  # type: ignore[arg-type]
        "sheriff": sheriff,
    }

    with pytest.raises(ValidationError):
        BoardDefinition.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seat_count", 5),
        (
            "role_bindings",
            [
                {
                    "role_ref": {"id": "wolf", "version": "1.0.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
                {
                    "role_ref": {"id": "wolf", "version": "1.1.0"},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
            ],
        ),
    ],
)
def test_board_definition_rejects_count_or_duplicate_role_id(field: str, value: object) -> None:
    values = _board()
    values.pop("roles")
    values[field] = value

    with pytest.raises((TypeError, ValueError, ValidationError)):
        BoardDefinition.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewed_by", "   "),
        ("reviewed_at", "2026-02-30"),
        ("status", "draft"),
        ("summary", "x" * 301),
        ("kind", "role"),
        ("schema_version", 2),
    ],
)
def test_board_definition_requires_reviewed_published_metadata(field: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        BoardDefinition.model_validate({**_board(), field: value})


@pytest.mark.parametrize("field", ["reviewed_by", "reviewed_at"])
def test_board_definition_rejects_missing_review_metadata(field: str) -> None:
    values = _board()
    values.pop(field)

    with pytest.raises(ValidationError):
        BoardDefinition.model_validate(values)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda values: values["mechanics"].append("voting@1.0.0"),
        lambda values: values["interactions"].append("fictional-edge@latest"),
        lambda values: values["mechanics"].append("voting@latest"),
        lambda values: values["interactions"].append("fictional-edge@1.0"),
        lambda values: values["night_windows"].append("wolf_team_chat"),
        lambda values: values["night_windows"][1].update({"phase": "VOTE"}),
        lambda values: values["reading_plan"].update(
            {"board_ref": {"id": "another-board", "version": "1.0.0"}}
        ),
    ],
)
def test_board_definition_rejects_invalid_dependencies_or_flow(mutator: object) -> None:
    values = _board()
    assert callable(mutator)
    mutator(values)  # type: ignore[union-attr]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        BoardDefinition.model_validate(values)


def test_board_definition_rejects_duplicate_dependency_across_kinds() -> None:
    values = _board()
    values["interactions"] = ["voting@1.0.0"]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        BoardDefinition.model_validate(values)


@pytest.mark.parametrize(
    "factions",
    [
        {"town": 1, "wolf": 2},
        {"town": 4, "wolf": 1},
    ],
)
def test_board_definition_requires_faction_counts_to_match_seats(
    factions: dict[str, int],
) -> None:
    with pytest.raises(ValidationError):
        BoardDefinition.model_validate({**_board(), "factions": factions})


@pytest.mark.parametrize(
    "day_flow",
    [
        {
            "vote": {
                "visibility_during_collection": "secret",
                "reveal_after_close": "ballots_and_totals",
                "tie_policy": "pk_then_no_exile_on_retie",
            },
            "pk": {"enabled": False},
        },
        {
            "vote": {
                "visibility_during_collection": "secret",
                "reveal_after_close": "ballots_and_totals",
                "tie_policy": "no_exile_on_tie",
            },
            "pk": {"enabled": True},
        },
    ],
)
def test_board_definition_rejects_inconsistent_pk_configuration(
    day_flow: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BoardDefinition.model_validate({**_board(), "day_flow": day_flow})


def test_board_definition_allows_unknown_tie_policy_for_runtime_gate() -> None:
    values = _board()
    values["day_flow"] = {
        "vote": {
            "visibility_during_collection": "secret",
            "reveal_after_close": "ballots_and_totals",
            "tie_policy": "custom_revote_variant",
        },
        "pk": {"enabled": False},
        "last_words": {
            "enabled": True,
            "eligible_death_causes": ["night_kill"],
        },
    }

    board = BoardDefinition.model_validate(values)

    assert board.vote.tie_policy == "custom_revote_variant"


def test_board_definition_requires_death_causes_for_enabled_last_words() -> None:
    values = _board()
    values["day_flow"] = {
        **values["day_flow"],  # type: ignore[arg-type]
        "last_words": {"enabled": True},
    }

    with pytest.raises(ValidationError):
        BoardDefinition.model_validate(values)


def test_board_definition_requires_discussion_window_when_wolf_chat_enabled() -> None:
    values = _board()
    values["night_windows"] = [
        {"window_id": "wolf_team_chat", "phase": GamePhase.NIGHT_ACTION},
        {"window_id": "wolf_kill", "depends_on": ["wolf_team_chat"]},
        {"window_id": "night_resolve", "phase": GamePhase.NIGHT_RESOLVE},
    ]

    with pytest.raises(ValidationError):
        BoardDefinition.model_validate(values)


def test_board_definition_requires_knife_window_reference() -> None:
    values = _board()
    values["knife_rule"] = {"available_after_window": "missing_window"}

    with pytest.raises(ValidationError):
        BoardDefinition.model_validate(values)


def test_board_definition_rejects_extra_fields() -> None:
    values = _board()
    values["unexpected"] = True

    with pytest.raises(ValidationError):
        BoardDefinition.model_validate(values)
