"""Tests for controlled paths and atomic persistence primitives."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from werewolf.persistence import (
    PathSecurityError,
    atomic_write_bytes,
    atomic_write_text,
    resolve_contained_path,
)


def test_resolve_contained_path_accepts_relative_child(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()

    resolved = resolve_contained_path(root, Path("nested") / "state.json")

    assert resolved == root.resolve() / "nested" / "state.json"


@pytest.mark.parametrize("child", ["../outside", "nested/../../outside", "/outside"])
def test_resolve_contained_path_rejects_escape_syntax(
    tmp_path: Path,
    child: str,
) -> None:
    root = tmp_path / "storage"
    root.mkdir()

    with pytest.raises(PathSecurityError):
        resolve_contained_path(root, child)


def test_resolve_contained_path_rejects_windows_drive_and_parent_syntax(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    root.mkdir()

    for child in (r"C:\outside\state.json", r"nested\..\..\outside"):
        with pytest.raises(PathSecurityError):
            resolve_contained_path(root, child)


def test_resolve_contained_path_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(PathSecurityError):
        resolve_contained_path(root, Path("linked") / "state.json")


@pytest.mark.asyncio
async def test_atomic_write_bytes_replaces_target_without_temp_artifacts(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")

    await atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"new"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


@pytest.mark.asyncio
async def test_atomic_write_text_uses_utf8_and_lf(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"

    await atomic_write_text(target, "第一行\r\n第二行\r第三行\n")

    assert target.read_bytes() == "第一行\n第二行\n第三行\n".encode()


@pytest.mark.asyncio
async def test_atomic_write_cleans_temp_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"keep")

    def fail_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("werewolf.persistence.atomic.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        await atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"keep"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []
