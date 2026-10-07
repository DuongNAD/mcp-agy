"""Async process execution and recursive process tree termination utilities.

Provides safe subprocess launching, real-time line streaming, timeout management,
and recursive process tree termination across Windows and POSIX platforms.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import AsyncGenerator, Dict, List, Optional, Tuple

import psutil

from mcp_agy.utils.logger import get_logger

logger = get_logger("mcp_agy.utils.process")

# How much of a child's stderr is kept. The tail is what matters: a CLI states why it gave up
# on its last lines. Bounded so a chatty child cannot grow the server's memory without limit.
STDERR_TAIL_LIMIT_BYTES = 8192


KILL_CHILDREN_ENV = "MCP_AGY_KILL_CHILDREN_ON_EXIT"

# Held for the life of the process. Closing the handle is what fires KILL_ON_JOB_CLOSE, and the
# OS closes it for us when this process dies - however it dies, including a hard kill.
_job_handle: Optional[int] = None


def kill_children_on_exit() -> bool:
    """Make the OS kill every process this server started if the server itself dies. Windows only.

    Why: `terminate_process_tree` only runs while this process is alive to run it. A client that
    gives up on a slow tool call kills the server outright, and nothing then reaps the agy runs
    it had going. Measured: after a hard kill with three jobs in flight, 49 processes survived -
    three agy.exe plus the ~16 MCP servers each of them loads - and kept spending RAM and
    quota. Every job started with `agy_start_task` is one more tree left behind.

    Method: put this process in a job object flagged KILL_ON_JOB_CLOSE. Children inherit the
    membership, and the OS tears the whole job down when the last handle closes. Nested jobs are
    supported since Windows 8, so this still works when a launcher already put us in one.

    Returns True when active. A failure is logged and returns False: a server that cannot reap
    its orphans is still a server worth starting. `MCP_AGY_KILL_CHILDREN_ON_EXIT=0` opts out.
    """
    global _job_handle
    if _job_handle is not None:
        return True
    if sys.platform != "win32":
        return False
    if (os.environ.get(KILL_CHILDREN_ENV) or "").strip().lower() in ("0", "false", "no", "off"):
        return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _IoCounters(ctypes.Structure):
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

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())

        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
        if not kernel32.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ) or not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(job)
            raise error

        _job_handle = int(job)
        logger.info("Child processes will be killed with this server (Windows job object).")
        return True
    except Exception as exc:
        logger.warning(
            f"Could not bind child processes to the server's lifetime: {exc}. Runs in flight "
            "when the server is killed will be left running."
        )
        return False


@dataclass
class SubprocessOutcome:
    """Out-of-band result of a streamed subprocess, filled in as the stream closes.

    `stream_subprocess_lines` yields stdout lines, so a caller that needs the exit code or
    the child's stderr has nowhere to receive them. Passing one of these in gives that
    channel: after the stream ends - normally, by timeout, or by cancellation - the fields
    below describe how the child actually finished.
    """

    returncode: Optional[int] = None
    stderr_tail: str = ""
    stderr_truncated: bool = False


async def _drain_stream_tail(stream: asyncio.StreamReader, sink: bytearray) -> bool:
    """Read a pipe to EOF, keeping only its last STDERR_TAIL_LIMIT_BYTES. Returns True if cut.

    Draining is not optional. A pipe nobody reads fills after roughly 64 KB and then blocks
    the child mid-write forever, which the caller sees as a hang until its timeout fires.
    """
    truncated = False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return truncated
        sink.extend(chunk)
        if len(sink) > STDERR_TAIL_LIMIT_BYTES:
            del sink[:-STDERR_TAIL_LIMIT_BYTES]
            truncated = True


def terminate_process_tree(pid: int, timeout: float = 3.0) -> None:
    """Recursively terminates a process and all of its descendant child processes.

    Safely kills child and grandchild processes (compilers, git, node tools, Python)
    using psutil with Windows `taskkill /F /T /PID` fallback.

    Args:
        pid: Process ID of the root parent process.
        timeout: Maximum seconds to wait for polite SIGTERM before sending SIGKILL.
    """
    if pid <= 0:
        return

    try:
        if not psutil.pid_exists(pid):
            return
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        all_procs = children + [parent]

        # Phase 1: Polite termination
        for proc in all_procs:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        # Phase 2: Wait for processes to exit
        _, alive = psutil.wait_procs(all_procs, timeout=timeout)

        # Phase 3: Forceful kill on remaining survivors
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return
    except Exception as e:
        logger.warning(
            f"psutil process tree termination encountered error for PID {pid}: {e}. "
            f"Attempting fallback termination."
        )
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except Exception as ex:
                logger.error(f"taskkill fallback failed for PID {pid}: {ex}")


async def stream_subprocess_lines(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_seconds: float = 600.0,
    outcome: Optional[SubprocessOutcome] = None,
) -> AsyncGenerator[str, None]:
    """Spawns an async subprocess and yields stdout lines in real-time with timeout protection.

    Guarantees clean process tree termination upon completion, timeout, cancellation, or error.

    Args:
        cmd: Command and arguments list.
        cwd: Working directory for subprocess.
        env: Environment variables dictionary.
        timeout_seconds: Maximum overall runtime in seconds.
        outcome: Optional SubprocessOutcome to receive the exit code and stderr tail once
            the stream closes. Stderr is drained either way; this only decides whether the
            caller gets to read it.

    Yields:
        Decoded and stripped stdout lines in real-time.

    Raises:
        asyncio.TimeoutError: If execution exceeds timeout_seconds.
        asyncio.CancelledError: If calling coroutine/task is cancelled.
    """
    logger.debug(f"Spawning subprocess: {' '.join(cmd)} (cwd={cwd})")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    stderr_sink = bytearray()
    stderr_task: Optional[asyncio.Task[bool]] = None
    if proc.stderr is not None:
        stderr_task = asyncio.create_task(_drain_stream_tail(proc.stderr, stderr_sink))

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds

        if proc.stdout is None:
            raise RuntimeError(f"Subprocess stdout pipe was not created for command: {cmd}")

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"Subprocess execution timed out after {timeout_seconds} seconds (PID {proc.pid})"
                )

            # Wait for next line with remaining timeout
            line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line_bytes:
                # EOF reached
                break

            line_str = line_bytes.decode("utf-8", errors="replace").strip()
            if line_str:
                yield line_str

        # Wait for process exit after EOF
        wait_remaining = max(1.0, deadline - loop.time())
        try:
            await asyncio.wait_for(proc.wait(), timeout=wait_remaining)
        except asyncio.TimeoutError:
            terminate_process_tree(proc.pid)
            await proc.wait()

    except (asyncio.TimeoutError, asyncio.CancelledError, GeneratorExit) as exc:
        logger.warning(
            f"Subprocess stream interrupted ({type(exc).__name__}). Terminating process tree PID {proc.pid}"
        )
        terminate_process_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"Subprocess stream encountered unexpected error: {e}. Terminating PID {proc.pid}")
        terminate_process_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass
        raise
    finally:
        if proc.returncode is None:
            terminate_process_tree(proc.pid)
            try:
                await proc.wait()
            except Exception:
                pass

        truncated = False
        if stderr_task is not None:
            # The child is gone by now, so EOF is already in the pipe and this returns at once.
            # The bound is there for the pathological case of a grandchild still holding the
            # write end open - waiting on that forever would trade one hang for another.
            try:
                truncated = await asyncio.wait_for(stderr_task, timeout=1.0)
            except BaseException:
                stderr_task.cancel()
                try:
                    await stderr_task
                except BaseException:
                    pass

        if outcome is not None:
            outcome.returncode = proc.returncode
            outcome.stderr_truncated = truncated
            outcome.stderr_tail = (
                bytes(stderr_sink).decode("utf-8", errors="replace").strip()
            )


async def run_subprocess_async(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_seconds: float = 600.0,
) -> Tuple[int, str, str]:
    """Executes a subprocess to completion asynchronously with timeout and tree cleanup.

    Args:
        cmd: Command and arguments list.
        cwd: Working directory for subprocess.
        env: Environment variables dictionary.
        timeout_seconds: Maximum allowed runtime in seconds.

    Returns:
        Tuple of (returncode, stdout_string, stderr_string).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
        return (
            proc.returncode if proc.returncode is not None else 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        terminate_process_tree(proc.pid)
        try:
            await proc.wait()
        except Exception:
            pass
        raise
