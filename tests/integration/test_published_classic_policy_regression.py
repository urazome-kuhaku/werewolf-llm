"""Regression coverage for the published classic board policy boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from werewolf.domain.enums import Channel, GamePhase
from werewolf.game import (
    EventType,
    GameManager,
    GameState,
    PlayerState,
    PublicVoteResultPayload,
    RulesetRef,
    SheriffCampaignSpeechRequest,
    VoteRequest,
    VoteWindow,
    build_sheriff_vote_window,
    load_action_registry,
)
from werewolf.game.setup import build_role_assignment_plan
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
from werewolf.knowledge.compiler import CompiledKnowledgePackage, KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader
from werewolf.knowledge.runtime_loader import (
    RuntimeKnowledgeBundle,
    load_runtime_knowledge_bundle_from_snapshot,
)
from werewolf.knowledge.service import QueryContext
from werewolf.knowledge.snapshot import KnowledgeSnapshot, KnowledgeSnapshotBuilder
from werewolf.moderator.night_flow import ModeratorNightError, ModeratorNightFlow
from werewolf.runtime.knowledge_bootstrap import build_knowledge_bootstrap_card
from werewolf.runtime.player_runtime import InitialContext, RuntimeConfig, Speech, SpeechResponse
from werewolf.runtime.scripted_runtime import ScriptedRuntime

NOW = datetime(2026, 10, 3, tzinfo=UTC)
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
BOARD_ID = "classic_12_seer_witch_hunter_idiot"
PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"


async def _actual_bundle(
    tmp_path: Path,
    *,
    game_id: str,
) -> tuple[RuntimeKnowledgeBundle, KnowledgeSnapshot, CompiledKnowledgePackage]:
    """Load, compile, freeze, and restore the checked-in published package."""

    loaded = await KnowledgePackageLoader(PUBLISHED_ROOT).load(BOARD_REF)
    compiled = KnowledgePackageCompiler().compile(loaded)
    compiled_store = CompiledKnowledgeStore(tmp_path / "compiled")
    await compiled_store.publish(compiled)
    persisted = await compiled_store.load(BOARD_REF, expected_board_ref=BOARD_REF)

    # The temporary compiled package must retain the actual published closure,
    # while this test deliberately avoids pinning a digest that legitimately
    # changes with release metadata.
    assert persisted.package_identity == compiled.package_identity
    manifest_files = persisted.manifest_payload["files"]
    assert isinstance(manifest_files, list)
    assert {entry["path"] for entry in manifest_files} == set(persisted.file_digests)
    assert {entry["path"]: entry["sha256"] for entry in manifest_files} == dict(
        persisted.file_digests
    )
    for entry in manifest_files:
        assert isinstance(entry["sha256"], str)
        assert len(entry["sha256"]) == 64
    document_entries = compiled.manifest_payload["documents"]
    assert isinstance(document_entries, list)
    assert len(document_entries) == 13
    assert {entry["path"]: entry["sha256"] for entry in document_entries} == dict(
        compiled.document_digests
    )

    snapshot = await KnowledgeSnapshotBuilder(compiled_store, tmp_path / "games").create(
        game_id,
        BOARD_REF,
    )
    assert snapshot.package_identity == persisted.package_identity
    assert snapshot.file_digests
    assert all(len(digest) == 64 for digest in snapshot.file_digests.values())
    bundle = await load_runtime_knowledge_bundle_from_snapshot(snapshot)
    return bundle, snapshot, compiled


def _ruleset(snapshot: KnowledgeSnapshot) -> RulesetRef:
    return RulesetRef(
        board_id=BOARD_ID,
        version="1.0.0",
        snapshot_id=snapshot.snapshot_id,
        manifest_sha256=snapshot.manifest_sha256,
    )


def _state(
    *,
    game_id: str,
    phase: GamePhase,
    snapshot: KnowledgeSnapshot,
    players: dict[int, PlayerState],
) -> GameState:
    return GameState(
        game_id=game_id,
        created_at=NOW,
        updated_at=NOW,
        phase=phase,
        day_no=1,
        ruleset=_ruleset(snapshot),
        players=players,
    )


def _speech(text: str):
    return lambda request: SpeechResponse(
        request_id=request.request_id,
        speech=Speech(text=text),
    )


async def _started_runtime(
    game_id: str,
    seat: int,
    scripts: list[object],
) -> ScriptedRuntime:
    runtime = ScriptedRuntime(scripts)
    await runtime.start(
        RuntimeConfig(session_id=f"{game_id}-seat-{seat}"),
        InitialContext(game_id=game_id, seat=seat, session_epoch=0),
    )
    return runtime


@pytest.mark.asyncio
async def test_published_classic_policy_survives_compile_freeze_and_bootstrap(
    tmp_path: Path,
) -> None:
    bundle, snapshot, _compiled = await _actual_bundle(tmp_path, game_id="published-policy")

    assert bundle.board.board_ref.format() == BOARD_REF
    assert bundle.board.knife_rule.final_target_required is False
    assert bundle.board.knife_rule.plan_confirmation_required is True
    assert bundle.board.day_flow.vote.visibility_during_collection == "secret"
    assert bundle.board.day_flow.vote.reveal_after_close == "ballots_and_totals"
    assert bundle.board.day_flow.sheriff.enabled is True
    assert bundle.package.package_payload["board_definition"] == bundle.board.model_dump(
        mode="json"
    )

    card = build_knowledge_bootstrap_card(
        bundle.service,
        QueryContext(
            game_id="published-policy",
            snapshot_id=snapshot.snapshot_id,
            seat=1,
            session_epoch=0,
        ),
        "wolf",
    )
    assert card.board.id == BOARD_ID
    assert card.snapshot_id == snapshot.snapshot_id
    assert tuple(read.tool for read in card.required_reads) == ("get_board", "get_role")
    assert card.your_role.id == "wolf"


@pytest.mark.asyncio
async def test_published_classic_night_requires_private_plan_and_shares_all_proposals(
    tmp_path: Path,
) -> None:
    bundle, snapshot, compiled = await _actual_bundle(tmp_path, game_id="published-night")
    plan = build_role_assignment_plan(
        bundle.board,
        compiled,
        seed=20261003,
        seats=tuple(range(1, bundle.board.seat_count + 1)),
    )
    players = dict(plan.players)
    game_id = "published-night"
    state = _state(
        game_id=game_id,
        phase=GamePhase.NIGHT_TEAM_CHAT,
        snapshot=snapshot,
        players=players,
    )
    manager = GameManager(state, registry=load_action_registry())
    wolf_seats = tuple(seat for seat, player in sorted(players.items()) if player.role_id == "wolf")
    assert len(wolf_seats) == 4
    coordinator = wolf_seats[0]
    runtimes = {
        seat: await _started_runtime(
            game_id,
            seat,
            [
                _speech(f"wolf-proposal-{seat}"),
                *([_speech("wolf-final-summary")] if seat == coordinator else []),
            ],
        )
        for seat in wolf_seats
    }
    flow = ModeratorNightFlow(
        manager,
        bundle.board,
        runtimes,
        snapshot_id=snapshot.snapshot_id,
    )

    await flow.open()
    for _ in wolf_seats:
        await flow.team_next()

    with pytest.raises(ModeratorNightError, match="WOLF_PLAN_REQUIRED"):
        await flow.advance()

    await flow.plan_next()
    coordinator_runtime = runtimes[coordinator]
    plan_requests = [
        request
        for request in coordinator_runtime.requests
        if "wolf-plan" in request.logical_request_id
    ]
    assert len(plan_requests) == 1
    plan_request = plan_requests[0]
    observed_proposals = {
        event.payload["content"]
        for event in plan_request.observation.events
        if event.event_type == EventType.TEAM_SPEECH.value
    }
    assert observed_proposals == {f"wolf-proposal-{seat}" for seat in wolf_seats}
    assert "wolf-final-summary" not in observed_proposals

    await flow.advance()
    opened_action = await flow.open()
    wolf_kill_code = next(
        action.action_code
        for action in load_action_registry().actions
        if action.action_name == "WOLF_KILL"
    )
    assert coordinator in opened_action.action_window.allowed_seats
    assert wolf_kill_code in opened_action.action_window.allowed_action_codes

    nonwolf = next(seat for seat, player in sorted(players.items()) if player.role_id != "wolf")
    private_delivery = await manager.peek_delivery(nonwolf, 0)
    assert not any(event.channel is Channel.TEAM for event in private_delivery)
    assert all(
        "wolf-proposal-" not in json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        for event in private_delivery
    )


def _vote_players(plan_players: dict[int, PlayerState]) -> dict[int, PlayerState]:
    return {
        seat: player.model_copy(update={"current_request_id": f"request-{seat}"})
        for seat, player in plan_players.items()
    }


@pytest.mark.asyncio
async def test_published_classic_day_and_sheriff_votes_hide_collection_and_reveal_clean_ballots(
    tmp_path: Path,
) -> None:
    bundle, snapshot, compiled = await _actual_bundle(tmp_path, game_id="published-votes")
    plan = build_role_assignment_plan(
        bundle.board,
        compiled,
        seed=20261003,
        seats=tuple(range(1, bundle.board.seat_count + 1)),
    )
    seats = tuple(range(1, bundle.board.seat_count + 1))
    candidates = (1, 2)

    day_manager = GameManager(
        _state(
            game_id="published-day-vote",
            phase=GamePhase.VOTE,
            snapshot=snapshot,
            players=_vote_players(dict(plan.players)),
        ),
        registry=load_action_registry(),
    )
    day_window = VoteWindow(
        window_id="published-day-vote-r1",
        game_id="published-day-vote",
        session_epoch=0,
        observation_revision=0,
        eligible_voters=seats,
        candidate_seats=candidates,
        vote_weights={seat: 1.0 for seat in seats},
        expected_request_ids={seat: f"request-{seat}" for seat in seats},
        allow_abstain=True,
    )
    await day_manager.open_vote_window(day_window)
    for seat in seats:
        current = await day_manager.snapshot()
        await day_manager.submit_vote(
            VoteRequest(
                request_id=f"request-{seat}",
                game_id=current.game_id,
                window_id=day_window.window_id,
                seat=seat,
                session_epoch=0,
                observation_revision=0,
                target_seat=1,
            ),
            expected_revision=current.state_revision,
        )
    pending_day = await day_manager.snapshot()
    assert not any(event.event_type is EventType.VOTE_RESULT for event in pending_day.events)
    pending_day = await day_manager.finalize_vote(expected_revision=pending_day.state_revision)
    assert not any(event.event_type is EventType.VOTE_RESULT for event in pending_day.events)
    confirmed_day = await day_manager.confirm_vote_tally(
        bundle.board,
        expected_revision=pending_day.state_revision,
    )
    day_payload = confirmed_day.events[-1].payload
    assert isinstance(day_payload, PublicVoteResultPayload)
    assert day_payload.vote_kind == "day"
    assert len(day_payload.ballots) == len(seats)
    day_encoded = json.dumps(day_payload.model_dump(mode="json"), ensure_ascii=False)
    assert all(
        private_key not in day_encoded
        for private_key in ("request_id", "session_epoch", "observation_revision")
    )

    sheriff_players = _vote_players(dict(plan.players))
    sheriff_manager = GameManager(
        _state(
            game_id="published-sheriff-vote",
            phase=GamePhase.DAY_ANNOUNCE,
            snapshot=snapshot,
            players=sheriff_players,
        ),
        registry=load_action_registry(),
    )
    await sheriff_manager.start_sheriff_election(bundle.board, candidates=candidates)
    for seat in candidates:
        current = await sheriff_manager.snapshot()
        await sheriff_manager.submit_sheriff_speech(
            SheriffCampaignSpeechRequest(
                request_id=f"sheriff-speech-{seat}",
                game_id=current.game_id,
                day_no=1,
                seat=seat,
                session_epoch=0,
                observation_revision=current.state_revision,
                text=f"candidate-{seat}",
            ),
            expected_revision=current.state_revision,
        )
    current = await sheriff_manager.snapshot()
    sheriff_window = build_sheriff_vote_window(
        game_id=current.game_id,
        day_no=1,
        observation_revision=current.state_revision,
        session_epoch=0,
        eligible_voters=seats,
        candidates=candidates,
        vote_weights={seat: 1.0 for seat in seats},
        expected_request_ids={seat: f"request-{seat}" for seat in seats},
        allow_abstain=True,
    )
    await sheriff_manager.open_sheriff_vote_window(
        sheriff_window,
        expected_revision=current.state_revision,
    )
    for seat in seats:
        current = await sheriff_manager.snapshot()
        await sheriff_manager.submit_sheriff_vote(
            VoteRequest(
                request_id=f"request-{seat}",
                game_id=current.game_id,
                window_id=sheriff_window.window_id,
                seat=seat,
                session_epoch=0,
                observation_revision=sheriff_window.observation_revision,
                target_seat=1,
            ),
            expected_revision=current.state_revision,
        )
    pending_sheriff = await sheriff_manager.snapshot()
    assert not any(event.event_type is EventType.VOTE_RESULT for event in pending_sheriff.events)
    pending_sheriff = await sheriff_manager.finalize_sheriff_election(
        bundle.board,
        expected_revision=pending_sheriff.state_revision,
    )
    assert not any(event.event_type is EventType.VOTE_RESULT for event in pending_sheriff.events)
    confirmed_sheriff = await sheriff_manager.confirm_sheriff_election(
        bundle.board,
        expected_revision=pending_sheriff.state_revision,
    )
    sheriff_payload = confirmed_sheriff.events[-1].payload
    assert isinstance(sheriff_payload, PublicVoteResultPayload)
    assert sheriff_payload.vote_kind == "sheriff"
    assert len(sheriff_payload.ballots) == len(seats)
    sheriff_encoded = json.dumps(sheriff_payload.model_dump(mode="json"), ensure_ascii=False)
    assert all(
        private_key not in sheriff_encoded
        for private_key in ("request_id", "session_epoch", "observation_revision")
    )
