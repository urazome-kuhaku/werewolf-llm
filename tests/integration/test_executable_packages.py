"""Executable plans and merged actions stay inside compiled game snapshots."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from werewolf.game import actions as actions_module
from werewolf.knowledge.compiled_store import (
    CompiledKnowledgeStore,
    CorruptCompiledPackageError,
)
from werewolf.knowledge.compiler import CompiledKnowledgePackage, KnowledgePackageCompiler
from werewolf.knowledge.package_loader import (
    KnowledgePackage,
    KnowledgePackageExecutionError,
    KnowledgePackageLoader,
)
from werewolf.knowledge.refs import VersionedRef
from werewolf.knowledge.runtime_loader import load_runtime_knowledge_bundle_from_snapshot
from werewolf.knowledge.snapshot import KnowledgeSnapshotBuilder
from werewolf.rules.compiler import ExecutionCompilerError
from werewolf.rules.interpreter import plan as plan_rules
from werewolf.rules.models import AbilityInstance, PlayerObservation, RuleObservation, SkillRequest

PROJECT_ROOT = Path(__file__).parents[2]
PUBLISHED_ROOT = PROJECT_ROOT / "vault" / "published"
BOARD_REF = "classic_12_seer_witch_hunter_idiot@1.0.0"
BOARD_ID = "classic_12_seer_witch_hunter_idiot"


async def _load_classic() -> tuple[KnowledgePackage, CompiledKnowledgePackage]:
    package = await KnowledgePackageLoader(PUBLISHED_ROOT).load(BOARD_REF)
    return package, KnowledgePackageCompiler().compile(package)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _as_historical_schema_one(
    package: CompiledKnowledgePackage,
) -> CompiledKnowledgePackage:
    """Rebuild the exact pre-executable package envelope used by older games."""

    payload = json.loads(package.canonical_package_json)
    payload["schema_version"] = 1
    payload.pop("executable")
    manifest = payload["manifest"]
    manifest["schema_version"] = 1
    manifest.pop("execution_sha256")
    package_json = _canonical(payload)
    manifest_json = _canonical(manifest)
    return replace(
        package,
        package_payload=payload,
        manifest_payload=manifest,
        canonical_package_json=package_json,
        canonical_manifest_json=manifest_json,
        package_identity=hashlib.sha256(package_json.encode("utf-8")).hexdigest(),
        manifest_sha256=hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
        execution=None,
        action_registry=None,
        execution_source=None,
        execution_source_sha256=None,
    )


def _declared_envelope(package: CompiledKnowledgePackage) -> dict[str, object]:
    return {
        "schema_version": 1,
        "execution": package.execution.model_dump(mode="json"),
        "action_definitions": [
            {
                "action_code": 203,
                "action_name": "EXILE_RESOLVE",
                "target_policy": "candidate",
                "target_count": 1,
            }
        ],
    }


def _ref(source: str, name: str) -> dict[str, object]:
    return {"op": "ref", "source": source, "name": name}


def _literal(value: object) -> dict[str, object]:
    return {"op": "literal", "value": value}


def _compare(op: str, left: object, right: object) -> dict[str, object]:
    return {"op": op, "left": left, "right": right}


def _novel_skill_envelope(
    package: CompiledKnowledgePackage,
    *,
    board_version: str = "1.0.0",
) -> dict[str, object]:
    """Add one never-before-named, two-target skill using package data only."""

    envelope = _declared_envelope(package)
    execution = cast(dict[str, Any], envelope["execution"])
    execution["board_version"] = board_version
    cast(list[dict[str, Any]], execution["actions"]).append(
        {"action_code": 777, "action_id": "MOON_ECHO", "allow_pass": True}
    )
    selector = {
        "op": "select",
        "source": "players",
        "where": {
            "op": "and",
            "values": [
                _compare("eq", _ref("item", "alive"), _literal(True)),
                _compare("ne", _ref("item", "seat"), _ref("actor", "seat")),
            ],
        },
        "map": _ref("item", "seat"),
    }
    actor_selector = {
        "op": "select",
        "source": "players",
        "where": _compare("eq", _ref("item", "role_id"), _literal("seer")),
        "map": _ref("item", "seat"),
    }
    skill_id = "moon_echo_never_seen_before"
    cast(list[dict[str, Any]], execution["state_declarations"]).append(
        {
            "skill_id": skill_id,
            "key": "last_actor",
            "value_type": "nullable_seat",
            "initial": None,
        }
    )
    cast(list[dict[str, Any]], execution["skills"]).append(
        {
            "skill_id": skill_id,
            "action_code": 777,
            "mode": "PLAYER",
            "grants": [{"grant_id": "seer_moon_echo", "actor_selector": actor_selector}],
            "timing": ["NIGHT_ACTION"],
            "condition": _compare(
                "ne",
                _ref("skill_state", "last_actor"),
                _ref("actor", "seat"),
            ),
            "targets": {
                "min_targets": 2,
                "max_targets": 2,
                "selector": selector,
                "allow_self": False,
            },
            "usage": {
                "max_uses": 1,
                "scope": "ROUND",
                "pass_records": True,
                "pass_updates_history": True,
            },
            "effects": [
                {
                    "effect_id": "moon_echo_facts",
                    "effect_type": "FACT",
                    "target": _ref("target", "seat"),
                    "fact_type": "moon_echo_targeted",
                }
            ],
            "pass_effects": [
                {
                    "effect_id": "moon_echo_pass_history",
                    "effect_type": "STATE_SET",
                    "state_key": "last_actor",
                    "value": _ref("actor", "seat"),
                }
            ],
        }
    )
    cast(list[dict[str, Any]], envelope["action_definitions"]).append(
        {
            "action_code": 777,
            "action_name": "MOON_ECHO",
            "target_policy": "other_alive",
            "target_count": 2,
        }
    )
    return envelope


def _rewrite_frontmatter(path: Path, update: Any) -> None:
    raw = path.read_text(encoding="utf-8")
    start = raw.index("---\n") + 4
    end = raw.index("\n---\n", start)
    values = yaml.safe_load(raw[start:end])
    assert isinstance(values, dict)
    update(values)
    encoded = yaml.safe_dump(values, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{encoded}---\n{raw[end + 5 :]}", encoding="utf-8")


def _copy_classic_as_v2(root: Path) -> None:
    shutil.copytree(PUBLISHED_ROOT, root)
    board_path = root / "boards" / BOARD_ID / "1.0.0" / "board.md"
    versioned_board_path = root / "boards" / BOARD_ID / "2.0.0"
    versioned_board_path.mkdir(parents=True)
    shutil.copy2(board_path, versioned_board_path / "board.md")
    new_board_ref = f"{BOARD_ID}@2.0.0"

    def update_board(values: dict[str, Any]) -> None:
        values["version"] = "2.0.0"
        cast(dict[str, Any], values["reading_plan"])["board_ref"] = new_board_ref

    _rewrite_frontmatter(versioned_board_path / "board.md", update_board)
    for role_path in (root / "roles").glob("*/*/role.md"):

        def add_board_ref(values: dict[str, Any]) -> None:
            refs = cast(list[str], values["board_compatibility"])
            if new_board_ref not in refs:
                refs.append(new_board_ref)

        _rewrite_frontmatter(role_path, add_board_ref)
    for kind, filename, field in (
        ("mechanics", "mechanic.md", "board_refs"),
        ("interactions", "interaction.md", "board_refs"),
    ):
        for document_path in (root / kind).glob(f"*/1.0.0/{filename}"):

            def add_board_ref(values: dict[str, Any], field_name: str = field) -> None:
                refs = cast(list[str], values[field_name])
                if new_board_ref not in refs:
                    refs.append(new_board_ref)

            _rewrite_frontmatter(document_path, add_board_ref)


@pytest.mark.asyncio
async def test_classic_compatibility_plan_and_registry_survive_source_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, compiled = await _load_classic()
    assert compiled.execution is not None
    assert compiled.action_registry is not None
    assert compiled.execution_source == "compat:classic-12-audited-2026-10-03"

    store = CompiledKnowledgeStore(tmp_path / "compiled")
    await store.publish(compiled)
    snapshot = await KnowledgeSnapshotBuilder(store, tmp_path / "games").create(
        "classic-freeze",
        BOARD_REF,
    )
    shutil.rmtree(store.root)

    def fail_live_registry(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("snapshot restore consulted the live action registry")

    monkeypatch.setattr(actions_module, "load_action_registry", fail_live_registry)
    restored = await load_runtime_knowledge_bundle_from_snapshot(snapshot)

    assert restored.package.package_identity == compiled.package_identity
    assert restored.package.execution == compiled.execution
    assert restored.package.action_registry == compiled.action_registry
    assert restored.package.execution_source == compiled.execution_source
    assert restored.package.action_registry.get(203).action_name == "EXILE_RESOLVE"


@pytest.mark.asyncio
async def test_historical_schema_one_restores_pinned_compat_without_live_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, current = await _load_classic()
    historical = _as_historical_schema_one(current)
    store = CompiledKnowledgeStore(tmp_path / "compiled")
    await store.publish(historical)
    snapshot = await KnowledgeSnapshotBuilder(store, tmp_path / "games").create(
        "classic-history",
        BOARD_REF,
    )
    shutil.rmtree(store.root)

    def fail_live_registry(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("legacy snapshot restore consulted the live action registry")

    monkeypatch.setattr(actions_module, "load_action_registry", fail_live_registry)
    restored = await load_runtime_knowledge_bundle_from_snapshot(snapshot)

    assert restored.package.package_identity == historical.package_identity
    assert restored.package.package_payload["schema_version"] == 1
    assert "executable" not in restored.package.package_payload
    assert restored.package.execution is not None
    assert restored.package.execution_source == "compat:classic-12-audited-2026-10-03"
    assert restored.package.action_registry is not None
    assert restored.package.action_registry.get(203).action_name == "EXILE_RESOLVE"


@pytest.mark.asyncio
async def test_execution_yaml_is_loaded_and_package_actions_are_merged(
    tmp_path: Path,
) -> None:
    _source, compiled = await _load_classic()
    published_copy = tmp_path / "published"
    shutil.copytree(PUBLISHED_ROOT, published_copy)
    envelope = _declared_envelope(compiled)
    execution_path = published_copy / "boards" / BOARD_ID / "1.0.0" / "execution.yaml"
    execution_path.write_text(
        yaml.safe_dump(envelope, sort_keys=True),
        encoding="utf-8",
    )

    loaded = await KnowledgePackageLoader(published_copy).load(BOARD_REF)
    declared = KnowledgePackageCompiler().compile(loaded)

    assert declared.execution_source == "declared"
    assert declared.execution is not None
    assert declared.action_registry is not None
    assert declared.action_registry.get(203).action_name == "EXILE_RESOLVE"
    assert (
        declared.execution_source_sha256
        == hashlib.sha256(_canonical(envelope).encode("utf-8")).hexdigest()
    )


@pytest.mark.asyncio
async def test_compiler_rejects_unsupported_expressions_and_action_conflicts() -> None:
    source, compiled = await _load_classic()
    declared = _declared_envelope(compiled)
    execution = declared["execution"]
    assert isinstance(execution, dict)
    skills = execution["skills"]
    assert isinstance(skills, list)

    unsupported = json.loads(json.dumps(declared))
    unsupported_execution = unsupported["execution"]
    unsupported_skills = unsupported_execution["skills"]
    witch_heal = next(item for item in unsupported_skills if item["skill_id"] == "witch_heal")
    witch_heal["condition"] = {"op": "eval", "value": "true"}
    with pytest.raises(ExecutionCompilerError):
        KnowledgePackageCompiler().compile(replace(source, execution_definition=unsupported))

    conflict = json.loads(json.dumps(declared))
    conflict["action_definitions"][0]["action_name"] = "OTHER_ACTION"
    with pytest.raises(ExecutionCompilerError, match="disagrees with registry"):
        KnowledgePackageCompiler().compile(replace(source, execution_definition=conflict))


@pytest.mark.asyncio
async def test_schema_two_store_rejects_missing_execution_artifact(tmp_path: Path) -> None:
    _source, compiled = await _load_classic()
    store = CompiledKnowledgeStore(tmp_path / "compiled")
    package_dir = await store.publish(compiled)
    (package_dir / "execution.json").unlink()

    with pytest.raises(CorruptCompiledPackageError):
        await store.load(VersionedRef(id=BOARD_ID, version="1.0.0"))


@pytest.mark.asyncio
async def test_execution_yaml_rejects_duplicate_mapping_keys(tmp_path: Path) -> None:
    published_copy = tmp_path / "published"
    shutil.copytree(PUBLISHED_ROOT, published_copy)
    execution_path = published_copy / "boards" / BOARD_ID / "1.0.0" / "execution.yaml"
    execution_path.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")

    with pytest.raises(KnowledgePackageExecutionError, match="valid UTF-8 YAML"):
        await KnowledgePackageLoader(published_copy).load(BOARD_REF)


@pytest.mark.asyncio
async def test_compiler_rejects_invalid_execution_references_and_bindings() -> None:
    source, compiled = await _load_classic()
    invalid_cases = (
        ("timing", "unsupported timing values"),
        ("grant", "ability grant IDs must be unique"),
        ("state", "undeclared skill state reference"),
        ("pass_state", "PASS state update writes undeclared state"),
        ("pass_disabled", "declares PASS behavior but its action does not allow PASS"),
        ("item_source", "unknown or untyped reference"),
        ("action_count", "target bounds disagree with action"),
    )
    for case, message in invalid_cases:
        envelope = _novel_skill_envelope(compiled)
        execution = cast(dict[str, Any], envelope["execution"])
        skill = next(
            item
            for item in cast(list[dict[str, Any]], execution["skills"])
            if item["skill_id"] == "moon_echo_never_seen_before"
        )
        if case == "timing":
            skill["timing"] = ["UNDECLARED_TIMING"]
        elif case == "grant":
            skill["grants"][0]["grant_id"] = "seer_inspect"
        elif case == "state":
            skill["condition"]["left"]["name"] = "missing_state"
        elif case == "pass_state":
            skill["pass_effects"][0]["state_key"] = "missing_state"
        elif case == "pass_disabled":
            action_spec = next(
                item
                for item in cast(list[dict[str, Any]], execution["actions"])
                if item["action_code"] == 777
            )
            action_spec["allow_pass"] = False
        elif case == "item_source":
            skill["targets"]["selector"]["source"] = "facts"
        else:
            action = next(
                item
                for item in cast(list[dict[str, Any]], envelope["action_definitions"])
                if item["action_code"] == 777
            )
            action["target_count"] = 1
        with pytest.raises(ExecutionCompilerError, match=message):
            KnowledgePackageCompiler().compile(replace(source, execution_definition=envelope))


@pytest.mark.asyncio
async def test_unknown_skill_action_data_compiles_restores_and_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, base = await _load_classic()
    envelope = _novel_skill_envelope(base)
    declared = KnowledgePackageCompiler().compile(replace(source, execution_definition=envelope))
    assert declared.execution is not None
    assert len(declared.execution.package_id) == 64
    assert declared.execution.board_version == "1.0.0"
    assert declared.action_registry is not None
    assert declared.action_registry.get(777).action_name == "MOON_ECHO"

    store = CompiledKnowledgeStore(tmp_path / "compiled")
    await store.publish(declared)
    snapshot = await KnowledgeSnapshotBuilder(store, tmp_path / "games").create(
        "novel-skill",
        BOARD_REF,
    )
    shutil.rmtree(store.root)

    def fail_live_registry(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("snapshot restore consulted the live action registry")

    monkeypatch.setattr(actions_module, "load_action_registry", fail_live_registry)
    restored = await load_runtime_knowledge_bundle_from_snapshot(snapshot)
    execution = restored.package.execution
    assert execution is not None
    skill = next(
        item for item in execution.skills if item.skill_id == "moon_echo_never_seen_before"
    )
    assert skill.action_code == 777
    assert execution.state_declarations[0].key == "last_actor"
    assert skill.pass_effects[0].state_key == "last_actor"

    observation = RuleObservation(
        board_id=BOARD_ID,
        board_version="1.0.0",
        revision=1,
        round_number=1,
        timing="NIGHT_ACTION",
        players=(
            PlayerObservation(seat=1, role_id="seer", faction_id="GOOD"),
            PlayerObservation(seat=2, role_id="wolf", faction_id="WOLF"),
            PlayerObservation(seat=3, role_id="villager", faction_id="GOOD"),
        ),
        ability_instances=(
            AbilityInstance(
                ability_instance_id="seer-moon-echo",
                skill_id="moon_echo_never_seen_before",
                actor_seat=1,
                grant_id="seer_moon_echo",
            ),
        ),
    )
    request = SkillRequest(
        request_id="moon-echo-1",
        ability_instance_id="seer-moon-echo",
        skill_id="moon_echo_never_seen_before",
        action_code=777,
        actor_seat=1,
        targets=(2, 3),
    )
    batch = plan_rules(execution, observation, (request,))
    assert batch.dispositions[0].status == "ACCEPTED"
    assert {fact.target_seat for fact in batch.facts} == {2, 3}

    pass_request = request.model_copy(
        update={"request_id": "moon-echo-pass", "targets": (), "passed": True}
    )
    pass_batch = plan_rules(execution, observation, (pass_request,))
    assert pass_batch.dispositions[0].status == "PASSED"
    assert pass_batch.state_updates[0].key == "last_actor"

    disabled_actions = tuple(
        item.model_copy(update={"allow_pass": False}) if item.action_code == 777 else item
        for item in execution.actions
    )
    disabled_execution = execution.model_copy(update={"actions": disabled_actions})
    rejected_pass = plan_rules(disabled_execution, observation, (pass_request,))
    assert rejected_pass.dispositions[0].status == "REJECTED"
    assert rejected_pass.dispositions[0].reason == "pass_not_allowed"


@pytest.mark.asyncio
async def test_old_and_new_execution_snapshots_coexist_after_registry_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source, old_package = await _load_classic()
    published_v2 = tmp_path / "published-v2"
    _copy_classic_as_v2(published_v2)
    envelope = _novel_skill_envelope(old_package, board_version="2.0.0")
    execution_path = published_v2 / "boards" / BOARD_ID / "2.0.0" / "execution.yaml"
    execution_path.write_text(yaml.safe_dump(envelope, sort_keys=True), encoding="utf-8")
    new_source = await KnowledgePackageLoader(published_v2).load(f"{BOARD_ID}@2.0.0")
    new_package = KnowledgePackageCompiler().compile(new_source)

    store = CompiledKnowledgeStore(tmp_path / "compiled")
    await store.publish(old_package)
    await store.publish(new_package)
    snapshots = KnowledgeSnapshotBuilder(store, tmp_path / "games")
    old_snapshot = await snapshots.create("old-game", BOARD_REF)
    new_snapshot = await snapshots.create("new-game", f"{BOARD_ID}@2.0.0")
    old_execution_id = old_package.execution.package_id
    new_execution_id = new_package.execution.package_id
    assert old_execution_id != new_execution_id

    shutil.rmtree(store.root)

    def fail_live_registry(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("snapshot restore consulted the refreshed global action registry")

    monkeypatch.setattr(actions_module, "load_action_registry", fail_live_registry)
    old_restored = await load_runtime_knowledge_bundle_from_snapshot(old_snapshot)
    new_restored = await load_runtime_knowledge_bundle_from_snapshot(new_snapshot)

    assert old_restored.package.execution is not None
    assert new_restored.package.execution is not None
    assert old_restored.package.execution.package_id == old_execution_id
    assert old_restored.package.execution.board_version == "1.0.0"
    assert all(
        skill.skill_id != "moon_echo_never_seen_before"
        for skill in old_restored.package.execution.skills
    )
    assert new_restored.package.execution.package_id == new_execution_id
    assert new_restored.package.execution.board_version == "2.0.0"
    assert any(
        skill.skill_id == "moon_echo_never_seen_before"
        for skill in new_restored.package.execution.skills
    )
