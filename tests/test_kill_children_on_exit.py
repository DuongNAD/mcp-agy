"""A server that dies must not leave the agy runs it started running behind it.

`terminate_process_tree` only works while the server is alive to call it. Measured on a hard
kill with three jobs in flight: 49 processes survived (3 agy.exe plus the MCP servers each one
loads), still spending RAM and Gemini quota. These tests use real processes, not mocks - the
whole claim is about what the OS does when a process dies.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import psutil
import pytest

from mcp_agy.utils import process as process_utils

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")

# Binds, starts a long-lived grandchild, reports its pid, then waits to be killed.
_SERVER = textwrap.dedent(
    """
    import subprocess, sys, time
    from mcp_agy.utils.process import kill_children_on_exit
    print("bound" if kill_children_on_exit() else "unbound", flush=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    print(child.pid, flush=True)
    time.sleep(600)
    """
)


def _wait_gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return not psutil.pid_exists(pid)


def _run_server(env_extra: dict[str, str] | None = None) -> tuple[subprocess.Popen, str, int]:
    import os

    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    env.update(env_extra or {})
    server = subprocess.Popen(
        [sys.executable, "-c", _SERVER], stdout=subprocess.PIPE, text=True, env=env
    )
    state = server.stdout.readline().strip()
    child_pid = int(server.stdout.readline().strip())
    return server, state, child_pid


def _cleanup(server: subprocess.Popen, child_pid: int) -> None:
    for pid in (server.pid, child_pid):
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
    server.wait(timeout=10)


def test_children_die_when_the_server_is_hard_killed():
    server, state, child_pid = _run_server({process_utils.KILL_CHILDREN_ENV: "1"})
    try:
        assert state == "bound"
        assert psutil.pid_exists(child_pid)

        psutil.Process(server.pid).kill()  # no chance to run any cleanup code

        assert _wait_gone(child_pid), "grandchild outlived its server"
    finally:
        _cleanup(server, child_pid)


def test_opt_out_leaves_children_alone():
    server, state, child_pid = _run_server({process_utils.KILL_CHILDREN_ENV: "0"})
    try:
        assert state == "unbound"

        psutil.Process(server.pid).kill()
        time.sleep(1.5)

        assert psutil.pid_exists(child_pid), "opt-out was ignored"
    finally:
        _cleanup(server, child_pid)


def test_binding_twice_is_harmless(monkeypatch):
    monkeypatch.setattr(process_utils, "_job_handle", 1234)
    assert process_utils.kill_children_on_exit() is True
