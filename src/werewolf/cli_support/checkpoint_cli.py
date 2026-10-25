"""Synchronous CLI adapter for private checkpoint diagnostics."""

from __future__ import annotations

import asyncio
from pathlib import Path

from werewolf.moderator.shell import ModeratorShell


def verify_private_checkpoint(
    config_path: str | Path,
    selector: str,
) -> dict[str, object]:
    """Verify one private waiting-choice checkpoint without starting sessions."""

    async def verify() -> dict[str, object]:
        shell = ModeratorShell(config_path)
        return await shell.checkpoint_verify(selector)

    return asyncio.run(verify())


__all__ = ["verify_private_checkpoint"]
