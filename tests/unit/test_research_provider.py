"""Focused contract tests for the bounded Agent Reach research provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from werewolf.ruleset_workbench import research_provider as research_provider_module
from werewolf.ruleset_workbench.research_provider import (
    AgentReachResearchProvider,
    InvalidResearchUrlError,
    ResearchProviderOutputLimitError,
    ResearchProviderResponseError,
    ResearchProviderTimeoutError,
    SearchQuery,
)


def test_default_executable_resolves_windows_mcporter_cmd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    resolved = str(tmp_path / "mcporter.cmd")

    monkeypatch.setattr(research_provider_module.os, "name", "nt")
    monkeypatch.setattr(
        research_provider_module.shutil,
        "which",
        lambda command: resolved if command == "mcporter.cmd" else None,
    )

    provider = AgentReachResearchProvider()

    assert provider.config.executable == resolved


class FakeProcess:
    def __init__(self, output: bytes, *, returncode: int = 0) -> None:
        self.stdout = None
        self.stderr = None
        self.returncode = returncode
        self._output = output
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._output, b""

    def kill(self) -> None:
        self.killed = True


class SlowProcess(FakeProcess):
    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(60)
        return self._output, b""

    async def wait(self) -> int:
        return 0


def factory_for(
    process: FakeProcess,
) -> tuple[Callable[..., Any], list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def factory(*args: Any, **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return process

    return factory, calls


@pytest.mark.asyncio
async def test_search_uses_http_exa_and_parses_mcp_content() -> None:
    result = {
        "results": [
            {
                "Title": "Classic rules",
                "URL": "https://example.com/rules",
                "Published": "2025-01-01",
                "Author": "Rules editor",
                "Highlights": ["Night action order"],
            },
        ],
    }
    wire = json.dumps(
        {"content": [{"type": "text", "text": json.dumps(result)}]},
    ).encode()
    process = FakeProcess(wire)
    factory, calls = factory_for(process)
    provider = AgentReachResearchProvider(process_factory=factory)

    hits = await provider.search(SearchQuery(query="classic werewolf", max_results=3))

    assert len(hits) == 1
    assert hits[0].title == "Classic rules"
    assert str(hits[0].url) == "https://example.com/rules"
    assert hits[0].highlights == ["Night action order"]
    args, kwargs = calls[0]
    assert args[0] == provider.config.executable
    assert args[1:7] == (
        "call",
        "--http-url",
        "https://mcp.exa.ai/mcp",
        "--tool",
        "web_search_exa",
        "--args",
    )
    assert json.loads(args[7]) == {"query": "classic werewolf", "numResults": 3}
    assert args[8:] == ("--output", "json")
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_search_parses_realistic_exa_text_content() -> None:
    text = """Title: Classic rules
URL: https://example.com/classic
Published: 2025-01-01
Author: Rules editor
Highlights: Night action order

---

Title: Variant notes
URL: https://example.com/variant
Published: 2024-12-12
Author: Archive
Highlights:
- Day vote resolves after discussion.
- Ties are recorded explicitly.
"""
    wire = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
    process = FakeProcess(wire)
    factory, _ = factory_for(process)
    provider = AgentReachResearchProvider(process_factory=factory)

    hits = await provider.search("classic werewolf")

    assert [hit.title for hit in hits] == ["Classic rules", "Variant notes"]
    assert hits[0].highlights == ["Night action order"]
    assert hits[1].highlights == [
        "Day vote resolves after discussion.",
        "Ties are recorded explicitly.",
    ]


@pytest.mark.asyncio
async def test_fetch_sends_urls_and_max_characters_and_returns_body() -> None:
    result = {
        "results": [
            {
                "url": "https://example.com/rules",
                "title": "Rules",
                "text": "The seer checks one player each night.",
            },
        ],
    }
    wire = json.dumps({"content": [{"type": "text", "text": json.dumps(result)}]}).encode()
    process = FakeProcess(wire)
    factory, calls = factory_for(process)
    provider = AgentReachResearchProvider(
        process_factory=factory,
        max_fetch_characters=1234,
    )

    document = await provider.fetch("https://example.com/rules")

    assert document.body == "The seer checks one player each night."
    assert document.text == document.body
    args, _ = calls[0]
    assert args[5] == "web_fetch_exa"
    assert json.loads(args[7]) == {
        "urls": ["https://example.com/rules"],
        "maxCharacters": 1234,
    }


@pytest.mark.asyncio
async def test_fetch_parses_realistic_exa_text_and_preserves_body() -> None:
    body = """
The seer checks one player each night.

"""
    text = f"""# Classic rules
URL: https://example.com/rules
Published: 2025-01-01
Author: Rules editor

{body}"""
    wire = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
    process = FakeProcess(wire)
    factory, _ = factory_for(process)
    provider = AgentReachResearchProvider(process_factory=factory)

    document = await provider.fetch("https://example.com/rules")

    assert document.title == "Classic rules"
    assert document.published == "2025-01-01"
    assert document.author == "Rules editor"
    assert document.body == body


@pytest.mark.asyncio
async def test_fetch_rejects_text_envelope_url_mismatch() -> None:
    text = "# Classic rules\nURL: https://example.com/other\n\nThe seer checks one player."
    wire = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
    process = FakeProcess(wire)
    factory, _ = factory_for(process)
    provider = AgentReachResearchProvider(process_factory=factory)

    with pytest.raises(ResearchProviderResponseError, match="does not match"):
        await provider.fetch("https://example.com/rules")


@pytest.mark.asyncio
async def test_fetch_rejects_non_public_targets_before_starting_process() -> None:
    async def unexpected_factory(*args: Any, **kwargs: Any) -> FakeProcess:
        raise AssertionError("the subprocess must not start")

    provider = AgentReachResearchProvider(process_factory=unexpected_factory)

    with pytest.raises(InvalidResearchUrlError):
        await provider.fetch("http://127.0.0.1/private")
    with pytest.raises(InvalidResearchUrlError):
        await provider.fetch("file:///private/rules")


@pytest.mark.asyncio
async def test_malformed_mcp_result_fails_explicitly() -> None:
    process = FakeProcess(b'{"content": [{"type": "text", "text": "{}"}]}')
    factory, _ = factory_for(process)
    provider = AgentReachResearchProvider(process_factory=factory)

    with pytest.raises(ResearchProviderResponseError, match="result records"):
        await provider.search("rules")


@pytest.mark.asyncio
async def test_text_rate_limit_and_malformed_fetch_are_rejected_explicitly() -> None:
    rate_limited = FakeProcess(
        json.dumps(
            {"content": [{"type": "text", "text": "Rate limit exceeded"}]},
        ).encode(),
    )
    rate_factory, _ = factory_for(rate_limited)
    provider = AgentReachResearchProvider(process_factory=rate_factory)

    with pytest.raises(ResearchProviderResponseError, match="rate-limit"):
        await provider.search("rules")

    malformed_fetch = FakeProcess(
        json.dumps(
            {
                "content": [
                    {
                        "type": "text",
                        "text": "# Rules\nURL: https://example.com/rules\nbody",
                    },
                ],
            },
        ).encode(),
    )
    fetch_factory, _ = factory_for(malformed_fetch)
    provider = AgentReachResearchProvider(process_factory=fetch_factory)

    with pytest.raises(ResearchProviderResponseError, match="metadata"):
        await provider.fetch("https://example.com/rules")


@pytest.mark.asyncio
async def test_timeout_and_output_limits_are_bounded() -> None:
    slow = SlowProcess(b"")
    slow_factory, _ = factory_for(slow)
    provider = AgentReachResearchProvider(
        process_factory=slow_factory,
        timeout_seconds=0.001,
    )
    with pytest.raises(ResearchProviderTimeoutError):
        await provider.search("rules")
    assert slow.killed is True

    large = FakeProcess(b"x" * 20)
    large_factory, _ = factory_for(large)
    provider = AgentReachResearchProvider(
        process_factory=large_factory,
        max_output_bytes=10,
    )
    with pytest.raises(ResearchProviderOutputLimitError):
        await provider.search("rules")
