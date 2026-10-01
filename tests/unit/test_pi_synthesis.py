"""Unit contracts for the restricted Pi ruleset authoring transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from werewolf.ruleset_workbench.claim_extractor import (
    ClaimExtractionError,
    ClaimExtractor,
)
from werewolf.ruleset_workbench.claims import ClaimStatus
from werewolf.ruleset_workbench.pi_synthesis import (
    EvidenceExcerpt,
    PiRuleSynthesisProvider,
    SynthesisBatchResult,
    SynthesisInputError,
    SynthesisProviderOutputLimitError,
    SynthesisProviderResponseError,
    SynthesisProviderTimeoutError,
    SynthesisResult,
    _build_environment,
    build_synthesis_prompt,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
SMOKE_CANDIDATE_ID = "classic_12_smoke"


def test_windows_environment_preserves_uppercase_systemroot(monkeypatch: Any) -> None:
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")

    environment = _build_environment()

    assert environment["SYSTEMROOT"] == r"C:\Windows"
    assert environment["SystemRoot"] == r"C:\Windows"


def _evidence(source_id: str, text: str, digest: str = HASH_A) -> EvidenceExcerpt:
    return EvidenceExcerpt(source_id=source_id, content_sha256=digest, excerpt=text)


def _wire(source_id: str, excerpt: str) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "claims": [
                {
                    "claim_id": f"claim-{source_id}",
                    "key": "witch.can_self_heal",
                    "value": False,
                    "scope": "ROLE",
                    "conditions": {},
                    "evidence": [{"source_id": source_id, "excerpt": excerpt}],
                },
            ],
            "draft": {"title": "A draft"},
        },
    ).encode()


class FakeProcess:
    def __init__(self, output: bytes, *, returncode: int = 0) -> None:
        self.output = output
        self.returncode = returncode
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.output, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


class SlowProcess(FakeProcess):
    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(60)
        return self.output, b""


class ChunkStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def read(self, _size: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class StreamingProcess(FakeProcess):
    def __init__(self, stdout: list[bytes], stderr: list[bytes]) -> None:
        super().__init__(b"")
        self.stdout = ChunkStream(stdout)
        self.stderr = ChunkStream(stderr)

    async def communicate(self) -> tuple[bytes, bytes]:
        raise AssertionError("bounded pipe readers must replace communicate()")


def _factory_for(
    processes: list[FakeProcess],
) -> tuple[Callable[..., Any], list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def factory(*args: Any, **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return processes.pop(0)

    return factory, calls


@pytest.mark.asyncio
async def test_synthesis_uses_independent_permission_minimized_sessions(tmp_path: Any) -> None:
    first = _evidence("source-a", "Only the first source excerpt.")
    second = _evidence("source-b", "Only the second source excerpt.", HASH_B)
    factory, calls = _factory_for(
        [
            FakeProcess(_wire(first.source_id, first.excerpt)),
            FakeProcess(_wire(second.source_id, second.excerpt)),
        ],
    )
    provider = PiRuleSynthesisProvider(
        executable="pi.cmd",
        session_root=tmp_path,
        process_factory=factory,
    )

    result = await provider.synthesize(
        "12人标准场",
        "classic-12",
        ((first,), (second,)),
    )

    assert [claim.claim_id for claim in result.claims] == ["claim-source-a", "claim-source-b"]
    assert result.claims[0].evidence[0].source_id == "source-a"
    assert result.claims[0].evidence[0].excerpt == first.excerpt
    assert all("status" not in claim.model_dump() for claim in result.claims)
    assert len(calls) == 2

    first_args = calls[0][0]
    second_args = calls[1][0]
    assert first_args[0:4] == ("pi.cmd", "--mode", "json", "--print")
    assert "--provider" in first_args
    assert first_args[first_args.index("--provider") + 1] == "github-copilot"
    assert first_args[first_args.index("--model") + 1] == "gpt-6-luna"
    for flag in (
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
    ):
        assert flag in first_args
    assert "--extension" not in first_args
    assert "--tools" not in first_args
    assert first_args[first_args.index("--") + 1].find("source-a") >= 0
    assert "source-b" not in first_args[first_args.index("--") + 1]
    assert second_args[second_args.index("--") + 1].find("source-b") >= 0
    assert "source-a" not in second_args[second_args.index("--") + 1]
    first_session_id = first_args[first_args.index("--session-id") + 1]
    second_session_id = second_args[second_args.index("--session-id") + 1]
    assert first_session_id != second_session_id
    assert calls[0][1]["stdin"] is asyncio.subprocess.DEVNULL


def test_prompt_is_bounded_and_contains_only_one_batch() -> None:
    first = _evidence("source-a", "a bounded excerpt")
    prompt = build_synthesis_prompt(
        "Board",
        "candidate",
        (first,),
        max_characters=5_000,
    )

    assert len(prompt) <= 5_000
    assert "\n" not in prompt
    assert "source-a" in prompt
    assert "a bounded excerpt" in prompt
    assert "direct rule" in prompt
    assert "claims MUST contain at least one claim" in prompt
    assert "女巫不可自救。" in prompt
    assert "do not put a direct rule only in draft" in prompt
    assert "contiguous verbatim substring" in prompt
    assert "dotted snake_case" in prompt
    assert "witch.can_self_heal" in prompt
    assert "single segment" in prompt
    assert '"claim_id"' in prompt
    assert '"source_id"' in prompt
    assert '"excerpt"' in prompt
    with pytest.raises(SynthesisInputError, match="exceeds"):
        build_synthesis_prompt("Board", "candidate", (first,), max_characters=20)


def test_prompt_allows_empty_claims_only_without_a_direct_rule() -> None:
    evidence = _evidence("source-a", "A general description with no explicit rule.")
    prompt = build_synthesis_prompt("Board", "candidate", (evidence,))

    assert "Return claims=[] only when no excerpt contains a direct rule" in prompt
    assert "Do not infer common practice" in prompt


def _smoke_result(evidence: EvidenceExcerpt, *, key: str) -> SynthesisResult:
    batch = SynthesisBatchResult.model_validate(
        {
            "schema_version": 1,
            "claims": [
                {
                    "claim_id": "claim-self-heal",
                    "key": key,
                    "value": False,
                    "scope": "ROLE",
                    "conditions": {},
                    "evidence": [
                        {
                            "source_id": evidence.source_id,
                            "excerpt": evidence.excerpt,
                        }
                    ],
                }
            ],
            "draft": None,
        },
        strict=True,
    )
    return SynthesisResult(
        board_name="12人标准场",
        ruleset_candidate_id=SMOKE_CANDIDATE_ID,
        claims=batch.claims,
        batches=(batch,),
        drafts=(),
    )


def test_smoke_validates_same_batch_into_unverified_rule_claim() -> None:
    excerpt = "女巫不可自救。"
    evidence = EvidenceExcerpt(
        source_id="synthetic_public_rule",
        content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        excerpt=excerpt,
    )

    extraction = ClaimExtractor().extract(
        ((evidence,),),
        _smoke_result(evidence, key="witch.can_self_heal"),
        ruleset_candidate_id=SMOKE_CANDIDATE_ID,
    )

    assert len(extraction.claims) == 1
    assert extraction.claims[0].key == "witch.can_self_heal"
    assert extraction.claims[0].status is ClaimStatus.UNVERIFIED
    assert extraction.ruleset_candidate_id == SMOKE_CANDIDATE_ID
    assert extraction.anchors[0].quote == excerpt


def test_smoke_rejects_single_segment_key_at_claim_boundary() -> None:
    excerpt = "女巫不可自救。"
    evidence = EvidenceExcerpt(
        source_id="synthetic_public_rule",
        content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        excerpt=excerpt,
    )

    with pytest.raises(ClaimExtractionError, match="failed RuleClaim validation"):
        ClaimExtractor().extract(
            ((evidence,),),
            _smoke_result(evidence, key="self_heal"),
            ruleset_candidate_id=SMOKE_CANDIDATE_ID,
        )


@pytest.mark.asyncio
async def test_timeout_kills_process_and_is_reported(tmp_path: Any) -> None:
    process = SlowProcess(b"")
    factory, _ = _factory_for([process])
    provider = PiRuleSynthesisProvider(
        executable="pi.cmd",
        session_root=tmp_path,
        timeout_seconds=0.001,
        process_factory=factory,
    )

    with pytest.raises(SynthesisProviderTimeoutError):
        await provider.synthesize("Board", "candidate", ((_evidence("source-a", "text"),),))
    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_output_limit_and_non_json_are_rejected(tmp_path: Any) -> None:
    oversized_factory, _ = _factory_for([FakeProcess(b"x" * 30)])
    oversized_provider = PiRuleSynthesisProvider(
        executable="pi.cmd",
        session_root=tmp_path / "large",
        max_output_bytes=10,
        process_factory=oversized_factory,
    )
    with pytest.raises(SynthesisProviderOutputLimitError):
        await oversized_provider.synthesize(
            "Board", "candidate", ((_evidence("source-a", "text"),),)
        )

    streaming_process = StreamingProcess([b"x" * 11], [b""])
    streaming_factory, _ = _factory_for([streaming_process])
    streaming_provider = PiRuleSynthesisProvider(
        executable="pi.cmd",
        session_root=tmp_path / "streaming-large",
        max_output_bytes=10,
        process_factory=streaming_factory,
    )
    with pytest.raises(SynthesisProviderOutputLimitError):
        await streaming_provider.synthesize(
            "Board", "candidate", ((_evidence("source-a", "text"),),)
        )
    assert streaming_process.killed is True
    assert streaming_process.waited is True

    invalid_factory, _ = _factory_for([FakeProcess(b"```json\n{}\n```")])
    invalid_provider = PiRuleSynthesisProvider(
        executable="pi.cmd",
        session_root=tmp_path / "invalid",
        process_factory=invalid_factory,
    )
    with pytest.raises(SynthesisProviderResponseError):
        await invalid_provider.synthesize("Board", "candidate", ((_evidence("source-a", "text"),),))


@pytest.mark.asyncio
async def test_supported_status_is_not_accepted_from_model(tmp_path: Any) -> None:
    payload = {
        "schema_version": 1,
        "claims": [
            {
                "claim_id": "claim-a",
                "key": "witch.can_self_heal",
                "value": False,
                "scope": "ROLE",
                "conditions": {},
                "evidence": [{"source_id": "source-a", "excerpt": "text"}],
                "status": "SUPPORTED",
            },
        ],
    }
    factory, _ = _factory_for([FakeProcess(json.dumps(payload).encode())])
    provider = PiRuleSynthesisProvider(
        executable="pi.cmd",
        session_root=tmp_path,
        process_factory=factory,
    )
    with pytest.raises(SynthesisProviderResponseError):
        await provider.synthesize("Board", "candidate", ((_evidence("source-a", "text"),),))
