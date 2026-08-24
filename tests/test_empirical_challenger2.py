"""Empirical Challenger 2 Adversarial Verification Harness for mcp_agy.

Conducts exhaustive empirical tests of:
1. FastMCP Stdio Transport Byte-Level Purity (Zero non-JSON-RPC bytes to stdout).
2. Logging routing strictness (DEBUG, INFO, WARNING, ERROR, CRITICAL + exceptions to stderr only).
3. NDJSON telemetry stream parser resilience against:
   - Malformed/truncated lines
   - 2MB+ payload sizes
   - Null values and missing fields
   - Extreme Unicode and emojis
   - Type mismatches and corrupt nested structures
4. High-frequency stdio stream integrity (100 rapid sequential calls).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import pytest

from mcp_agy.core.backend import AGYBackend, MockAGYBackend
from mcp_agy.core.cli_backend import SubprocessCLIBackend, _parse_token_usage, _safe_dict, _safe_float, _safe_int
from mcp_agy.core.models import (
    BackendType,
    ChatResult,
    ExecutionRequest,
    ExecutionResult,
    StreamEvent,
    StreamEventType,
    TaskExecutionResult,
    TokenUsage,
    ToolEvent,
    ToolStatus,
)
from mcp_agy.server import create_mcp_server
from mcp_agy.utils.logger import configure_logging, get_logger, setup_logger

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


class EmpiricalSubprocessTester:
    """Manages real OS subprocess stdio JSON-RPC communication."""

    def __init__(self, log_level: str = "DEBUG") -> None:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONPATH"] = str(SRC_DIR)
        env["MCP_AGY_BACKEND"] = "mock"

        cmd = [sys.executable, "-m", "mcp_agy.cli", "--transport", "stdio", "--log-level", log_level]

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self.req_id = 0
        self.stderr_lines: List[str] = []
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def _read_stderr(self) -> None:
        try:
            if self.proc.stderr:
                for line in iter(self.proc.stderr.readline, ""):
                    self.stderr_lines.append(line)
        except Exception:
            pass

    def send_raw(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        self.req_id += 1
        payload = {"jsonrpc": "2.0", "id": self.req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send_raw(json.dumps(payload))
        return self.req_id

    def send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.send_raw(json.dumps(payload))

    def read_stdout_line(self) -> str:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        return line

    def initialize(self) -> Dict[str, Any]:
        req_id = self.send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "empirical-challenger-2", "version": "1.0.0"},
            },
        )
        line = self.read_stdout_line()
        assert line, "No line received on stdout during initialize"
        data = json.loads(line)
        assert data.get("jsonrpc") == "2.0"
        assert data.get("id") == req_id
        assert "result" in data

        self.send_notification("notifications/initialized")
        return data

    def close(self) -> Tuple[str, str]:
        # Do not close stdin here: `communicate()` closes it itself, and the POSIX
        # implementation flushes it first - flushing a closed file raises ValueError. See the
        # matching note in tests/test_stream_purity_adversarial.py.
        try:
            stdout_rem, _ = self.proc.communicate(timeout=5.0)
        except Exception:
            self.proc.kill()
            stdout_rem, _ = self.proc.communicate()
        self._stderr_thread.join(timeout=1.0)
        return stdout_rem or "", "".join(self.stderr_lines)


class TestEmpiricalStreamPurityAndSubprocess:
    """Empirical adversarial verification of FastMCP stdio transport."""

    def test_startup_steady_state_and_shutdown_zero_stdout_pollution(self):
        """Verify absolute stdout purity during startup, steady state, and shutdown."""
        tester = EmpiricalSubprocessTester(log_level="DEBUG")
        try:
            # Idle startup phase (0.2s)
            time.sleep(0.2)
            assert tester.proc.poll() is None, "Subprocess died prematurely"

            # Initialization handshake
            init_res = tester.initialize()
            assert init_res["result"]["serverInfo"]["name"] == "mcp-agy"

            # Execute tools/list
            list_id = tester.send_request("tools/list")
            list_line = tester.read_stdout_line()
            list_data = json.loads(list_line)
            assert list_data.get("id") == list_id
            assert "result" in list_data

            # Execute 10 sequential chat requests
            for i in range(10):
                c_id = tester.send_request(
                    "tools/call",
                    {"name": "agy_chat", "arguments": {"prompt": f"Empirical query {i}"}},
                )
                c_line = tester.read_stdout_line()
                # Strict JSON-RPC frame validation
                c_data = json.loads(c_line)
                assert c_data.get("jsonrpc") == "2.0"
                assert c_data.get("id") == c_id
                assert "result" in c_data

        finally:
            stdout_rem, stderr_out = tester.close()

        # Strict byte-level stdout verification on close
        assert stdout_rem.strip() == "", f"Trailing unparsed stdout data detected: {stdout_rem!r}"

        # Ensure logging was written to stderr and contains expected messages
        assert len(stderr_out) > 0
        assert "agy_chat invoked" in stderr_out or "Starting mcp-agy" in stderr_out

    def test_high_frequency_stdio_integrity_100_calls(self, tmp_path):
        """Sends 100 rapid sequential requests to test frame tearing and buffer stability."""
        tester = EmpiricalSubprocessTester()
        try:
            tester.initialize()

            for i in range(100):
                req_id = tester.send_request(
                    "tools/call",
                    {"name": "agy_chat", "arguments": {"prompt": f"Burst request #{i}"}},
                )
                line = tester.read_stdout_line()
                assert line.endswith("\n") or line.endswith("\r\n")
                data = json.loads(line)
                assert data.get("id") == req_id
                assert data.get("jsonrpc") == "2.0"
                assert "result" in data
        finally:
            stdout_rem, _ = tester.close()

        assert stdout_rem.strip() == ""


class TestEmpiricalLoggingSeverityAndStackTraces:
    """Verifies that all logging severities and stack traces route 100% to stderr."""

    @pytest.mark.parametrize("level_name", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_all_severities_exclusive_stderr(self, level_name, capsys):
        logger = setup_logger(name=f"emp_log_{level_name.lower()}", level=level_name)
        getattr(logger, level_name.lower())(f"Message for {level_name}")

        captured = capsys.readouterr()
        assert captured.out == "", f"Violation: stdout polluted for level {level_name}: {captured.out!r}"
        assert f"Message for {level_name}" in captured.err

    def test_exception_traceback_exclusive_stderr(self, capsys):
        logger = setup_logger(name="emp_exc_logger", level="DEBUG")
        try:
            1 / 0
        except ZeroDivisionError:
            logger.exception("Zero division handled safely")

        captured = capsys.readouterr()
        assert captured.out == "", f"Violation: stdout polluted during logger.exception: {captured.out!r}"
        assert "Zero division handled safely" in captured.err
        assert "ZeroDivisionError: division by zero" in captured.err
        assert "Traceback (most recent call last):" in captured.err


class TestEmpiricalNDJSONParserHardening:
    """Adversarial testing of NDJSON stream parser across edge cases."""

    @pytest.mark.asyncio
    async def test_parser_with_2mb_payload_and_nested_structures(self):
        """Stress-test stream parser with 2.5MB payload and deep dictionary structures."""
        payload_2mb = "X" * (2500 * 1024)
        deep_dict = {f"k_{i}": f"v_{i}" for i in range(5000)}

        stream_lines = [
            json.dumps({"event": "init", "conversation_id": "conv-2mb-test"}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_info": {
                        "name": "write_to_file",
                        "parameters": {"TargetFile": "src/large_file.py", "data": deep_dict},
                        "output": "Wrote 2.5MB file",
                    },
                    "duration_seconds": 1.25,
                },
            }),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": payload_2mb,
                    "usage": {
                        "input_tokens": 50000,
                        "output_tokens": 150000,
                        "thinking_tokens": 25000,
                        "total_tokens": 225000,
                        "execution_time_ms": 1250.0,
                        "cost_estimate": 0.45,
                    },
                },
            }),
        ]

        async def fake_stream(*args, **kwargs) -> AsyncIterator[str]:
            for l in stream_lines:
                yield l

        backend = SubprocessCLIBackend()
        backend._cached_path = sys.executable

        from unittest.mock import patch
        with patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=fake_stream), \
             patch.object(backend, "is_available", return_value=True):

            req = ExecutionRequest(prompt="2MB payload test")
            res = await backend.execute(req)

            assert res.success is True
            assert len(res.response_text) == len(payload_2mb)
            assert res.token_usage.total_tokens == 225000
            assert res.token_usage.cost_estimate == 0.45
            assert "src/large_file.py" in res.modified_files
            assert len(res.tool_calls) == 1
            assert len(res.tool_calls[0].arguments["data"]) == 5000

    @pytest.mark.asyncio
    async def test_parser_with_null_and_malformed_fields(self):
        """Stress-test parser against nulls, missing keys, and invalid types in every field."""
        adversarial_lines = [
            "",  # empty
            "   ",  # whitespace
            "{broken",  # invalid JSON
            "null",  # null json
            "12345",  # int json
            '["a", "b"]',  # list json
            json.dumps({"event": None}),
            json.dumps({"event": "init", "conversation_id": None, "init": None}),
            json.dumps({"event": "step_update", "step_update": None}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": None,
                    "state": None,
                    "tool_name": None,
                    "tool_info": None,
                    "usage": None,
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": 12345,  # type confusion: int instead of str
                    "tool_info": {
                        "name": None,
                        "parameters": "not a dict",  # type confusion
                        "output": None,
                    },
                    "duration_seconds": "invalid_float",  # invalid float
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": None,
                    "usage": {
                        "input_tokens": None,
                        "output_tokens": -50,  # negative int
                        "total_tokens": "not_an_int",
                        "cost_estimate": None,
                    },
                },
            }),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": None,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": None,
                    },
                },
            }),
        ]

        async def fake_stream(*args, **kwargs) -> AsyncIterator[str]:
            for l in adversarial_lines:
                yield l

        backend = SubprocessCLIBackend()
        backend._cached_path = sys.executable

        from unittest.mock import patch
        with patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=fake_stream), \
             patch.object(backend, "is_available", return_value=True):

            req = ExecutionRequest(prompt="Null and malformed fields test")
            res = await backend.execute(req)

            assert res.success is True
            assert res.status == "success"
            assert res.token_usage.input_tokens == 100
            assert res.token_usage.output_tokens == 0

    @pytest.mark.asyncio
    async def test_parser_with_extreme_unicode_and_emojis(self):
        """Stress-test parser against complex Unicode: emojis, surrogates, ZWJ sequences, math symbols, RTL."""
        extreme_unicode = (
            "👩‍💻👨‍👩‍👧‍👦 🌟🔥🚀💯 "  # ZWJ emojis and symbols
            "CJK: 繁體中文 简体中文 日本語 한국어 "
            "RTL: العربية עִבְרִית "
            "Math & Special: ∇×E = -∂B/∂t, ∫∫∫_V (∇·F)dV, ¬(P ∧ Q) ⇔ (¬P ∨ ¬Q) "
            "Quotes & Escapes: \", ', `, \\, /, \t, \n, \r, \b, \f "
            "Hieroglyphs & Ancient: 𓀀𓀁𓀂 𐌀𐌁𐌂"
        )

        stream_lines = [
            json.dumps({"event": "init", "conversation_id": "conv-unicode-extreme"}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": extreme_unicode,
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_info": {
                        "name": "write_to_file",
                        "parameters": {"TargetFile": "src/unicode_🚀_файл.py"},
                        "output": extreme_unicode,
                    },
                },
            }),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": extreme_unicode,
                },
            }),
        ]

        async def fake_stream(*args, **kwargs) -> AsyncIterator[str]:
            for l in stream_lines:
                yield l

        backend = SubprocessCLIBackend()
        backend._cached_path = sys.executable

        from unittest.mock import patch
        with patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=fake_stream), \
             patch.object(backend, "is_available", return_value=True):

            req = ExecutionRequest(prompt="Extreme unicode test")
            res = await backend.execute(req)

            assert res.success is True
            assert extreme_unicode in res.response_text
            assert "src/unicode_🚀_файл.py" in res.modified_files
            assert len(res.tool_calls) == 1
            assert res.tool_calls[0].output == extreme_unicode
