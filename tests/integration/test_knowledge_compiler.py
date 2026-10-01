"""Integration tests for deterministic published knowledge compilation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_knowledge_package_loader import BOARD, _write_package

from werewolf.knowledge.compiler import (
    KnowledgeCompilerReferenceError,
    KnowledgeCompilerTopicError,
    KnowledgePackageCompiler,
)
from werewolf.knowledge.package_loader import KnowledgePackageLoader

_FRONTMATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)


def _rewrite_document(path: Path, *, body: str, values: dict[str, Any] | None = None) -> None:
    """Replace one fixture document body while retaining its published metadata."""

    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(raw)
    assert match is not None
    frontmatter = values if values is not None else yaml.safe_load(match.group("body"))
    assert isinstance(frontmatter, dict)
    encoded = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{encoded}---\n{body}", encoding="utf-8")


def _write_compilable_package(
    root: Path,
    *,
    bootstrap_topics: list[str] | None = None,
    with_role_override: bool = True,
) -> None:
    """Create the loader fixture with anchored Markdown sections for compilation."""

    _write_package(root)
    board_path = root / "boards" / "test-board" / "1.0.0" / "board.md"
    board_raw = board_path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(board_raw)
    assert match is not None
    board_values = yaml.safe_load(match.group("body"))
    assert isinstance(board_values, dict)
    if bootstrap_topics is not None:
        reading_plan = board_values["reading_plan"]
        assert isinstance(reading_plan, dict)
        reading_plan["bootstrap_topics"] = bootstrap_topics
    if with_role_override:
        roles = board_values["roles"]
        assert isinstance(roles, list)
        binding = roles[0]
        assert isinstance(binding, dict)
        binding["effective_rules"] = {"can_self_heal": False}
        binding["override_claim_refs"] = ["claim-board"]

    documents = {
        "boards/test-board/1.0.0/board.md": ("# 测试板\n\n## 概览 {#overview}\n\n板子概览。\n"),
        "roles/wolf/1.0.0/role.md": ("# 狼人\n\n## 角色规则 {#role_rules}\n\n角色规则。\n"),
        "mechanics/voting/1.0.0/mechanic.md": (
            "# 投票\n\n## 投票规则 {#voting_rules}\n\n投票规则。\n"
        ),
        "interactions/wolf-voting/1.0.0/interaction.md": (
            "# 狼人投票交互\n\n## 交互规则 {#interaction_rules}\n\n交互规则。\n"
        ),
    }
    for relative_path, body in documents.items():
        path = root / relative_path
        values = board_values if relative_path.startswith("boards/") else None
        _rewrite_document(path, body=body, values=values)


@pytest.mark.asyncio
async def test_compiler_is_repeatable_and_keeps_effective_role_provenance(
    tmp_path: Path,
) -> None:
    _write_compilable_package(tmp_path)
    package = await KnowledgePackageLoader(tmp_path).load(BOARD)
    compiler = KnowledgePackageCompiler()

    first = compiler.compile(package)
    second = compiler.compile(package)

    assert first.canonical_package_json == second.canonical_package_json
    assert first.canonical_manifest_json == second.canonical_manifest_json
    assert first.package_identity == second.package_identity
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.index.lookup_exact("role:wolf@1.0.0") is not None
    assert any(document.kind == "topic" for document in first.index.lookup_topic("overview"))
    assert first.package_payload["board_definition"] == package.board.model.model_dump(mode="json")
    assert first.manifest_payload["board_definition_sha256"]

    profile = first.effective_roles["wolf"]
    assert profile.effective_rules == {"can_self_heal": False}
    assert profile.override_provenance == {"can_self_heal": ("claim-board",)}
    assert profile.payload["board_ref"] == BOARD


@pytest.mark.asyncio
async def test_compiler_hashes_change_when_published_content_changes(tmp_path: Path) -> None:
    _write_compilable_package(tmp_path)
    loader = KnowledgePackageLoader(tmp_path)
    compiler = KnowledgePackageCompiler()

    first = compiler.compile(await loader.load(BOARD))
    role_path = tmp_path / "roles" / "wolf" / "1.0.0" / "role.md"
    role_path.write_text(
        role_path.read_text(encoding="utf-8") + "\n补充一条已发布规则。\n",
        encoding="utf-8",
    )
    second = compiler.compile(await loader.load(BOARD))

    role_relative_path = "roles/wolf/1.0.0/role.md"
    assert first.document_digests[role_relative_path] != second.document_digests[role_relative_path]
    assert first.manifest_sha256 != second.manifest_sha256
    assert first.package_identity != second.package_identity


@pytest.mark.asyncio
async def test_compiler_rejects_missing_board_section_reference(tmp_path: Path) -> None:
    _write_compilable_package(tmp_path, bootstrap_topics=["board:missing"])
    package = await KnowledgePackageLoader(tmp_path).load(BOARD)

    with pytest.raises(KnowledgeCompilerReferenceError, match="missing"):
        KnowledgePackageCompiler().compile(package)


@pytest.mark.asyncio
async def test_compiler_rejects_unresolved_generic_topic_reference(tmp_path: Path) -> None:
    _write_compilable_package(tmp_path, bootstrap_topics=["topic:missing"])
    package = await KnowledgePackageLoader(tmp_path).load(BOARD)

    with pytest.raises(KnowledgeCompilerTopicError, match="topic:missing"):
        KnowledgePackageCompiler().compile(package)
