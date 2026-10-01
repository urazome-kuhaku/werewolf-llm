"""Tests for the stable domain enumerations."""

import json

from werewolf.domain import Channel, GamePhase, RunStatus


def test_enum_values_are_stable_json_strings() -> None:
    for enum_member in (*GamePhase, *RunStatus, *Channel):
        assert enum_member.value == enum_member.name
        assert json.loads(json.dumps(enum_member)) == enum_member.value

    assert GamePhase.CREATED.value == "CREATED"
    assert GamePhase.SHERIFF_ELECTION_SPEECH.value == "SHERIFF_ELECTION_SPEECH"
    assert GamePhase.SHERIFF_ELECTION.value == "SHERIFF_ELECTION"
    assert GamePhase.SHERIFF_ELECTION_PK_SPEECH.value == "SHERIFF_ELECTION_PK_SPEECH"
    assert GamePhase.SHERIFF_ELECTION_PK.value == "SHERIFF_ELECTION_PK"
    assert GamePhase.SHERIFF_TRANSFER.value == "SHERIFF_TRANSFER"
    assert GamePhase.FINISHED.value == "FINISHED"
    assert RunStatus.WAITING_GM.value == "WAITING_GM"
    assert Channel.GM_ONLY.value == "GM_ONLY"


def test_phase_and_run_status_are_orthogonal() -> None:
    assert GamePhase.DAY_SPEECH != RunStatus.RUNNING
    assert GamePhase.DAY_SPEECH.value not in {status.value for status in RunStatus}
    assert RunStatus.PAUSED.value not in {phase.value for phase in GamePhase}
    assert RunStatus.PAUSED is RunStatus("PAUSED")
    assert GamePhase.DAY_SPEECH is GamePhase("DAY_SPEECH")


def test_channel_values_are_distinct_and_reversible() -> None:
    assert {channel.value for channel in Channel} == {
        "PUBLIC",
        "TEAM",
        "PRIVATE",
        "GM_ONLY",
    }
    assert Channel("PRIVATE") is Channel.PRIVATE
