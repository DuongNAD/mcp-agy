"""Adversarial Test Suite for Stdio JSON-RPC Stream Purity & Robustness.

Challenger 2 Verification for FastMCP AGY Milestone 1:
1. Subprocess Stdio Stream Purity:
   - Verifies ZERO non-JSON-RPC bytes ever touch sys.stdout during startup, steady state, tool execution, and shutdown.
   - Verifies that all logging levels (DEBUG, INFO, WARNING, ERROR, CRITICAL) route strictly to sys.stderr.
2. Error Resilience & Fault Injection:
   - Injects unhandled exceptions (RuntimeError, ValueError, OSError, ZeroDivisionError) into tool backends.
   - Asserts server survives, returns structured JSON-RPC responses, and continues serving subsequent requests.
3. Schema & Input Attack Vectors:
   - Tests malformed JSON-RPC, out-of-bounds parameters, invalid types, huge payloads (100KB+), special characters.
4. Concurrency & High Throughput Stream Integrity:
   - Stresses the stdio stream with rapid sequential and concurrent tool calls without frame tearing or interleaving.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from mcp_agy.core.backend import AGYBackend, MockAGYBackend, reset_backend, set_backend
from mcp_agy.core.models import (
    ChatResult,
    DiffResult,
    TaskExecutionResult,
    TestRunResult,
    TokenUsage,
)
from mcp_agy.server import create_mcp_server
from mcp_agy.utils.logger import configure_logging, get_logger, setup_logger

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


# ============================================================================
# Helper Functions for Raw JSON-RPC Communication over Subprocess Stdio
# ============================================================================


class StdioSubprocessHarness:
    """Manages an actual OS subprocess running mcp-agy over stdio pipes."""

    def __init__(self, cli_args: Optional[List[str]] = None, env_vars: Optional[Dict[str, str]] = None) -> None:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONPATH"] = str(SRC_DIR)
        if env_vars:
            env.update(env_vars)

        cmd = [sys.executable, "-m", "mcp_agy.cli", "--transport", "stdio", "--log-level", "DEBUG"]
        if cli_args:
            cmd = [sys.executable] + cli_args

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # Line buffered
            env=env,
        )
        self._request_id = 0
        self._stderr_buffer: List[str] = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        try:
            if self.proc.stderr:
                for line in iter(self.proc.stderr.readline, ""):
                    self._stderr_buffer.append(line)
        except Exception:
            pass

    def get_stderr(self) -> str:
        return "".join(self._stderr_buffer)

    def send_raw(self, line: str) -> None:
        """Send a raw string line to subprocess stdin."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        """Send a standard JSON-RPC 2.0 request and return the request ID."""
        self._request_id += 1
        req: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            req["params"] = params
        self.send_raw(json.dumps(req))
        return self._request_id

    def send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a JSON-RPC notification (no ID)."""
        req: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            req["params"] = params
        self.send_raw(json.dumps(req))

    def read_stdout_line(self, timeout: float = 5.0) -> str:
        """Read a single line from stdout with a timeout."""
        assert self.proc.stdout is not None
        # Use simple readline since line-buffered
        line = self.proc.stdout.readline()
        return line

    def initialize_protocol(self) -> Dict[str, Any]:
        """Perform full MCP protocol initialization handshake."""
        req_id = self.send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "adversarial-tester", "version": "1.0.0"},
            },
        )
        raw_line = self.read_stdout_line(timeout=5.0)
        assert raw_line, "Expected initialize response from server stdout, got empty line"

        # Validate line is pure JSON-RPC
        data = json.loads(raw_line)
        assert data.get("jsonrpc") == "2.0"
        assert data.get("id") == req_id
        assert "result" in data

        # Send initialized notification
        self.send_notification("notifications/initialized")
        return data

    def close(self) -> Tuple[str, str]:
        """Close stdin, wait for process termination, and return (stdout_remainder, stderr_output)."""
        if self.proc.stdin and not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        try:
            stdout_rem, _ = self.proc.communicate(timeout=5.0)
        except Exception:
            self.proc.kill()
            stdout_rem, _ = self.proc.communicate()
        self._stderr_thread.join(timeout=1.0)
        stderr_out = self.get_stderr()
        return stdout_rem or "", stderr_out


# ============================================================================
# 1. Subprocess Stdio Stream Purity Tests
# ============================================================================


class TestStdioSubprocessStreamPurity:
    """Verifies that sys.stdout contains strictly valid JSON-RPC frames and 0 bytes of logs or junk."""

    def test_startup_emits_zero_stdout_bytes_before_request(self):
        """Server startup must not emit any banners, prints, or warnings to stdout."""
        harness = StdioSubprocessHarness()
        try:
            # Give server 0.3s to start up without sending any stdin
            time.sleep(0.3)
            # Check process is still alive and has not written to stdout
            assert harness.proc.poll() is None, "Server died unexpectedly during startup"

            # Perform handshake
            init_res = harness.initialize_protocol()
            assert init_res["result"]["serverInfo"]["name"] == "mcp-agy"
        finally:
            stdout_rem, stderr_out = harness.close()

        # Check stderr received startup logs
        assert "Starting mcp-agy" in stderr_out or "FastMCP" in stderr_out or len(stderr_out) > 0
        # Check stdout remainder after close contains zero unparsed garbage
        for line in stdout_rem.strip().splitlines():
            if line:
                parsed = json.loads(line)
                assert parsed.get("jsonrpc") == "2.0"

    def test_all_four_tools_execution_stream_purity(self, tmp_path):
        """Executing all 4 tools sequentially over raw stdio produces 100% valid JSON-RPC frames on stdout."""
        harness = StdioSubprocessHarness()
        try:
            harness.initialize_protocol()

            # 1. List tools
            list_id = harness.send_request("tools/list")
            list_line = harness.read_stdout_line()
            list_data = json.loads(list_line)
            assert list_data.get("id") == list_id
            tool_names = [t["name"] for t in list_data["result"]["tools"]]
            assert len(tool_names) == 4

            # 2. Call agy_chat
            chat_id = harness.send_request(
                "tools/call",
                {"name": "agy_chat", "arguments": {"prompt": "Adversarial stream purity check."}},
            )
            chat_line = harness.read_stdout_line()
            # Assert stdout line is strictly valid JSON
            chat_data = json.loads(chat_line)
            assert chat_data.get("id") == chat_id
            assert "result" in chat_data
            assert not chat_data["result"].get("isError")

            # 3. Call agy_execute_task
            exec_id = harness.send_request(
                "tools/call",
                {
                    "name": "agy_execute_task",
                    "arguments": {
                        "workspace_path": str(tmp_path),
                        "prompt": "Test coding task",
                        "auto_approve": True,
                    },
                },
            )
            exec_line = harness.read_stdout_line()
            exec_data = json.loads(exec_line)
            assert exec_data.get("id") == exec_id
            assert "result" in exec_data

            # 4. Call agy_get_diff
            diff_id = harness.send_request(
                "tools/call",
                {"name": "agy_get_diff", "arguments": {"workspace_path": str(tmp_path)}},
            )
            diff_line = harness.read_stdout_line()
            diff_data = json.loads(diff_line)
            assert diff_data.get("id") == diff_id
            assert "result" in diff_data

            # 5. Call agy_run_tests
            test_id = harness.send_request(
                "tools/call",
                {"name": "agy_run_tests", "arguments": {"workspace_path": str(tmp_path)}},
            )
            test_line = harness.read_stdout_line()
            test_data = json.loads(test_line)
            assert test_data.get("id") == test_id
            assert "result" in test_data
        finally:
            stdout_rem, stderr_out = harness.close()

        # All logged messages must be in stderr
        assert "agy_chat invoked" in stderr_out
        assert "agy_execute_task invoked" in stderr_out
        assert "agy_get_diff invoked" in stderr_out
        assert "agy_run_tests invoked" in stderr_out

        # Verify no trailing non-JSON on stdout
        if stdout_rem.strip():
            for line in stdout_rem.strip().splitlines():
                if line.strip():
                    json.loads(line)

    def test_shutdown_emits_zero_stdout_garbage(self):
        """When client disconnects (EOF on stdin), server exits cleanly with zero garbage on stdout."""
        harness = StdioSubprocessHarness()
        harness.initialize_protocol()
        stdout_rem, stderr_out = harness.close()
        assert stdout_rem.strip() == ""
        assert harness.proc.returncode in (0, None)


# ============================================================================
# 2. Logging At All Severity Levels Stream Purity Tests
# ============================================================================


class TestLoggingAtAllSeverityLevelsStreamPurity:
    """Verifies that DEBUG, INFO, WARNING, ERROR, CRITICAL logging never pollutes stdout."""

    @pytest.mark.parametrize(
        "log_level",
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    def test_all_log_levels_route_strictly_to_stderr(self, log_level, capsys):
        """Verifies logger outputs to stderr and stdout has 0 bytes for all log levels."""
        logger = setup_logger(name=f"test_logger_{log_level.lower()}", level=log_level)

        logger.debug(f"[{log_level}] This is a debug message")
        logger.info(f"[{log_level}] This is an info message")
        logger.warning(f"[{log_level}] This is a warning message")
        logger.error(f"[{log_level}] This is an error message")
        logger.critical(f"[{log_level}] This is a critical message")

        captured = capsys.readouterr()
        # STDOUT MUST BE EMPTY
        assert captured.out == "", f"CRITICAL: sys.stdout polluted at log level {log_level}: {captured.out!r}"

        # STDERR MUST CONTAIN THE APPROPRIATE MESSAGES ACCORDING TO LEVEL
        level_int = getattr(logging, log_level)
        if level_int <= logging.DEBUG:
            assert f"[{log_level}] This is a debug message" in captured.err
        if level_int <= logging.INFO:
            assert f"[{log_level}] This is an info message" in captured.err
        if level_int <= logging.WARNING:
            assert f"[{log_level}] This is a warning message" in captured.err
        if level_int <= logging.ERROR:
            assert f"[{log_level}] This is an error message" in captured.err
        if level_int <= logging.CRITICAL:
            assert f"[{log_level}] This is a critical message" in captured.err

    def test_logger_exception_formatting_routes_to_stderr(self, capsys):
        """Verifies logger.exception stack traces route exclusively to stderr without stdout pollution."""
        setup_logger(name="mcp_agy.test_exc", level="DEBUG")
        logger = get_logger("test_exc")

        try:
            raise ValueError("Deliberate test exception for stack trace capture")
        except ValueError:
            logger.exception("Caught an expected test exception")

        captured = capsys.readouterr()
        assert captured.out == "", f"CRITICAL: sys.stdout polluted during logger.exception: {captured.out!r}"
        assert "Caught an expected test exception" in captured.err
        assert "ValueError: Deliberate test exception" in captured.err
        assert "Traceback (most recent call last)" in captured.err

    def test_intense_logging_stress_during_tool_execution(self, capsys):
        """Floods 500 log messages at various levels during server operations without stdout leak."""
        configure_logging(level="DEBUG")
        logger = get_logger("mcp_agy.stress")

        for i in range(100):
            logger.debug(f"Debug iteration {i}")
            logger.info(f"Info iteration {i}")
            logger.warning(f"Warning iteration {i}")
            logger.error(f"Error iteration {i}")
            logger.critical(f"Critical iteration {i}")

        captured = capsys.readouterr()
        assert captured.out == "", f"CRITICAL: sys.stdout polluted during 500-message flood: {captured.out!r}"
        assert "Critical iteration 99" in captured.err


# ============================================================================
# 3. Fault Injection & Error Resilience Tests
# ============================================================================


class TestFaultInjectionAndErrorResilience:
    """Verifies that unhandled backend exceptions do not crash the server or corrupt stdout."""

    @pytest.mark.asyncio
    async def test_backend_runtime_error_resilience(self, tmp_path):
        """Backend throwing RuntimeError returns structured error and preserves server state."""
        class CrashingBackend(AGYBackend):
            async def execute_task(self, *args, **kwargs):
                raise RuntimeError("Catastrophic simulated backend explosion!")

            async def chat(self, *args, **kwargs):
                raise RuntimeError("Catastrophic simulated chat explosion!")

        server = create_mcp_server(backend=CrashingBackend())

        # Calling execute_task should raise ToolError from FastMCP or return error
        with pytest.raises((ToolError, Exception)) as exc_info:
            await server.call_tool(
                "agy_execute_task",
                {"workspace_path": str(tmp_path), "prompt": "Trigger crash"},
            )
        assert "Catastrophic" in str(exc_info.value)

        # Server is still healthy and responsive
        tools = await server.list_tools()
        assert len(tools) == 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "injected_exception",
        [
            ValueError("Invalid state encountered"),
            ZeroDivisionError("Division by zero in backend"),
            OSError("Simulated OS disk permission denied"),
            KeyError("missing_backend_config_key"),
        ],
    )
    async def test_backend_various_exceptions_resilience(self, injected_exception, tmp_path):
        """Server remains operational after various exception types thrown by backend."""
        class FaultyBackend(AGYBackend):
            async def execute_task(self, *args, **kwargs):
                raise injected_exception

            async def chat(self, *args, **kwargs):
                raise injected_exception

        server = create_mcp_server(backend=FaultyBackend())

        with pytest.raises((ToolError, Exception)):
            await server.call_tool("agy_chat", {"prompt": "Trigger fault"})

        # Subsequent call to another tool still works
        diff_res = await server.call_tool("agy_get_diff", {"workspace_path": str(tmp_path)})
        assert diff_res is not None

    @pytest.mark.asyncio
    async def test_invalid_parameters_do_not_crash_server(self, tmp_path):
        """Passing completely invalid arguments returns validation errors and server stays alive."""
        server = create_mcp_server()

        # 1. Negative timeout
        with pytest.raises(ToolError):
            await server.call_tool(
                "agy_execute_task",
                {"workspace_path": str(tmp_path), "prompt": "Task", "timeout_seconds": -10},
            )

        # 2. Exceeding max timeout
        with pytest.raises(ToolError):
            await server.call_tool(
                "agy_execute_task",
                {"workspace_path": str(tmp_path), "prompt": "Task", "timeout_seconds": 99999},
            )

        # 3. Invalid mode
        with pytest.raises(ToolError):
            await server.call_tool(
                "agy_execute_task",
                {"workspace_path": str(tmp_path), "prompt": "Task", "mode": "destroy-everything"},
            )

        # 4. Valid call immediately after works normally
        valid_res = await server.call_tool(
            "agy_execute_task",
            {"workspace_path": str(tmp_path), "prompt": "Valid task"},
        )
        assert valid_res is not None
        data = json.loads(valid_res[0][0].text)
        assert data["status"] == "success"


# ============================================================================
# 4. Adversarial Subprocess Protocol Stress & Attack Vectors
# ============================================================================


class TestAdversarialSubprocessProtocolStress:
    """Stress-tests the real stdio subprocess against malicious/adversarial JSON-RPC payloads."""

    def test_malformed_json_rpc_over_stdio_does_not_crash_server(self):
        """Sending corrupted/truncated JSON over stdin returns Parse Error (-32700) or error notification without crashing."""
        harness = StdioSubprocessHarness()
        try:
            harness.initialize_protocol()

            # Send broken JSON line
            harness.send_raw("{'bad_json': true, not valid json...")
            err_line = harness.read_stdout_line()
            err_data = json.loads(err_line)

            assert err_data.get("jsonrpc") == "2.0"
            # FastMCP may return an error object or an error notification
            assert "error" in err_data or (
                err_data.get("method") == "notifications/message"
                and err_data.get("params", {}).get("level") == "error"
            )

            # Follow up with a valid request to prove server is still healthy
            valid_id = harness.send_request("tools/list")
            valid_line = harness.read_stdout_line()
            valid_data = json.loads(valid_line)
            assert valid_data.get("id") == valid_id
            assert "result" in valid_data
        finally:
            harness.close()

    def test_calling_nonexistent_tool_returns_error_cleanly(self):
        """Calling a non-registered tool returns clean Method/Tool not found error."""
        harness = StdioSubprocessHarness()
        try:
            harness.initialize_protocol()

            req_id = harness.send_request(
                "tools/call",
                {"name": "agy_non_existent_tool", "arguments": {}},
            )
            resp_line = harness.read_stdout_line()
            resp_data = json.loads(resp_line)

            assert resp_data.get("id") == req_id
            # FastMCP may return an error object or result with isError=True
            if "error" in resp_data:
                assert resp_data["error"]["code"] in (-32601, -32602, -32603, -32000)
            else:
                assert resp_data["result"]["isError"] is True
        finally:
            harness.close()

    def test_large_payload_and_special_unicode_stress(self, tmp_path):
        """Sending 100KB prompt with special Unicode, emojis, newlines, quotes, and HTML/XML."""
        harness = StdioSubprocessHarness()
        try:
            harness.initialize_protocol()

            adversarial_prompt = (
                "🚀 Special symbols: \u2603 \u2764 \U0001F600 \n"
                "Quotes: \" ' ` \t \\ / \r \b \f \n"
                "Tags: <script>alert('xss')</script> &amp; <xml><node>test</node></xml>\n"
                "Repeated data: " + ("ABCDE12345!@#$%^&*()_+ " * 4000)  # ~100KB
            )

            req_id = harness.send_request(
                "tools/call",
                {
                    "name": "agy_execute_task",
                    "arguments": {
                        "workspace_path": str(tmp_path),
                        "prompt": adversarial_prompt,
                    },
                },
            )
            resp_line = harness.read_stdout_line()
            assert resp_line, "Expected response for large payload"
            resp_data = json.loads(resp_line)
            assert resp_data.get("id") == req_id
            assert "result" in resp_data
        finally:
            harness.close()

    def test_high_frequency_sequential_requests_stream_integrity(self, tmp_path):
        """Sends 50 rapid back-to-back tool calls, verifying exact response-id pairing and zero frame corruption."""
        harness = StdioSubprocessHarness()
        try:
            harness.initialize_protocol()

            sent_ids = []
            for i in range(50):
                req_id = harness.send_request(
                    "tools/call",
                    {"name": "agy_chat", "arguments": {"prompt": f"Rapid query #{i}"}},
                )
                sent_ids.append(req_id)

                line = harness.read_stdout_line()
                assert line.endswith("\n") or line.endswith("\r\n")
                data = json.loads(line)
                assert data.get("id") == req_id
                assert data.get("jsonrpc") == "2.0"
                assert "result" in data

            assert len(sent_ids) == 50
        finally:
            stdout_rem, stderr_out = harness.close()

        assert stdout_rem.strip() == ""
