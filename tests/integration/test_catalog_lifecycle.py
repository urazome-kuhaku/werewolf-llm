"""Catalog CLI, real scenario gate, version pinning, and lease lifecycle checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from werewolf.cli import app  # type: ignore[import-untyped]
from werewolf.domain.admission import CatalogAdmission
from werewolf.knowledge.catalog import (
    CatalogAdmissionError,
    CatalogEntry,
    CatalogError,
    CatalogSelection,
    CatalogStore,
)
from werewolf.moderator.shell import ModeratorShell

PROJECT_ROOT = Path(__file__).parents[2]
SOURCE_PACKAGE = PROJECT_ROOT / "tests" / "fixtures" / "scenario_quasar"
SCENARIO_SOURCE = PROJECT_ROOT / "tests" / "fixtures" / "scenarios"
_RUNNER = CliRunner()


def _candidate(
    tmp_path: Path,
    version: str,
    *,
    scenario_names: tuple[str, ...],
    board_id: str = "quasar_echo_lab",
    change_content: bool = False,
) -> tuple[Path, tuple[Path, ...]]:
    workbench = tmp_path / f"workbench-{version}-{uuid4().hex[:6]}"
    draft = workbench / "draft"
    shutil.copytree(SOURCE_PACKAGE, draft)
    original_board_id = "quasar_echo_lab"
    board_root = draft / "boards" / original_board_id
    if board_id != original_board_id:
        board_root.rename(draft / "boards" / board_id)
        board_root = draft / "boards" / board_id
        for path in draft.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".md", ".yaml"}:
                path.write_text(
                    path.read_text(encoding="utf-8").replace(original_board_id, board_id),
                    encoding="utf-8",
                )
    old_board = board_root / "1.0.0"
    new_board = board_root / version
    if version != "1.0.0":
        old_board.rename(new_board)
        board_file = new_board / "board.md"
        board_file.write_text(
            board_file.read_text(encoding="utf-8")
            .replace("version: 1.0.0", f"version: {version}")
            .replace(f"{board_id}@1.0.0", f"{board_id}@{version}"),
            encoding="utf-8",
        )
        execution_file = new_board / "execution.yaml"
        execution_file.write_text(
            execution_file.read_text(encoding="utf-8").replace(
                "board_version: 1.0.0", f"board_version: {version}"
            ),
            encoding="utf-8",
        )
    elif change_content:
        board_file = old_board / "board.md"
        board_file.write_text(
            board_file.read_text(encoding="utf-8").replace(
                "Quasar Echo Integration Board", "Quasar Echo Changed Content"
            ),
            encoding="utf-8",
        )

    board_ref = f"{board_id}@{version}"
    file_entries = [
        {
            "path": path.relative_to(draft).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(draft.rglob("*"))
        if path.is_file()
    ]
    workbench.mkdir(parents=True, exist_ok=True)
    (workbench / "publish-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": board_id,
                "board_ref": board_ref,
                "status": "CANDIDATE_PENDING_HUMAN_REVIEW",
                "files": file_entries,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    scenarios: list[Path] = []
    for name in scenario_names:
        source = SCENARIO_SOURCE / f"{name}.yaml"
        content = source.read_text(encoding="utf-8").replace(
            f"{original_board_id}@1.0.0", board_ref
        )
        if version != "1.0.0":
            content = content.replace(
                f"scenario_id: {name.replace('quasar-', 'quasar-')}",
                f"scenario_id: {name.replace('quasar-', 'quasar-')}-{version.replace('.', '-')}",
            )
        scenario_path = tmp_path / f"{name}-{version}.yaml"
        scenario_path.write_text(content, encoding="utf-8")
        scenarios.append(scenario_path)
    return workbench, tuple(scenarios)


def _stage(root: Path, ref: str, workbench: Path, scenarios: tuple[Path, ...]) -> Any:
    args = [
        "catalog",
        "stage",
        "--root",
        str(root),
        "--board",
        ref,
        "--workbench",
        str(workbench),
    ]
    for scenario in scenarios:
        args.extend(("--scenario", str(scenario)))
    return _RUNNER.invoke(app, args)


def _activate(root: Path, ref: str) -> Any:
    return _RUNNER.invoke(app, ["catalog", "activate", "--root", str(root), "--board", ref])


def _play_init(root: Path, ref: str, output: Path, game_id: str) -> Any:
    return _RUNNER.invoke(
        app,
        [
            "play",
            "init",
            "--output",
            str(output),
            "--all-scripted",
            "--catalog-root",
            str(root),
            "--board",
            ref,
            "--game-id",
            game_id,
        ],
    )


def _run_scenario(config: Path, scenario: Path, output: Path) -> Any:
    return _RUNNER.invoke(
        app,
        [
            "scenario",
            "run",
            "--config",
            str(config),
            "--scenario",
            str(scenario),
            "--output",
            str(output),
        ],
    )


def test_catalog_cli_runs_real_gates_pins_versions_and_releases_archives(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    v1 = "quasar_echo_lab@1.0.0"
    v2 = "quasar_echo_lab@2.0.0"
    v3 = "quasar_echo_lab@3.0.0"
    scenarios = ("quasar-command-positive", "quasar-source-full")

    workbench, scenario_paths = _candidate(tmp_path, "1.0.0", scenario_names=scenarios)
    staged = _stage(root, v1, workbench, scenario_paths)
    assert staged.exit_code == 0, staged.output
    activated = _activate(root, v1)
    assert activated.exit_code == 0, activated.stdout
    v1_gate = json.loads(activated.stdout)
    assert v1_gate["status"] == "ACTIVE"
    assert [item["status"] for item in v1_gate["scenario_statuses"]] == ["PASS", "PASS"]
    assert v1_gate["covered_player_skill_ids"] == ["quasar_echo_unfamiliar"]

    changed_v1_workbench_before_unload, changed_v1_scenarios_before_unload = _candidate(
        tmp_path,
        "1.0.0",
        scenario_names=scenarios,
        change_content=True,
    )
    changed_before_unload = _stage(
        root,
        v1,
        changed_v1_workbench_before_unload,
        changed_v1_scenarios_before_unload,
    )
    assert changed_before_unload.exit_code == 1
    assert "different immutable identity" in changed_before_unload.stderr

    v2_workbench, v2_scenarios = _candidate(tmp_path, "2.0.0", scenario_names=scenarios)
    staged_v2 = _stage(root, v2, v2_workbench, v2_scenarios)
    assert staged_v2.exit_code == 0, staged_v2.output
    activated_v2 = _activate(root, v2)
    assert activated_v2.exit_code == 0, activated_v2.stdout

    sibling_ref = "quasar_sibling@1.0.0"
    sibling_workbench, sibling_scenarios = _candidate(
        tmp_path,
        "1.0.0",
        board_id="quasar_sibling",
        scenario_names=scenarios,
    )
    staged_sibling = _stage(root, sibling_ref, sibling_workbench, sibling_scenarios)
    assert staged_sibling.exit_code == 0, staged_sibling.output
    activated_sibling = _activate(root, sibling_ref)
    assert activated_sibling.exit_code == 0, activated_sibling.stdout
    store = CatalogStore(root)
    assert store.resolve_active("quasar_echo_lab")[0].ref.format() == v2
    assert store.resolve_active("quasar_sibling")[0].ref.format() == sibling_ref

    # A focused failing scenario executes through the same gate runner and
    # cannot replace the active v2 default.
    v3_workbench, v3_scenarios = _candidate(
        tmp_path,
        "3.0.0",
        scenario_names=("quasar-command-assertion-fail",),
    )
    staged_v3 = _stage(root, v3, v3_workbench, v3_scenarios)
    assert staged_v3.exit_code == 0, staged_v3.stdout
    failed_gate = _activate(root, v3)
    assert failed_gate.exit_code == 1, failed_gate.stdout
    failed_gate_report = json.loads(failed_gate.stdout)
    assert failed_gate_report["status"] == "FAIL"
    assert failed_gate_report["scenario_statuses"] == [
        {"scenario_id": "quasar-command-assertion-fail-3-0-0", "status": "FAIL"}
    ]

    assert store.resolve_active("quasar_echo_lab")[0].ref.format() == v2
    old_entry, old_selection = store.resolve_active(v1)
    assert old_entry.ref.format() == v1
    assert old_selection.catalog_id == store.catalog_id
    wrong_gate = old_selection.model_dump(mode="json")
    wrong_gate["gate_sha256"] = "0" * 64
    with pytest.raises(CatalogError, match="selection differs"):
        store.verify_selection(CatalogSelection.model_validate(wrong_gate, strict=True))

    old_setup = _play_init(
        root,
        v1,
        tmp_path / "old-v1-setup",
        f"catalog-old-v1-{uuid4().hex[:8]}",
    )
    assert old_setup.exit_code == 0, old_setup.output
    old_config = Path(json.loads(old_setup.stdout)["config_path"])
    full_scenario = SCENARIO_SOURCE.parent / "scenarios" / "quasar-source-full.yaml"
    run_report_path = tmp_path / "old-v1-run-report.json"
    finished = _run_scenario(old_config, full_scenario, run_report_path)
    assert finished.exit_code == 0, finished.stdout
    finished_report = json.loads(run_report_path.read_text(encoding="utf-8"))
    assert finished_report["status"] == "PASS"
    assert finished_report["game_result"]["phase"] == "FINISHED"
    assert finished_report["game_result"]["run_status"] == "CLOSED"
    assert finished_report["archive"]["archive_verified"] is True
    assert finished_report["archive"]["snapshot_verified"] is True

    game_id = finished_report["game_result"]["game_id"]
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    closed = registry["tombstones"][game_id]
    assert closed["status"] == "CLOSED_ARCHIVE"
    assert registry["leases"][game_id]["status"] == "RELEASED"
    with pytest.raises(CatalogAdmissionError, match="CLOSED_ARCHIVE"):
        asyncio.run(
            store.acquire_lease(
                selection=old_selection,
                game_id=game_id,
                package_identity=old_entry.package_identity,
            )
        )

    # A selection from one catalog root cannot acquire or release an equal
    # board identity in another root.
    other_store = CatalogStore(tmp_path / "other-catalog")
    assert other_store.catalog_id != store.catalog_id
    with pytest.raises(CatalogError, match="root"):
        asyncio.run(
            other_store.acquire_lease(
                selection=old_selection,
                game_id=f"catalog-foreign-{uuid4().hex[:8]}",
                package_identity=old_entry.package_identity,
            )
        )
    old_admission = CatalogAdmission.model_validate(
        {field: registry["leases"][game_id][field] for field in CatalogAdmission.model_fields},
        strict=True,
    )
    assert not asyncio.run(
        other_store.close_lease_after_archive(
            old_admission,
            game_id,
            archive_id=closed["archive_id"],
            snapshot_id=closed["snapshot_id"],
            final_state_revision=closed["final_state_revision"],
        )
    )

    status = _RUNNER.invoke(
        app,
        ["catalog", "status", "--root", str(root), "--board", "quasar_echo_lab"],
    )
    assert status.exit_code == 0, status.stdout
    assert json.loads(status.stdout)["active"]["quasar_echo_lab"] == "2.0.0"

    retired_v1 = _RUNNER.invoke(app, ["catalog", "retire", "--root", str(root), "--board", v1])
    assert retired_v1.exit_code == 0, retired_v1.stdout
    retired_setup = _play_init(
        root,
        v1,
        tmp_path / "retired-v1-setup",
        f"catalog-retired-v1-{uuid4().hex[:8]}",
    )
    assert retired_setup.exit_code == 1
    retired_entry, _ = store.resolve_active(v2)
    assert retired_entry.ref.format() == v2

    unloaded_v1 = _RUNNER.invoke(app, ["catalog", "unload", "--root", str(root), "--board", v1])
    assert unloaded_v1.exit_code == 0, unloaded_v1.stdout
    changed_v1_workbench, changed_v1_scenarios = _candidate(
        tmp_path,
        "1.0.0",
        scenario_names=scenarios,
        change_content=True,
    )
    changed_same_ref = _stage(root, v1, changed_v1_workbench, changed_v1_scenarios)
    assert changed_same_ref.exit_code == 1
    assert "different immutable identity" in changed_same_ref.stderr

    v2_selection = store.resolve_active(v2)[1]
    retry_game_id = f"catalog-retry-{uuid4().hex[:8]}"
    retry_setup = _play_init(
        root,
        v2,
        tmp_path / "retry-v2-setup",
        retry_game_id,
    )
    assert retry_setup.exit_code == 0, retry_setup.stdout
    retry_config = Path(json.loads(retry_setup.stdout)["config_path"])

    entered_factory = asyncio.Event()

    async def wait_then_never_factory(*_args: object) -> object:
        entered_factory.set()
        await asyncio.Event().wait()

    shell = ModeratorShell(retry_config, runtime_factory=wait_then_never_factory)

    async def cancel_start_then_retry() -> tuple[CatalogAdmission, CatalogAdmission]:
        await shell.execute("new")
        await shell.execute("next")
        await shell.execute("next")
        start_task = asyncio.create_task(shell.start())
        await entered_factory.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert shell.state.catalog_admission is None
        assert shell.state.phase.value == "ASSIGNED"

        first_record = json.loads((root / "registry.json").read_text(encoding="utf-8"))["leases"][
            retry_game_id
        ]
        assert first_record["status"] == "CANCELLED"
        first = CatalogAdmission.model_validate(
            {field: first_record[field] for field in CatalogAdmission.model_fields},
            strict=True,
        )

        shell.runtime_factory = None
        await shell.start()
        second = shell.state.catalog_admission
        assert second is not None
        assert second.lease_generation == first.lease_generation + 1
        assert shell.catalog_selection == v2_selection
        assert not await store.cancel_lease(first, retry_game_id)
        await shell._close_sessions("catalog lifecycle integration cleanup")
        return first, second

    first_lease, second_lease = asyncio.run(cancel_start_then_retry())
    assert second_lease.lease_generation == first_lease.lease_generation + 1
    assert not (root / ".catalog-writer.lock").exists()

    retired_v2 = _RUNNER.invoke(app, ["catalog", "retire", "--root", str(root), "--board", v2])
    assert retired_v2.exit_code == 0, retired_v2.stdout
    refused_unload = _RUNNER.invoke(app, ["catalog", "unload", "--root", str(root), "--board", v2])
    assert refused_unload.exit_code == 1
    assert "live game lease" in refused_unload.stderr
    assert asyncio.run(store.cancel_lease(second_lease, retry_game_id))
    unloaded_v2 = _RUNNER.invoke(app, ["catalog", "unload", "--root", str(root), "--board", v2])
    assert unloaded_v2.exit_code == 0, unloaded_v2.stdout


def test_catalog_writer_lock_waits_for_cancelled_acquisition_and_registry_schema_is_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import werewolf.knowledge.catalog as catalog_module

    root = tmp_path / "catalog-lock"
    store = CatalogStore(root)
    acquire_entered = Event()
    allow_acquire = Event()
    original_acquire = catalog_module._acquire_exclusive

    def delayed_acquire(path: Path) -> None:
        acquire_entered.set()
        if not allow_acquire.wait(timeout=5):
            raise TimeoutError("test lock acquisition was not released")
        original_acquire(path)

    monkeypatch.setattr(catalog_module, "_acquire_exclusive", delayed_acquire)

    async def cancel_lock_wait() -> None:
        async def enter_lock() -> None:
            async with store._writer_lock():
                raise AssertionError("cancelled lock waiter must not enter")

        task = asyncio.create_task(enter_lock())
        await asyncio.to_thread(acquire_entered.wait, 5)
        task.cancel()
        allow_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_lock_wait())
    assert not (root / ".catalog-writer.lock").exists()

    ref = catalog_module.VersionedRef(id="board", version="1.0.0")
    entry = CatalogEntry(
        ref=ref,
        catalog_id=store.catalog_id,
        path=root / "entries" / ref.id / ref.version,
        manifest={},
        stage_digest="a" * 64,
        package_identity="b" * 64,
        gate_sha256="c" * 64,
    )
    monkeypatch.setattr(store, "load_entry", lambda _ref: entry)
    staging = root / ".stage-entry"
    staging.mkdir()
    (staging / "payload").write_text("staged", encoding="utf-8")
    entry.path.parent.mkdir(parents=True)
    registry = catalog_module._registry_template(store.catalog_id)
    stage_write_started = Event()
    allow_stage_write = Event()
    original_write = store._write_registry

    async def delayed_stage_write(value: Any) -> None:
        stage_write_started.set()
        await asyncio.to_thread(allow_stage_write.wait, 5)
        await original_write(value)

    monkeypatch.setattr(store, "_write_registry", delayed_stage_write)

    async def cancel_stage_commit() -> None:
        task = asyncio.create_task(
            catalog_module._await_uncancellable(
                store._commit_stage(
                    staging=staging,
                    destination=entry.path,
                    ref=ref,
                    stage_digest=entry.stage_digest,
                    registry=registry,
                    update_registry=True,
                )
            )
        )
        await asyncio.to_thread(stage_write_started.wait, 5)
        task.cancel()
        allow_stage_write.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_stage_commit())
    assert entry.path.is_dir()
    assert not staging.exists()
    assert store._load_registry()["entry_digests"] == {ref.format(): entry.stage_digest}

    unload_write_started = Event()
    allow_unload_write = Event()

    async def delayed_unload_write(value: Any) -> None:
        unload_write_started.set()
        await asyncio.to_thread(allow_unload_write.wait, 5)
        await original_write(value)

    monkeypatch.setattr(store, "_write_registry", delayed_unload_write)

    async def cancel_unload_commit() -> None:
        task = asyncio.create_task(
            catalog_module._await_uncancellable(
                store._unload_transaction(entry.path, store._load_registry())
            )
        )
        await asyncio.to_thread(unload_write_started.wait, 5)
        task.cancel()
        allow_unload_write.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_unload_commit())
    assert not entry.path.exists()
    assert not tuple(root.glob(".catalog-unload-*"))
    assert store._load_registry()["entry_digests"] == {ref.format(): entry.stage_digest}

    registry = catalog_module._registry_template(store.catalog_id)
    registry["schema_version"] = True
    (root / "registry.json").write_bytes(catalog_module._canonical_json(registry))
    with pytest.raises(CatalogError, match="schema_version"):
        store.status()
