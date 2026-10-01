"""Explicit scope for running a workbench candidate as an experiment.

Candidate rules remain rejected by normal runtime loading.  The playable
runner can opt into this scope after it has verified the setup report.  A
``ContextVar`` keeps the exception local to the current async task and avoids
turning an experimental candidate into a process-wide approval.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_EXPERIMENTAL_PREVIEW: ContextVar[bool] = ContextVar(
    "werewolf_experimental_preview",
    default=False,
)


def experimental_preview_enabled() -> bool:
    """Return whether the current task explicitly opted into candidate rules."""

    return _EXPERIMENTAL_PREVIEW.get()


@contextmanager
def experimental_preview() -> Iterator[None]:
    """Temporarily allow pending-review rules in the current task only."""

    token: Token[bool] = _EXPERIMENTAL_PREVIEW.set(True)
    try:
        yield
    finally:
        _EXPERIMENTAL_PREVIEW.reset(token)


experimental_preview_scope = experimental_preview


__all__ = ["experimental_preview", "experimental_preview_enabled", "experimental_preview_scope"]
