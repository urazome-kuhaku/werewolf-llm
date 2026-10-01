"""Read-only validation helpers used by the top-level command line interface.

The CLI is deliberately a thin boundary.  It reports stable, non-sensitive
summaries while the knowledge and persistence packages remain responsible for
their full schemas and integrity checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from werewolf.knowledge.board import BoardDefinition
from werewolf.knowledge.frontmatter import FrontmatterParseError, parse_markdown
from werewolf.knowledge.package_loader import load_knowledge_package
from werewolf.knowledge.refs import VersionedRef
from werewolf.persistence.archive import ARCHIVE_MANIFEST_FILENAME, GameArchiveStore
from werewolf.persistence.snapshot import GameSnapshotStore


class DiagnosticFailure(ValueError):
    """Raised when a user supplied file cannot pass a CLI diagnostic."""


_MAX_CONFIG_BYTES = 1 * 1024 * 1024
_MAX_VERSION_OUTPUT_BYTES = 64 * 1024
_VERSION_PATTERN = re.compile(r"\b(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?\b")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|secret|password|credential)\b\s*[:=]\s*)[^,\s}]+"
)
_T = TypeVar("_T")


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    """Read one user supplied file without allowing unbounded diagnostics."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DiagnosticFailure(f"{label} could not be read: {path}") from exc
    if len(data) > limit:
        raise DiagnosticFailure(f"{label} exceeds the {limit} byte diagnostic limit")
    return data


def _safe_error(exc: BaseException) -> str:
    """Return a short error summary with common secret assignments redacted."""

    text = str(exc).strip().replace("\r", " ").replace("\n", " ")
    text = _SECRET_ASSIGNMENT.sub(r"\1<redacted>", text)
    return text[:500] or type(exc).__name__


def _run_async_sync(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run one async persistence check from sync CLI code, including inside a loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _mapping(value: object, *, name: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise DiagnosticFailure(f"{name} must be a mapping")
    return value


def _require_mapping(parent: Mapping[str, object], key: str, *, name: str) -> Mapping[str, object]:
    value = parent.get(key)
    if value is None:
        raise DiagnosticFailure(f"{name}.{key} is required")
    return _mapping(value, name=f"{name}.{key}")


def _positive_int(value: object, *, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise DiagnosticFailure(f"{name} must be a positive integer")


def _load_config(path: Path) -> Mapping[str, object]:
    raw = _read_bounded(path, limit=_MAX_CONFIG_BYTES, label="configuration file")
    try:
        loaded = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DiagnosticFailure(f"configuration YAML is invalid: {_safe_error(exc)}") from exc
    return _mapping(loaded, name="configuration root")


def validate_config_file(path: str | os.PathLike[str]) -> dict[str, object]:
    """Validate the stable V1 configuration envelope without printing values."""

    config_path = Path(path).expanduser().resolve()
    config = _load_config(config_path)
    if config.get("schema_version") != 1:
        raise DiagnosticFailure("configuration schema_version must be 1")

    allowed = {
        "schema_version",
        "play_setup",
        "runtime",
        "research",
        "game",
        "players",
        "logging",
        "paths",
        "scripts",
        "project",
    }
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise DiagnosticFailure(f"configuration has unknown top-level fields: {', '.join(unknown)}")

    runtime_value = config.get("runtime", {})
    runtime = _mapping(runtime_value, name="configuration.runtime")
    pi_value = runtime.get("pi", {})
    pi = _mapping(pi_value, name="configuration.runtime.pi")
    executable = pi.get("executable")
    if executable is not None and (not isinstance(executable, str) or not executable.strip()):
        raise DiagnosticFailure("configuration.runtime.pi.executable must be a string or null")
    compatible = pi.get("compatible_version")
    if compatible is not None and (not isinstance(compatible, str) or not compatible.strip()):
        raise DiagnosticFailure("configuration.runtime.pi.compatible_version must be a string")
    for key in ("auto_compaction", "auto_retry"):
        if key in pi and type(pi[key]) is not bool:
            raise DiagnosticFailure(f"configuration.runtime.pi.{key} must be a boolean")
    unknown_pi = sorted(
        set(pi) - {"executable", "compatible_version", "auto_compaction", "auto_retry"}
    )
    if unknown_pi:
        raise DiagnosticFailure(
            "configuration.runtime.pi has unknown fields: " + ", ".join(unknown_pi)
        )

    research = config.get("research")
    if research is not None:
        research = _mapping(research, name="configuration.research")
        if "min_independent_sources" in research:
            _positive_int(
                research["min_independent_sources"],
                name="research.min_independent_sources",
            )

    game = _require_mapping(config, "game", name="configuration")
    board = _require_mapping(game, "board", name="configuration.game")
    for key in ("id", "version"):
        value = board.get(key)
        if not isinstance(value, str) or not value.strip():
            raise DiagnosticFailure(f"configuration.game.board.{key} must be a non-empty string")
    if "game_id" in game and (not isinstance(game["game_id"], str) or not game["game_id"].strip()):
        raise DiagnosticFailure("configuration.game.game_id must be a non-empty string")
    timeout = game.get("timeout")
    if timeout is not None:
        timeout_map = _mapping(timeout, name="configuration.game.timeout")
        for name, value in timeout_map.items():
            _positive_int(value, name=f"configuration.game.timeout.{name}")

    players = config.get("players")
    if players is not None:
        if type(players) is not list or not players:
            raise DiagnosticFailure("configuration.players must be a non-empty list")
        seen_seats: set[int] = set()
        for index, raw_player in enumerate(players):
            player = _mapping(raw_player, name=f"configuration.players[{index}]")
            unknown_player = sorted(
                set(player) - {"seat", "runtime", "provider", "model", "reasoning"}
            )
            if unknown_player:
                raise DiagnosticFailure(
                    f"configuration.players[{index}] has unknown fields: "
                    + ", ".join(unknown_player)
                )
            seat = player.get("seat")
            if type(seat) is not int or seat < 1 or seat > 64:
                raise DiagnosticFailure(f"configuration.players[{index}].seat must be 1..64")
            if seat in seen_seats:
                raise DiagnosticFailure("configuration.players seat values must be unique")
            seen_seats.add(seat)
            runtime_name = player.get("runtime")
            if runtime_name not in {"pi", "scripted"}:
                raise DiagnosticFailure(
                    f"configuration.players[{index}].runtime must be pi or scripted"
                )
            for key in ("provider", "model"):
                value = player.get(key)
                if runtime_name == "pi" and (not isinstance(value, str) or not value.strip()):
                    raise DiagnosticFailure(
                        f"configuration.players[{index}].{key} is required for pi players"
                    )
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise DiagnosticFailure(
                        f"configuration.players[{index}].{key} must be a non-empty string"
                    )

    for name in ("paths", "scripts"):
        value = config.get(name)
        if value is not None and not isinstance(value, (dict, list)):
            raise DiagnosticFailure(f"configuration.{name} must be a mapping or list")

    return {
        "status": "valid",
        "path": str(config_path),
        "schema_version": 1,
        "board": {"id": board["id"], "version": board["version"]},
        "pi_executable_configured": executable is not None,
    }


def _normalize_board_frontmatter(frontmatter: Mapping[str, object]) -> dict[str, object]:
    """Convert compact ``id@version`` nested refs for strict board parsing."""

    values = dict(frontmatter)
    bindings = values.get("role_bindings", values.get("roles"))
    if isinstance(bindings, list):
        normalized: list[object] = []
        for binding in bindings:
            if isinstance(binding, dict) and isinstance(binding.get("role_ref"), str):
                item = dict(binding)
                item["role_ref"] = VersionedRef.parse(item["role_ref"]).model_dump()
                normalized.append(item)
            else:
                normalized.append(binding)
        if "role_bindings" in values:
            values["role_bindings"] = normalized
        else:
            values["roles"] = normalized
    reading_plan = values.get("reading_plan")
    if isinstance(reading_plan, dict) and isinstance(reading_plan.get("board_ref"), str):
        plan = dict(reading_plan)
        plan["board_ref"] = VersionedRef.parse(plan["board_ref"]).model_dump()
        values["reading_plan"] = plan
    return values


def _package_root_for_board(path: Path) -> Path | None:
    """Return a conventional published root when this board has one."""

    if path.parent.name == "board.md" or path.parent.name == "":
        return None
    version_dir = path.parent
    board_dir = version_dir.parent
    boards_dir = board_dir.parent
    root = boards_dir.parent
    if boards_dir.name != "boards" or not (root / "roles").is_dir():
        return None
    return root


def validate_rules_file(path: str | os.PathLike[str]) -> dict[str, object]:
    """Validate one board Markdown file and, when available, its dependency closure."""

    board_path = Path(path).expanduser().resolve()
    raw = _read_bounded(board_path, limit=_MAX_CONFIG_BYTES, label="board document")
    try:
        parsed = parse_markdown(raw)
        board = BoardDefinition.model_validate(_normalize_board_frontmatter(parsed.frontmatter))
    except (FrontmatterParseError, ValidationError, TypeError, ValueError) as exc:
        raise DiagnosticFailure(f"board validation failed: {_safe_error(exc)}") from exc

    package_status = "not_checked"
    package_root = _package_root_for_board(board_path)
    if package_root is not None:
        try:
            asyncio.run(load_knowledge_package(package_root, board.board_ref))
        except Exception as exc:
            raise DiagnosticFailure(
                f"board dependency validation failed: {_safe_error(exc)}"
            ) from exc
        package_status = "validated"

    return {
        "status": "valid",
        "path": str(board_path),
        "kind": "board",
        "board_id": board.board_id,
        "version": board.version,
        "seat_count": board.seat_count,
        "role_count": len(board.role_bindings),
        "mechanic_count": len(board.mechanic_refs),
        "interaction_count": len(board.interaction_refs),
        "dependency_closure": package_status,
    }


def verify_archive_path(path: str | os.PathLike[str]) -> dict[str, object]:
    """Verify an immutable archive or compatible snapshot directory."""

    archive_path = Path(path).expanduser().resolve()
    archive_manifest_path = archive_path / ARCHIVE_MANIFEST_FILENAME
    if archive_manifest_path.exists() or archive_manifest_path.is_symlink():
        try:
            archive_manifest = _run_async_sync(GameArchiveStore(archive_path).verify(archive_path))
        except Exception as exc:
            raise DiagnosticFailure(f"archive verification failed: {_safe_error(exc)}") from exc
        return {
            "status": "valid",
            "kind": "archive",
            "path": str(archive_path),
            "archive_id": archive_manifest.archive_id,
            "game_id": archive_manifest.game_id,
            "snapshot_id": archive_manifest.final_snapshot_id,
            "snapshot_revision": archive_manifest.final_snapshot_revision,
            "manifest_sha256": archive_manifest.manifest_sha256,
            "file_count": archive_manifest.file_count,
            "total_size": archive_manifest.total_size,
        }

    manifest_path = archive_path / "snapshot_manifest.json"
    raw = _read_bounded(manifest_path, limit=_MAX_CONFIG_BYTES, label="archive manifest")
    try:
        from werewolf.persistence.snapshot import _manifest_from_bytes

        snapshot_manifest = _manifest_from_bytes(raw)
        GameSnapshotStore._verify_directory_sync(
            archive_path,
            snapshot_manifest.game_id,
            hashlib.sha256(raw).hexdigest(),
        )
    except Exception as exc:
        raise DiagnosticFailure(f"archive verification failed: {_safe_error(exc)}") from exc
    return {
        "status": "valid",
        "path": str(archive_path),
        "game_id": snapshot_manifest.game_id,
        "snapshot_id": snapshot_manifest.snapshot_id,
        "snapshot_revision": snapshot_manifest.snapshot_revision,
        "manifest_sha256": snapshot_manifest.manifest_sha256,
    }


def resolve_pi_executable(
    config_path: str | os.PathLike[str] | None = None,
) -> tuple[str | None, str]:
    """Resolve Pi in the documented precedence order without revealing credentials."""

    configured: object | None = None
    if config_path is not None:
        configured = _load_config(Path(config_path).expanduser().resolve())
        runtime = configured.get("runtime")
        if isinstance(runtime, dict) and isinstance(runtime.get("pi"), dict):
            configured = runtime["pi"].get("executable")
        else:
            configured = None
    if isinstance(configured, str) and configured.strip():
        return str(Path(configured).expanduser().resolve()), "config"
    environment = os.environ.get("WEREWOLF_PI_EXECUTABLE")
    if environment and environment.strip():
        return str(Path(environment).expanduser().resolve()), "environment"
    found = shutil.which("pi")
    return (str(Path(found).resolve()), "PATH") if found else (None, "unresolved")


def pi_doctor(config_path: str | os.PathLike[str] | None = None) -> dict[str, object]:
    """Run safe local Pi checks; authentication and account mutation are skipped."""

    executable, source = resolve_pi_executable(config_path)
    checks: dict[str, str] = {
        "executable": "failed" if executable is None else "passed",
        "version": "not_run",
        "rpc_jsonl": "deferred",
        "session_roundtrip": "deferred",
        "steer_abort": "deferred",
        "extension_http_mock": "deferred",
        "restricted_tools": "deferred",
        "auth": "skipped_no_reauth",
        "settled_usage_events": "deferred",
    }
    report: dict[str, object] = {
        "status": "failed" if executable is None else "validating",
        "executable": executable,
        "executable_source": source,
        "checks": checks,
        "credentials": "redacted",
    }
    if executable is None:
        report["error"] = "Pi executable was not found in config, environment, or PATH"
        return report

    target = Path(executable)
    if not target.exists() or not target.is_file():
        checks["executable"] = "failed"
        report["status"] = "failed"
        report["error"] = "resolved Pi executable does not exist"
        return report
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        checks["version"] = "failed"
        report["status"] = "failed"
        report["error"] = _safe_error(exc)
        return report

    output = (completed.stdout + completed.stderr)[:_MAX_VERSION_OUTPUT_BYTES]
    version_text = output.decode("utf-8", errors="replace")
    match = _VERSION_PATTERN.search(version_text)
    version = match.group(0) if match else None
    checks["version"] = "passed" if completed.returncode == 0 and version else "failed"
    report["version"] = version
    report["status"] = "valid" if checks["version"] == "passed" else "failed"
    if checks["version"] != "passed":
        report["error"] = "Pi did not return a parseable version from --version"
    elif version is not None and not version.startswith("0.87."):
        checks["version_compatible"] = "failed"
        report["status"] = "failed"
        report["error"] = "Pi version is outside the supported 0.87.x range"
    else:
        checks["version_compatible"] = "passed"
    return report


def json_report(report: Mapping[str, object]) -> str:
    """Serialize one diagnostic report deterministically for scripts."""

    return json.dumps(report, ensure_ascii=True, sort_keys=True)


__all__ = [
    "DiagnosticFailure",
    "json_report",
    "pi_doctor",
    "resolve_pi_executable",
    "validate_config_file",
    "validate_rules_file",
    "verify_archive_path",
]
