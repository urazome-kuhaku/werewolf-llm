"""Build an isolated, experimental game setup for the classic board.

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
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from werewolf.game import load_action_registry
from werewolf.knowledge.action_contract import validate_role_action_contract
from werewolf.knowledge.compiled_store import CompiledKnowledgeStore
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


def _validate_manifest(workbench: Path) -> tuple[dict[str, object], str, Path]:
    manifest_path = workbench / "publish-manifest.json"
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaySetupError("classic candidate publish-manifest.json could not be read") from exc
    if not isinstance(manifest, dict):
        raise PlaySetupError("classic candidate manifest must be a JSON object")
    if manifest.get("schema_version") != 1:
        raise PlaySetupError("classic candidate manifest has an unsupported schema version")
    if manifest.get("candidate_id") != CLASSIC_CANDIDATE_ID:
        raise PlaySetupError("classic candidate manifest has an unexpected candidate ID")
    if manifest.get("board_ref") != CLASSIC_BOARD_REF:
        raise PlaySetupError("classic candidate manifest has an unexpected board reference")
    if manifest.get("status") != _CANDIDATE_STATUS:
        raise PlaySetupError("classic candidate is not in the expected pending-human-review status")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise PlaySetupError("classic candidate manifest has no dependency file list")
    seen_paths: set[str] = set()
    draft_root = (workbench / "draft").resolve()
    for entry in files:
        if not isinstance(entry, dict):
            raise PlaySetupError("classic candidate manifest contains an invalid file entry")
        relative = entry.get("path")
        expected = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or relative in seen_paths
        ):
            raise PlaySetupError("classic candidate manifest contains an unsafe file path")
        seen_paths.add(relative)
        if not isinstance(expected, str) or len(expected) != 64:
            raise PlaySetupError("classic candidate manifest contains an invalid file digest")
        try:
            path = resolve_contained_path(draft_root, relative)
        except PathSecurityError as exc:
            raise PlaySetupError(
                "classic candidate manifest contains a path outside draft"
            ) from exc
        if path.is_symlink() or not path.is_file():
            raise PlaySetupError(f"classic candidate dependency is missing: {relative}")
        if _sha256(path) != expected:
            raise PlaySetupError(f"classic candidate dependency hash mismatch: {relative}")
    return manifest, hashlib.sha256(raw).hexdigest(), workbench / "draft"


def _validate_inputs(
    *,
    pi_seats: Sequence[int],
    provider: str,
    model: str,
    reasoning: str,
    game_id: str,
    all_scripted: bool,
) -> tuple[int, ...]:
    seats_input = tuple(pi_seats)
    if len(seats_input) != len(set(seats_input)):
        raise PlaySetupError("Pi seats must not contain duplicates")
    seats = tuple(sorted(seats_input))
    if any(type(seat) is not int or not 1 <= seat <= 12 for seat in seats):
        raise PlaySetupError("Pi seats must be unique integers from 1 through 12")
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
    source_manifest_sha256: str,
) -> dict[str, object]:
    pi_set = set(pi_seats)
    players: list[dict[str, object]] = []
    for seat in range(1, 13):
        entry: dict[str, object] = {
            "seat": seat,
            "runtime": "pi" if seat in pi_set else "scripted",
        }
        if seat in pi_set:
            entry.update({"provider": provider, "model": model, "reasoning": reasoning})
        players.append(entry)
    return {
        "schema_version": 1,
        "play_setup": {
            "status": "EXPERIMENTAL_CANDIDATE",
            "candidate_status": _CANDIDATE_STATUS,
            "source_manifest_sha256": source_manifest_sha256,
            "board_ref": CLASSIC_BOARD_REF,
        },
        "game": {
            "game_id": game_id,
            "board": {"id": CLASSIC_CANDIDATE_ID, "version": "1.0.0"},
            "seed": seed,
            "moderator_mode": "human_assisted",
        },
        "paths": {"compiled_root": "preview/compiled", "games_root": "games"},
        "players": players,
    }


async def _compile_preview(
    source_root: Path, preview_root: Path
) -> tuple[KnowledgePackage, CompiledKnowledgePackage]:
    package = await KnowledgePackageLoader(source_root).load(VersionedRef.parse(CLASSIC_BOARD_REF))
    validate_role_action_contract(
        {role_id: document.model for role_id, document in package.roles.items()},
        load_action_registry(),
    )
    compiled = KnowledgePackageCompiler().compile(package)
    compiled_store = CompiledKnowledgeStore(preview_root / "compiled")
    await compiled_store.publish(compiled)
    await compiled_store.load(CLASSIC_BOARD_REF, expected_board_ref=CLASSIC_BOARD_REF)
    return package, compiled


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
) -> dict[str, object]:
    seats = _validate_inputs(
        pi_seats=pi_seats,
        provider=provider,
        model=model,
        reasoning=reasoning,
        game_id=game_id,
        all_scripted=all_scripted,
    )
    output = output.expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise PlaySetupError(f"output directory already exists: {output}")
    workbench = project_root / "vault" / "_workbench" / CLASSIC_WORKBENCH_DIRNAME
    manifest, source_digest, source_root = _validate_manifest(workbench)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid4().hex}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
        preview_root = staging / "preview"
        package, compiled = await _compile_preview(source_root, preview_root)
        manifest_paths = sorted(
            cast(list[dict[str, object]], manifest["files"]),
            key=lambda item: cast(str, item["path"]),
        )
        package_paths = sorted(document.relative_path for document in package.documents)
        listed_paths = [cast(str, item["path"]) for item in manifest_paths]
        if listed_paths != package_paths:
            raise PlaySetupError(
                "classic candidate manifest dependency list does not match the loaded package"
            )
        config = _game_config(
            game_id=game_id,
            seed=seed,
            pi_seats=seats,
            provider=provider,
            model=model,
            reasoning=reasoning,
            source_manifest_sha256=source_digest,
        )
        _write_text(
            staging / "game.yaml",
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        )
        (staging / "games").mkdir()
        launch_config = output / "game.yaml"
        report: dict[str, object] = {
            "status": "EXPERIMENTAL_CANDIDATE",
            "candidate_status": manifest["status"],
            "board_ref": CLASSIC_BOARD_REF,
            "source_manifest_sha256": source_digest,
            "source_files_verified": len(cast(list[object], manifest["files"])),
            "package_identity": compiled.package_identity,
            "compiled_manifest_sha256": compiled.manifest_sha256,
            "config_path": "game.yaml",
            "preview_compiled_root": "preview/compiled",
            "pi_seats": list(seats),
            "scripted_seats": [seat for seat in range(1, 13) if seat not in set(seats)],
            "action_contract": "validated",
            # The report is copied out of the generated directory frequently.
            # Keep the command directly runnable from the repository root and
            # quote the absolute Windows path so spaces in a workspace path do
            # not turn the config argument into multiple tokens.
            "launch": (
                f'uv run werewolf play run --config "{launch_config}" --experimental-preview'
            ),
        }
        _write_json(staging / "setup-report.json", report)
        os.rename(staging, output)
    except PlaySetupError:
        _remove_tree(staging)
        raise
    except Exception as exc:
        _remove_tree(staging)
        raise PlaySetupError(f"experimental play setup failed: {exc}") from exc
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
) -> dict[str, object]:
    """Build one isolated preview package and a normal moderator config."""

    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
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
        )
    )


__all__ = ["CLASSIC_BOARD_REF", "CLASSIC_CANDIDATE_ID", "PlaySetupError", "build_play_setup"]
