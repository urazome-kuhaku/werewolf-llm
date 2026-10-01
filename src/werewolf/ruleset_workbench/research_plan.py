"""Deterministic board research plans and Agent Reach preflight.

This module owns the first, deliberately small step of the ruleset
workbench.  A board name is expanded into a fixed set of Chinese search
queries that cover the rule dimensions required by the knowledge design.  A
single ``agent-reach doctor --json`` invocation is performed before a plan is
handed to the search provider.  The doctor is launched directly with
``create_subprocess_exec``; no shell, socket transport, or page fetching is
involved here.

The plan is intentionally independent from evidence storage.  Search and
fetch code can consume :class:`PlannedSearchQuery` values after this module
has confirmed that the configured Exa HTTP route is available.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .research_provider import SearchQuery

MAX_BOARD_NAME_LENGTH = 128
MAX_QUERY_LENGTH = 512
MAX_SEARCH_QUERIES = 8
DEFAULT_QUERY_RESULTS = 5
# Agent Reach may initialize its mcporter-backed HTTPS route before emitting
# the JSON report.  On Windows this regularly takes tens of seconds, so the
# default must cover startup while remaining a bounded subprocess wait.
DEFAULT_DOCTOR_TIMEOUT_SECONDS = 90.0
DEFAULT_DOCTOR_MAX_OUTPUT_BYTES = 256_000
EXA_HTTP_ENDPOINT = "https://mcp.exa.ai/mcp"

_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_SOCKET_PATTERN = re.compile(r"\b(?:socket|websocket|ws|wss)\b", re.IGNORECASE)
_HTTP_PATTERN = re.compile(r"\bhttps?\b|https?://", re.IGNORECASE)

ProcessFactory: TypeAlias = Callable[..., Awaitable[Any]]


@unique
class SearchTopic(StrEnum):
    """Stable coverage dimensions represented in a research plan."""

    OFFICIAL = "OFFICIAL"
    COMPOSITION = "COMPOSITION"
    ROLE_ABILITIES = "ROLE_ABILITIES"
    NIGHT_ORDER = "NIGHT_ORDER"
    WOLF_TEAM = "WOLF_TEAM"
    DAY_VOTING = "DAY_VOTING"
    DEATH_AND_VICTORY = "DEATH_AND_VICTORY"
    INTERACTIONS_AND_VARIANTS = "INTERACTIONS_AND_VARIANTS"


class AgentReachTransport(StrEnum):
    """Transport selected for the supported Exa research route."""

    MCPORTER_HTTPS = "mcporter_https"


class AgentReachPreflightError(RuntimeError):
    """Base class for an unsuccessful Agent Reach preflight."""


class AgentReachDoctorUnavailableError(AgentReachPreflightError):
    """The configured Agent Reach executable could not be started."""


class AgentReachDoctorTimeoutError(AgentReachPreflightError):
    """The doctor process exceeded its bounded timeout."""


class AgentReachDoctorOutputLimitError(AgentReachPreflightError):
    """The doctor process exceeded its bounded output allowance."""


class AgentReachDoctorCommandError(AgentReachPreflightError):
    """The doctor process returned a non-zero status."""


class AgentReachDoctorResponseError(AgentReachPreflightError):
    """The doctor process did not return a valid machine report."""


class UnsupportedAgentReachBackendError(AgentReachPreflightError):
    """The active search route is unavailable or not the Exa HTTP route."""


class InvalidResearchPlanError(ValueError):
    """The supplied board name or query plan is invalid."""


class PlannedSearchQuery(BaseModel):
    """One bounded, deterministic query generated from a board name."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    topic: SearchTopic
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    purpose: str = Field(min_length=1, max_length=256)
    max_results: int = Field(default=DEFAULT_QUERY_RESULTS, ge=1, le=10)

    @field_validator("query", "purpose")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query text must not be empty")
        if _CONTROL_CHARACTER_PATTERN.search(value):
            raise ValueError("query text must not contain control characters")
        return value

    def as_provider_query(self) -> SearchQuery:
        """Convert this plan item to the existing bounded provider request."""

        return SearchQuery(query=self.query, max_results=self.max_results)


class AgentReachDoctorStatus(BaseModel):
    """Credential-free result of one ``agent-reach doctor --json`` run."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    executable: str = Field(min_length=1, max_length=4_096)
    doctor_exit_code: int = 0
    active_backend: str = Field(min_length=1, max_length=128)
    available: bool = True
    transport: AgentReachTransport = AgentReachTransport.MCPORTER_HTTPS
    endpoint: str = EXA_HTTP_ENDPOINT
    capabilities: tuple[str, ...] = ()

    @field_validator("executable", "active_backend", "endpoint")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("doctor status text must not be empty")
        if _CONTROL_CHARACTER_PATTERN.search(value):
            raise ValueError("doctor status text must not contain control characters")
        return value

    @property
    def ready(self) -> bool:
        """Whether the status can be passed to the search provider."""

        return (
            self.available
            and _is_supported_exa_backend(self.active_backend)
            and self.transport is AgentReachTransport.MCPORTER_HTTPS
        )

    @property
    def is_ready(self) -> bool:
        """Compatibility spelling for callers using an explicit predicate."""

        return self.ready


class ResearchSearchPlan(BaseModel):
    """Immutable search plan for one board and its optional preflight result."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    board_name: str = Field(min_length=1, max_length=MAX_BOARD_NAME_LENGTH)
    queries: tuple[PlannedSearchQuery, ...]
    preflight: AgentReachDoctorStatus | None = None

    @field_validator("board_name")
    @classmethod
    def validate_board_name(cls, value: str) -> str:
        return _normalize_board_name(value)

    @field_validator("queries")
    @classmethod
    def validate_queries(
        cls,
        value: tuple[PlannedSearchQuery, ...],
    ) -> tuple[PlannedSearchQuery, ...]:
        if not value:
            raise ValueError("research plan must contain at least one query")
        if len(value) > MAX_SEARCH_QUERIES:
            raise ValueError(f"research plan cannot contain more than {MAX_SEARCH_QUERIES} queries")
        keys: set[str] = set()
        for item in value:
            key = _canonical_query(item.query)
            if key in keys:
                raise ValueError("research plan queries must be unique")
            keys.add(key)
        return value

    @property
    def query_texts(self) -> tuple[str, ...]:
        """Return query text in its stable execution order."""

        return tuple(item.query for item in self.queries)

    @property
    def provider_queries(self) -> tuple[SearchQuery, ...]:
        """Return bounded requests accepted by the existing provider."""

        return tuple(item.as_provider_query() for item in self.queries)

    @property
    def is_preflighted(self) -> bool:
        """Whether a doctor result has been attached to this plan."""

        return self.preflight is not None

    def with_preflight(self, status: AgentReachDoctorStatus) -> ResearchSearchPlan:
        """Attach one successful doctor result without mutating this plan."""

        if not isinstance(status, AgentReachDoctorStatus):
            raise TypeError("status must be an AgentReachDoctorStatus")
        if not status.ready:
            raise UnsupportedAgentReachBackendError(
                "cannot attach a non-ready Agent Reach status to a research plan",
            )
        return self.model_copy(update={"preflight": status})


@dataclass(frozen=True, slots=True)
class AgentReachDoctorConfig:
    """Bounds and executable selection for one doctor invocation."""

    executable: str = "agent-reach"
    timeout_seconds: float = DEFAULT_DOCTOR_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_DOCTOR_MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        executable = os.fspath(self.executable)
        if not executable:
            raise ValueError("doctor executable must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("doctor timeout_seconds must be positive")
        if self.max_output_bytes < 1:
            raise ValueError("doctor max_output_bytes must be positive")
        object.__setattr__(self, "executable", executable)


def _normalize_board_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("board_name must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized).strip()
    if not normalized:
        raise InvalidResearchPlanError("board_name must not be empty")
    if len(normalized) > MAX_BOARD_NAME_LENGTH:
        raise InvalidResearchPlanError(
            f"board_name must be at most {MAX_BOARD_NAME_LENGTH} characters",
        )
    if _CONTROL_CHARACTER_PATTERN.search(normalized):
        raise InvalidResearchPlanError("board_name must not contain control characters")
    return normalized


def _canonical_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized).strip()
    return normalized.casefold()


def deduplicate_query_texts(
    queries: Iterable[str],
    *,
    max_queries: int = MAX_SEARCH_QUERIES,
) -> tuple[str, ...]:
    """Normalize and de-duplicate query text while preserving first-seen order."""

    if isinstance(queries, (str, bytes)):
        raise TypeError("queries must be an iterable of strings")
    if not isinstance(max_queries, int) or isinstance(max_queries, bool) or max_queries < 1:
        raise ValueError("max_queries must be a positive integer")

    result: list[str] = []
    seen: set[str] = set()
    for raw in queries:
        if not isinstance(raw, str):
            raise TypeError("every query must be a string")
        value = _WHITESPACE_PATTERN.sub(" ", unicodedata.normalize("NFKC", raw)).strip()
        if not value:
            continue
        if len(value) > MAX_QUERY_LENGTH:
            raise InvalidResearchPlanError(
                f"query must be at most {MAX_QUERY_LENGTH} characters",
            )
        if _CONTROL_CHARACTER_PATTERN.search(value):
            raise InvalidResearchPlanError("query must not contain control characters")
        key = _canonical_query(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) > max_queries:
            raise InvalidResearchPlanError(f"query count cannot exceed {max_queries}")
    return tuple(result)


_QUERY_TEMPLATES: tuple[tuple[SearchTopic, str, str], ...] = (
    (SearchTopic.OFFICIAL, "{board} 狼人杀 官方 规则", "优先寻找官方或赛事规则原文"),
    (SearchTopic.COMPOSITION, "{board} 狼人杀 板子配置 人数 阵营 角色", "核对人数、阵营和角色组成"),
    (
        SearchTopic.ROLE_ABILITIES,
        "{board} 狼人杀 角色技能 使用限制",
        "核对各角色技能、资源和目标限制",
    ),
    (
        SearchTopic.NIGHT_ORDER,
        "{board} 狼人杀 夜间行动顺序 信息可见性",
        "核对夜间顺序、信息和结算窗口",
    ),
    (
        SearchTopic.WOLF_TEAM,
        "{board} 狼人杀 狼队 相认 刀口 归票",
        "核对狼队可见关系、刀口和沟通规则",
    ),
    (
        SearchTopic.DAY_VOTING,
        "{board} 狼人杀 发言 警长 投票 PK 弃票",
        "核对发言、警长、投票、PK和弃票",
    ),
    (
        SearchTopic.DEATH_AND_VICTORY,
        "{board} 狼人杀 死亡 遗言 胜利条件",
        "核对死亡公布、遗言和胜负检查",
    ),
    (
        SearchTopic.INTERACTIONS_AND_VARIANTS,
        "{board} 狼人杀 特殊交互 规则变体 版本",
        "核对角色交互、平台或赛事变体",
    ),
)


def build_search_plan(
    board_name: str,
    *,
    max_results: int = DEFAULT_QUERY_RESULTS,
) -> ResearchSearchPlan:
    """Build the fixed eight-dimension plan for a board name.

    The function is pure: it performs no subprocess or network operation and
    does not write a workbench artifact.  ``prepare_research_plan`` adds the
    one required Agent Reach preflight when a caller is ready to search.
    """

    board = _normalize_board_name(board_name)
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise TypeError("max_results must be an integer")
    if not 1 <= max_results <= 10:
        raise ValueError("max_results must be between 1 and 10")

    queries: list[PlannedSearchQuery] = []
    seen: set[str] = set()
    for topic, template, purpose in _QUERY_TEMPLATES:
        query = template.format(board=board)
        key = _canonical_query(query)
        if key in seen:
            continue
        seen.add(key)
        queries.append(
            PlannedSearchQuery(
                topic=topic,
                query=query,
                purpose=purpose,
                max_results=max_results,
            ),
        )

    return ResearchSearchPlan(board_name=board, queries=tuple(queries))


async def run_agent_reach_doctor(
    *,
    executable: str | os.PathLike[str] = "agent-reach",
    timeout_seconds: float = DEFAULT_DOCTOR_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_DOCTOR_MAX_OUTPUT_BYTES,
    process_factory: ProcessFactory | None = None,
) -> AgentReachDoctorStatus:
    """Run one bounded, shell-free Agent Reach doctor and validate its route."""

    configured = AgentReachDoctorConfig(
        executable=_resolve_executable(os.fspath(executable)),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    factory = process_factory or asyncio.create_subprocess_exec
    command = (configured.executable, "doctor", "--json")
    try:
        process = await factory(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AgentReachDoctorUnavailableError(
            f"Agent Reach executable not found: {configured.executable}",
        ) from exc
    except OSError as exc:
        raise AgentReachDoctorUnavailableError(
            f"Agent Reach doctor could not start: {configured.executable}",
        ) from exc

    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                _communicate_bounded(process, configured.max_output_bytes),
                timeout=configured.timeout_seconds,
            )
        except TimeoutError as exc:
            await _terminate_process(process)
            raise AgentReachDoctorTimeoutError(
                f"Agent Reach doctor timed out after {configured.timeout_seconds:g}s",
            ) from exc
        except _OutputLimitExceeded as exc:
            await _terminate_process(process)
            raise AgentReachDoctorOutputLimitError(
                f"Agent Reach doctor output exceeds {configured.max_output_bytes} bytes",
            ) from exc

        returncode = getattr(process, "returncode", None)
        if returncode is None:
            wait = getattr(process, "wait", None)
            if wait is not None:
                returncode = await wait()
        if returncode not in (None, 0):
            detail = stderr.decode("utf-8", errors="replace")[:2_000].strip()
            suffix = f": {detail}" if detail else ""
            raise AgentReachDoctorCommandError(
                f"Agent Reach doctor exited with status {returncode}{suffix}",
            )

        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentReachDoctorResponseError(
                "Agent Reach doctor returned malformed JSON",
            ) from exc
        return _status_from_doctor_payload(
            payload,
            executable=configured.executable,
        )
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    except AgentReachPreflightError:
        raise
    except Exception as exc:
        raise AgentReachDoctorResponseError(
            "Agent Reach doctor report could not be interpreted",
        ) from exc


async def preflight_agent_reach(**kwargs: Any) -> AgentReachDoctorStatus:
    """Descriptive alias for :func:`run_agent_reach_doctor`."""

    return await run_agent_reach_doctor(**kwargs)


async def prepare_research_plan(
    board_name: str,
    *,
    executable: str | os.PathLike[str] = "agent-reach",
    timeout_seconds: float = DEFAULT_DOCTOR_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_DOCTOR_MAX_OUTPUT_BYTES,
    process_factory: ProcessFactory | None = None,
    max_results: int = DEFAULT_QUERY_RESULTS,
) -> ResearchSearchPlan:
    """Build a plan and run exactly one preflight before returning it."""

    plan = build_search_plan(board_name, max_results=max_results)
    status = await run_agent_reach_doctor(
        executable=executable,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        process_factory=process_factory,
    )
    return plan.with_preflight(status)


async def build_research_plan(
    board_name: str,
    **kwargs: Any,
) -> ResearchSearchPlan:
    """Compatibility alias for the plan-plus-preflight workflow."""

    return await prepare_research_plan(board_name, **kwargs)


async def _communicate_bounded(process: Any, limit: int) -> tuple[bytes, bytes]:
    stdout_stream = getattr(process, "stdout", None)
    stderr_stream = getattr(process, "stderr", None)
    if stdout_stream is None or stderr_stream is None:
        stdout, stderr = await process.communicate()
        stdout_bytes = _as_bytes(stdout)
        stderr_bytes = _as_bytes(stderr)
        if len(stdout_bytes) + len(stderr_bytes) > limit:
            raise _OutputLimitExceeded
        return stdout_bytes, stderr_bytes

    stdout_task = asyncio.create_task(_read_limited(stdout_stream, limit))
    stderr_task = asyncio.create_task(_read_limited(stderr_stream, limit))
    tasks: set[asyncio.Task[bytes]] = {stdout_task, stderr_task}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
        if pending:
            await asyncio.gather(*pending)
        stdout = stdout_task.result()
        stderr = stderr_task.result()
        if len(stdout) + len(stderr) > limit:
            raise _OutputLimitExceeded
        return stdout, stderr
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _read_limited(stream: Any, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = _as_bytes(await stream.read(min(65_536, limit - total + 1)))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise _OutputLimitExceeded
        chunks.append(chunk)


async def _terminate_process(process: Any) -> None:
    terminate = getattr(process, "terminate", None)
    if terminate is not None:
        try:
            terminate()
        except (OSError, ProcessLookupError):
            pass
    wait = getattr(process, "wait", None)
    if wait is not None:
        try:
            await asyncio.wait_for(wait(), timeout=1.0)
            return
        except (TimeoutError, ProcessLookupError, OSError):
            pass
    kill = getattr(process, "kill", None)
    if kill is not None:
        try:
            kill()
        except (OSError, ProcessLookupError):
            pass
    if wait is not None:
        try:
            await asyncio.wait_for(wait(), timeout=1.0)
        except (TimeoutError, ProcessLookupError, OSError):
            pass


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError("doctor subprocess output must be bytes or text")


class _OutputLimitExceeded(Exception):
    """Internal signal from bounded stream readers."""


def _resolve_executable(executable: str) -> str:
    if os.name == "nt" and executable == "agent-reach":
        return shutil.which("agent-reach.exe") or executable
    return executable


def _status_from_doctor_payload(
    payload: Any,
    *,
    executable: str,
) -> AgentReachDoctorStatus:
    candidate = _find_search_backend(payload)
    if candidate is None:
        raise AgentReachDoctorResponseError(
            "Agent Reach doctor report has no search active_backend",
        )

    active_backend = candidate.get("active_backend")
    if not isinstance(active_backend, str) or not active_backend.strip():
        raise AgentReachDoctorResponseError(
            "Agent Reach doctor search report has no active_backend",
        )
    active_backend = active_backend.strip()
    if not _is_supported_exa_backend(active_backend):
        raise UnsupportedAgentReachBackendError(
            f"unsupported Agent Reach search backend: {active_backend}",
        )

    available = _extract_availability(candidate)
    if available is False:
        raise UnsupportedAgentReachBackendError(
            "Agent Reach Exa search backend is reported unavailable",
        )
    if available is None:
        raise AgentReachDoctorResponseError(
            "Agent Reach doctor search report has no availability status",
        )

    transport_signal = _extract_transport_signal(candidate)
    if transport_signal is not None and _SOCKET_PATTERN.search(transport_signal):
        raise UnsupportedAgentReachBackendError(
            "Agent Reach search route uses Socket/WebSocket transport",
        )
    if (
        transport_signal is not None
        and not _HTTP_PATTERN.search(transport_signal)
        and "mcporter" not in transport_signal.casefold()
    ):
        raise UnsupportedAgentReachBackendError(
            "Agent Reach search route is not an HTTP/HTTPS transport",
        )

    capabilities = _extract_capabilities(candidate)
    return AgentReachDoctorStatus(
        executable=executable,
        doctor_exit_code=0,
        active_backend=active_backend,
        available=True,
        transport=AgentReachTransport.MCPORTER_HTTPS,
        endpoint=EXA_HTTP_ENDPOINT,
        capabilities=capabilities,
    )


def _find_search_backend(payload: Any) -> Mapping[str, Any] | None:
    candidates: list[tuple[int, Mapping[str, Any]]] = []

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            if "active_backend" in value:
                backend = value.get("active_backend")
                score = 0
                lowered_path = " ".join(path).casefold()
                if "search" in lowered_path or "exa" in lowered_path:
                    score += 20
                if backend == "exa_search":
                    score += 10
                if "available" in value or "status" in value:
                    score += 2
                candidates.append((score, value))
            for key, child in value.items():
                if isinstance(key, str):
                    visit(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (str(index),))

    visit(payload, ())
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _extract_availability(candidate: Mapping[str, Any]) -> bool | None:
    for key in ("available", "is_available", "enabled", "ready"):
        value = candidate.get(key)
        if isinstance(value, bool):
            return value
    for key in ("status", "state", "health"):
        value = candidate.get(key)
        if isinstance(value, str):
            normalized = value.casefold().strip()
            if (
                normalized in {"available", "ready", "ok", "healthy", "connected", "configured"}
                or "available" in normalized
                or "ready" in normalized
            ) and not any(
                marker in normalized
                for marker in ("unavailable", "not available", "not_available", "not ready")
            ):
                return True
            if normalized in {
                "unavailable",
                "disabled",
                "offline",
                "error",
                "not_configured",
                "not configured",
            }:
                return False
    return None


def _is_supported_exa_backend(value: str) -> bool:
    """Accept Agent Reach's human label as well as its stable backend ID.

    Agent Reach 1.5 reports the active search backend as ``Exa via
    mcporter``.  Older and newer versions may expose the stable
    ``exa_search`` ID instead.  Both labels describe the same HTTP route;
    labels mentioning a socket transport are deliberately rejected.
    """

    normalized = value.casefold().strip()
    if _SOCKET_PATTERN.search(normalized):
        return False
    if normalized == "exa_search":
        return True
    return "exa" in normalized and "mcporter" in normalized


def _extract_transport_signal(candidate: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    for key in (
        "transport",
        "protocol",
        "route",
        "method",
        "command",
        "endpoint",
        "url",
        "backend_command",
    ):
        value = candidate.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            parts.extend(item for item in value if isinstance(item, str))
    return " ".join(parts) or None


def _extract_capabilities(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    value = candidate.get("capabilities")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    cleaned = {
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip() and not _CONTROL_CHARACTER_PATTERN.search(item)
    }
    return tuple(sorted(cleaned))


# Public aliases used by callers that prefer shorter workbench names.
SearchPlan = ResearchSearchPlan
DoctorStatus = AgentReachDoctorStatus
PreflightStatus = AgentReachDoctorStatus
PlannedQuery = PlannedSearchQuery


__all__ = [
    "AgentReachDoctorCommandError",
    "AgentReachDoctorConfig",
    "AgentReachDoctorOutputLimitError",
    "AgentReachDoctorResponseError",
    "AgentReachDoctorStatus",
    "AgentReachDoctorTimeoutError",
    "AgentReachDoctorUnavailableError",
    "AgentReachPreflightError",
    "AgentReachTransport",
    "DEFAULT_DOCTOR_MAX_OUTPUT_BYTES",
    "DEFAULT_DOCTOR_TIMEOUT_SECONDS",
    "DEFAULT_QUERY_RESULTS",
    "EXA_HTTP_ENDPOINT",
    "InvalidResearchPlanError",
    "MAX_SEARCH_QUERIES",
    "PlannedQuery",
    "PlannedSearchQuery",
    "PreflightStatus",
    "ResearchSearchPlan",
    "SearchPlan",
    "SearchTopic",
    "UnsupportedAgentReachBackendError",
    "build_research_plan",
    "build_search_plan",
    "deduplicate_query_texts",
    "preflight_agent_reach",
    "prepare_research_plan",
    "run_agent_reach_doctor",
]
