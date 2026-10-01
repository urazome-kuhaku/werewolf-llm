"""Tests for the moderator's strict player configuration boundary."""

from datetime import date
from pathlib import Path

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.board import BoardDefinition
from werewolf.moderator.config import (
    ModeratorConfigError,
    load_player_configuration,
    parse_player_configuration,
)


def _board() -> BoardDefinition:
    board_id = "config-test-board"
    version = "1.0.0"
    return BoardDefinition.model_validate(
        {
            "schema_version": 1,
            "kind": "board",
            "id": board_id,
            "version": version,
            "name": "配置测试板",
            "locale": "zh-CN",
            "status": "published",
            "reviewed_by": "test",
            "reviewed_at": date(2026, 9, 28),
            "summary": "用于主持器配置边界测试。",
            "seat_count": 4,
            "factions": {"town": 2, "wolf": 2},
            "roles": [
                {
                    "role_ref": {"id": "wolf", "version": version},
                    "count": 2,
                    "effective_rules": {},
                    "override_claim_refs": [],
                },
                {
                    "role_ref": {"id": "villager", "version": version},
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
            "mechanics": ["voting@1.0.0"],
            "interactions": ["config-edge@1.0.0"],
            "reading_plan": {
                "board_ref": {"id": board_id, "version": version},
                "bootstrap_topics": ["board:overview"],
                "role_required_topics": {"wolf": ["role:wolf"]},
                "phase_topics": {"VOTE": ["mechanic:voting"]},
                "high_risk_topics": ["interaction:config-edge"],
                "suggested_queries": ["配置测试"],
            },
            "claim_refs": ["claim-config"],
            "source_refs": ["source-config"],
        }
    )


def _raw_players() -> dict[str, object]:
    return {
        "schema_version": 1,
        "game": {"game_id": "config-game"},
        "players": [
            {"seat": 1, "runtime": "pi", "provider": "provider_a", "model": "gpt-6-luna"},
            {"seat": 2, "runtime": "pi", "provider": "provider_b", "model": "gpt-6-luna"},
            {"seat": 3, "runtime": "pi", "provider": "provider_a", "model": "gpt-6-luna"},
            {"seat": 4, "runtime": "pi", "provider": "provider_b", "model": "gpt-6-luna"},
        ],
    }


def test_parse_derives_sorted_seat_scoped_sessions(tmp_path: Path) -> None:
    parsed = parse_player_configuration(_raw_players(), board=_board(), game_root=tmp_path)

    assert parsed.game_id == "config-game"
    assert [player.seat for player in parsed.players] == [1, 2, 3, 4]
    assert parsed.players[0].session_id == "config-game-seat-01"
    assert (
        parsed.players[0].session_dir
        == (tmp_path / ".runtime" / "players" / "config-game" / "seat_01").resolve()
    )
    assert parsed.players[0].session_dir.is_relative_to(tmp_path.resolve())
    assert parsed.players[0].to_runtime_config().provider == "provider_a"
    assert parsed.players[0].to_runtime_config().session_dir == parsed.players[0].session_dir


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda value: value["players"].__setitem__(1, {**value["players"][1], "seat": 1}),
            "unique",
        ),
        (lambda value: value["players"].pop(), "count"),
        (lambda value: value["players"].__setitem__(0, {"seat": 1, "runtime": "pi"}), "provider"),
        (
            lambda value: value["players"].__setitem__(
                0, {**value["players"][0], "api_key": "secret"}
            ),
            "invalid",
        ),
    ],
)
def test_parse_rejects_invalid_or_secret_player_entries(change: object, message: str) -> None:
    raw = _raw_players()
    change(raw)  # type: ignore[operator]

    with pytest.raises(ModeratorConfigError, match=message):
        parse_player_configuration(raw, board=_board(), game_root=".")


def test_scripted_runtime_requires_an_explicit_test_switch(tmp_path: Path) -> None:
    raw = _raw_players()
    raw["players"][0] = {"seat": 1, "runtime": "scripted"}  # type: ignore[index]

    with pytest.raises(ModeratorConfigError, match="test-only"):
        parse_player_configuration(raw, board=_board(), game_root=tmp_path)

    parsed = parse_player_configuration(
        raw, board=_board(), game_root=tmp_path, allow_scripted=True
    )
    assert parsed.players[0].runtime == "scripted"
    assert parsed.players[0].to_runtime_config().provider is None


def test_missing_players_and_custom_session_dir_are_clear_errors(tmp_path: Path) -> None:
    raw = _raw_players()
    raw.pop("players")
    with pytest.raises(ModeratorConfigError, match="configuration.players is required"):
        parse_player_configuration(raw, board=_board(), game_root=tmp_path)

    raw = _raw_players()
    raw["players"][0] = {  # type: ignore[index]
        **raw["players"][0],  # type: ignore[index]
        "session_dir": "C:/outside",
    }
    with pytest.raises(ModeratorConfigError, match="invalid"):
        parse_player_configuration(raw, board=_board(), game_root=tmp_path)


def test_load_player_configuration_reads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "players.yaml"
    path.write_text(
        "schema_version: 1\n"
        "game:\n  game_id: config-game\n"
        "players:\n"
        + "\n".join(
            f"  - seat: {seat}\n    runtime: pi\n    provider: provider_a\n    model: gpt-6-luna"
            for seat in range(1, 5)
        )
        + "\n",
        encoding="utf-8",
    )

    parsed = load_player_configuration(path, board=_board(), game_root=tmp_path)
    assert len(parsed.players) == 4
