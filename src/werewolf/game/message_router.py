"""Pure authorization and incremental delivery for game events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .events import DeliveryCursor, GameEvent


class DeliverySessionError(ValueError):
    """Raised when a caller uses a cursor from another runtime session."""


class DeliveryCursorError(ValueError):
    """Raised when an acknowledgement candidate is not valid for the peek."""


class MessageRouter:
    """Read-only message router.

    ``events`` and ``cursors`` are snapshots owned by the caller.  The router
    never modifies either collection.  A caller places the cursor returned by
    :meth:`prepare_ack` into the same serialized ``GameManager`` commit as
    the successful runtime result.
    """

    def __init__(
        self,
        events: Iterable[GameEvent],
        cursors: Mapping[int, DeliveryCursor] | None = None,
    ) -> None:
        event_list = tuple(events)
        if any(not isinstance(event, GameEvent) for event in event_list):
            raise TypeError("events must contain GameEvent instances")
        ids = tuple(event.event_id for event in event_list)
        if len(set(ids)) != len(ids):
            raise ValueError("event IDs must be unique")
        if ids != tuple(sorted(ids)):
            raise ValueError("events must be ordered by ascending event_id")
        self._events = event_list
        self._events_by_id = {event.event_id: event for event in event_list}
        self._cursors = dict(cursors or {})
        for seat, cursor in self._cursors.items():
            if not isinstance(seat, int) or seat < 1:
                raise ValueError("cursor keys must be positive seat numbers")
            if not isinstance(cursor, DeliveryCursor):
                raise TypeError("cursors must contain DeliveryCursor instances")

    @property
    def events(self) -> tuple[GameEvent, ...]:
        """Return the immutable event snapshot used by this router."""

        return self._events

    def cursor_for(self, seat: int, *, session_epoch: int | None = None) -> DeliveryCursor:
        """Read a seat cursor, using an empty cursor for a new session."""

        self._validate_seat(seat)
        cursor = self._cursors.get(seat)
        if cursor is None:
            return DeliveryCursor(session_epoch=0 if session_epoch is None else session_epoch)
        if session_epoch is not None and cursor.session_epoch != session_epoch:
            raise DeliverySessionError(
                f"session_epoch mismatch for seat {seat}: "
                f"cursor={cursor.session_epoch}, requested={session_epoch}"
            )
        return cursor

    def peek_delivery(self, seat: int, session_epoch: int | None = None) -> tuple[GameEvent, ...]:
        """Return authorized, unacknowledged events without advancing a cursor.

        An in-flight batch is returned verbatim on retry, which makes a failed
        runtime request safe to repeat.  For a new batch, only events after
        ``committed_event_id`` and addressed to ``seat`` are returned.
        """

        cursor = self.cursor_for(seat, session_epoch=session_epoch)
        if session_epoch is None:
            session_epoch = cursor.session_epoch
        if cursor.in_flight_event_ids:
            return tuple(
                self._events_by_id[event_id]
                for event_id in cursor.in_flight_event_ids
                if event_id in self._events_by_id and seat in self._events_by_id[event_id].audience
            )
        return tuple(
            event
            for event in self._events
            if event.event_id > cursor.committed_event_id and seat in event.audience
        )

    def prepare_ack(
        self,
        seat: int,
        session_epoch: int,
        *,
        request_id: str,
        event_ids: tuple[int, ...] | None = None,
    ) -> DeliveryCursor:
        """Build the cursor candidate to commit with a successful request."""

        if not request_id:
            raise DeliveryCursorError("request_id must not be empty")
        cursor = self.cursor_for(seat, session_epoch=session_epoch)
        visible = self.peek_delivery(seat, session_epoch)
        visible_ids = tuple(event.event_id for event in visible)
        selected = visible_ids if event_ids is None else event_ids
        if tuple(sorted(set(selected))) != tuple(selected):
            raise DeliveryCursorError("event_ids must be sorted and unique")
        if not set(selected).issubset(visible_ids):
            raise DeliveryCursorError("event_ids must be authorized and currently unacknowledged")
        if selected != visible_ids[: len(selected)]:
            raise DeliveryCursorError("event_ids must form a prefix of the current delivery")
        if cursor.in_flight_event_ids and tuple(selected) != cursor.in_flight_event_ids:
            raise DeliveryCursorError("retry must preserve the in-flight event batch")
        return cursor.with_in_flight(request_id, selected)

    # This descriptive alias is useful to commit code and keeps the fact that
    # routing itself does not mutate state visible at call sites.
    ack_candidate = prepare_ack
    ack_delivery = prepare_ack

    @staticmethod
    def _validate_seat(seat: int) -> None:
        if not isinstance(seat, int) or isinstance(seat, bool) or not 1 <= seat <= 64:
            raise ValueError("seat must be an integer between 1 and 64")


__all__ = ["DeliveryCursorError", "DeliverySessionError", "MessageRouter"]
