"""Integration coverage for evidence-to-review authoring orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from werewolf.ruleset_workbench import (
    REVIEW_MARKDOWN_FILENAME,
    AuthoringPipeline,
    ClaimStatus,
    CorruptReviewMarkdownError,
    DraftClaim,
    DraftEvidenceReference,
    ResearchJobStatus,
    ResearchPipeline,
    ResearchPipelineConfig,
    ResearchSearchPlan,
    SourceClass,
    SynthesisBatchResult,
    SynthesisResult,
    WorkbenchBundleStore,
)
from werewolf.ruleset_workbench.research_plan import (
    AgentReachDoctorStatus,
    AgentReachTransport,
    build_search_plan,
)
from werewolf.ruleset_workbench.research_provider import FetchedDocument, SearchHit


def _ready_plan(board_name: str) -> ResearchSearchPlan:
    status = AgentReachDoctorStatus(
        executable="agent-reach",
        active_backend="Exa via mcporter",
        transport=AgentReachTransport.MCPORTER_HTTPS,
    )
    return build_search_plan(board_name).with_preflight(status)


class _EvidenceProvider:
    async def search(self, query: object) -> list[SearchHit]:
        del query
        return [SearchHit(title="规则来源", url="https://example.com/rules")]

    async def fetch(self, url: str) -> FetchedDocument:
        return FetchedDocument(
            url=url,
            body="女巫不可自救。",
            title="规则来源",
            author="测试来源",
        )


class _SynthesisProvider:
    def __init__(self, mode: Literal["claim", "empty", "citation", "failure"]) -> None:
        self.mode = mode

    async def synthesize(
        self,
        board_name: str,
        ruleset_candidate_id: str,
        evidence_batches: tuple[tuple[object, ...], ...],
    ) -> SynthesisResult:
        del board_name
        if self.mode == "failure":
            raise RuntimeError("Pi authoring unavailable")
        if self.mode == "empty":
            claim_batch = SynthesisBatchResult()
        else:
            source = evidence_batches[0][0]
            quote = "女巫不可自救。" if self.mode == "claim" else "不存在于来源。"
            claim_batch = SynthesisBatchResult(
                claims=(
                    DraftClaim(
                        claim_id="witch-self-heal",
                        key="witch.can_self_heal",
                        value=False,
                        scope="ROLE",
                        conditions={"night": 1},
                        evidence=(
                            DraftEvidenceReference(
                                source_id=source.source_id,
                                excerpt=quote,
                            ),
                        ),
                        confidence=0.8,
                    ),
                ),
            )
        return SynthesisResult(
            board_name="12人标准场",
            ruleset_candidate_id=ruleset_candidate_id,
            batches=(claim_batch,),
        )


def _pipeline(tmp_path: Path) -> ResearchPipeline:
    return ResearchPipeline(
        _EvidenceProvider(),
        workbench_root=tmp_path / "vault" / "_workbench",
        config=ResearchPipelineConfig(source_class=SourceClass.OTHER),
        plan_factory=_ready_plan,
    )


@pytest.mark.asyncio
async def test_authoring_persists_source_closed_unverified_bundle_and_analysis(
    tmp_path: Path,
) -> None:
    research = _pipeline(tmp_path)
    authoring = AuthoringPipeline(research, _SynthesisProvider("claim"))

    result = await authoring.run("12人标准场")

    assert result.succeeded is True
    assert result.status is ResearchJobStatus.DRAFTED
    assert result.bundle is not None
    assert result.analysis is not None
    assert result.extraction is not None
    assert result.extraction.claims[0].status is ClaimStatus.UNVERIFIED
    assert result.bundle.claims[0].status is ClaimStatus.UNVERIFIED
    assert (result.research.job_root / "bundle.sha256").is_file()
    assert (result.research.job_root / "artifacts.manifest.json").is_file()
    assert (result.research.job_root / "analysis.manifest.json").is_file()
    review_path = result.research.job_root / REVIEW_MARKDOWN_FILENAME
    assert review_path.is_file()
    review = review_path.read_bytes()
    assert review == result.review_markdown
    assert b"UNVERIFIED" in review
    assert b"https://example.com/rules" in review
    assert "女巫不可自救。".encode() in review
    assert b"NOT_EVALUATED" in review

    # The same saved inputs render identically, while a tampered file is
    # rejected instead of being silently replaced.
    assert await authoring.review_markdown_store.materialize(result.job_dir_name) == review
    review_path.write_bytes(review + b"tampered\n")
    with pytest.raises(CorruptReviewMarkdownError):
        await authoring.review_markdown_store.verify(result.job_dir_name)

    stored = await WorkbenchBundleStore(research.job_store).load_bundle(result.job_dir_name)
    assert stored.claims[0].status is ClaimStatus.UNVERIFIED


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "citation"])
async def test_authoring_rejects_empty_or_unclosed_pi_output(
    tmp_path: Path,
    mode: Literal["empty", "citation"],
) -> None:
    research = _pipeline(tmp_path)
    authoring = AuthoringPipeline(research, _SynthesisProvider(mode))

    result = await authoring.run("12人标准场")

    assert result.status is ResearchJobStatus.FAILED
    assert result.job.resume_status is ResearchJobStatus.EXTRACTING
    assert not (result.research.job_root / "bundle.sha256").exists()
    assert result.bundle is None
    if mode == "empty":
        assert "no valid claims" in result.failures[0].message
    else:
        assert "exact substring" in result.failures[0].message


@pytest.mark.asyncio
async def test_authoring_retains_pi_failure_as_recoverable_job(tmp_path: Path) -> None:
    research = _pipeline(tmp_path)
    authoring = AuthoringPipeline(research, _SynthesisProvider("failure"))

    result = await authoring.run("12人标准场")

    assert result.status is ResearchJobStatus.FAILED
    assert result.job.failure_reason == "Pi authoring unavailable"
    assert result.job.resume_status is ResearchJobStatus.EXTRACTING
    assert result.synthesis is None
    assert not (result.research.job_root / "bundle.json").exists()
