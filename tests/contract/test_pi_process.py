"""Contract tests for one isolated Pi RPC process boundary."""

from __future__ import annotations

import asyncio
import ctypes
from pathlib import Path
from typing import Any

import pytest

from werewolf.runtime.pi_process import (
    PiProcess,
    PiProcessConfig,
    PiProcessProtocolError,
    _WindowsBasicLimitInformation,
    build_pi_argv,
    build_pi_environment,
)


class _FakePipe:
    def __init__(self, chunks: list[bytes] | None = None, *, on_close: Any = None) -> None:
        self._chunks = list(chunks or [])
        self.writes: list[bytes] = []
        self._on_close = on_close
        self._closed = False

    async def read(self, _size: int) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self._closed = True
        if self._on_close is not None:
            self._on_close()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class _FakeProcess:
    def __init__(self, stdout: list[bytes], stderr: list[bytes]) -> None:
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self.stdin = _FakePipe(on_close=self.exit)
        self.stdout = _FakePipe(stdout)
        self.stderr = _FakePipe(stderr)
        self.pid = 4242
        self.terminated = False
        self.killed = False

    def exit(self, returncode: int = 0) -> None:
        if self.returncode is None:
            self.returncode = returncode
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.exit(143)

    def kill(self) -> None:
        self.killed = True
        self.exit(-9)


class _FakeJob:
    def __init__(self) -> None:
        self.terminated = False
        self.closed = False

    def terminate(self) -> None:
        self.terminated = True

    def close(self) -> None:
        self.closed = True


def _config(tmp_path: Path, **overrides: Any) -> PiProcessConfig:
    values: dict[str, Any] = {
        "session_root": tmp_path,
        "provider": "github-copilot",
        "model": "gpt-test",
        "knowledge_base_url": "http://127.0.0.1:4321/v1",
        "knowledge_token": "seat-token-secret",
        "executable": r"C:\nvm4w\nodejs\pi.cmd",
        "provider_environment": {"GITHUB_COPILOT_TOKEN": "provider-secret"},
    }
    values.update(overrides)
    return PiProcessConfig(**values)


def test_windows_launcher_uses_independent_argv_and_no_shell_wrapper(tmp_path: Path) -> None:
    config = _config(tmp_path, system_prompt=tmp_path / "prompt.md")
    args = build_pi_argv(
        config,
        session_id="00000000-0000-4000-8000-000000000000",
        session_dir=tmp_path / "seat" / "session",
    )

    assert args[0].endswith("pi.cmd")
    assert "cmd" not in {part.lower() for part in args}
    assert "cmd /c" not in " ".join(args).lower()
    assert args[args.index("--session-id") + 1] == "00000000-0000-4000-8000-000000000000"
    assert args[args.index("--system-prompt") + 1].endswith("prompt.md")
    assert args[args.index("--tools") + 1] == (
        "get_board,get_role,get_mechanic,get_interaction,get_rule_topic,"
        "search_rules,get_skill_status"
    )


def test_append_system_prompt_remains_explicit_and_mutually_exclusive(tmp_path: Path) -> None:
    config = _config(tmp_path, append_system_prompt=tmp_path / "append.md")
    args = build_pi_argv(config, session_id="session", session_dir=tmp_path / "seat")
    assert args[args.index("--append-system-prompt") + 1].endswith("append.md")

    with pytest.raises(ValueError, match="mutually exclusive"):
        _config(
            tmp_path,
            system_prompt=tmp_path / "replace.md",
            append_system_prompt=tmp_path / "append.md",
        )


def test_environment_is_allowlisted_and_provider_scoped(tmp_path: Path) -> None:
    config = _config(tmp_path, pi_config_dir=tmp_path / "pi-config")
    environment = build_pi_environment(
        config,
        source_environment={
            "PATH": r"C:\Windows",
            "SystemRoot": r"C:\Windows",
            "OPENAI_API_KEY": "must-not-cross",
            "OTHER_SECRET": "must-not-cross",
        },
    )

    assert environment["PATH"] == r"C:\Windows"
    assert environment["SystemRoot"] == r"C:\Windows"
    assert environment["SYSTEMROOT"] == r"C:\Windows"
    assert environment["WEREWOLF_KNOWLEDGE_TOKEN"] == "seat-token-secret"
    assert environment["GITHUB_COPILOT_TOKEN"] == "provider-secret"
    assert environment["PI_CODING_AGENT_DIR"].endswith("pi-config")
    assert "OPENAI_API_KEY" not in environment
    assert "OTHER_SECRET" not in environment


def test_windows_job_object_limit_information_matches_sdk_layout() -> None:
    """Keep the Win32 structure prefix aligned with the Windows SDK."""

    fields = [name for name, _ in _WindowsBasicLimitInformation._fields_]
    assert fields[:2] == ["PerProcessUserTimeLimit", "PerJobUserTimeLimit"]
    assert _WindowsBasicLimitInformation.PerProcessUserTimeLimit.offset == 0
    assert _WindowsBasicLimitInformation.PerJobUserTimeLimit.offset == ctypes.sizeof(
        ctypes.c_longlong
    )
    assert _WindowsBasicLimitInformation.LimitFlags.offset == ctypes.sizeof(ctypes.c_longlong) * 2


@pytest.mark.asyncio
async def test_stdout_is_decoded_and_stderr_is_bounded_and_redacted(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    fake = _FakeProcess(
        [b'{"type":"response","id":"x"}\n', b'{"text":"\xe7\xbb\x88"}'],
        [b"token=seat-", b"token-secret ", b"provider-secret " + b"x" * 512],
    )

    async def factory(*args: str, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return fake

    process = await PiProcess(
        _config(tmp_path, max_stderr_bytes=80),
        process_factory=factory,
        platform_name="posix",
    ).start()
    await process.send_jsonl({"id": "command-1", "type": "get_state"})
    assert fake.stdin.writes == [b'{"id":"command-1","type":"get_state"}\n']
    assert await process.read_record() == {"type": "response", "id": "x"}
    assert await process.read_record() == {"text": "终"}
    await process.close()

    assert "seat-token-secret" not in process.stderr_text
    assert "provider-secret" not in process.stderr_text
    assert len(process.stderr_text.encode("utf-8")) <= 80
    assert process.cwd != process.session_dir
    assert process.cwd.is_dir()
    assert process.session_dir.is_dir()
    assert captured["kwargs"]["cwd"] == str(process.cwd)


@pytest.mark.asyncio
async def test_windows_start_adds_new_process_group_and_closes_job(tmp_path: Path) -> None:
    fake = _FakeProcess([], [])
    job = _FakeJob()
    captured: dict[str, Any] = {}

    async def factory(*args: str, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return fake

    def make_job(process: _FakeProcess) -> _FakeJob:
        assert process is fake
        return job

    process = await PiProcess(
        _config(tmp_path),
        process_factory=factory,
        job_factory=make_job,
        platform_name="nt",
    ).start()
    assert captured["kwargs"]["creationflags"] & 0x00000200
    await process.close()
    assert job.closed


@pytest.mark.asyncio
async def test_malformed_stdout_fails_closed_and_terminates_process(tmp_path: Path) -> None:
    fake = _FakeProcess([b"not-json\n"], [])

    async def factory(*args: str, **kwargs: Any) -> _FakeProcess:
        return fake

    process = await PiProcess(
        _config(tmp_path), process_factory=factory, platform_name="posix"
    ).start()
    with pytest.raises(PiProcessProtocolError):
        await process.read_record()
    await process.close()
    assert fake.terminated or fake.killed
