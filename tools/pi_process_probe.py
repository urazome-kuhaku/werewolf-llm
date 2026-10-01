"""No-model Windows diagnostics for the PiProcess Job Object boundary.

This probe starts the real ``pi.cmd`` with the same non-secret launch shape as
PiProcess, sends no RPC records, and reports only Win32 status codes and
redacted process metadata.  It intentionally does not load provider tokens.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import os
import struct
import sys
import uuid
from pathlib import Path

from werewolf.runtime.pi_process import (
    PiProcess,
    PiProcessConfig,
    _WindowsExtendedLimitInformation,
    build_pi_argv,
    build_pi_environment,
)

CREATE_NEW_PROCESS_GROUP = 0x00000200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


def _last_error(kernel32: object) -> int:
    return int(ctypes.get_last_error())


def _process_job_state(kernel32: object, pid: int) -> tuple[bool | None, int | None]:
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None, _last_error(kernel32)
    try:
        in_job = ctypes.wintypes.BOOL()
        ok = kernel32.IsProcessInJob(handle, None, ctypes.byref(in_job))
        if not ok:
            return None, _last_error(kernel32)
        return bool(in_job.value), None
    finally:
        kernel32.CloseHandle(handle)


async def main() -> int:
    if os.name != "nt":
        print("platform=non-windows")
        return 2
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    print(f"platform={os.name} pointer_bits={struct.calcsize('P') * 8}")
    current_state = _process_job_state(kernel32, os.getpid())
    print(f"current_pid={os.getpid()} current_in_job={current_state[0]} error={current_state[1]}")

    root = Path.cwd() / ".pi-process-probe"
    root.mkdir(parents=True, exist_ok=True)
    config = PiProcessConfig(
        session_root=root,
        provider="github-copilot",
        model="gpt-6-luna",
        knowledge_base_url="http://127.0.0.1:4321/v1",
        knowledge_token="probe-token",
        provider_environment={},
    )
    session_id = str(uuid.uuid4())
    session_dir = root / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    argv = build_pi_argv(config, session_id=session_id, session_dir=session_dir)
    print(f"argv0={argv[0]}")
    print(f"argv_count={len(argv)} creationflags=0x{CREATE_NEW_PROCESS_GROUP:08x}")

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(root),
        env=build_pi_environment(config),
        creationflags=CREATE_NEW_PROCESS_GROUP,
    )
    print(f"child_pid={process.pid}")
    child_state = _process_job_state(kernel32, process.pid)
    print(f"child_in_job={child_state[0]} error={child_state[1]}")
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        print(f"api=CreateJobObjectW ok=false error={_last_error(kernel32)}")
    else:
        print("api=CreateJobObjectW ok=true")
        try:
            info = _WindowsExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                job,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            print(
                "api=SetInformationJobObject "
                f"ok={bool(ok)} error={None if ok else _last_error(kernel32)} "
                f"struct_size={ctypes.sizeof(info)}"
            )
            process_handle = kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_TERMINATE,
                False,
                process.pid,
            )
            if not process_handle:
                print(f"api=OpenProcess ok=false error={_last_error(kernel32)}")
            else:
                try:
                    ok = kernel32.AssignProcessToJobObject(job, process_handle)
                    print(
                        "api=AssignProcessToJobObject "
                        f"ok={bool(ok)} error={None if ok else _last_error(kernel32)}"
                    )
                finally:
                    kernel32.CloseHandle(process_handle)
        finally:
            kernel32.CloseHandle(job)
    if process.stdin is not None:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()

    # Exercise the production lifecycle as well.  No RPC record is written,
    # so Pi cannot make a provider/model request during this check.
    lifecycle = PiProcess(config)
    await lifecycle.start()
    print(
        f"pi_process_started=true pid={lifecycle.process.pid if lifecycle.process else None} "
        f"argv_count={len(lifecycle.argv)}"
    )
    await lifecycle.close("no-model Windows Job Object probe")
    print(f"pi_process_closed=true returncode={lifecycle.returncode}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
