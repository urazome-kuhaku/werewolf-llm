"""Restricted Pi transport for ruleset authoring.

The Pi process in this module is an authoring assistant, not a rules
authority.  It receives only bounded evidence excerpts and returns an
untrusted JSON draft.  In particular, this module deliberately does not
construct :class:`RuleClaim`, assign a claim status, validate quotations, or
write a workbench/Vault artifact.  Those operations belong to the deterministic
claim extraction and publication stages.

Each evidence batch is sent to a fresh, non-interactive Pi session.  The
session has no tools, extensions, skills, context files, prompt templates, or
themes.  Keeping the process boundary here makes the transport straightforward
to replace with a fixture in tests and keeps it independent of game runtime
state.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from werewolf.knowledge.refs import KnowledgeId

DEFAULT_PROVIDER = "github-copilot"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_THINKING = "minimal"
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_OUTPUT_BYTES = 512_000
DEFAULT_MAX_PROMPT_CHARACTERS = 32_000
DEFAULT_MAX_EVIDENCE_CHARACTERS = 4_000
MAX_BATCHES = 128
MAX_CLAIMS_PER_BATCH = 256
MAX_DRAFT_CHARACTERS = 24_000

_SHA256_LENGTH = 64
_SHA256_HEX = frozenset("0123456789abcdef")
_CLAIM_ID_MAX_LENGTH = 128
_KEY_MAX_LENGTH = 128
_SCOPE_MAX_LENGTH = 32

BoardName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, strip_whitespace=True, strict=True),
]
CandidateId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, strip_whitespace=True, strict=True),
]


def _list_to_tuple(value: object) -> object:
    """Accept JSON arrays while retaining immutable validated results."""

    if isinstance(value, list):
        return tuple(value)
    return value


class SynthesisProviderError(RuntimeError):
    """Base class for expected Pi authoring transport failures."""


class SynthesisInputError(SynthesisProviderError, ValueError):
    """The bounded board, batch, or evidence input is invalid."""


class SynthesisProviderUnavailableError(SynthesisProviderError):
    """The configured Pi executable could not be started or found."""


class SynthesisProviderTimeoutError(SynthesisProviderError):
    """The independent Pi session exceeded its wall-clock limit."""


class SynthesisProviderOutputLimitError(SynthesisProviderError):
    """Pi produced more output than the transport is willing to retain."""


class SynthesisProviderCommandError(SynthesisProviderError):
    """Pi exited unsuccessfully."""


class SynthesisProviderResponseError(SynthesisProviderError):
    """Pi output was not a strict, supported JSON draft."""


class EvidenceExcerpt(BaseModel):
    """A bounded, immutable excerpt allowed into one authoring prompt.

    ``content_sha256`` identifies the frozen complete source content.  The
    excerpt is intentionally kept as text rather than a URL/path so that an
    authoring session cannot fetch or discover more source material.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    source_id: KnowledgeId
    content_sha256: str
    excerpt: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=DEFAULT_MAX_EVIDENCE_CHARACTERS,
            strip_whitespace=True,
            strict=True,
        ),
    ]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value):
            raise ValueError("source_id must be a lowercase logical identifier")
        return value

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        if len(value) != _SHA256_LENGTH or any(character not in _SHA256_HEX for character in value):
            raise ValueError("content_sha256 must be a lowercase SHA-256 hexadecimal digest")
        return value

    @property
    def text(self) -> str:
        """Compatibility spelling for callers that call the excerpt text."""

        return self.excerpt


# ``SynthesisEvidence`` reads naturally in provider code and keeps a stable
# public alias if the workbench later adds richer evidence metadata.
SynthesisEvidence = EvidenceExcerpt


class DraftEvidenceReference(BaseModel):
    """A model supplied, verbatim citation attached to a draft claim."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    source_id: KnowledgeId
    excerpt: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=DEFAULT_MAX_EVIDENCE_CHARACTERS,
            strip_whitespace=True,
            strict=True,
        ),
    ]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value):
            raise ValueError("source_id must be a lowercase logical identifier")
        return value

    @property
    def quote(self) -> str:
        """A descriptive alias used by callers discussing quotations."""

        return self.excerpt


class DraftClaim(BaseModel):
    """Untrusted claim-shaped JSON returned by Pi.

    There is intentionally no ``status`` field.  A Pi response cannot become
    ``SUPPORTED`` by transport parsing; deterministic citation checking and
    human review happen in later workbench stages.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    claim_id: Annotated[
        str,
        StringConstraints(min_length=1, max_length=_CLAIM_ID_MAX_LENGTH, strict=True),
    ]
    key: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=_KEY_MAX_LENGTH,
            strip_whitespace=True,
            strict=True,
        ),
    ]
    value: JsonValue
    scope: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=_SCOPE_MAX_LENGTH,
            strip_whitespace=True,
            strict=True,
        ),
    ]
    conditions: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: Annotated[
        tuple[DraftEvidenceReference, ...],
        BeforeValidator(_list_to_tuple),
        Field(min_length=1),
    ]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extraction_note: Annotated[str, StringConstraints(max_length=2_000, strict=True)] | None = None

    @field_validator("claim_id")
    @classmethod
    def validate_claim_id(cls, value: str) -> str:
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value):
            raise ValueError("claim_id must be a lowercase logical identifier")
        return value


class SynthesisBatchResult(BaseModel):
    """One strict result from one isolated evidence batch."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    claims: Annotated[
        tuple[DraftClaim, ...],
        BeforeValidator(_list_to_tuple),
        Field(max_length=MAX_CLAIMS_PER_BATCH),
    ] = ()
    draft: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def reject_supported_statuses(self) -> SynthesisBatchResult:
        # The model shape already excludes ``status``.  This validator is a
        # named invariant to make the publication boundary obvious to callers.
        return self


class SynthesisResult(BaseModel):
    """Aggregate untrusted drafts from all isolated evidence batches."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    schema_version: Literal[1] = 1
    board_name: BoardName
    ruleset_candidate_id: CandidateId
    claims: Annotated[
        tuple[DraftClaim, ...],
        BeforeValidator(_list_to_tuple),
    ] = ()
    batches: Annotated[
        tuple[SynthesisBatchResult, ...],
        BeforeValidator(_list_to_tuple),
        Field(min_length=1, max_length=MAX_BATCHES),
    ]
    drafts: Annotated[
        tuple[dict[str, JsonValue], ...],
        BeforeValidator(_list_to_tuple),
    ] = ()


ProcessFactory = Callable[..., Awaitable[Any]]


class RuleSynthesisProvider(Protocol):
    """Minimal workbench protocol implemented by :class:`PiRuleSynthesisProvider`."""

    async def synthesize(
        self,
        board_name: str,
        ruleset_candidate_id: str,
        evidence_batches: Sequence[Sequence[EvidenceExcerpt]],
    ) -> SynthesisResult:
        """Produce untrusted claim/draft JSON from bounded evidence batches."""


def _resolve_executable(executable: str | None) -> str:
    if executable is not None:
        if not executable.strip():
            raise SynthesisInputError("executable must not be empty")
        return executable
    configured = os.environ.get("WEREWOLF_PI_EXECUTABLE")
    if configured and configured.strip():
        return configured
    candidates = ("pi.cmd", "pi") if os.name == "nt" else ("pi",)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise SynthesisProviderUnavailableError("Pi executable was not found")


def _validate_request_text(board_name: str, candidate_id: str) -> tuple[str, str]:
    if not isinstance(board_name, str) or not board_name.strip() or len(board_name.strip()) > 256:
        raise SynthesisInputError("board_name must be non-empty and at most 256 characters")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id.strip()
        or len(candidate_id.strip()) > 64
    ):
        raise SynthesisInputError(
            "ruleset_candidate_id must be non-empty and at most 64 characters"
        )
    normalized_candidate = candidate_id.strip()
    if any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in normalized_candidate
    ):
        raise SynthesisInputError("ruleset_candidate_id must be a lowercase logical identifier")
    return board_name.strip(), normalized_candidate


def _normalize_batches(
    evidence_batches: Sequence[Sequence[EvidenceExcerpt]],
) -> tuple[tuple[EvidenceExcerpt, ...], ...]:
    if isinstance(evidence_batches, (str, bytes, bytearray)):
        raise SynthesisInputError("evidence_batches must be a sequence of evidence batches")
    try:
        batches = tuple(tuple(batch) for batch in evidence_batches)
    except TypeError as exc:
        raise SynthesisInputError(
            "evidence_batches must be a sequence of evidence batches"
        ) from exc
    if not batches:
        raise SynthesisInputError("evidence_batches must contain at least one batch")
    if len(batches) > MAX_BATCHES:
        raise SynthesisInputError(
            f"evidence_batches cannot contain more than {MAX_BATCHES} batches"
        )
    normalized: list[tuple[EvidenceExcerpt, ...]] = []
    for batch_index, batch in enumerate(batches):
        if not batch:
            raise SynthesisInputError(f"evidence batch {batch_index} must not be empty")
        records: list[EvidenceExcerpt] = []
        seen: set[str] = set()
        for item in batch:
            if not isinstance(item, EvidenceExcerpt):
                try:
                    item = EvidenceExcerpt.model_validate(item, strict=True)
                except Exception as exc:
                    raise SynthesisInputError(
                        f"evidence batch {batch_index} contains invalid evidence",
                    ) from exc
            if item.source_id in seen:
                raise SynthesisInputError(
                    f"evidence batch {batch_index} contains duplicate source_id {item.source_id}",
                )
            seen.add(item.source_id)
            records.append(item)
        normalized.append(tuple(records))
    return tuple(normalized)


def build_synthesis_prompt(
    board_name: str,
    ruleset_candidate_id: str,
    evidence: Sequence[EvidenceExcerpt],
    *,
    batch_index: int = 0,
    batch_count: int = 1,
    max_characters: int = DEFAULT_MAX_PROMPT_CHARACTERS,
) -> str:
    """Build one bounded prompt containing only one evidence batch.

    The prompt is JSON-framed so evidence punctuation cannot accidentally
    become an instruction delimiter.  The evidence text remains verbatim
    within the JSON string; no web page or filesystem location is included.
    """

    board_name, ruleset_candidate_id = _validate_request_text(board_name, ruleset_candidate_id)
    if (
        isinstance(max_characters, bool)
        or not isinstance(max_characters, int)
        or max_characters < 1
    ):
        raise SynthesisInputError("max_characters must be a positive integer")
    if batch_count < 1 or not 0 <= batch_index < batch_count:
        raise SynthesisInputError("batch_index must be within batch_count")
    if not evidence:
        raise SynthesisInputError("evidence batch must not be empty")
    records: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, EvidenceExcerpt):
            raise SynthesisInputError("evidence must contain EvidenceExcerpt values")
        records.append(
            {
                "source_id": item.source_id,
                "content_sha256": item.content_sha256,
                "excerpt": item.excerpt,
            },
        )
    request = {
        "schema_version": 1,
        "board_name": board_name,
        "ruleset_candidate_id": ruleset_candidate_id,
        "batch_index": batch_index,
        "batch_count": batch_count,
        "evidence": records,
    }
    prompt = (
        "Restricted ruleset authoring. Read only the supplied evidence and return one "
        "strict JSON object, with no prose or Markdown. This is an untrusted draft: "
        "never add status or SUPPORTED. If any excerpt states a direct rule, claim, "
        "prohibition, or permission, claims MUST contain at least one claim. Short "
        "Chinese markers 不可, 不得, 禁止, 必须, and 可以 still state rules; "
        "女巫不可自救。 is a direct prohibition and must produce a conservative "
        "self-healing claim. Return claims=[] only when no excerpt contains a direct "
        "rule. Do not infer common practice, implication, or missing details, and do "
        "not put a direct rule only in draft. Top-level fields are exactly "
        "schema_version=1, claims, and draft (object or null). Each claim has exactly "
        "claim_id (lowercase id), key (dotted snake_case), value (JSON value), scope, "
        "conditions (object), and non-empty evidence; optional confidence (0..1) and "
        "extraction_note are allowed. Each evidence item has exactly source_id and "
        "excerpt: copy source_id from the request and use a contiguous verbatim "
        "substring of that source excerpt, preserving punctuation; never paraphrase "
        "or translate. A claim key MUST be dotted snake_case with at least two "
        "lowercase snake_case segments, for example witch.can_self_heal; never use "
        "a single segment such as rule_key. Shape: "
        '{"schema_version":1,"claims":[{"claim_id":"rule-id",'
        '"key":"witch.can_self_heal","value":false,"scope":"ROLE",'
        '"conditions":{},"evidence":[{"source_id":"source-id",'
        '"excerpt":"exact text"}]}],"draft":{}}. REQUEST: '
        + json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    )
    if len(prompt) > max_characters:
        raise SynthesisInputError(
            f"synthesis prompt exceeds {max_characters} characters",
        )
    return prompt


def _build_environment() -> dict[str, str]:
    """Keep process inheritance narrow while retaining Pi auth/config basics."""

    allowed_exact = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PI_CODING_AGENT_DIR",
        "PI_PACKAGE_DIR",
        "PI_TELEMETRY",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "SystemRoot",
        "TEMP",
        "TMP",
        "USERPROFILE",
    }
    allowed_provider = {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_COPILOT_TOKEN",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in allowed_exact or key in allowed_provider
    }
    # Windows launchers and Node may look up either spelling. ``os.environ``
    # preserves the spelling present in the parent process, so a service
    # wrapper can otherwise leave one alias absent.
    system_root = environment.get("SystemRoot") or environment.get("SYSTEMROOT")
    if system_root:
        environment.setdefault("SystemRoot", system_root)
        environment.setdefault("SYSTEMROOT", system_root)
    return environment


def _extract_json_text(stdout: bytes | str) -> object:
    """Extract the final model text from Pi text/JSON event output.

    ``--mode json`` emits JSONL events.  Fixtures and future Pi modes may emit
    the result object directly, so both forms are accepted.  The model text
    itself must parse as one JSON object with no surrounding Markdown.
    """

    if isinstance(stdout, bytes):
        try:
            text = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SynthesisProviderResponseError("Pi output is not valid UTF-8") from exc
    elif isinstance(stdout, str):
        text = stdout
    else:
        raise SynthesisProviderResponseError("Pi output must be UTF-8 text")
    if not text.strip():
        raise SynthesisProviderResponseError("Pi returned empty output")
    stripped = text.strip()
    candidates: list[str] = [stripped]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        candidates = lines
    model_texts: list[str] = []
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and "claims" in decoded:
            model_texts.append(candidate)
            continue
        if not isinstance(decoded, dict):
            continue
        message = decoded.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                model_texts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "text"
                        and isinstance(item.get("text"), str)
                    ):
                        model_texts.append(cast(str, item["text"]))
        for key in ("text", "final_text"):
            value = decoded.get(key)
            if isinstance(value, str):
                model_texts.append(value)
    if not model_texts:
        raise SynthesisProviderResponseError("Pi output did not contain a JSON draft")
    raw = model_texts[-1].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SynthesisProviderResponseError("Pi model response is not strict JSON") from exc
    if not isinstance(parsed, dict):
        raise SynthesisProviderResponseError("Pi model response must be a JSON object")
    return parsed


def _parse_batch_result(stdout: bytes | str) -> SynthesisBatchResult:
    try:
        payload = _extract_json_text(stdout)
        return SynthesisBatchResult.model_validate(payload, strict=True)
    except SynthesisProviderResponseError:
        raise
    except Exception as exc:
        raise SynthesisProviderResponseError(
            "Pi JSON draft does not match the strict schema"
        ) from exc


class PiRuleSynthesisProvider:
    """Bounded Pi authoring transport using one isolated session per batch."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        provider: str = DEFAULT_PROVIDER,
        model: str = DEFAULT_MODEL,
        thinking: str = DEFAULT_THINKING,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_prompt_characters: int = DEFAULT_MAX_PROMPT_CHARACTERS,
        session_root: str | Path | None = None,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self.executable = _resolve_executable(executable)
        if not provider.strip() or not model.strip() or not thinking.strip():
            raise SynthesisInputError("provider, model, and thinking must not be empty")
        if timeout_seconds <= 0:
            raise SynthesisInputError("timeout_seconds must be positive")
        if isinstance(max_output_bytes, bool) or max_output_bytes < 1:
            raise SynthesisInputError("max_output_bytes must be positive")
        if isinstance(max_prompt_characters, bool) or max_prompt_characters < 1:
            raise SynthesisInputError("max_prompt_characters must be positive")
        self.provider = provider
        self.model = model
        self.thinking = thinking
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.max_prompt_characters = max_prompt_characters
        self.session_root = Path(session_root) if session_root is not None else None
        self._process_factory = process_factory

    async def synthesize(
        self,
        board_name: str,
        ruleset_candidate_id: str,
        evidence_batches: Sequence[Sequence[EvidenceExcerpt]],
    ) -> SynthesisResult:
        """Run one short-lived, permission-minimized session for each batch."""

        board_name, ruleset_candidate_id = _validate_request_text(board_name, ruleset_candidate_id)
        batches = _normalize_batches(evidence_batches)
        results: list[SynthesisBatchResult] = []
        for batch_index, batch in enumerate(batches):
            prompt = build_synthesis_prompt(
                board_name,
                ruleset_candidate_id,
                batch,
                batch_index=batch_index,
                batch_count=len(batches),
                max_characters=self.max_prompt_characters,
            )
            results.append(await self._run_batch(prompt))
        claims = tuple(claim for result in results for claim in result.claims)
        drafts = tuple(result.draft for result in results if result.draft is not None)
        return SynthesisResult(
            board_name=board_name,
            ruleset_candidate_id=ruleset_candidate_id,
            claims=claims,
            batches=tuple(results),
            drafts=drafts,
        )

    async def extract_claims(
        self,
        board_name: str,
        ruleset_candidate_id: str,
        evidence_batches: Sequence[Sequence[EvidenceExcerpt]],
    ) -> tuple[DraftClaim, ...]:
        """Convenience method returning only untrusted claim drafts."""

        result = await self.synthesize(board_name, ruleset_candidate_id, evidence_batches)
        return result.claims

    async def _run_batch(self, prompt: str) -> SynthesisBatchResult:
        session_id = str(uuid.uuid4())
        owned_session_dir = self.session_root is None
        if self.session_root is None:
            session_dir = Path(tempfile.mkdtemp(prefix="werewolf-pi-authoring-"))
        else:
            session_dir = self.session_root / f"batch-{session_id}"
            session_dir.mkdir(parents=True, exist_ok=False)
        args = [
            self.executable,
            "--mode",
            "json",
            "--print",
            "--provider",
            self.provider,
            "--model",
            self.model,
            "--thinking",
            self.thinking,
            "--session-dir",
            str(session_dir),
            "--session-id",
            session_id,
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
            "--",
            prompt,
        ]
        process: Any = None
        try:
            factory = self._process_factory or asyncio.create_subprocess_exec
            try:
                process = await factory(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(session_dir),
                    env=_build_environment(),
                )
            except OSError as exc:
                raise SynthesisProviderUnavailableError(
                    "could not start Pi authoring session"
                ) from exc
            try:
                stdout, stderr = await asyncio.wait_for(
                    _communicate_bounded(process, self.max_output_bytes),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError as exc:
                await _terminate_process(process)
                raise SynthesisProviderTimeoutError(
                    f"Pi authoring session timed out after {self.timeout_seconds:g}s",
                ) from exc
            except _OutputLimitExceeded as exc:
                await _terminate_process(process)
                raise SynthesisProviderOutputLimitError(
                    f"Pi authoring output exceeds {self.max_output_bytes} bytes",
                ) from exc
            stdout_bytes = _as_bytes(stdout)
            stderr_bytes = _as_bytes(stderr)
            if len(stdout_bytes) + len(stderr_bytes) > self.max_output_bytes:
                await _terminate_process(process)
                raise SynthesisProviderOutputLimitError(
                    f"Pi authoring output exceeds {self.max_output_bytes} bytes",
                )
            returncode = getattr(process, "returncode", None)
            if returncode is None and hasattr(process, "wait"):
                returncode = await process.wait()
            if returncode not in (0, None):
                detail = stderr_bytes.decode("utf-8", errors="replace")[:512].strip()
                suffix = f": {detail}" if detail else ""
                raise SynthesisProviderCommandError(
                    f"Pi authoring process exited with code {returncode}{suffix}",
                )
            return _parse_batch_result(stdout_bytes)
        finally:
            if owned_session_dir:
                shutil.rmtree(session_dir, ignore_errors=True)


def _as_bytes(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise SynthesisProviderResponseError("Pi process output must be bytes or text")


class _OutputLimitExceeded(Exception):
    """Internal signal raised as soon as a bounded pipe exceeds its limit."""


async def _communicate_bounded(process: Any, limit: int) -> tuple[bytes, bytes]:
    """Drain both pipes concurrently while retaining only bounded output."""

    stdout_stream = getattr(process, "stdout", None)
    stderr_stream = getattr(process, "stderr", None)
    if stdout_stream is None or stderr_stream is None:
        # Small fake processes used in unit tests may only implement
        # communicate(). Real asyncio subprocesses always expose both pipes.
        stdout, stderr = await process.communicate()
        stdout_bytes = _as_bytes(stdout)
        stderr_bytes = _as_bytes(stderr)
        if len(stdout_bytes) + len(stderr_bytes) > limit:
            raise _OutputLimitExceeded
        return stdout_bytes, stderr_bytes

    stdout_task = asyncio.create_task(_read_limited(stdout_stream, limit))
    stderr_task = asyncio.create_task(_read_limited(stderr_stream, limit))
    tasks = {stdout_task, stderr_task}
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
        chunk = await stream.read(min(65_536, limit - total + 1))
        chunk_bytes = _as_bytes(chunk)
        if not chunk_bytes:
            return b"".join(chunks)
        total += len(chunk_bytes)
        if total > limit:
            raise _OutputLimitExceeded
        chunks.append(chunk_bytes)


async def _terminate_process(process: Any) -> None:
    for method_name in ("kill", "terminate"):
        method = getattr(process, method_name, None)
        if callable(method):
            try:
                method()
            except (OSError, ProcessLookupError):
                pass
            break
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            await wait()
        except (OSError, ProcessLookupError, asyncio.CancelledError):
            pass


__all__ = [
    "DEFAULT_MAX_EVIDENCE_CHARACTERS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_PROMPT_CHARACTERS",
    "DEFAULT_MODEL",
    "DEFAULT_PROVIDER",
    "DEFAULT_THINKING",
    "DEFAULT_TIMEOUT_SECONDS",
    "DraftClaim",
    "DraftEvidenceReference",
    "EvidenceExcerpt",
    "PiRuleSynthesisProvider",
    "RuleSynthesisProvider",
    "SynthesisBatchResult",
    "SynthesisEvidence",
    "SynthesisInputError",
    "SynthesisProviderCommandError",
    "SynthesisProviderError",
    "SynthesisProviderOutputLimitError",
    "SynthesisProviderResponseError",
    "SynthesisProviderTimeoutError",
    "SynthesisProviderUnavailableError",
    "SynthesisResult",
    "build_synthesis_prompt",
]
