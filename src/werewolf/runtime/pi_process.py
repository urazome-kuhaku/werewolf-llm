"""Isolated Pi RPC subprocess lifecycle.

This module owns the operating-system boundary around one Pi session.  It does
not interpret turns; :mod:`pi_rpc_protocol` remains responsible for normalizing
the records returned by Pi.  A process has one seat-specific working tree,
one stdin writer, and one byte-framed stdout reader for its entire lifetime.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import os
import re
import signal
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeAlias, cast

from .pi_rpc_protocol import JsonlDecoder, PiProtocolError, serialize_jsonl

DEFAULT_PI_EXECUTABLE: Final[str] = "pi.cmd" if os.name == "nt" else "pi"
DEFAULT_THINKING: Final[str] = "high"
DEFAULT_CLOSE_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_MAX_STDERR_BYTES: Final[int] = 64 * 1024
DEFAULT_PI_TOOLS: Final[tuple[str, ...]] = (
    "get_board",
    "get_role",
    "get_mechanic",
    "get_interaction",
    "get_rule_topic",
    "search_rules",
    "get_skill_status",
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_FIELD_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|bearer|password)"
    r"(\s*[=:]\s*)([\"']?)([^\s,;\"']+)\2"
)
_REDACTED = "[REDACTED]"


class PiProcessError(RuntimeError):
    """Base error raised by the Pi process boundary."""


class PiProcessLifecycleError(PiProcessError):
    """The process is not in a state that accepts the requested operation."""


class PiProcessProtocolError(PiProcessError):
    """Pi emitted malformed JSONL or an invalid UTF-8 stream."""


class PiProcessExitError(PiProcessError):
    """The Pi process exited before the caller finished consuming records."""


class PiProcessSpawnError(PiProcessError):
    """Pi could not be started or its process tree could not be bound."""


class _Process(Protocol):
    stdin: Any
    stdout: Any
    stderr: Any
    returncode: int | None
    pid: int

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory: TypeAlias = Callable[..., Awaitable[_Process]]


class _ProcessTreeJob(Protocol):
    def terminate(self) -> None: ...

    def close(self) -> None: ...


JobFactory: TypeAlias = Callable[[_Process], _ProcessTreeJob]


@dataclass(frozen=True, slots=True)
class PiProcessConfig:
    """Configuration for one seat's Pi RPC process.

    ``provider_environment`` is an explicit, seat-scoped credential mapping.
    It is appended to a small OS/Node allowlist and never populated from the
    parent environment wholesale.
    """

    session_root: Path
    provider: str
    model: str
    knowledge_base_url: str
    knowledge_token: str = field(repr=False)
    seat: int = 1
    executable: str | Path = DEFAULT_PI_EXECUTABLE
    compatible_version: str | None = None
    thinking: str = DEFAULT_THINKING
    # ``append_system_prompt`` remains for embedders that explicitly need
    # Pi's public append behavior.
    append_system_prompt: Path | None = None
    extension: Path | None = None
    pi_config_dir: Path | None = None
    provider_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    tools: tuple[str, ...] = DEFAULT_PI_TOOLS
    close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES
    # ``system_prompt`` replaces Pi's coding prompt and is intentionally last
    # so adding replacement mode does not shift existing positional fields.
    system_prompt: Path | None = None

    def __post_init__(self) -> None:
        for name in ("provider", "model", "thinking", "knowledge_base_url", "knowledge_token"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be a non-empty string without NUL")
        if self.compatible_version is not None and (
            not isinstance(self.compatible_version, str)
            or not self.compatible_version.strip()
            or "\x00" in self.compatible_version
        ):
            raise ValueError("compatible_version must be a non-empty string without NUL")
        if isinstance(self.seat, bool) or not isinstance(self.seat, int) or self.seat < 1:
            raise ValueError("seat must be a positive integer")
        if not self.knowledge_base_url.startswith(("http://", "https://")):
            raise ValueError("knowledge_base_url must use HTTP or HTTPS")
        if (
            isinstance(self.close_timeout_seconds, bool)
            or not isinstance(self.close_timeout_seconds, (int, float))
            or self.close_timeout_seconds <= 0
        ):
            raise ValueError("close_timeout_seconds must be positive")
        if (
            isinstance(self.max_stderr_bytes, bool)
            or not isinstance(self.max_stderr_bytes, int)
            or self.max_stderr_bytes < 1
        ):
            raise ValueError("max_stderr_bytes must be a positive integer")
        if not self.tools or any(
            not isinstance(tool, str) or not tool.strip() for tool in self.tools
        ):
            raise ValueError("tools must contain non-empty strings")
        if self.system_prompt is not None and self.append_system_prompt is not None:
            raise ValueError("system_prompt and append_system_prompt are mutually exclusive")
        for key, value in self.provider_environment.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
                raise ValueError(f"invalid provider environment variable name: {key!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"invalid provider environment value for {key!r}")
        object.__setattr__(self, "session_root", Path(self.session_root))
        object.__setattr__(self, "executable", str(self.executable))
        if self.system_prompt is not None:
            object.__setattr__(self, "system_prompt", Path(self.system_prompt))
        if self.append_system_prompt is not None:
            object.__setattr__(self, "append_system_prompt", Path(self.append_system_prompt))
        object.__setattr__(
            self, "provider_environment", MappingProxyType(dict(self.provider_environment))
        )
        object.__setattr__(self, "tools", tuple(self.tools))


_BASE_ENVIRONMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
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
)


def build_pi_environment(
    config: PiProcessConfig,
    *,
    source_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the strict environment passed to one Pi process.

    The source mapping is injectable for tests.  Only the OS/Node allowlist,
    explicit provider credentials, and seat-scoped knowledge variables are
    retained.  In particular, ``OPENAI_API_KEY`` or other credentials do not
    cross the boundary unless explicitly supplied in ``provider_environment``.
    """

    source = os.environ if source_environment is None else source_environment
    environment = {
        name: value
        for name, value in source.items()
        if name in _BASE_ENVIRONMENT_NAMES and isinstance(value, str) and "\x00" not in value
    }
    system_root = environment.get("SystemRoot") or environment.get("SYSTEMROOT")
    if system_root:
        environment.setdefault("SystemRoot", system_root)
        environment.setdefault("SYSTEMROOT", system_root)
    environment.update(config.provider_environment)
    if config.pi_config_dir is not None:
        environment["PI_CODING_AGENT_DIR"] = str(Path(config.pi_config_dir).resolve())
    environment["WEREWOLF_KNOWLEDGE_BASE_URL"] = config.knowledge_base_url
    environment["WEREWOLF_KNOWLEDGE_TOKEN"] = config.knowledge_token
    return environment


def build_pi_argv(
    config: PiProcessConfig,
    *,
    session_id: str,
    session_dir: Path,
) -> tuple[str, ...]:
    """Build independent argv entries; no ``cmd /c`` or shell string exists."""

    args: list[str] = [
        str(config.executable),
        "--mode",
        "rpc",
        "--session-dir",
        str(session_dir.resolve()),
        "--session-id",
        session_id,
        "--provider",
        config.provider,
        "--model",
        config.model,
        "--thinking",
        config.thinking,
        "--no-builtin-tools",
        "--tools",
        ",".join(config.tools),
        "--no-extensions",
    ]
    if config.extension is not None:
        args.extend(("--extension", str(Path(config.extension).resolve())))
    args.extend(
        (
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
        )
    )
    prompt_path: Path | None = None
    prompt_option: str | None = None
    if config.system_prompt is not None:
        prompt_option = "--system-prompt"
        prompt_path = config.system_prompt
    elif config.append_system_prompt is not None:
        prompt_option = "--append-system-prompt"
        prompt_path = config.append_system_prompt
    if prompt_option is not None and prompt_path is not None:
        # Keep this option as two independent argv entries.  Pi accepts it
        # anywhere among the global options; its resource loader reads the
        # referenced file contents before constructing the system message.
        args.extend((prompt_option, str(Path(prompt_path).resolve())))
    return tuple(args)


class _QueueEnd:
    pass


@dataclass(frozen=True, slots=True)
class _QueueFailure:
    error: BaseException


class _WindowsBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        # JOBOBJECT_BASIC_LIMIT_INFORMATION starts with both per-process and
        # per-job CPU time limits.  Omitting the first LARGE_INTEGER shifts
        # every following field and makes SetInformationJobObject fail with
        # ERROR_BAD_LENGTH (24) on 64-bit Windows.
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.wintypes.DWORD),
        ("SchedulingClass", ctypes.wintypes.DWORD),
    ]


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _WindowsExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsBasicLimitInformation),
        ("IoInfo", _WindowsIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    """Small ctypes wrapper for a kill-on-close Windows Job Object."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def __init__(self, process: _Process) -> None:
        if os.name != "nt":
            raise PiProcessSpawnError("Windows Job Object requested on a non-Windows host")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = ctypes.wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.wintypes.BOOL
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
        kernel32.TerminateJobObject.restype = ctypes.wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise PiProcessSpawnError(
                "could not create Windows Job Object "
                f"(CreateJobObjectW Win32 error {ctypes.get_last_error()})"
            )
        self._handle = handle
        info = _WindowsExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise PiProcessSpawnError(
                "could not configure Windows Job Object "
                "(SetInformationJobObject "
                f"Win32 error {error})"
            )
        access = self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE
        process_handle = kernel32.OpenProcess(access, False, process.pid)
        if not process_handle:
            error = ctypes.get_last_error()
            self.close()
            raise PiProcessSpawnError(
                "could not open Pi process for Job Object binding "
                f"(OpenProcess Win32 error {error})"
            )
        try:
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                error = ctypes.get_last_error()
                self.close()
                raise PiProcessSpawnError(
                    "could not bind Pi process to Job Object "
                    f"(AssignProcessToJobObject Win32 error {error})"
                )
        finally:
            kernel32.CloseHandle(process_handle)

    def terminate(self) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._kernel32.CloseHandle(handle)


def _redact_stderr(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    return _SECRET_FIELD_RE.sub(r"\1\2" + _REDACTED, text)


class PiProcess:
    """One seat-scoped Pi process and its byte-safe RPC streams."""

    def __init__(
        self,
        config: PiProcessConfig,
        *,
        process_factory: ProcessFactory | None = None,
        job_factory: JobFactory | None = None,
        platform_name: str | None = None,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._process_factory = process_factory or cast(
            ProcessFactory, asyncio.create_subprocess_exec
        )
        self._job_factory = job_factory
        self._platform_name = os.name if platform_name is None else platform_name
        self._source_environment = source_environment
        self._process: _Process | None = None
        self._job: _ProcessTreeJob | None = None
        self._session_id: str | None = None
        self._session_dir: Path | None = None
        self._cwd: Path | None = None
        self._argv: tuple[str, ...] | None = None
        self._stdin_lock = asyncio.Lock()
        self._record_queue: asyncio.Queue[object] = asyncio.Queue()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self._decoder = JsonlDecoder()
        self._stderr_buffer = bytearray()
        self._stderr_redaction_tail = b""
        self._stderr_truncated = False
        self._failure: BaseException | None = None
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def process(self) -> _Process | None:
        return self._process

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise PiProcessLifecycleError("Pi process has not started")
        return self._session_id

    @property
    def session_dir(self) -> Path:
        if self._session_dir is None:
            raise PiProcessLifecycleError("Pi process has not started")
        return self._session_dir

    @property
    def cwd(self) -> Path:
        if self._cwd is None:
            raise PiProcessLifecycleError("Pi process has not started")
        return self._cwd

    @property
    def argv(self) -> tuple[str, ...]:
        if self._argv is None:
            raise PiProcessLifecycleError("Pi process has not started")
        return self._argv

    @property
    def stderr_text(self) -> str:
        return bytes(self._stderr_buffer).decode("utf-8", errors="replace")

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr_truncated

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    @property
    def started(self) -> bool:
        return self._process is not None

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> PiProcess:
        if self._closed:
            raise PiProcessLifecycleError("Pi process has already been closed")
        if self._process is not None:
            raise PiProcessLifecycleError("Pi process has already started")
        session_id = str(uuid.uuid4())
        self._prepare_directories(session_id)
        session_dir = self._session_dir
        cwd = self._cwd
        assert session_dir is not None and cwd is not None
        argv = build_pi_argv(self.config, session_id=session_id, session_dir=session_dir)
        environment = build_pi_environment(self.config, source_environment=self._source_environment)
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(cwd),
            "env": environment,
        }
        if self._platform_name == "nt":
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = await self._process_factory(*argv, **kwargs)
        except (OSError, ValueError) as exc:
            raise PiProcessSpawnError("could not start Pi RPC process") from exc
        self._process = process
        self._session_id = session_id
        self._argv = argv
        if self._platform_name == "nt":
            try:
                factory = self._job_factory or _WindowsJob
                self._job = factory(process)
            except Exception as exc:
                await self._kill_process_tree()
                raise PiProcessSpawnError(
                    "could not bind Pi process to a kill-on-close Job Object"
                ) from exc
        self._stdout_task = asyncio.create_task(self._read_stdout(), name=f"pi-{session_id}-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"pi-{session_id}-stderr")
        self._watcher_task = asyncio.create_task(
            self._watch_process(), name=f"pi-{session_id}-watch"
        )
        return self

    async def send_record(self, record: Mapping[str, Any]) -> None:
        """Serialize and write one strict JSONL RPC command."""

        await self.send_bytes(serialize_jsonl(record))

    async def send_jsonl(self, record: Mapping[str, Any]) -> None:
        """Explicit alias for adapters that name the wire format directly."""

        await self.send_record(record)

    async def send_bytes(self, data: bytes) -> None:
        """Write already serialized JSONL bytes under the stdin lock."""

        process = self._require_running()
        if not isinstance(data, bytes) or not data.endswith(b"\n"):
            raise ValueError("Pi RPC bytes must be UTF-8 JSONL ending in LF")
        stdin = process.stdin
        if stdin is None:
            raise PiProcessLifecycleError("Pi RPC stdin is unavailable")
        async with self._stdin_lock:
            try:
                stdin.write(data)
                drain = getattr(stdin, "drain", None)
                if drain is not None:
                    await drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise PiProcessExitError("Pi RPC stdin closed") from exc

    async def read_record(self) -> object | None:
        """Read one decoded stdout JSON value, or ``None`` at clean EOF."""

        self._require_started()
        item = await self._record_queue.get()
        if isinstance(item, _QueueFailure):
            raise item.error
        if isinstance(item, _QueueEnd):
            if self._failure is not None:
                raise self._failure
            return None
        return item

    async def read(self) -> object | None:
        """Alias used by adapters that treat the process as an RPC reader."""

        return await self.read_record()

    async def records(self) -> AsyncIterator[object]:
        """Yield decoded records until the process reaches EOF."""

        while True:
            record = await self.read_record()
            if record is None:
                return
            yield record

    async def close(self, reason: str = "normal shutdown") -> None:
        """Close stdin, then boundedly terminate the complete process tree."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            if process is None:
                return
            stdin = process.stdin
            if stdin is not None:
                try:
                    stdin.close()
                    wait_closed = getattr(stdin, "wait_closed", None)
                    if wait_closed is not None:
                        await wait_closed()
                except (BrokenPipeError, ConnectionError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=self.config.close_timeout_seconds)
            except TimeoutError:
                await self._kill_process_tree()
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self.config.close_timeout_seconds
                    )
                except TimeoutError:
                    pass
            finally:
                self._close_job()
                await self._finish_tasks()

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._publish_failure(PiProcessProtocolError("Pi RPC stdout is unavailable"))
            return
        try:
            while True:
                chunk = await process.stdout.read(64 * 1024)
                if not chunk:
                    break
                for record in self._decoder.feed(chunk):
                    await self._record_queue.put(record)
            for record in self._decoder.finish():
                await self._record_queue.put(record)
        except PiProtocolError:
            error = PiProcessProtocolError("Pi RPC stdout violated JSONL protocol")
            await self._kill_process_tree()
            self._publish_failure(error)
        except asyncio.CancelledError:
            raise
        except Exception:
            error = PiProcessProtocolError("Pi RPC stdout reader failed")
            await self._kill_process_tree()
            self._publish_failure(error)

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                self._append_stderr(chunk)
            self._append_stderr(b"", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Diagnostics must never prevent process cleanup or RPC reads.
            return

    async def _watch_process(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            returncode = await process.wait()
            for task in (self._stdout_task, self._stderr_task):
                if task is not None and task is not asyncio.current_task():
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
            if returncode not in (0, None) and self._failure is None and not self._closed:
                self._publish_failure(
                    PiProcessExitError(f"Pi process exited with code {returncode}")
                )
        except asyncio.CancelledError:
            raise
        finally:
            await self._record_queue.put(_QueueEnd())

    def _append_stderr(self, chunk: bytes, *, flush: bool = False) -> None:
        secrets = (self.config.knowledge_token, *self.config.provider_environment.values())
        # Keep a small prefix beyond the longest secret so a field such as
        # ``token=<secret>`` cannot be split between two pipe reads and evade
        # replacement.  This is bounded independently from stderr output.
        tail_size = max((len(secret.encode("utf-8")) for secret in secrets), default=0) + 64
        combined = self._stderr_redaction_tail + chunk
        if flush or tail_size == 0:
            visible = combined
            self._stderr_redaction_tail = b""
        elif len(combined) <= tail_size:
            self._stderr_redaction_tail = combined
            return
        else:
            visible = combined[:-tail_size]
            self._stderr_redaction_tail = combined[-tail_size:]
        remaining = self.config.max_stderr_bytes - len(self._stderr_buffer)
        if remaining <= 0:
            self._stderr_truncated = True
            return
        text = visible.decode("utf-8", errors="replace")
        redacted = _redact_stderr(text, secrets).encode("utf-8")
        self._stderr_buffer.extend(redacted[:remaining])
        if len(visible) > remaining or len(redacted) > remaining:
            self._stderr_truncated = True

    def _prepare_directories(self, session_id: str) -> None:
        root = self.config.session_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        base = root / f"seat_{self.config.seat}_{session_id}"
        session_dir = base / "session"
        cwd = base / "cwd"
        session_dir.mkdir(parents=True, exist_ok=False)
        cwd.mkdir(parents=True, exist_ok=False)
        self._session_dir = session_dir
        self._cwd = cwd

    def _require_started(self) -> _Process:
        if self._process is None:
            raise PiProcessLifecycleError("Pi process has not started")
        return self._process

    def _require_running(self) -> _Process:
        process = self._require_started()
        if self._closed or process.returncode is not None:
            raise PiProcessLifecycleError("Pi process is closed or has exited")
        return process

    def _publish_failure(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = error
            self._record_queue.put_nowait(_QueueFailure(error))

    async def _kill_process_tree(self) -> None:
        process = self._process
        if process is None:
            return
        if self._job is not None:
            try:
                self._job.terminate()
            except Exception:
                pass
            return
        if self._platform_name != "nt" and hasattr(os, "killpg") and process.pid:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            await asyncio.wait_for(
                process.wait(), timeout=min(self.config.close_timeout_seconds, 1.0)
            )
        except TimeoutError:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass

    def _close_job(self) -> None:
        if self._job is not None:
            try:
                self._job.close()
            finally:
                self._job = None

    async def _finish_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task for task in (self._stdout_task, self._stderr_task, self._watcher_task) if task
        ]
        for task in tasks:
            if task is current or task.done():
                continue
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            except Exception:
                pass


__all__ = [
    "DEFAULT_CLOSE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_STDERR_BYTES",
    "DEFAULT_PI_EXECUTABLE",
    "DEFAULT_PI_TOOLS",
    "DEFAULT_THINKING",
    "PiProcess",
    "PiProcessConfig",
    "PiProcessError",
    "PiProcessExitError",
    "PiProcessLifecycleError",
    "PiProcessProtocolError",
    "PiProcessSpawnError",
    "build_pi_argv",
    "build_pi_environment",
]
