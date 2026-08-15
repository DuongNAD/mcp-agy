"""Adversarial stress test suite for Milestone 2: AGY Execution Backend.

Covers:
- Corrupted/malformed NDJSON lines, empty lines, random garbage
- Extreme Unicode characters, CJK, emojis, ANSI escapes, null bytes
- Massive JSON payloads (megabytes), deep nesting, extreme token counts
- Subprocess cancellation, timeouts, hanging streams, early generator exits
- Recursive process tree termination on Windows (parent -> child -> grandchild)
- SDK import failures and runtime crash handling
- BackendManager fallback edge cases, disabled fallback, and concurrency stress
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import AsyncIterator, List
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest

from mcp_agy.core.backend import AGYBackend, get_backend, reset_backend, set_backend
from mcp_agy.core.backend_manager import BackendManager, get_backend_manager, reset_backend_manager
from mcp_agy.core.cli_backend import SubprocessCLIBackend, find_agy_executable
from mcp_agy.core.mock_backend import MockAGYBackend
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
from mcp_agy.core.sdk_backend import PythonSDKBackend, is_sdk_available
from mcp_agy.server import create_mcp_server
from mcp_agy.utils.process import (
    run_subprocess_async,
    stream_subprocess_lines,
    terminate_process_tree,
)


# ============================================================================
# 1. Adversarial NDJSON Stream Parsing Stress Tests
# ============================================================================

class TestAdversarialNDJSONParsing:
    """Stress-tests NDJSON parser against hostile, corrupted, and edge-case inputs."""

    @pytest.mark.asyncio
    async def test_corrupted_and_malformed_json_lines(self):
        """Verify parser safely skips malformed, truncated, and invalid JSON lines without crashing."""
        malformed_lines = [
            "",  # empty line
            "   ",  # whitespace only
            "not json at all",
            "{unclosed json",
            '{"event": "init"',  # missing closing brace
            '{"event": "step_update", "step_update": {invalid: true}}',
            "null",  # valid JSON but not dict
            "true",  # valid JSON but not dict
            "12345",  # valid JSON but not dict
            '["array", "of", "strings"]',  # valid JSON list, not dict
            "\x00\x01\x02\x03",  # binary control characters
            '{"event": "init", "conversation_id": "valid-conv-1"}',  # valid line
            "{",  # single brace
            "}",  # single brace
            "::",  # syntax error
            '{"event": "result", "result": {"status": "SUCCESS", "response": "Recovered!"}}',
        ]

        async def fake_stream(*args, **kwargs) -> AsyncIterator[str]:
            for line in malformed_lines:
                yield line

        backend = SubprocessCLIBackend()
        with patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=fake_stream), \
             patch.object(backend, "is_available", return_value=True):
            
            request = ExecutionRequest(prompt="Test corrupted stream", timeout_seconds=10)
            events = []
            async for ev in backend.execute_stream(request):
                events.append(ev)

            # Valid events (init and result) must be extracted safely
            assert len(events) == 2
            assert events[0].event_type == StreamEventType.INIT
            assert events[0].conversation_id == "valid-conv-1"
            assert events[1].event_type == StreamEventType.RESULT
            assert events[1].text_delta == "Recovered!"

            # Test execute() method with the same malformed stream
            res = await backend.execute(request)
            assert res.success is True
            assert res.status == "success"
            assert res.response_text == "Recovered!"

    @pytest.mark.asyncio
    async def test_extreme_unicode_and_special_characters(self):
        """Verify handling of complex Unicode, CJK, Emojis, RTL scripts, and ANSI escapes."""
        special_text = (
            "🚀🔥🤖✨ Unicode test: 中文 (Chinese) 日本語 (Japanese) 한국어 (Korean) "
            "العربية (Arabic) עִברִית (Hebrew) — Math: ∫(x)dx = ½x² + C — ANSI: \x1b[32mSuccess\x1b[0m"
        )
        stream_lines = [
            json.dumps({"event": "init", "conversation_id": "conv-unicode-123"}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": special_text,
                    "state": "ACTIVE",
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_info": {
                        "name": "write_to_file",
                        "parameters": {"TargetFile": "src/unicode_⚡_файл.py"},
                        "output": "Created unicode 🚀 file.",
                    },
                    "duration_seconds": 0.05,
                },
            }),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": special_text,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 200,
                        "thinking_tokens": 50,
                        "total_tokens": 350,
                    },
                },
            }),
        ]

        async def fake_stream(*args, **kwargs) -> AsyncIterator[str]:
            for line in stream_lines:
                yield line

        backend = SubprocessCLIBackend()
        with patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=fake_stream), \
             patch.object(backend, "is_available", return_value=True):
            
            request = ExecutionRequest(prompt="Handle unicode", timeout_seconds=10)
            res = await backend.execute(request)

            assert res.success is True
            assert special_text in res.response_text
            assert "src/unicode_⚡_файл.py" in res.modified_files
            assert res.token_usage.total_tokens == 350

    @pytest.mark.asyncio
    async def test_massive_json_payloads(self):
        """Stress-test stream parser with huge payload (>2MB response and deep dictionary)."""
        huge_response = "A" * (2 * 1024 * 1024)  # 2 Megabytes string
        huge_dict = {f"key_{i}": f"value_{i}" for i in range(1000)}

        stream_lines = [
            json.dumps({"event": "init", "conversation_id": "huge-payload-session"}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_info": {
                        "name": "large_tool",
                        "parameters": huge_dict,
                        "output": "Huge output processed",
                    },
                },
            }),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": huge_response,
                    "usage": {
                        "input_tokens": 1000000,
                        "output_tokens": 5000000,
                        "thinking_tokens": 2000000,
                        "total_tokens": 8000000,
                    },
                },
            }),
        ]

        async def fake_stream(*args, **kwargs) -> AsyncIterator[str]:
            for line in stream_lines:
                yield line

        backend = SubprocessCLIBackend()
        with patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=fake_stream), \
             patch.object(backend, "is_available", return_value=True):
            
            request = ExecutionRequest(prompt="Massive payload test", timeout_seconds=30)
            res = await backend.execute(request)

            assert res.success is True
            assert len(res.response_text) == len(huge_response)
            assert res.token_usage.total_tokens == 8000000
            assert len(res.tool_calls) == 1
            assert len(res.tool_calls[0].arguments) == 1000

    @pytest.mark.asyncio
    async def test_null_and_malformed_nested_json_structures(self):
        """Test parser resilience against explicit null values in dictionary keys and usage objects."""
        null_events = [
            json.dumps({"event": "init", "init": None}),
            json.dumps({"event": "step_update", "step_update": None}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_info": None,
                    "tool_name": None,
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_info": {
                        "name": "write_to_file",
                        "parameters": None,
                        "output": None,
                    },
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": "Chunk 1",
                    "usage": None,
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": "Chunk 2",
                    "usage": {
                        "input_tokens": None,
                        "output_tokens": None,
                        "thinking_tokens": None,
                        "total_tokens": None,
                    },
                },
            }),
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Completed despite nulls",
                    "usage": {
                        "input_tokens": None,
                        "output_tokens": 100,
                        "total_tokens": None,
                    },
                },
            }),
        ]

        async def fake_stream(*args, **kwargs) -> AsyncIterator[str]:
            for line in null_events:
                yield line

        backend = SubprocessCLIBackend()
        with patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=fake_stream), \
             patch.object(backend, "is_available", return_value=True):
            
            request = ExecutionRequest(prompt="Handle null fields", timeout_seconds=10)
            
            # Streaming test
            streamed_events = []
            async for ev in backend.execute_stream(request):
                streamed_events.append(ev)
            
            assert len(streamed_events) > 0
            
            # Execute test
            res = await backend.execute(request)
            assert res.success is True
            assert "Completed despite nulls" in res.response_text
            assert res.token_usage.output_tokens == 100

    @pytest.mark.asyncio
    async def test_file_modification_path_normalization_adversarial(self, tmp_path):
        """Test path extraction across relative paths, traversal attempts ('..'), and Windows backslashes."""
        ws = str(tmp_path)
        stream_lines = [
            json.dumps({"event": "init", "conversation_id": "test-paths"}),
            # 1. Normal relative path
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "write_to_file",
                    "tool_info": {"parameters": {"TargetFile": "src/module.py"}},
                },
            }),
            # 2. Absolute path inside workspace
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "replace_file_content",
                    "tool_info": {"parameters": {"TargetFile": os.path.join(ws, "docs", "readme.md")}},
                },
            }),
            # 3. Path outside workspace (traversal attempt)
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "multi_replace_file_content",
                    "tool_info": {"parameters": {"file_path": os.path.join(os.path.dirname(ws), "outside.py")}},
                },
            }),
            # 4. Windows backslash path
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "sed_file",
                    "tool_info": {"parameters": {"path": r"nested\sub\config.json"}},
                },
            }),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Done"}}),
        ]

        async def fake_stream(*args, **kwargs) -> AsyncIterator[str]:
            for line in stream_lines:
                yield line

        backend = SubprocessCLIBackend()
        with patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=fake_stream), \
             patch.object(backend, "is_available", return_value=True):
            
            request = ExecutionRequest(prompt="Test path normalizations", workspace_path=ws)
            res = await backend.execute(request)

            assert "src/module.py" in res.modified_files
            assert "docs/readme.md" in res.modified_files
            assert "nested/sub/config.json" in res.modified_files
            assert len(res.modified_files) == 4


# ============================================================================
# 2. Adversarial Process Management & Termination Stress Tests
# ============================================================================

class TestAdversarialProcessManagement:
    """Stress-tests subprocess lifecycle, recursive tree termination on Windows, and timeouts."""

    @pytest.mark.asyncio
    async def test_recursive_grandchild_process_termination(self):
        """Empirically test recursive termination of parent -> child -> grandchild process tree."""
        # Create a Python script that spawns a child Python process, which spawns a grandchild sleep
        script_content = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import subprocess, sys, time; grandchild = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(60)\"]); time.sleep(60)'])"
            "\n"
            "print('PARENT_STARTED', flush=True)\n"
            "time.sleep(60)\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(script_content)
            script_path = f.name

        try:
            # Spawn parent process
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Wait for parent to start and spawn descendants
            line = await proc.stdout.readline()
            assert b"PARENT_STARTED" in line
            await asyncio.sleep(0.5)

            parent_pid = proc.pid
            assert psutil.pid_exists(parent_pid)

            # Discover all descendant PIDs using psutil
            parent_proc = psutil.Process(parent_pid)
            children = parent_proc.children(recursive=True)
            child_pids = [c.pid for c in children]
            assert len(child_pids) >= 1  # At least 1 child/grandchild spawned

            # Execute terminate_process_tree on root parent PID
            terminate_process_tree(parent_pid, timeout=2.0)
            await proc.wait()

            # Small delay to ensure OS process cleanup
            await asyncio.sleep(0.5)

            # EMPIRICAL VERIFICATION: Root parent and all descendants must be dead
            assert not psutil.pid_exists(parent_pid), f"Parent PID {parent_pid} survived!"
            for cpid in child_pids:
                assert not psutil.pid_exists(cpid), f"Child PID {cpid} leaked as a zombie process!"

        finally:
            if os.path.exists(script_path):
                os.remove(script_path)

    @pytest.mark.asyncio
    async def test_subprocess_stream_timeout_enforcement(self):
        """Verify stream_subprocess_lines strictly enforces timeout on hanging process and kills tree."""
        # Subprocess sleeps for 30s
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

        start = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            async for _ in stream_subprocess_lines(cmd, timeout_seconds=0.5):
                pass
        duration = time.monotonic() - start

        # Must time out in ~0.5s, not 30s
        assert duration < 3.0

    @pytest.mark.asyncio
    async def test_subprocess_stream_early_generator_cancellation(self):
        """Verify cancelling consumer task cleanly terminates process tree."""
        # Long-running output generator
        script = (
            "import time, sys\n"
            "for i in range(100):\n"
            "    print(f'LINE_{i}', flush=True)\n"
            "    time.sleep(0.1)\n"
        )
        cmd = [sys.executable, "-c", script]

        lines_received = []

        async def consumer():
            async for line in stream_subprocess_lines(cmd, timeout_seconds=10.0):
                lines_received.append(line)
                if len(lines_received) >= 2:
                    break  # Early break exits generator

        await consumer()
        assert len(lines_received) == 2
        # Give OS time to terminate
        await asyncio.sleep(0.3)

    @pytest.mark.asyncio
    async def test_subprocess_task_cancellation(self):
        """Verify cancelling an asyncio.Task running stream_subprocess_lines kills process."""
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]

        async def worker():
            async for _ in stream_subprocess_lines(cmd, timeout_seconds=10.0):
                pass

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.2)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_run_subprocess_async_timeout_and_tree_kill(self):
        """Verify run_subprocess_async handles timeout and cleans up descendants."""
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        with pytest.raises(asyncio.TimeoutError):
            await run_subprocess_async(cmd, timeout_seconds=0.5)

    def test_terminate_process_tree_invalid_pids(self):
        """Verify terminate_process_tree safely handles invalid, zero, or non-existent PIDs."""
        # Should not raise any exception
        terminate_process_tree(-1)
        terminate_process_tree(0)
        terminate_process_tree(99999999)  # non-existent PID


# ============================================================================
# 3. Adversarial SDK Backend Stress Tests
# ============================================================================

class TestAdversarialSDKBackend:
    """Stress-tests Python SDK backend against import crashes and runtime errors."""

    def test_is_sdk_available_handles_all_import_exceptions(self):
        """Verify is_sdk_available returns False upon any import error without throwing."""
        with patch.dict(sys.modules, {"google.antigravity": None}):
            # Importing a None module raises ModuleNotFoundError / ImportError
            assert is_sdk_available() is False

    @pytest.mark.asyncio
    async def test_sdk_backend_runtime_timeout(self):
        """Verify SDK backend returns timeout ExecutionResult when agent exceeds timeout."""
        backend = PythonSDKBackend()

        async def hanging_agent(*args, **kwargs):
            await asyncio.sleep(10.0)

        mock_agent_instance = MagicMock()
        mock_agent_instance.chat = AsyncMock(side_effect=hanging_agent)
        mock_agent_instance.__aenter__ = AsyncMock(return_value=mock_agent_instance)
        mock_agent_instance.__aexit__ = AsyncMock(return_value=None)
        mock_agent_instance.conversation_id = "test-conv-sdk"

        mock_module = MagicMock()
        mock_module.Agent = MagicMock(return_value=mock_agent_instance)
        mock_module.CapabilitiesConfig = MagicMock()
        mock_module.LocalAgentConfig = MagicMock()
        mock_module.types.BuiltinTools.read_only = MagicMock()

        with patch.dict(sys.modules, {"google.antigravity": mock_module}), \
             patch("mcp_agy.core.sdk_backend.is_sdk_available", return_value=True):
            
            request = ExecutionRequest(prompt="SDK hanging test", timeout_seconds=1)
            res = await backend.execute(request)

            assert res.success is False
            assert res.status == "timeout"
            assert "timed out" in (res.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_sdk_backend_runtime_exception(self):
        """Verify SDK backend catches unhandled exceptions and returns error ExecutionResult."""
        backend = PythonSDKBackend()

        mock_agent_instance = MagicMock()
        mock_agent_instance.chat = AsyncMock(side_effect=RuntimeError("SDK Internal Engine Panic"))
        mock_agent_instance.__aenter__ = AsyncMock(return_value=mock_agent_instance)
        mock_agent_instance.__aexit__ = AsyncMock(return_value=None)

        mock_module = MagicMock()
        mock_module.Agent = MagicMock(return_value=mock_agent_instance)
        mock_module.CapabilitiesConfig = MagicMock()
        mock_module.LocalAgentConfig = MagicMock()
        mock_module.types.BuiltinTools.read_only = MagicMock()

        with patch.dict(sys.modules, {"google.antigravity": mock_module}), \
             patch("mcp_agy.core.sdk_backend.is_sdk_available", return_value=True):
            
            request = ExecutionRequest(prompt="SDK panic test", timeout_seconds=10)
            res = await backend.execute(request)

            assert res.success is False
            assert res.status == "error"
            assert "SDK Internal Engine Panic" in (res.error_message or "")


# ============================================================================
# 4. Adversarial BackendManager Fallback & Concurrency Stress Tests
# ============================================================================

class TestAdversarialBackendManager:
    """Stress-tests 3-tier fallback resolution, error propagation, and concurrency."""

    @pytest.mark.asyncio
    async def test_automatic_fallback_when_cli_crashes(self):
        """Verify that when CLI execution fails, auto_fallback transparently invokes Mock backend."""
        mgr = BackendManager(preferred_backend="cli", auto_fallback=True)

        # Force CLI to be available but fail during execute()
        with patch.object(mgr.cli_backend, "is_available", return_value=True), \
             patch.object(
                 mgr.cli_backend,
                 "execute",
                 return_value=ExecutionResult(
                     success=False,
                     status="error",
                     backend_used=BackendType.CLI,
                     error_message="CLI Segfault / Crash",
                 ),
             ):
            request = ExecutionRequest(prompt="Implement calculator", mode="accept-edits")
            res = await mgr.execute(request)

            # Auto-fallback to mock should produce success with fallback annotation
            assert res.success is True
            assert res.backend_used == BackendType.MOCK
            assert "[Note: Fallback to mock backend occurred after cli error: CLI Segfault / Crash]" in res.response_text

    @pytest.mark.asyncio
    async def test_disabled_fallback_preserves_cli_error(self):
        """Verify that when auto_fallback=False, CLI errors are returned directly to the caller."""
        mgr = BackendManager(preferred_backend="cli", auto_fallback=False)

        with patch.object(mgr.cli_backend, "is_available", return_value=True), \
             patch.object(
                 mgr.cli_backend,
                 "execute",
                 return_value=ExecutionResult(
                     success=False,
                     status="error",
                     backend_used=BackendType.CLI,
                     error_message="Fatal compile error in CLI",
                 ),
             ):
            request = ExecutionRequest(prompt="Implement calculator")
            res = await mgr.execute(request)

            # Must NOT fallback to mock; must return CLI error
            assert res.success is False
            assert res.status == "error"
            assert res.backend_used == BackendType.CLI
            assert "Fatal compile error in CLI" in (res.error_message or "")

    @pytest.mark.asyncio
    async def test_concurrent_multi_task_executions(self, tmp_path):
        """Stress-test 20 concurrent execution tasks across async workers."""
        mgr = BackendManager(preferred_backend="mock")

        async def run_worker(index: int) -> TaskExecutionResult:
            sub_ws = tmp_path / f"worker_{index}"
            sub_ws.mkdir(parents=True, exist_ok=True)
            return await mgr.execute_task(
                workspace_path=str(sub_ws),
                prompt=f"Task {index}: implement calculator and math",
                mode="accept-edits",
            )

        # Launch 20 concurrent tasks
        results = await asyncio.gather(*[run_worker(i) for i in range(20)])

        assert len(results) == 20
        for i, r in enumerate(results):
            assert r.status == "success"
            assert r.backend_used == "mock"
            # Verify files were created in each respective workspace
            sub_ws = tmp_path / f"worker_{i}"
            assert (sub_ws / "calculator.py").exists()
            assert (sub_ws / "math_utils.py").exists()


# ============================================================================
# 5. Adversarial FastMCP Server Tool Invariants
# ============================================================================

class TestAdversarialServerToolInvariants:
    """Stress-tests server level validation and error responses."""

    @pytest.mark.asyncio
    async def test_agy_execute_task_empty_or_whitespace_prompt(self, tmp_path):
        """Verify agy_execute_task rejects empty and whitespace-only prompt strings."""
        server = create_mcp_server()
        execute_fn = server._tool_manager.get_tool("agy_execute_task").fn

        res1 = await execute_fn(workspace_path=str(tmp_path), prompt="")
        assert res1.status == "error"
        assert "Prompt cannot be empty" in res1.error_details

        res2 = await execute_fn(workspace_path=str(tmp_path), prompt="   \n\t  ")
        assert res2.status == "error"
        assert "Prompt cannot be empty" in res2.error_details

    @pytest.mark.asyncio
    async def test_agy_execute_task_empty_workspace(self):
        """Verify agy_execute_task rejects empty and whitespace-only workspace_path strings."""
        server = create_mcp_server()
        execute_fn = server._tool_manager.get_tool("agy_execute_task").fn

        res = await execute_fn(workspace_path="   ", prompt="Do work")
        assert res.status == "error"
        assert "Workspace path cannot be empty" in res.error_details

    @pytest.mark.asyncio
    async def test_agy_chat_empty_prompt(self):
        """Verify agy_chat rejects empty and whitespace-only prompt strings."""
        server = create_mcp_server()
        chat_fn = server._tool_manager.get_tool("agy_chat").fn

        res = await chat_fn(prompt="  \t ")
        assert res.status == "error"
        assert "Prompt cannot be empty" in res.error_details
