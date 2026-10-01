"""Moderator adapter for the board-defined daytime coordinator.

This module deliberately contains no daytime rules.  ``DayCoordinator`` owns
the authoritative lifecycle and board guards; this adapter only turns the
moderator command vocabulary into coordinator calls and produces a small
JSON-safe progress view for the shell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from werewolf.domain.enums import GamePhase
from werewolf.game.day import (
    DayCoordinator,
    DaySpeechProgress,
    DayVoteProgress,
)
from werewolf.game.manager import GameManager
from werewolf.game.serial_turn import SerialSpeechResult
from werewolf.game.state import GameState
from werewolf.game.vote_turn import VoteTurnResult
from werewolf.knowledge.board import BoardDefinition
from werewolf.runtime.player_runtime import PlayerRuntime


class ModeratorDayFlow:
    """Bind one running moderator game to its daytime coordinator.

    A fresh adapter may be created after a process restart.  The coordinator
    restores durable vote/PK information from ``GameState`` and recreates its
    speech scheduler when the persisted queue is opened again.
    """

    def __init__(
        self,
        manager: GameManager,
        board: BoardDefinition,
        runtimes: Mapping[int, PlayerRuntime],
        *,
        snapshot_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._manager = manager
        self.coordinator = DayCoordinator(
            manager,
            board,
            runtimes,
            snapshot_id=snapshot_id,
            timeout_seconds=timeout_seconds,
        )

    @property
    def state(self) -> GameState:
        return self._manager.state

    async def announce(self, content: str | None = None) -> GameState:
        return await self.coordinator.announce(content)

    async def ensure_announcement(self, content: str | None = None) -> GameState:
        """Publish the day announcement while retaining DAY_ANNOUNCE.

        The first-day sheriff command uses this to announce night deaths before
        the dedicated sheriff election phase begins.
        """

        return await self.coordinator.commit_announcement(content)

    async def open_speech(
        self,
        queue: Sequence[int] | None = None,
        *,
        is_pk: bool | None = None,
    ) -> DaySpeechProgress:
        """Open ordinary or PK speech, recovering the branch from phase.

        ``None`` is the shell-friendly form: after the first tied vote the
        durable ``VOTE_PK_SPEECH`` phase selects the PK branch automatically.
        An explicit flag remains available to callers that want a strict
        request contract.
        """

        if is_pk is None:
            is_pk = self.state.phase is GamePhase.VOTE_PK_SPEECH
        return await self.coordinator.open_speech(queue, is_pk=is_pk)

    async def next_speech(self) -> SerialSpeechResult:
        return await self.coordinator.run_next_speech()

    async def retry_speech(self) -> SerialSpeechResult:
        return await self.coordinator.retry_speech()

    async def close_speech(self) -> GameState:
        if self.state.phase is GamePhase.VOTE_PK_SPEECH:
            return await self.coordinator.advance_from_pk_speech()
        return await self.coordinator.advance_from_speech()

    async def open_vote(self, *, is_pk: bool | None = None) -> DayVoteProgress:
        """Open the current vote branch, inferring PK after a tied vote."""

        if is_pk is None:
            is_pk = self.state.phase is GamePhase.VOTE_PK
        return await self.coordinator.open_vote(is_pk=is_pk)

    async def next_vote(self, seat: int | None = None) -> VoteTurnResult:
        return await self.coordinator.run_next_vote(seat=seat)

    async def retry_vote(self, seat: int | None = None) -> VoteTurnResult:
        return await self.coordinator.retry_vote(seat=seat)

    async def collect_vote(self, *, force: bool = False) -> GameState:
        return await self.coordinator.finalize_vote(force=force)

    async def confirm_vote(self) -> GameState:
        return await self.coordinator.confirm_vote()

    async def confirm_exile(self, target_seat: int | None = None) -> GameState:
        return await self.coordinator.confirm_exile(target_seat)

    async def finish(self) -> GameState:
        return await self.coordinator.finish_resolution()

    def progress(self) -> dict[str, object]:
        """Return moderator-safe progress without exposing private ballots."""

        coordinator = self.coordinator
        speech = coordinator.speech_progress
        vote = coordinator.vote_progress
        payload: dict[str, object] = {
            "phase": self.state.phase.value,
            "speech": None,
            "vote": None,
        }
        if speech is not None:
            # ``DaySpeechProgress.queue`` is the queue captured when the
            # coordinator opened the window.  After a speech commit it is a
            # historical snapshot and can still contain seats that have
            # already spoken.  The authoritative queue lives in GameState;
            # use it for recovery and runner decisions so an exhausted queue
            # is reported as empty instead of scheduling one more turn.
            current_queue = self.state.current_queue
            payload["speech"] = {
                "phase": speech.phase.value,
                "queue": list(current_queue if current_queue is not None else speech.queue.seats),
                "is_pk": speech.is_pk,
            }
        if vote is not None:
            payload["vote"] = {
                "window_id": vote.window.window_id,
                "status": vote.window.model_dump(mode="json"),
                "visibility_during_collection": vote.visibility_during_collection,
                "is_pk": vote.is_pk,
            }
        return payload


__all__ = ["ModeratorDayFlow"]
