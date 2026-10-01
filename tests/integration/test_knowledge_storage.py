"""Integration tests for the read-only trusted Markdown knowledge store."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from werewolf.knowledge.storage import (
    KnowledgeMarkdownPathError,
    KnowledgeMarkdownStore,
    KnowledgeMarkdownTooLargeError,
)


def _markdown(*, name: str = "女巫", body: str = "# 女巫\n\n正文。\n") -> str:
    return f"---\nname: {name}\nkind: role\n---\n{body}"


@pytest.mark.asyncio
async def test_loads_utf8_markdown_and_returns_original_digest(tmp_path: Path) -> None:
    root = tmp_path / "published"
    document_path = root / "roles" / "witch.md"
    document_path.parent.mkdir(parents=True)
    raw = _markdown().encode("utf-8")
    document_path.write_bytes(raw)

    loaded = await KnowledgeMarkdownStore(root).load(Path("roles") / "witch.md")

    assert loaded.relative_path == "roles/witch.md"
    assert loaded.frontmatter == {"name": "女巫", "kind": "role"}
    assert loaded.parsed.body == "# 女巫\n\n正文。\n"
    assert loaded.body == loaded.parsed.body
    assert loaded.content_sha256 == hashlib.sha256(raw).hexdigest()
    assert loaded.sha256 == loaded.content_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.md",
        "roles/../witch.md",
        "./roles/witch.md",
        "roles//witch.md",
        "roles\\witch.md",
        "C:/outside.md",
        "witch.txt",
        "/absolute.md",
    ],
)
async def test_rejects_unsafe_or_non_markdown_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    root = tmp_path / "published"
    root.mkdir()

    with pytest.raises(KnowledgeMarkdownPathError):
        await KnowledgeMarkdownStore(root).load(relative_path)


@pytest.mark.asyncio
async def test_rejects_documents_over_the_configured_limit(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir()
    (root / "large.md").write_text(_markdown(body="x" * 128), encoding="utf-8")

    with pytest.raises(KnowledgeMarkdownTooLargeError, match="maximum"):
        await KnowledgeMarkdownStore(root, max_document_bytes=32).load("large.md")


@pytest.mark.asyncio
async def test_digest_changes_when_file_bytes_change(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir()
    document_path = root / "board.md"
    document_path.write_text(_markdown(name="初始"), encoding="utf-8")
    store = KnowledgeMarkdownStore(root)

    first = await store.load("board.md")
    document_path.write_text(_markdown(name="修改后"), encoding="utf-8")
    second = await store.load("board.md")

    assert first.content_sha256 != second.content_sha256
    assert first.frontmatter["name"] == "初始"
    assert second.frontmatter["name"] == "修改后"


@pytest.mark.asyncio
async def test_missing_document_is_not_created_or_written(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir()
    missing = root / "missing.md"

    with pytest.raises(FileNotFoundError):
        await KnowledgeMarkdownStore(root).load("missing.md")

    assert not missing.exists()
    assert tuple(root.iterdir()) == ()


@pytest.mark.asyncio
async def test_rejects_symlink_target_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(_markdown(name="外部"), encoding="utf-8")
    link = root / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(KnowledgeMarkdownPathError):
        await KnowledgeMarkdownStore(root).load("linked.md")

    if sys.platform == "win32":
        # The test above is meaningful on Windows when the process can create
        # a symlink; this branch documents that no fallback is required.
        assert link.is_symlink()
