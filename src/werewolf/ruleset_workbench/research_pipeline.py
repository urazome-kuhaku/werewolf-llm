"""Controlled evidence acquisition for one ruleset research job.

The pipeline is the small orchestration boundary between the existing
research transport and the workbench source archive.  It intentionally stops
at ``SourceEvidence``: Pi synthesis, claim extraction, conflict analysis and
publishing are later stages and are not called here.

The provider is treated as an untrusted transport.  Search results are
bounded and de-duplicated before fetching, fetched bodies are hashed before
they are archived, and every accepted or rejected URL receives an audit
record.  A partial provider failure is therefore inspectable and never turns
an error page or a summary into source evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from werewolf.persistence import atomic_write_text, resolve_contained_path

from .evidence import SourceClass, SourceEvidence
from .evidence_builder import build_source_evidence
from .jobs import ResearchJob, ResearchJobStatus
from .research_plan import ResearchSearchPlan, prepare_research_plan
from .research_provider import FetchedDocument, RuleResearchProvider, SearchHit
from .source_archive import SourceArchive, SourceArchiveResult
from .store import WorkbenchJobStore

AUDIT_FILENAME = "research-audit.json"
AUDIT_SCHEMA_VERSION = 1
DEFAULT_LOCALE = "zh-CN"
DEFAULT_MAX_SOURCES = 32
DEFAULT_MAX_FETCH_CONCURRENCY = 4
DEFAULT_MAX_TOTAL_BYTES = 8_000_000
DEFAULT_MAX_EXCERPT_LENGTH = 4_000
DEFAULT_RETRIEVAL_METHOD = "jina_https"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ResearchPipelineError(RuntimeError):
    """Base class for controlled evidence-pipeline failures."""


class ResearchPipelinePreflightError(ResearchPipelineError):
    """The plan could not be prepared with a ready Exa HTTPS route."""


class ResearchPipelineStorageError(ResearchPipelineError):
    """The audit record or source archive could not be made durable."""


class ResearchPipelineLimitError(ValueError):
    """A pipeline bound is invalid."""


class ResearchPipelinePlanFactory(Protocol):
    def __call__(self, board_name: str) -> Any:
        """Build and preflight a plan for one board."""


@dataclass(frozen=True, slots=True)
class ResearchPipelineConfig:
    """Resource and evidence bounds for one acquisition run."""

    locale: str = DEFAULT_LOCALE
    max_sources: int = DEFAULT_MAX_SOURCES
    max_fetch_concurrency: int = DEFAULT_MAX_FETCH_CONCURRENCY
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_excerpt_length: int = DEFAULT_MAX_EXCERPT_LENGTH
    retrieval_method: str = DEFAULT_RETRIEVAL_METHOD
    source_class: SourceClass = SourceClass.OTHER

    def __post_init__(self) -> None:
        if not isinstance(self.locale, str) or not self.locale.strip():
            raise ResearchPipelineLimitError("locale must be a non-empty string")
        for name in (
            "max_sources",
            "max_fetch_concurrency",
            "max_total_bytes",
            "max_excerpt_length",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ResearchPipelineLimitError(f"{name} must be a positive integer")
        if len(self.retrieval_method.strip()) == 0 or len(self.retrieval_method) > 64:
            raise ResearchPipelineLimitError(
                "retrieval_method must contain 1 to 64 non-whitespace characters",
            )
        if not isinstance(self.source_class, SourceClass):
            raise TypeError("source_class must be a SourceClass")


@dataclass(frozen=True, slots=True)
class ResearchPipelineFailure:
    """A bounded failure report linked to a query or URL when available."""

    phase: str
    message: str
    query: str | None = None
    topic: str | None = None
    url: str | None = None

    def as_json(self) -> dict[str, str]:
        result = {"phase": self.phase, "message": self.message}
        if self.query is not None:
            result["query"] = self.query
        if self.topic is not None:
            result["topic"] = self.topic
        if self.url is not None:
            result["url"] = self.url
        return result


@dataclass(frozen=True, slots=True)
class ResearchAuditRecord:
    """One query-to-source trace, including deduplication outcomes."""

    query: str
    topic: str
    url: str
    searched_at: datetime
    fetched_at: datetime | None = None
    content_sha256: str | None = None
    source_id: str | None = None
    archive_path: str | None = None
    outcome: str = "SEARCH_HIT"
    duplicate_of: str | None = None
    error: str | None = None

    def as_json(self) -> dict[str, str | None]:
        return {
            "query": self.query,
            "topic": self.topic,
            "url": self.url,
            "searched_at": _timestamp(self.searched_at),
            "fetched_at": _timestamp(self.fetched_at),
            "content_sha256": self.content_sha256,
            "source_id": self.source_id,
            "archive_path": self.archive_path,
            "outcome": self.outcome,
            "duplicate_of": self.duplicate_of,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ResearchPipelineResult:
    """Durable output of the evidence-only acquisition stage."""

    board_name: str
    job: ResearchJob
    job_dir_name: str
    job_root: Path
    plan: ResearchSearchPlan | None
    evidence: tuple[SourceEvidence, ...]
    archives: tuple[SourceArchiveResult, ...]
    audit: tuple[ResearchAuditRecord, ...]
    failures: tuple[ResearchPipelineFailure, ...]

    @property
    def status(self) -> ResearchJobStatus:
        """Return the persisted job state at pipeline completion."""

        return self.job.status

    @property
    def source_count(self) -> int:
        return len(self.evidence)

    @property
    def audit_path(self) -> Path:
        return self.job_root / AUDIT_FILENAME

    @property
    def succeeded(self) -> bool:
        """Whether at least one immutable source was collected."""

        return bool(self.evidence) and self.job.status is ResearchJobStatus.EVIDENCE_COLLECTED


@dataclass(frozen=True, slots=True)
class _Candidate:
    query: str
    topic: str
    url: str
    hit: SearchHit
    searched_at: datetime


@dataclass(slots=True)
class _MutableAudit:
    query: str
    topic: str
    url: str
    searched_at: datetime
    fetched_at: datetime | None = None
    content_sha256: str | None = None
    source_id: str | None = None
    archive_path: str | None = None
    outcome: str = "SEARCH_HIT"
    duplicate_of: str | None = None
    error: str | None = None

    def freeze(self) -> ResearchAuditRecord:
        return ResearchAuditRecord(
            query=self.query,
            topic=self.topic,
            url=self.url,
            searched_at=self.searched_at,
            fetched_at=self.fetched_at,
            content_sha256=self.content_sha256,
            source_id=self.source_id,
            archive_path=self.archive_path,
            outcome=self.outcome,
            duplicate_of=self.duplicate_of,
            error=self.error,
        )


class ResearchPipeline:
    """Run the bounded K1 search, fetch, evidence and archive stages."""

    def __init__(
        self,
        provider: RuleResearchProvider,
        *,
        workbench_root: str | Path = Path("vault") / "_workbench",
        job_store: WorkbenchJobStore | None = None,
        config: ResearchPipelineConfig | None = None,
        plan_factory: ResearchPipelinePlanFactory = prepare_research_plan,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if provider is None:
            raise TypeError("provider is required")
        self.provider = provider
        self.config = config or ResearchPipelineConfig()
        self.job_store = job_store or WorkbenchJobStore(workbench_root)
        self.plan_factory = plan_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def run(self, board_name: str) -> ResearchPipelineResult:
        """Create a job and collect evidence for ``board_name``."""

        created_at = _require_now(self._now())
        job, job_dir_name = await self.job_store.create_job(
            board_name,
            self.config.locale,
            created_at,
        )
        job_root = resolve_contained_path(self.job_store.root, job_dir_name)
        failures: list[ResearchPipelineFailure] = []
        audit: list[_MutableAudit] = []
        plan: ResearchSearchPlan | None = None
        evidence: list[SourceEvidence] = []
        archives: list[SourceArchiveResult] = []

        try:
            plan_value = self.plan_factory(board_name)
            plan = await plan_value if inspect.isawaitable(plan_value) else plan_value
            _require_ready_plan(plan)
        except Exception as exc:
            failure = ResearchPipelineFailure(
                phase="preflight",
                message=_error_message(exc),
            )
            failures.append(failure)
            await self._persist_audit(
                job_root,
                board_name=job.board_name,
                plan=plan,
                audit=audit,
                failures=failures,
            )
            job = await self.job_store.fail_job(
                job_dir_name,
                failure.message,
                ResearchJobStatus.CREATED,
                self._next_time(created_at),
            )
            return ResearchPipelineResult(
                board_name=job.board_name,
                job=job,
                job_dir_name=job_dir_name,
                job_root=job_root,
                plan=plan,
                evidence=(),
                archives=(),
                audit=(),
                failures=tuple(failures),
            )

        job = await self.job_store.advance_job(
            job_dir_name,
            ResearchJobStatus.SEARCHING,
            self._next_time(job.updated_at),
        )
        candidates = await self._search(plan, audit, failures)
        await self._persist_audit(
            job_root,
            board_name=job.board_name,
            plan=plan,
            audit=audit,
            failures=failures,
        )

        source_archive = SourceArchive(
            job_root,
            max_archive_bytes=self.config.max_total_bytes,
        )
        await self._fetch_and_archive(
            candidates,
            audit,
            failures,
            evidence,
            archives,
            source_archive,
        )
        await self._persist_audit(
            job_root,
            board_name=job.board_name,
            plan=plan,
            audit=audit,
            failures=failures,
        )

        if evidence:
            job = await self.job_store.advance_job(
                job_dir_name,
                ResearchJobStatus.EVIDENCE_COLLECTED,
                self._next_time(job.updated_at),
            )
        else:
            message = "research produced no source evidence"
            failures.append(ResearchPipelineFailure(phase="evidence", message=message))
            job = await self.job_store.fail_job(
                job_dir_name,
                message,
                ResearchJobStatus.SEARCHING,
                self._next_time(job.updated_at),
            )
            await self._persist_audit(
                job_root,
                board_name=job.board_name,
                plan=plan,
                audit=audit,
                failures=failures,
            )

        return ResearchPipelineResult(
            board_name=job.board_name,
            job=job,
            job_dir_name=job_dir_name,
            job_root=job_root,
            plan=plan,
            evidence=tuple(evidence),
            archives=tuple(archives),
            audit=tuple(item.freeze() for item in audit),
            failures=tuple(failures),
        )

    async def _search(
        self,
        plan: ResearchSearchPlan,
        audit: list[_MutableAudit],
        failures: list[ResearchPipelineFailure],
    ) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        seen_urls: dict[str, str] = {}
        for planned in plan.queries:
            searched_at = _require_now(self._now())
            try:
                hits = await self.provider.search(planned.as_provider_query())
                if not isinstance(hits, Sequence) or isinstance(hits, (str, bytes)):
                    raise TypeError("search provider returned a non-sequence result")
            except Exception as exc:
                failures.append(
                    ResearchPipelineFailure(
                        phase="search",
                        topic=planned.topic.value,
                        query=planned.query,
                        message=_error_message(exc),
                    ),
                )
                continue

            for raw_hit in hits:
                if not isinstance(raw_hit, SearchHit):
                    failures.append(
                        ResearchPipelineFailure(
                            phase="search",
                            topic=planned.topic.value,
                            query=planned.query,
                            message="search provider returned an invalid hit",
                        ),
                    )
                    continue
                url = str(raw_hit.url)
                key = _canonical_url(url)
                record = _MutableAudit(
                    query=planned.query,
                    topic=planned.topic.value,
                    url=url,
                    searched_at=searched_at,
                )
                if key in seen_urls:
                    record.outcome = "DUPLICATE_URL"
                    record.duplicate_of = seen_urls[key]
                    audit.append(record)
                    continue
                if len(candidates) >= self.config.max_sources:
                    record.outcome = "SOURCE_LIMIT"
                    record.error = "maximum unique source count reached"
                    audit.append(record)
                    continue
                seen_urls[key] = url
                audit.append(record)
                candidates.append(
                    _Candidate(
                        query=planned.query,
                        topic=planned.topic.value,
                        url=url,
                        hit=raw_hit,
                        searched_at=searched_at,
                    ),
                )
        return candidates

    async def _fetch_and_archive(
        self,
        candidates: Sequence[_Candidate],
        audit: list[_MutableAudit],
        failures: list[ResearchPipelineFailure],
        evidence: list[SourceEvidence],
        archives: list[SourceArchiveResult],
        source_archive: SourceArchive,
    ) -> None:
        semaphore = asyncio.Semaphore(self.config.max_fetch_concurrency)

        async def fetch_one(
            candidate: _Candidate,
        ) -> tuple[_Candidate, FetchedDocument | Exception, datetime]:
            async with semaphore:
                try:
                    document = await self.provider.fetch(candidate.url)
                    return candidate, document, _require_now(self._now())
                except Exception as exc:
                    return candidate, exc, _require_now(self._now())

        fetched = await asyncio.gather(*(fetch_one(item) for item in candidates))
        seen_content: dict[str, tuple[str, str]] = {}
        total_bytes = 0
        for candidate, value, fetched_at in fetched:
            record = _find_audit(audit, candidate.url)
            if isinstance(value, Exception):
                message = _error_message(value)
                _update_record(record, fetched_at=fetched_at, outcome="FETCH_FAILED", error=message)
                failures.append(
                    ResearchPipelineFailure(
                        phase="fetch",
                        topic=candidate.topic,
                        query=candidate.query,
                        url=candidate.url,
                        message=message,
                    ),
                )
                continue
            document = value
            if not isinstance(document, FetchedDocument):
                message = "fetch provider returned an invalid document"
                _update_record(record, fetched_at=fetched_at, outcome="FETCH_FAILED", error=message)
                failures.append(
                    ResearchPipelineFailure(
                        phase="fetch",
                        topic=candidate.topic,
                        query=candidate.query,
                        url=candidate.url,
                        message=message,
                    ),
                )
                continue
            if _canonical_url(str(document.url)) != _canonical_url(candidate.url):
                message = "fetched document URL does not match requested URL"
                _update_record(record, fetched_at=fetched_at, outcome="FETCH_FAILED", error=message)
                failures.append(
                    ResearchPipelineFailure(
                        phase="fetch",
                        topic=candidate.topic,
                        query=candidate.query,
                        url=candidate.url,
                        message=message,
                    ),
                )
                continue

            digest = hashlib.sha256(document.body.encode("utf-8")).hexdigest()
            _update_record(record, fetched_at=fetched_at, content_sha256=digest)
            previous = seen_content.get(digest)
            if previous is not None:
                source_id, previous_url = previous
                _update_record(
                    record,
                    outcome="DUPLICATE_CONTENT",
                    duplicate_of=previous_url,
                    source_id=source_id,
                )
                continue
            body_bytes = len(document.body.encode("utf-8"))
            if total_bytes + body_bytes > self.config.max_total_bytes:
                message = "maximum total source bytes reached"
                _update_record(
                    record,
                    fetched_at=fetched_at,
                    outcome="SOURCE_SIZE_LIMIT",
                    error=message,
                )
                failures.append(
                    ResearchPipelineFailure(
                        phase="archive",
                        topic=candidate.topic,
                        query=candidate.query,
                        url=candidate.url,
                        message=message,
                    ),
                )
                continue
            try:
                source = build_source_evidence(
                    document,
                    source_class=self.config.source_class,
                    publisher=document.author,
                    retrieval_method=self.config.retrieval_method,
                    fetched_at=fetched_at,
                    published_at=_parse_published_at(document.published),
                    max_excerpt_length=self.config.max_excerpt_length,
                )
                receipt = await source_archive.archive(document, source)
            except Exception as exc:
                message = _error_message(exc)
                _update_record(record, outcome="ARCHIVE_FAILED", error=message)
                failures.append(
                    ResearchPipelineFailure(
                        phase="archive",
                        topic=candidate.topic,
                        query=candidate.query,
                        url=candidate.url,
                        message=message,
                    ),
                )
                continue
            seen_content[digest] = (source.source_id, candidate.url)
            total_bytes += body_bytes
            evidence.append(source)
            archives.append(receipt)
            _update_record(
                record,
                outcome="ARCHIVED" if not receipt.already_present else "ARCHIVED_EXISTING",
                source_id=source.source_id,
                archive_path=receipt.relative_path,
            )

    async def _persist_audit(
        self,
        job_root: Path,
        *,
        board_name: str,
        plan: ResearchSearchPlan | None,
        audit: Sequence[_MutableAudit],
        failures: Sequence[ResearchPipelineFailure],
    ) -> None:
        payload: dict[str, Any] = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "board_name": board_name,
            "plan": plan.model_dump(mode="json") if plan is not None else None,
            "records": [item.freeze().as_json() for item in audit],
            "failures": [item.as_json() for item in failures],
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            destination = resolve_contained_path(
                self.job_store.root,
                Path(job_root.name) / AUDIT_FILENAME,
            )
            await atomic_write_text(destination, encoded)
        except Exception as exc:
            raise ResearchPipelineStorageError("research audit could not be persisted") from exc

    def _next_time(self, current: datetime) -> datetime:
        candidate = _require_now(self._now())
        return candidate if candidate >= current else current


ResearchOrchestrator = ResearchPipeline


async def run_research_pipeline(
    board_name: str,
    *,
    provider: RuleResearchProvider,
    workbench_root: str | Path = Path("vault") / "_workbench",
    job_store: WorkbenchJobStore | None = None,
    config: ResearchPipelineConfig | None = None,
    plan_factory: ResearchPipelinePlanFactory = prepare_research_plan,
    now: Callable[[], datetime] | None = None,
) -> ResearchPipelineResult:
    """Convenience entry point for starting controlled research by board name."""

    return await ResearchPipeline(
        provider,
        workbench_root=workbench_root,
        job_store=job_store,
        config=config,
        plan_factory=plan_factory,
        now=now,
    ).run(board_name)


def _require_ready_plan(plan: ResearchSearchPlan) -> None:
    if not isinstance(plan, ResearchSearchPlan):
        raise ResearchPipelinePreflightError("plan factory returned an invalid research plan")
    if not plan.is_preflighted or plan.preflight is None or not plan.preflight.ready:
        raise ResearchPipelinePreflightError("research plan is not ready for Exa HTTPS search")


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = hostname
    if parts.username is not None:
        netloc = parts.netloc.lower()
    elif port is not None and not (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _find_audit(audit: Sequence[_MutableAudit], url: str) -> _MutableAudit:
    for item in audit:
        if item.url == url and item.outcome == "SEARCH_HIT":
            return item
    raise ResearchPipelineError("audit record for fetched URL is missing")


def _update_record(record: _MutableAudit, **changes: Any) -> None:
    for key, value in changes.items():
        setattr(record, key, value)


def _parse_published_at(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != datetime.now(UTC).utcoffset():
        raise ValueError("pipeline clock must return an aware UTC datetime")
    return value.astimezone(UTC)


def _error_message(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        message = error.__class__.__name__
    return message[:2_000]


__all__ = [
    "AUDIT_FILENAME",
    "AUDIT_SCHEMA_VERSION",
    "DEFAULT_LOCALE",
    "DEFAULT_MAX_EXCERPT_LENGTH",
    "DEFAULT_MAX_FETCH_CONCURRENCY",
    "DEFAULT_MAX_SOURCES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_RETRIEVAL_METHOD",
    "ResearchAuditRecord",
    "ResearchOrchestrator",
    "ResearchPipeline",
    "ResearchPipelineConfig",
    "ResearchPipelineError",
    "ResearchPipelineFailure",
    "ResearchPipelineLimitError",
    "ResearchPipelinePreflightError",
    "ResearchPipelineResult",
    "ResearchPipelineStorageError",
    "run_research_pipeline",
]
