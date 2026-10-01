"""Bounded Agent Reach/HTTPS research transport for the rules workbench.

The provider in this module is deliberately only a transport adapter.  It
does not interpret a page as a rule and it has no access to game state.  The
default adapter invokes ``mcporter`` with the Exa HTTP MCP endpoint.  Keeping
the subprocess boundary here makes it possible for the workbench and its
tests to use a deterministic evidence import path when the host tool is not
available.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000
DEFAULT_MAX_FETCH_CHARACTERS = 200_000
DEFAULT_MAX_RESULTS = 10
MAX_QUERY_LENGTH = 1_000
MAX_URL_LENGTH = 4_096
MAX_TEXT_RESPONSE_CHARACTERS = 2_000_000
MAX_TEXT_SEARCH_RECORDS = 50
MAX_TEXT_LINE_CHARACTERS = 8_000

_TEXT_FIELD_PATTERN = re.compile(
    r"^(?P<field>Title|URL|Published|Author|Highlights)\s*:\s*(?P<value>.*)$",
    re.IGNORECASE,
)
_RATE_LIMIT_PATTERN = re.compile(
    r"\b(?:rate[\s_-]*limit(?:ed|exceeded)?|too\s+many\s+requests|"
    r"quota(?:\s+exceeded)?|(?:http|status)\s*[:=]?\s*429|429)\b",
    re.IGNORECASE,
)


class ResearchProviderError(RuntimeError):
    """Base class for expected research transport failures."""


class ResearchProviderUnavailableError(ResearchProviderError):
    """The configured host executable cannot be started."""


class ResearchProviderTimeoutError(ResearchProviderError):
    """The host executable exceeded the configured timeout."""


class ResearchProviderOutputLimitError(ResearchProviderError):
    """The host executable produced more output than permitted."""


class ResearchProviderCommandError(ResearchProviderError):
    """The host executable returned a non-zero exit status."""


class ResearchProviderResponseError(ResearchProviderError):
    """The MCP response could not be interpreted as the requested result."""


class InvalidResearchUrlError(ResearchProviderError, ValueError):
    """A fetch URL is not an allowed public HTTP(S) URL."""


class SearchQuery(BaseModel):
    """A bounded search request independent from any game state."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    locale: str | None = Field(default=None, max_length=32)
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=50)

    @field_validator("query", "locale")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("search text must not be empty")
        return value


class SearchHit(BaseModel):
    """One untrusted search result returned by the research backend."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    title: str = Field(min_length=1, max_length=1_000)
    url: AnyHttpUrl
    published: str | None = Field(default=None, max_length=256)
    author: str | None = Field(default=None, max_length=512)
    highlights: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("title", "published", "author")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        _validate_public_http_url(str(value))
        return value

    @field_validator("highlights")
    @classmethod
    def normalize_highlights(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class FetchedDocument(BaseModel):
    """A bounded page body and optional metadata returned by ``web_fetch_exa``."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    url: AnyHttpUrl
    body: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=1_000)
    published: str | None = Field(default=None, max_length=256)
    author: str | None = Field(default=None, max_length=512)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        _validate_public_http_url(str(value))
        return value

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        # Keep fetched page text byte-for-byte equivalent at the string level.
        # Trimming here would destroy leading/trailing evidence.
        if not value.strip():
            raise ValueError("document body must not be empty")
        return value

    @field_validator("title", "published", "author")
    @classmethod
    def strip_document_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @property
    def text(self) -> str:
        """Compatibility spelling for callers that refer to page text."""

        return self.body

    @property
    def content(self) -> str:
        """Compatibility spelling for callers that refer to page content."""

        return self.body


class RuleResearchProvider(Protocol):
    """Transport contract used by the research workflow."""

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        """Search for candidate source pages."""

    async def fetch(self, url: str) -> FetchedDocument:
        """Fetch one source page."""


ProcessFactory = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class AgentReachConfig:
    """Safe bounds for one Agent Reach subprocess invocation."""

    executable: str = "mcporter"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_fetch_characters: int = DEFAULT_MAX_FETCH_CHARACTERS

    def __post_init__(self) -> None:
        executable = os.fspath(self.executable)
        if executable != "mcporter" and not os.path.isabs(executable):
            raise ValueError("research executable must be an absolute path")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        if self.max_fetch_characters < 1:
            raise ValueError("max_fetch_characters must be positive")


class AgentReachResearchProvider:
    """Invoke Exa's HTTP MCP tools through the installed ``mcporter`` binary."""

    def __init__(
        self,
        *,
        executable: str | os.PathLike[str] = "mcporter",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_fetch_characters: int = DEFAULT_MAX_FETCH_CHARACTERS,
        process_factory: ProcessFactory | None = None,
        timeout: float | None = None,
    ) -> None:
        # ``timeout`` is accepted as a small compatibility convenience for
        # callers that use the shorter name; the documented configuration is
        # ``timeout_seconds``.
        if timeout is not None:
            timeout_seconds = timeout
        self.config = AgentReachConfig(
            executable=_resolve_executable(os.fspath(executable)),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_fetch_characters=max_fetch_characters,
        )
        self._process_factory = process_factory

    async def search(self, query: SearchQuery | str) -> list[SearchHit]:
        """Run ``web_search_exa`` and parse its bounded MCP JSON response."""

        request = query if isinstance(query, SearchQuery) else SearchQuery(query=query)
        args = {
            "query": request.query,
            "numResults": request.max_results,
        }
        stdout = await self._call_tool("web_search_exa", args)
        payload = _decode_mcp_payload(stdout, "search")
        records = _extract_search_records(payload)
        if records is None:
            raise ResearchProviderResponseError("search response does not contain result records")
        hits: list[SearchHit] = []
        for record in records[: request.max_results]:
            try:
                hit = _search_hit_from_record(record)
            except (TypeError, ValueError) as exc:
                raise ResearchProviderResponseError(
                    "search response contains a malformed result record",
                ) from exc
            hits.append(hit)
        return hits

    async def fetch(self, url: str) -> FetchedDocument:
        """Run ``web_fetch_exa`` after rejecting unsafe URL targets."""

        target = _validate_public_http_url(url)
        args = {
            "urls": [target],
            "maxCharacters": self.config.max_fetch_characters,
        }
        stdout = await self._call_tool("web_fetch_exa", args)
        payload = _decode_mcp_payload(stdout, "fetch")
        document = _document_from_payload(payload, target)
        if len(document.body) > self.config.max_fetch_characters:
            raise ResearchProviderOutputLimitError(
                "fetched document body exceeds max_fetch_characters",
            )
        return document

    async def _call_tool(self, tool: str, arguments: Mapping[str, Any]) -> bytes:
        command = [
            self.config.executable,
            "call",
            "--http-url",
            EXA_MCP_URL,
            "--tool",
            tool,
            "--args",
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            "--output",
            "json",
        ]
        factory = self._process_factory or asyncio.create_subprocess_exec
        try:
            process = await factory(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ResearchProviderUnavailableError(
                f"research backend executable not found: {self.config.executable}",
            ) from exc
        except OSError as exc:
            raise ResearchProviderUnavailableError(
                f"research backend could not start: {self.config.executable}",
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                self._communicate_bounded(process),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError as exc:
            await _terminate_process(process)
            raise ResearchProviderTimeoutError(
                f"research backend timed out after {self.config.timeout_seconds:g}s",
            ) from exc
        except _OutputLimitExceeded as exc:
            await _terminate_process(process)
            raise ResearchProviderOutputLimitError(
                f"research backend output exceeds {self.config.max_output_bytes} bytes",
            ) from exc

        returncode = getattr(process, "returncode", None)
        if returncode is None and hasattr(process, "wait"):
            returncode = await process.wait()
        if returncode not in (None, 0):
            message = stderr.decode("utf-8", errors="replace")[:2_000].strip()
            detail = f": {message}" if message else ""
            raise ResearchProviderCommandError(
                f"research backend exited with status {returncode}{detail}",
            )
        return stdout

    async def _communicate_bounded(self, process: Any) -> tuple[bytes, bytes]:
        stdout_stream = getattr(process, "stdout", None)
        stderr_stream = getattr(process, "stderr", None)
        if stdout_stream is None or stderr_stream is None:
            # This fallback also keeps a small fake subprocess convenient for
            # unit tests.  Real asyncio subprocesses always expose both pipes.
            stdout, stderr = await process.communicate()
            stdout = _as_bytes(stdout)
            stderr = _as_bytes(stderr)
            if len(stdout) + len(stderr) > self.config.max_output_bytes:
                raise _OutputLimitExceeded
            return stdout, stderr

        output_limit = self.config.max_output_bytes
        stdout_task = asyncio.create_task(_read_limited(stdout_stream, output_limit))
        stderr_task = asyncio.create_task(_read_limited(stderr_stream, output_limit))
        tasks = {stdout_task, stderr_task}
        pending: set[asyncio.Task[bytes]] = set(tasks)
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
            if pending:
                await asyncio.gather(*pending)
            stdout, stderr = stdout_task.result(), stderr_task.result()
            if len(stdout) + len(stderr) > output_limit:
                raise _OutputLimitExceeded
            return stdout, stderr
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class _OutputLimitExceeded(Exception):
    """Internal signal used while draining subprocess pipes."""


async def _read_limited(stream: Any, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(65_536, limit - total + 1))
        chunk = _as_bytes(chunk)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise _OutputLimitExceeded
        chunks.append(chunk)


async def _terminate_process(process: Any) -> None:
    kill = getattr(process, "kill", None)
    if kill is not None:
        kill()
    wait = getattr(process, "wait", None)
    if wait is not None:
        try:
            await wait()
        except (ProcessLookupError, asyncio.CancelledError):
            pass


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError("subprocess output must be bytes or text")


def _resolve_executable(executable: str) -> str:
    """Resolve the bundled Windows command shim before spawning a process.

    ``asyncio.create_subprocess_exec`` does not consistently apply Windows'
    ``PATHEXT`` lookup to a bare command.  Node's global executable is often
    exposed as ``mcporter.cmd`` on that platform, so resolve that shim to the
    path returned by ``shutil.which`` while keeping a configurable absolute
    executable untouched.  Leaving the default name unchanged when it is not
    installed preserves the existing, clear ``FileNotFoundError`` path.
    """

    if executable != "mcporter" or os.name != "nt":
        return executable
    resolved = shutil.which("mcporter.cmd")
    return resolved or executable


def _validate_public_http_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
        raise InvalidResearchUrlError("research URL must be a bounded HTTP(S) string")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise InvalidResearchUrlError("research URL is malformed") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise InvalidResearchUrlError("research URL must use http or https")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise InvalidResearchUrlError("research URL must contain a public host without credentials")
    host = parts.hostname.rstrip(".").lower()
    if host in {
        "localhost",
        "metadata.google.internal",
        "metadata.azure.internal",
        "instance-data",
        "instance-data.ec2.internal",
    } or host.endswith((".localhost", ".local")):
        raise InvalidResearchUrlError("research URL host is not publicly reachable")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise InvalidResearchUrlError("research URL host is not publicly reachable")
    try:
        parts.port
    except ValueError as exc:
        raise InvalidResearchUrlError("research URL contains an invalid port") from exc
    return value


def _decode_mcp_payload(stdout: bytes, operation: str) -> Any:
    try:
        raw: Any = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchProviderResponseError(
            f"{operation} backend returned malformed JSON",
        ) from exc
    return _unwrap_mcp_payload(raw, operation)


def _unwrap_mcp_payload(payload: Any, operation: str) -> Any:
    if isinstance(payload, Mapping):
        if payload.get("isError") is True or payload.get("error"):
            raise ResearchProviderResponseError(f"{operation} backend returned an MCP error")
        if "result" in payload and isinstance(payload["result"], (Mapping, list, str)):
            return _unwrap_mcp_payload(payload["result"], operation)
        content = payload.get("content")
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping):
                    text_value = item.get("text")
                    if isinstance(text_value, str):
                        text_parts.append(text_value)
            if not text_parts:
                raise ResearchProviderResponseError(
                    f"{operation} backend returned empty MCP content",
                )
            text = "\n".join(text_parts)
            try:
                nested = json.loads(text)
            except json.JSONDecodeError:
                return text
            return _unwrap_mcp_payload(nested, operation)
    return payload


def _extract_search_records(payload: Any) -> list[Mapping[str, Any]] | None:
    if isinstance(payload, str):
        return _extract_text_search_records(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return None
    for key in ("results", "searchResults", "items", "documents"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    for key in ("data", "result"):
        value = payload.get(key)
        records = _extract_search_records(value)
        if records is not None:
            return records
    if _lookup(payload, "title") is not None and _lookup(payload, "url") is not None:
        return [payload]
    return None


def _extract_text_search_records(text: str) -> list[Mapping[str, Any]] | None:
    """Parse the line-oriented text emitted by Exa's MCP search tool.

    mcporter --output json wraps this response in an MCP content item, but
    the content itself is plain text rather than JSON. The parser is
    intentionally strict: every non-empty block must look like a result, so
    an error page or an HTML fragment cannot become a fabricated search hit.
    """

    _validate_text_response_size(text, "search")
    if not text.strip():
        return None
    _raise_for_backend_text_error(text, "search")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"(?m)^[ \t]*-{3,}[ \t]*$", normalized)
    records: list[Mapping[str, Any]] = []
    for block in blocks:
        if not block.strip():
            continue
        records.append(_search_record_from_text_block(block))
        if len(records) > MAX_TEXT_SEARCH_RECORDS:
            raise ResearchProviderResponseError(
                "search response contains too many text result records",
            )
    return records or None


def _search_record_from_text_block(block: str) -> Mapping[str, Any]:
    fields: dict[str, Any] = {"highlights": []}
    current_field: str | None = None
    for raw_line in block.split("\n"):
        if len(raw_line) > MAX_TEXT_LINE_CHARACTERS:
            raise ResearchProviderResponseError(
                "search response contains an overlong text record line",
            )
        line = raw_line.strip()
        if not line:
            continue
        match = _TEXT_FIELD_PATTERN.match(line)
        if match is None:
            if current_field == "highlights":
                highlight = _strip_highlight_marker(line)
                if highlight:
                    fields["highlights"].append(highlight)
                continue
            raise ResearchProviderResponseError(
                "search response contains malformed text result metadata",
            )
        field = match.group("field").lower()
        value = match.group("value").strip()
        if field == "highlights":
            if "highlights_seen" in fields:
                raise ResearchProviderResponseError(
                    "search response contains duplicate text result metadata",
                )
            fields["highlights_seen"] = True
            current_field = "highlights"
            if value:
                fields["highlights"].append(_strip_highlight_marker(value))
            continue
        if field in fields:
            raise ResearchProviderResponseError(
                "search response contains duplicate text result metadata",
            )
        if not value:
            raise ResearchProviderResponseError(
                "search response contains empty text result metadata",
            )
        fields[field] = value
        current_field = field

    title = fields.get("title")
    url = fields.get("url")
    if not isinstance(title, str) or not isinstance(url, str):
        raise ResearchProviderResponseError(
            "search response text result requires title and url",
        )
    return {
        "title": title,
        "url": url,
        "published": fields.get("published"),
        "author": fields.get("author"),
        "highlights": [item for item in fields["highlights"] if item],
    }


def _strip_highlight_marker(value: str) -> str:
    if value[:2] in {"- ", "* ", "• "}:
        return value[2:].strip()
    return value.strip()


def _validate_text_response_size(text: str, operation: str) -> None:
    if len(text) > MAX_TEXT_RESPONSE_CHARACTERS:
        raise ResearchProviderOutputLimitError(
            f"{operation} text response exceeds {MAX_TEXT_RESPONSE_CHARACTERS} characters",
        )


def _raise_for_backend_text_error(text: str, operation: str) -> None:
    """Turn recognizable provider error text into an explicit response error."""

    stripped = text.strip()
    first_line = stripped.splitlines()[0].strip().lower() if stripped else ""
    if not _RATE_LIMIT_PATTERN.search(stripped):
        return
    if first_line.startswith(
        (
            "error",
            "failed",
            "failure",
            "status",
            "http",
            "429",
            "rate limit",
            "quota",
            "too many",
        ),
    ):
        raise ResearchProviderResponseError(
            f"{operation} backend returned a rate-limit error",
        )


def _search_hit_from_record(record: Mapping[str, Any]) -> SearchHit:
    title = _lookup(record, "title")
    url = _lookup(record, "url", "link")
    if not isinstance(title, str) or not isinstance(url, str):
        raise ValueError("search result requires title and url")
    published = _lookup(record, "published", "publishedDate", "published_at", "date")
    author = _lookup(record, "author", "authors")
    highlights = _lookup(record, "highlights", "highlight")
    if isinstance(author, list):
        author = ", ".join(str(item) for item in author if item is not None)
    if isinstance(highlights, str):
        highlights = [highlights]
    if not isinstance(highlights, list):
        highlights = []
    if published is not None and not isinstance(published, str):
        published = str(published)
    if author is not None and not isinstance(author, str):
        author = str(author)
    return SearchHit(
        title=title,
        url=cast(AnyHttpUrl, url),
        published=published,
        author=author,
        highlights=[str(item) for item in highlights if isinstance(item, (str, int, float))],
    )


def _document_from_payload(payload: Any, requested_url: str) -> FetchedDocument:
    if isinstance(payload, str):
        _validate_text_response_size(payload, "fetch")
        _raise_for_backend_text_error(payload, "fetch")
        parsed = _parse_fetched_text(payload)
        if parsed is None:
            body = payload
            metadata: Mapping[str, Any] = {}
        else:
            metadata, body = parsed
    else:
        record = _find_document_record(payload)
        if record is None:
            raise ResearchProviderResponseError("fetch response does not contain page body")
        body = _lookup(
            record,
            "body",
            "text",
            "content",
            "markdown",
            "pageContent",
            "page_content",
            "rawContent",
            "raw_content",
        )
        metadata = record
    if not isinstance(body, str) or not body.strip():
        raise ResearchProviderResponseError("fetch response does not contain page body")
    document_url = requested_url
    source_url = _lookup(metadata, "url")
    if source_url is not None:
        if not isinstance(source_url, str):
            raise ResearchProviderResponseError(
                "fetch response URL metadata is not a string",
            )
        try:
            source_url = _validate_public_http_url(source_url)
        except InvalidResearchUrlError as exc:
            raise ResearchProviderResponseError(
                "fetch response URL metadata contains an invalid URL",
            ) from exc
        if source_url != requested_url:
            raise ResearchProviderResponseError(
                "fetch response URL does not match requested URL",
            )
        document_url = source_url
    title = _lookup(metadata, "title")
    published = _lookup(metadata, "published", "publishedDate", "published_at", "date")
    author = _lookup(metadata, "author", "authors")
    if isinstance(author, list):
        author = ", ".join(str(item) for item in author if item is not None)
    return FetchedDocument(
        url=cast(AnyHttpUrl, document_url),
        body=body,
        title=title if isinstance(title, str) else None,
        published=str(published) if published is not None else None,
        author=author if isinstance(author, str) else None,
    )


def _parse_fetched_text(text: str) -> tuple[Mapping[str, str | None], str] | None:
    """Parse Exa's text fetch envelope while preserving the page body exactly."""

    if not text.startswith("#"):
        return None
    lines = text.splitlines(keepends=True)
    if not lines:
        return None
    first_line = lines[0].rstrip("\r\n")
    if not re.fullmatch(r"#\s+.+", first_line):
        raise ResearchProviderResponseError(
            "fetch response contains malformed text metadata title",
        )
    title = first_line[1:].strip()
    metadata: dict[str, str | None] = {"title": title}
    separator_index: int | None = None
    for index, raw_line in enumerate(lines[1:], start=1):
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            separator_index = index
            break
        match = re.match(
            r"^(?P<field>URL|Published|Author)\s*:\s*(?P<value>.*)$",
            line,
            re.IGNORECASE,
        )
        if match is None:
            raise ResearchProviderResponseError(
                "fetch response contains malformed text metadata",
            )
        field = match.group("field").lower()
        if field in metadata:
            raise ResearchProviderResponseError(
                "fetch response contains duplicate text metadata",
            )
        value = match.group("value").strip()
        if not value:
            raise ResearchProviderResponseError(
                "fetch response contains empty text metadata",
            )
        metadata[field] = value
    if separator_index is None:
        raise ResearchProviderResponseError(
            "fetch response text metadata is missing its body separator",
        )
    source_url = metadata.get("url")
    if not isinstance(source_url, str):
        raise ResearchProviderResponseError(
            "fetch response text metadata requires a URL",
        )
    try:
        _validate_public_http_url(source_url)
    except InvalidResearchUrlError as exc:
        raise ResearchProviderResponseError(
            "fetch response text metadata contains an invalid URL",
        ) from exc
    body = "".join(lines[separator_index + 1 :])
    if not body.strip():
        raise ResearchProviderResponseError(
            "fetch response text metadata is missing the page body",
        )
    return metadata, body


def _find_document_record(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        for key in ("results", "documents", "items", "data", "result"):
            value = payload.get(key)
            found = _find_document_record(value)
            if found is not None:
                return found
        if any(
            _lookup(payload, key) is not None for key in ("body", "text", "content", "markdown")
        ):
            return payload
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for value in payload:
            found = _find_document_record(value)
            if found is not None:
                return found
    return None


def _lookup(record: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in record.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return None


__all__ = [
    "AgentReachConfig",
    "AgentReachResearchProvider",
    "FetchedDocument",
    "InvalidResearchUrlError",
    "ResearchProviderCommandError",
    "ResearchProviderError",
    "ResearchProviderOutputLimitError",
    "ResearchProviderResponseError",
    "ResearchProviderTimeoutError",
    "ResearchProviderUnavailableError",
    "RuleResearchProvider",
    "SearchHit",
    "SearchQuery",
]
