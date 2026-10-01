"""Load and compile the isolated White Wolf King plus Guard candidate."""

from __future__ import annotations

from pathlib import Path

import pytest

from werewolf.game.actions import load_action_registry
from werewolf.knowledge.action_contract import validate_role_action_contract
from werewolf.knowledge.compiler import KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackageLoader

CANDIDATE_ROOT = (
    Path(__file__).parents[2] / "vault" / "_workbench" / "white_wolf_guard_20260930" / "draft"
)
BOARD_REF = "classic_12_white_wolf_guard@1.0.0"


@pytest.mark.asyncio
async def test_white_wolf_guard_candidate_loads_and_compiles() -> None:
    package = await KnowledgePackageLoader(CANDIDATE_ROOT).load(BOARD_REF)
    compiled = KnowledgePackageCompiler().compile(package)

    assert package.board.model.board_id == "classic_12_white_wolf_guard"
    assert package.board.model.seat_count == 12
    bindings = {binding.role_ref.id: binding.count for binding in package.board.model.role_bindings}
    assert bindings["white_wolf_king"] == 1
    assert bindings["guard"] == 1
    white_wolf_king = package.roles["white_wolf_king"].model
    guard = package.roles["guard"].model
    assert white_wolf_king.abilities[0].action_code == 107
    assert white_wolf_king.abilities[0].timing.value == "DAY_SPEECH"
    assert guard.abilities[0].action_code == 106
    assert guard.abilities[0].timing.value == "NIGHT_ACTION"
    validate_role_action_contract(
        {role_id: record.model for role_id, record in package.roles.items()},
        load_action_registry(),
    )
    assert compiled.package_id == BOARD_REF
