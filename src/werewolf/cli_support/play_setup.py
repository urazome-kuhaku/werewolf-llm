"""Build an isolated, preview game setup from one pinned rules package.

The setup command is intentionally a preparation boundary.  It reads the
candidate workbench package, verifies its manifest and action contract, and
materializes a compiled preview store below the requested output directory.
It never promotes a candidate into ``vault/published`` or writes
``vault/compiled``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from werewolf.knowledge.compiled_store import (
    CompiledKnowledgePackageLoad,
    CompiledKnowledgeStore,
)
from werewolf.knowledge.compiler import CompiledKnowledgePackage, KnowledgePackageCompiler
from werewolf.knowledge.package_loader import KnowledgePackage, KnowledgePackageLoader
from werewolf.knowledge.refs import VersionedRef
from werewolf.persistence import PathSecurityError, resolve_contained_path

CLASSIC_CANDIDATE_ID: Final = "classic_12_seer_witch_hunter_idiot"
CLASSIC_BOARD_REF: Final = f"{CLASSIC_CANDIDATE_ID}@1.0.0"
CLASSIC_WORKBENCH_DIRNAME: Final = "official_12_20260928"
_CANDIDATE_STATUS: Final = "CANDIDATE_PENDING_HUMAN_REVIEW"
_VALID_REASONING: Final = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


class PlaySetupError(ValueError):
    """Raised when an experimental setup cannot be built safely."""


def _canonical_json(value: object) -> bytes:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (serialized + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_tree(path: Path) -> None:
    if path.exists() or path.is_symlink():
        shutil.rmtree(path)


def _validate_manifest(
    workbench: Path,
    *,
    board_ref: VersionedRef,
) -> tuple[dict[str, object], str, Path]:
    manifest_path = workbench / "publish-manifest.json"
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaySetupError("workbench publish-manifest.json could not be read") from exc
    if not isinstance(manifest, dict):
        raise PlaySetupError("workbench manifest must be a JSON object")
    if manifest.get("schema_version") != 1:
        raise PlaySetupError("workbench manifest has an unsupported schema version")
    if manifest.get("candidate_id") != board_ref.id:
        raise PlaySetupError("workbench manifest has an unexpected candidate ID")
    if manifest.get("board_ref") != board_ref.format():
        raise PlaySetupError("workbench manifest has an unexpected board reference")
    if manifest.get("status") != _CANDIDATE_STATUS:
        raise PlaySetupError("workbench source is not pending human review")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise PlaySetupError("workbench manifest has no dependency file list")
    seen_paths: set[str] = set()
    draft_root = (workbench / "draft").resolve()
    for entry in files:
        if not isinstance(entry, dict):
            raise PlaySetupError("workbench manifest contains an invalid file entry")
        relative = entry.get("path")
        expected = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or relative in seen_paths
        ):
            raise PlaySetupError("workbench manifest contains an unsafe file path")
        seen_paths.add(relative)
        if not isinstance(expected, str) or len(expected) != 64:
            raise PlaySetupError("workbench manifest contains an invalid file digest")
        try:
            path = resolve_contained_path(draft_root, relative)
        except PathSecurityError as exc:
            raise PlaySetupError("workbench manifest contains a path outside draft") from exc
        if path.is_symlink() or not path.is_file():
            raise PlaySetupError(f"workbench dependency is missing: {relative}")
        if _sha256(path) != expected:
            raise PlaySetupError(f"workbench dependency hash mismatch: {relative}")
    return manifest, hashlib.sha256(raw).hexdigest(), workbench / "draft"


def _validate_inputs(
    *,
    pi_seats: Sequence[int],
    provider: str,
    model: str,
    reasoning: str,
    game_id: str,
    all_scripted: bool,
    seat_count: int,
) -> tuple[int, ...]:
    seats_input = tuple(pi_seats)
    if len(seats_input) != len(set(seats_input)):
        raise PlaySetupError("Pi seats must not contain duplicates")
    seats = tuple(sorted(seats_input))
    if any(type(seat) is not int or not 1 <= seat <= seat_count for seat in seats):
        raise PlaySetupError(f"Pi seats must be unique integers from 1 through {seat_count}")
    if all_scripted and seats:
        raise PlaySetupError("--all-scripted cannot be combined with --pi-seats")
    if not all_scripted and not seats:
        raise PlaySetupError("provide --pi-seats or use --all-scripted")
    if not provider or any(ch.isspace() for ch in provider) or len(provider) > 256:
        raise PlaySetupError("provider must be a non-empty identifier without spaces")
    if not model or any(ch.isspace() for ch in model) or len(model) > 256:
        raise PlaySetupError("model must be a non-empty identifier without spaces")
    if reasoning not in _VALID_REASONING:
        raise PlaySetupError(
            "reasoning must be one of minimal, low, medium, high, xhigh, max, ultra"
        )
    if (
        not game_id
        or len(game_id) > 64
        or not game_id[0].isalnum()
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in game_id)
    ):
        raise PlaySetupError("game_id must contain only lowercase letters, digits, '-' or '_'")
    return seats


def _game_config(
    *,
    game_id: str,
    seed: int,
    pi_seats: Sequence[int],
    provider: str,
    model: str,
    reasoning: str,
    board_ref: VersionedRef,
    seat_count: int,
    setup_status: str,
    source_kind: str,
    source_digest: str,
    candidate_status: str | None,
) -> dict[str, object]:
    pi_set = set(pi_seats)
    players: list[dict[str, object]] = []
    for seat in range(1, seat_count + 1):
        entry: dict[str, object] = {
            "seat": seat,
            "runtime": "pi" if seat in pi_set else "scripted",
        }
        if seat in pi_set:
            entry.update({"provider": provider, "model": model, "reasoning": reasoning})
        players.append(entry)
    setup: dict[str, object] = {
        "status": setup_status,
        "board_ref": board_ref.format(),
        "source_kind": source_kind,
        "source_digest": source_digest,
    }
    if candidate_status is not None:
        setup["candidate_status"] = candidate_status
        setup["source_manifest_sha256"] = source_digest
    return {
        "schema_version": 1,
        "play_setup": setup,
        "game": {
            "game_id": game_id,
            "board": {"id": board_ref.id, "version": board_ref.version},
            "seed": seed,
            "moderator_mode": "human_assisted",
        },
        "paths": {"compiled_root": "preview/compiled", "games_root": "games"},
        "players": players,
    }


async def _compile_source(
    source_root: Path,
    board_ref: VersionedRef,
) -> tuple[KnowledgePackage, CompiledKnowledgePackage]:
    package = await KnowledgePackageLoader(source_root).load(board_ref)
    compiled = KnowledgePackageCompiler().compile(package)
    _require_execution_package(compiled)
    return package, compiled


def _require_execution_package(compiled: CompiledKnowledgePackage) -> None:
    if compiled.execution is None or compiled.action_registry is None:
        raise PlaySetupError(
            "rules package has no supported frozen execution plan; play init cannot start it"
        )


def _require_execution_load(package: CompiledKnowledgePackageLoad) -> None:
    executable = package.package_payload.get("executable")
    if (
        not isinstance(executable, Mapping)
        or not isinstance(executable.get("execution"), Mapping)
        or not isinstance(executable.get("action_registry"), Mapping)
    ):
        raise PlaySetupError(
            "rules package has no supported frozen execution plan; play init cannot start it"
        )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(_canonical_json(payload))


async def _build(
    output: Path,
    *,
    project_root: Path,
    pi_seats: Sequence[int],
    provider: str,
    model: str,
    reasoning: str,
    game_id: str,
    seed: int,
    all_scripted: bool,
    board_ref: VersionedRef,
    workbench: Path | None,
    published_root: Path | None,
    compiled_root: Path | None,
) -> dict[str, object]:
    output = output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise PlaySetupError(f"output directory already exists: {output}")
    selected_sources = [
        workbench is not None,
        published_root is not None,
        compiled_root is not None,
    ]
    if sum(selected_sources) > 1:
        raise PlaySetupError(
            "choose only one source: --workbench, --published-root, or --compiled-root"
        )
    if not any(selected_sources):
        # Preserve the classic workbench setup for callers using the legacy
        # no-source form.
        workbench = project_root / "vault" / "_workbench" / CLASSIC_WORKBENCH_DIRNAME

    source_kind = "workbench" if workbench is not None else "published"
    if compiled_root is not None:
        source_kind = "compiled"
    manifest: dict[str, object] | None = None
    package: KnowledgePackage | None = None
    compiled: CompiledKnowledgePackage | None = None
    compiled_load: CompiledKnowledgePackageLoad | None = None
    compiled_source_store: CompiledKnowledgeStore | None = None
    source_files_verified = 0
    if workbench is not None:
        manifest, source_digest, source_root = _validate_manifest(
            workbench.expanduser().resolve(),
            board_ref=board_ref,
        )
        package, compiled = await _compile_source(source_root, board_ref)
        source_files_verified = len(cast(list[object], manifest["files"]))
    elif compiled_root is not None:
        compiled_source_store = CompiledKnowledgeStore(compiled_root.expanduser().resolve())
        try:
            compiled_load = await compiled_source_store.load(
                board_ref,
                expected_board_ref=board_ref,
            )
        except Exception as exc:
            raise PlaySetupError(f"compiled rules package could not be verified: {exc}") from exc
        _require_execution_load(compiled_load)
        source_digest = compiled_load.package_identity
        source_files_verified = len(compiled_load.documents)
    else:
        source = published_root or project_root / "vault" / "published"
        try:
            package = await KnowledgePackageLoader(source.expanduser().resolve()).load(board_ref)
        except Exception as exc:
            raise PlaySetupError(f"published rules package could not be loaded: {exc}") from exc
        if any(
            getattr(document.model, "status", None) != "published" for document in package.documents
        ):
            raise PlaySetupError("published root contains a package that is not marked published")
        compiled = KnowledgePackageCompiler().compile(package)
        _require_execution_package(compiled)
        source_digest = hashlib.sha256(
            _canonical_json(
                {
                    "board_ref": board_ref.format(),
                    "documents": {
                        document.relative_path: document.content_sha256
                        for document in package.documents
                    },
                }
            )
        ).hexdigest()
        source_files_verified = len(package.documents)
    if compiled is not None:
        seat_count = _package_seat_count(package, compiled)
    else:
        assert compiled_load is not None
        seat_count = _package_payload_seat_count(compiled_load.package_payload)
    seats = _validate_inputs(
        pi_seats=pi_seats,
        provider=provider,
        model=model,
        reasoning=reasoning,
        game_id=game_id,
        all_scripted=all_scripted,
        seat_count=seat_count,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
        preview_root = staging / "preview"
        compiled_store = CompiledKnowledgeStore(preview_root / "compiled")
        if compiled is not None:
            await compiled_store.publish(compiled)
        else:
            assert compiled_load is not None and compiled_source_store is not None
            shutil.copytree(
                compiled_source_store.package_path(board_ref),
                compiled_store.package_path(board_ref),
            )
        preview_load = await compiled_store.load(board_ref, expected_board_ref=board_ref)
        if (
            compiled_load is not None
            and preview_load.package_identity != compiled_load.package_identity
        ):
            raise PlaySetupError("compiled preview copy does not match the source package")
        if manifest is not None and package is not None:
            manifest_paths = sorted(
                cast(list[dict[str, object]], manifest["files"]),
                key=lambda item: cast(str, item["path"]),
            )
            package_paths = sorted(document.relative_path for document in package.documents)
            listed_paths = [cast(str, item["path"]) for item in manifest_paths]
            if listed_paths != package_paths:
                raise PlaySetupError(
                    "workbench manifest dependency list does not match the loaded package"
                )
        is_candidate = manifest is not None
        setup_status = "EXPERIMENTAL_CANDIDATE" if is_candidate else "FROZEN_RULESET_PREVIEW"
        candidate_status = str(manifest["status"]) if manifest is not None else None
        config = _game_config(
            game_id=game_id,
            seed=seed,
            pi_seats=seats,
            provider=provider,
            model=model,
            reasoning=reasoning,
            board_ref=board_ref,
            seat_count=seat_count,
            setup_status=setup_status,
            source_kind=source_kind,
            source_digest=source_digest,
            candidate_status=candidate_status,
        )
        _write_text(
            staging / "game.yaml",
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        )
        (staging / "games").mkdir()
        launch_config = output / "game.yaml"
        report: dict[str, object] = {
            "status": setup_status,
            "board_ref": board_ref.format(),
            "source_kind": source_kind,
            "source_digest": source_digest,
            "source_files_verified": source_files_verified,
            "package_identity": (
                compiled.package_identity if compiled is not None else preview_load.package_identity
            ),
            "compiled_manifest_sha256": (
                compiled.manifest_sha256 if compiled is not None else preview_load.manifest_sha256
            ),
            "config_path": "game.yaml",
            "preview_compiled_root": "preview/compiled",
            "seat_count": seat_count,
            "pi_seats": list(seats),
            "scripted_seats": [seat for seat in range(1, seat_count + 1) if seat not in set(seats)],
            "action_contract": "compiled",
            # The report is copied out of the generated directory frequently.
            # Keep the command directly runnable from the repository root and
            # quote the absolute Windows path so spaces in a workspace path do
            # not turn the config argument into multiple tokens.
            "launch": f'uv run werewolf play run --config "{launch_config}"'
            + (" --experimental-preview" if is_candidate else ""),
        }
        if candidate_status is not None:
            report["candidate_status"] = candidate_status
            report["source_manifest_sha256"] = source_digest
        _write_json(staging / "setup-report.json", report)
        os.rename(staging, output)
    except PlaySetupError:
        _remove_tree(staging)
        raise
    except Exception as exc:
        _remove_tree(staging)
        raise PlaySetupError(f"play setup failed: {exc}") from exc
    return {**report, "output": str(output), "config_path": str(output / "game.yaml")}


def build_play_setup(
    output: str | Path,
    *,
    pi_seats: Sequence[int] = (),
    provider: str = "github-copilot",
    model: str = "gpt-6-luna",
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = "medium",
    game_id: str = "classic-play-001",
    seed: int = 20260930,
    all_scripted: bool = False,
    project_root: str | Path | None = None,
    board_ref: str = CLASSIC_BOARD_REF,
    workbench: str | Path | None = None,
    published_root: str | Path | None = None,
    compiled_root: str | Path | None = None,
) -> dict[str, object]:
    """Build one isolated preview package from a pinned ruleset source."""

    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    try:
        reference = VersionedRef.parse(board_ref)
    except ValueError as exc:
        raise PlaySetupError("board_ref must be an exact id@version reference") from exc
    if reference.id == "latest" or reference.version == "latest":
        raise PlaySetupError("board_ref must be pinned and must not use latest")
    return asyncio.run(
        _build(
            Path(output),
            project_root=root.resolve(),
            pi_seats=pi_seats,
            provider=provider,
            model=model,
            reasoning=reasoning,
            game_id=game_id,
            seed=seed,
            all_scripted=all_scripted,
            board_ref=reference,
            workbench=Path(workbench) if workbench is not None else None,
            published_root=Path(published_root) if published_root is not None else None,
            compiled_root=Path(compiled_root) if compiled_root is not None else None,
        )
    )


def _package_seat_count(
    package: KnowledgePackage | None,
    compiled: CompiledKnowledgePackage,
) -> int:
    if package is not None:
        count = getattr(package.board.model, "seat_count", None)
    else:
        board = compiled.package_payload.get("board_definition")
        count = board.get("seat_count") if isinstance(board, Mapping) else None
    if type(count) is not int or not 1 <= count <= 64:
        raise PlaySetupError("rules package board has an invalid seat_count")
    return count


def _package_payload_seat_count(payload: Mapping[str, object]) -> int:
    board = payload.get("board_definition")
    count = board.get("seat_count") if isinstance(board, Mapping) else None
    if type(count) is not int or not 1 <= count <= 64:
        raise PlaySetupError("rules package board has an invalid seat_count")
    return count


__all__ = ["CLASSIC_BOARD_REF", "CLASSIC_CANDIDATE_ID", "PlaySetupError", "build_play_setup"]
