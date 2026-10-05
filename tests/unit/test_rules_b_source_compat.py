"""Source compiler compatibility for compiler-derived execution metadata."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import cast

import pytest

from werewolf.domain.enums import GamePhase
from werewolf.knowledge.board import NightWindow
from werewolf.knowledge.package_loader import KnowledgePackage, load_knowledge_package
from werewolf.rules.compiler import ExecutionCompilerError, compile_execution_definition
from werewolf.rules.models import ExecutionPackage

_SOURCE_FIXTURE_PATH = Path(__file__).parents[1] / "integration" / "test_rules_scripted_runtime.py"
_SOURCE_FIXTURE_SPEC = spec_from_file_location(
    "rules_scripted_runtime_fixture", _SOURCE_FIXTURE_PATH
)
if _SOURCE_FIXTURE_SPEC is None or _SOURCE_FIXTURE_SPEC.loader is None:
    raise RuntimeError("could not load the shared source package fixture")
_SOURCE_FIXTURE = module_from_spec(_SOURCE_FIXTURE_SPEC)
_SOURCE_FIXTURE_SPEC.loader.exec_module(_SOURCE_FIXTURE)
SOURCE_BOARD_ID = cast(str, getattr(_SOURCE_FIXTURE, "SOURCE_BOARD_ID"))
_write_source_package = cast(Callable[..., None], getattr(_SOURCE_FIXTURE, "_write_source_package"))


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    return value


def _execution_input(package: KnowledgePackage) -> dict[str, object]:
    raw = cast(dict[str, object], _thaw(package.execution_definition))
    return cast(dict[str, object], raw["execution"])


def _with_execution(
    package: KnowledgePackage,
    execution: dict[str, object],
) -> KnowledgePackage:
    raw = cast(dict[str, object], _thaw(package.execution_definition))
    raw["execution"] = execution
    return replace(package, execution_definition=raw)


async def _source_package(tmp_path: Path) -> KnowledgePackage:
    published_root = tmp_path / "published"
    _write_source_package(
        published_root,
        version="1.0.0",
        target_count=2,
        modes=["strike", "scan"],
    )
    return await load_knowledge_package(
        published_root,
        f"{SOURCE_BOARD_ID}@1.0.0",
    )


@pytest.mark.asyncio
async def test_empty_derived_defaults_match_omitted_source_and_keep_execution_digest(
    tmp_path: Path,
) -> None:
    package = await _source_package(tmp_path)
    explicit_source = _execution_input(package)

    assert explicit_source["player_field_values"] is None
    assert explicit_source["window_metadata"] == []
    assert explicit_source["boundary_policy"] is None

    explicit = compile_execution_definition(package)
    assert explicit is not None
    omitted_source = dict(explicit_source)
    for field_name in ("player_field_values", "window_metadata", "boundary_policy"):
        del omitted_source[field_name]

    omitted = compile_execution_definition(_with_execution(package, omitted_source))
    assert omitted is not None
    assert explicit.execution.package_id == omitted.execution.package_id
    tuple_defaults_source = dict(omitted_source)
    tuple_defaults_source["window_metadata"] = ()
    tuple_defaults = compile_execution_definition(_with_execution(package, tuple_defaults_source))
    assert tuple_defaults is not None
    assert explicit.execution.package_id == tuple_defaults.execution.package_id
    assert (
        explicit.execution.package_id == ExecutionPackage.model_validate(omitted_source).package_id
    )
    assert explicit.execution.player_field_values is None
    assert explicit.execution.window_metadata == ()
    assert explicit.execution.boundary_policy is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    (
        ("player_field_values", {}, "player_field_values is derived"),
        ("player_field_values", {"role_ids": ["quasar_oracle"]}, "player_field_values is derived"),
        ("window_metadata", None, "window_metadata is derived"),
        ("window_metadata", {}, "window_metadata is derived"),
        (
            "window_metadata",
            [{"window_id": "forged", "order": 1, "phase": "NIGHT_ACTION"}],
            "window_metadata is derived",
        ),
        ("boundary_policy", {}, "boundary_policy is derived"),
        ("boundary_policy", {"last_words_enabled": False}, "boundary_policy is derived"),
    ),
)
async def test_source_rejects_nonempty_or_wrongly_shaped_derived_metadata(
    tmp_path: Path,
    field_name: str,
    value: object,
    message: str,
) -> None:
    package = await _source_package(tmp_path)
    execution = _execution_input(package)
    execution[field_name] = value

    with pytest.raises(ExecutionCompilerError, match=message):
        compile_execution_definition(_with_execution(package, execution))


@pytest.mark.asyncio
async def test_source_compiler_derives_complex_windows_player_fields_and_changed_boundaries(
    tmp_path: Path,
) -> None:
    package = await _source_package(tmp_path)
    execution = _execution_input(package)
    skills = cast(list[dict[str, object]], execution["skills"])
    effects = cast(list[dict[str, object]], skills[0]["effects"])
    effects.append(
        {
            "effect_id": "set-citizen-role",
            "effect_type": "PLAYER_FIELD_SET",
            "target": {"op": "ref", "source": "actor", "name": "seat"},
            "value": {"op": "literal", "value": "quasar_citizen"},
            "player_field": "role_id",
        }
    )

    board = package.board.model
    windows = [
        NightWindow(window_id="lobby", order=1, phase=GamePhase.NIGHT_TEAM_CHAT),
        NightWindow(
            window_id="first_action",
            order=2,
            phase=GamePhase.NIGHT_ACTION,
            depends_on=["lobby"],
        ),
        NightWindow(
            window_id="second_action",
            order=3,
            phase=GamePhase.NIGHT_ACTION,
            depends_on=["first_action"],
        ),
        NightWindow(
            window_id="close",
            order=4,
            phase=GamePhase.NIGHT_RESOLVE,
            depends_on=["second_action"],
        ),
    ]
    last_words = board.day_flow.last_words.model_copy(
        update={"enabled": True, "eligible_death_causes": ["quasar_scan_mark"]}
    )
    changed_board = board.model_copy(
        update={
            "night_windows": windows,
            "day_flow": board.day_flow.model_copy(update={"last_words": last_words}),
        }
    )
    changed_package = replace(
        _with_execution(package, execution),
        board=replace(package.board, model=changed_board),
    )

    compiled = compile_execution_definition(changed_package)
    assert compiled is not None
    derived = compiled.execution
    assert tuple(
        (window.window_id, window.order, window.phase, window.depends_on)
        for window in derived.window_metadata
    ) == (
        ("lobby", 1, "NIGHT_TEAM_CHAT", ()),
        ("first_action", 2, "NIGHT_ACTION", ("lobby",)),
        ("second_action", 3, "NIGHT_ACTION", ("first_action",)),
        ("close", 4, "NIGHT_RESOLVE", ("second_action",)),
    )
    assert derived.boundary_policy is not None
    assert derived.boundary_policy.last_words_enabled is True
    assert derived.boundary_policy.eligible_death_causes == ("quasar_scan_mark",)
    assert derived.player_field_values is not None
    assert derived.player_field_values.role_ids == ("quasar_citizen", "quasar_oracle")
    assert derived.player_field_values.faction_ids == ("good", "wolf")
    assert derived.player_field_values.victory_group_ids == ("good", "villager", "wolf")
    assert derived.player_field_values.chat_group_ids == ("wolf",)


@pytest.mark.asyncio
async def test_execution_source_keeps_unknown_fields_strict(tmp_path: Path) -> None:
    package = await _source_package(tmp_path)
    execution = _execution_input(package)
    execution["caller_extension"] = {}

    with pytest.raises(ExecutionCompilerError, match="execution.yaml is invalid"):
        compile_execution_definition(_with_execution(package, execution))
