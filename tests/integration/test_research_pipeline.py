"""Integration coverage for the bounded evidence acquisition pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from werewolf.ruleset_workbench.evidence import SourceClass
from werewolf.ruleset_workbench.research_pipeline import (
    ResearchPipeline,
    ResearchPipelineConfig,
)
from werewolf.ruleset_workbench.research_plan import (
    AgentReachDoctorStatus,
    AgentReachTransport,
    build_search_plan,
)
from werewolf.ruleset_workbench.research_provider import FetchedDocument, SearchHit
from werewolf.ruleset_workbench.source_archive import SourceArchive


def _plan(board_name: str):
    status = AgentReachDoctorStatus(
        executable="agent-reach",
        active_backend="Exa via mcporter",
        transport=AgentReachTransport.MCPORTER_HTTPS,
    )
    return build_search_plan(board_name).with_preflight(status)


def _hit(url: str, title: str = "规则来源") -> SearchHit:
    return SearchHit(title=title, url=url)


def _document(url: str, body: str) -> FetchedDocument:
    return FetchedDocument(url=url, body=body, title="来源正文", author="测试来源")


class FakeProvider:
    def __init__(self, *, fail_search: set[str] | None = None, fail_fetch: set[str] | None = None):
        self.fail_search = fail_search or set()
        self.fail_fetch = fail_fetch or set()
        self.search_calls: list[str] = []
        self.fetch_calls: list[str] = []

    async def search(self, query: Any) -> list[SearchHit]:
        self.search_calls.append(query.query)
        if query.query in self.fail_search:
            raise RuntimeError("search refused")
        # Every topic repeats the first URL.  The second topic adds a source
        # whose body intentionally duplicates the first body.
        if query.query.endswith("官方 规则"):
            return [_hit("https://example.com/rules", "官方规则")]
        if "角色技能" in query.query:
            return [
                _hit("https://example.com/rules", "重复 URL"),
                _hit("https://example.com/copy", "重复正文"),
            ]
        return []

    async def fetch(self, url: str) -> FetchedDocument:
        self.fetch_calls.append(url)
        if url in self.fail_fetch:
            await asyncio.sleep(0)
            raise TimeoutError("fetch timed out")
        return _document(url, "女巫不可自救。" if url.endswith("rules") else "女巫不可自救。")


async def _ready_plan(board: str):
    return _plan(board)


@pytest.mark.asyncio
async def test_pipeline_searches_eight_topics_deduplicates_and_archives(tmp_path: Path) -> None:
    provider = FakeProvider()
    pipeline = ResearchPipeline(
        provider,
        workbench_root=tmp_path / "vault" / "_workbench",
        plan_factory=_ready_plan,
        config=ResearchPipelineConfig(source_class=SourceClass.OTHER),
    )

    result = await pipeline.run("12人标准场")

    assert len(provider.search_calls) == 8
    assert provider.fetch_calls == ["https://example.com/rules", "https://example.com/copy"]
    assert result.succeeded is True
    assert result.source_count == 1
    assert {record.outcome for record in result.audit} >= {
        "ARCHIVED",
        "DUPLICATE_URL",
        "DUPLICATE_CONTENT",
    }
    assert result.archives[0].relative_path.startswith("sources/raw/")
    assert result.archives[0].relative_path.endswith(".txt")
    assert result.audit_path.is_file()
    assert result.audit_path.resolve().is_relative_to(result.job_root.resolve())
    assert await pipeline.job_store.load_job(result.job_dir_name) == result.job

    body = await SourceArchive(result.job_root).read(result.evidence[0])
    assert body == "女巫不可自救。"


@pytest.mark.asyncio
async def test_pipeline_keeps_partial_failure_report_and_source_closure(tmp_path: Path) -> None:
    plan = _plan("12人标准场")
    failing_query = plan.queries[1].query
    provider = FakeProvider(fail_search={failing_query}, fail_fetch={"https://example.com/rules"})
    pipeline = ResearchPipeline(
        provider,
        workbench_root=tmp_path / "vault" / "_workbench",
        plan_factory=_ready_plan,
    )

    result = await pipeline.run("12人标准场")

    assert result.succeeded is True
    assert result.status.value == "EVIDENCE_COLLECTED"
    assert any(item.phase == "search" for item in result.failures)
    assert any(item.phase == "fetch" for item in result.failures)
    assert len(result.evidence) == 1
    assert result.audit_path.is_file()


@pytest.mark.asyncio
async def test_pipeline_rejects_unready_plan_without_provider_calls(tmp_path: Path) -> None:
    provider = FakeProvider()
    pipeline = ResearchPipeline(
        provider,
        workbench_root=tmp_path / "vault" / "_workbench",
        plan_factory=_unready_plan,
    )

    result = await pipeline.run("12人标准场")

    assert result.status.value == "FAILED"
    assert result.failures[0].phase == "preflight"
    assert provider.search_calls == []
    assert provider.fetch_calls == []


async def _unready_plan(board: str):
    return build_search_plan(board)
