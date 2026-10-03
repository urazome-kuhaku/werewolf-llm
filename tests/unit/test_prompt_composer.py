"""Unit tests for the frozen Reading Skill and seat prompt boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from werewolf.runtime.knowledge_bootstrap import (
    KnowledgeBootstrapBoard,
    KnowledgeBootstrapCard,
    KnowledgeBootstrapRole,
    KnowledgeRequiredRead,
)
from werewolf.runtime.prompt_composer import (
    DEFAULT_MAX_READING_SKILL_BYTES,
    PromptComposerError,
    ReadingSkillIntegrityError,
    ReadingSkillNotFoundError,
    ReadingSkillTooLargeError,
    SystemPromptPathError,
    SystemPromptTooLargeError,
    compose_system_prompt,
    load_frozen_reading_skill,
    write_system_prompt,
)


@pytest.fixture()
def card() -> KnowledgeBootstrapCard:
    return KnowledgeBootstrapCard(
        board=KnowledgeBootstrapBoard(
            id="classic_12",
            version="1.0.0",
            name="经典十二人场",
            summary="当前板子规则导航。",
        ),
        your_role=KnowledgeBootstrapRole(
            id="witch",
            name="女巫",
            summary="你的公开角色规则摘要。",
        ),
        snapshot_id="snapshot-1",
        required_reads=(
            KnowledgeRequiredRead(tool="get_board", id="classic_12"),
            KnowledgeRequiredRead(tool="get_role", id="witch"),
        ),
    )


def _skill(tmp_path: Path, content: str = "# Frozen reading skill\n") -> tuple[Path, str]:
    path = tmp_path / "werewolf_reading.md"
    raw = content.encode("utf-8")
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_loads_utf8_skill_only_when_digest_matches(tmp_path: Path) -> None:
    path, digest = _skill(tmp_path, "重读规则。\n")

    assert load_frozen_reading_skill(path, digest) == "重读规则。\n"
    path.write_text("被篡改。\n", encoding="utf-8")
    with pytest.raises(ReadingSkillIntegrityError, match="SHA-256"):
        load_frozen_reading_skill(path, digest)


def test_missing_invalid_utf8_too_large_and_symlink_are_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    with pytest.raises(ReadingSkillNotFoundError):
        load_frozen_reading_skill(missing, "0" * 64)

    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff")
    digest = hashlib.sha256(invalid.read_bytes()).hexdigest()
    with pytest.raises(ReadingSkillIntegrityError, match="UTF-8"):
        load_frozen_reading_skill(invalid, digest)

    large, digest = _skill(tmp_path, "x" * 32)
    with pytest.raises(ReadingSkillTooLargeError):
        load_frozen_reading_skill(large, digest, max_bytes=1)

    target, digest = _skill(tmp_path, "target\n")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(ReadingSkillIntegrityError, match="symlink"):
        load_frozen_reading_skill(link, digest)


def test_composed_prompt_is_stable_and_contains_only_own_card_context(
    tmp_path: Path, card: KnowledgeBootstrapCard
) -> None:
    skill, digest = _skill(tmp_path, "调用工具后再决定。\n")

    first = compose_system_prompt(card, digest, reading_skill_path=skill)
    second = compose_system_prompt(card, digest, reading_skill_path=skill)

    assert first == second
    assert "经典十二人场" in first
    assert "女巫" in first
    assert "snapshot-1" in first
    assert "调用工具后再决定。" in first
    assert "重新调用" in first
    assert "seer" not in first
    assert "effective_rules" not in first
    assert "完整规则" not in first


def test_strategy_layer_uses_trusted_faction_and_keeps_legacy_common_only(
    tmp_path: Path, card: KnowledgeBootstrapCard
) -> None:
    skill, digest = _skill(tmp_path, "读取冻结规则。\n")

    good = compose_system_prompt(card, digest, reading_skill_path=skill, faction_id="good")
    # A special role ID is irrelevant; only the trusted faction selects the
    # wolf layer.
    wolf = compose_system_prompt(card, digest, reading_skill_path=skill, faction_id="wolf")
    assert "好人阵营策略" in good
    assert "狼人阵营策略" in wolf
    assert "悍跳" in wolf
    assert "好人阵营策略" not in wolf

    legacy = compose_system_prompt(card, digest, reading_skill_path=skill)
    assert "共同决策原则" in legacy
    assert "好人阵营策略" not in legacy

    with pytest.raises(PromptComposerError, match="unsupported faction_id"):
        compose_system_prompt(card, digest, reading_skill_path=skill, faction_id="neutral")


def test_composer_rejects_oversized_final_prompt(
    tmp_path: Path, card: KnowledgeBootstrapCard
) -> None:
    skill, digest = _skill(tmp_path, "短技能\n")

    with pytest.raises(SystemPromptTooLargeError):
        compose_system_prompt(card, digest, reading_skill_path=skill, max_prompt_bytes=1)


def test_writer_requires_absolute_regular_destination_and_is_atomic(tmp_path: Path) -> None:
    destination = tmp_path / "seat-3-system-prompt.txt"
    prompt = "系统提示\n"

    assert write_system_prompt(destination.absolute(), prompt) == destination.absolute()
    assert destination.read_text(encoding="utf-8") == prompt
    assert not list(tmp_path.glob(".*.tmp"))

    with pytest.raises(SystemPromptPathError, match="absolute"):
        write_system_prompt(Path("relative-prompt.txt"), prompt)
    with pytest.raises(SystemPromptTooLargeError):
        write_system_prompt(destination.absolute(), prompt, max_bytes=1)

    link = tmp_path / "prompt-link.txt"
    try:
        link.symlink_to(destination)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(SystemPromptPathError, match="symlink"):
        write_system_prompt(link.absolute(), prompt)


def test_skill_limit_is_positive_and_default_is_bounded(tmp_path: Path) -> None:
    path, digest = _skill(tmp_path, "x")
    assert DEFAULT_MAX_READING_SKILL_BYTES > 0
    with pytest.raises(ValueError, match="positive integer"):
        load_frozen_reading_skill(path, digest, max_bytes=0)
