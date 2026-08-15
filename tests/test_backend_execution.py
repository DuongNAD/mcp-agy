"""Comprehensive test suite for Milestone 2 AGY Execution Backend architecture.

Covers:
1. Process Management & Tree Termination (`mcp_agy.utils.process`)
2. Extended Pydantic Models & Converters (`mcp_agy.core.models`)
3. Subprocess CLI Execution Backend & NDJSON Streaming Parser (`mcp_agy.core.cli_backend`)
4. Python SDK Execution Backend & Dynamic Import Guarding (`mcp_agy.core.sdk_backend`)
5. High-Fidelity Mock AGY Simulation Engine (`mcp_agy.core.mock_backend`)
6. Backend Manager & 3-Tier Fallback Hierarchy (`mcp_agy.core.backend_manager`)
7. Global Backend Registry & FastMCP Server Integration (`mcp_agy.core.backend`, `mcp_agy.server`)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any, AsyncIterator, List
from unittest.mock import MagicMock, patch

import psutil
import pytest

from mcp_agy.core.backend import get_backend, reset_backend, set_backend
from mcp_agy.core.backend_manager import (
    BackendManager,
    get_backend_manager,
    reset_backend_manager,
    set_backend_manager,
)
from mcp_agy.core.cli_backend import (
    SubprocessCLIBackend,
    find_agy_executable,
)
from mcp_agy.core.mock_backend import MockAGYBackend
from mcp_agy.core.models import (
    BackendType,
    ExecutionRequest,
    ExecutionResult,
    StreamEvent,
    StreamEventType,
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


# ==============================================================================
# 1. Process Management & Tree Termination Tests
# ==============================================================================

class TestProcessManagement:
    """Tests for process lifecycle management, timeouts, and tree termination."""

    @pytest.mark.asyncio
    async def test_terminate_process_tree_live_process(self) -> None:
        """Verify terminate_process_tree terminates a spawned long-running process."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.pid is not None
        assert psutil.pid_exists(proc.pid)

        # Terminate process tree
        terminate_process_tree(proc.pid, timeout=1.0)
        await proc.wait()

        # Process should be terminated
        assert not psutil.pid_exists(proc.pid)

    def test_terminate_process_tree_invalid_and_nonexistent_pid(self) -> None:
        """Verify terminate_process_tree handles non-existent or invalid PIDs safely."""
        # Non-positive PIDs
        terminate_process_tree(0)
        terminate_process_tree(-1)

        # High non-existent PID
        terminate_process_tree(99999999)

    @pytest.mark.asyncio
    async def test_stream_subprocess_lines_success(self) -> None:
        """Verify stream_subprocess_lines yields lines in real-time."""
        code = (
            "import sys\n"
            "print('line1', flush=True)\n"
            "print('line2', flush=True)\n"
            "print('line3', flush=True)\n"
        )
        lines: List[str] = []
        async for line in stream_subprocess_lines(
            [sys.executable, "-c", code],
            timeout_seconds=5.0,
        ):
            lines.append(line)

        assert lines == ["line1", "line2", "line3"]

    @pytest.mark.asyncio
    async def test_stream_subprocess_lines_timeout(self) -> None:
        """Verify stream_subprocess_lines enforces timeout and cleans up process."""
        code = "import time; time.sleep(30)"
        with pytest.raises(asyncio.TimeoutError):
            async for _ in stream_subprocess_lines(
                [sys.executable, "-c", code],
                timeout_seconds=0.3,
            ):
                pass

    @pytest.mark.asyncio
    async def test_stream_subprocess_lines_cancellation(self) -> None:
        """Verify cancelling the streaming task cleans up the subprocess."""
        code = "import time, sys\nprint('start', flush=True)\ntime.sleep(30)"

        async def _consumer() -> List[str]:
            out = []
            async for line in stream_subprocess_lines(
                [sys.executable, "-c", code],
                timeout_seconds=30.0,
            ):
                out.append(line)
            return out

        task = asyncio.create_task(_consumer())
        await asyncio.sleep(0.2)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_run_subprocess_async_success(self) -> None:
        """Verify run_subprocess_async executes command and returns code, stdout, stderr."""
        code = "import sys\nsys.stdout.write('hello out')\nsys.stderr.write('hello err')"
        rc, out, err = await run_subprocess_async(
            [sys.executable, "-c", code],
            timeout_seconds=5.0,
        )
        assert rc == 0
        assert out == "hello out"
        assert err == "hello err"

    @pytest.mark.asyncio
    async def test_run_subprocess_async_timeout(self) -> None:
        """Verify run_subprocess_async raises TimeoutError on timeout."""
        with pytest.raises(asyncio.TimeoutError):
            await run_subprocess_async(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=0.3,
            )


# ==============================================================================
# 2. Models & Converters Tests
# ==============================================================================

class TestModelsAndConverters:
    """Tests for Milestone 2 Pydantic models, enums, and conversion methods."""

    def test_execution_result_to_task_execution_result(self) -> None:
        """Verify ExecutionResult converts accurately to TaskExecutionResult."""
        usage = TokenUsage(
            input_tokens=100,
            output_tokens=50,
            thinking_tokens=20,
            total_tokens=170,
            execution_time_ms=1234.5,
            cost_estimate=0.002,
        )
        res = ExecutionResult(
            success=True,
            status="success",
            response_text="Created feature.",
            tool_calls=[
                ToolEvent(tool_name="write_to_file", status=ToolStatus.DONE, arguments={"TargetFile": "a.py"})
            ],
            token_usage=usage,
            backend_used=BackendType.CLI,
            duration_seconds=1.2345,
            duration_ms=1234.5,
            session_id="sess-123",
            modified_files=["a.py"],
            diff_summary="1 file modified",
        )

        task_res = res.to_task_execution_result()
        assert task_res.status == "success"
        assert task_res.conversation_id == "sess-123"
        assert task_res.response == "Created feature."
        assert task_res.modified_files == ["a.py"]
        assert task_res.diff_summary == "1 file modified"
        assert task_res.duration_seconds == 1.2345
        assert task_res.tokens_used.total_tokens == 170
        assert task_res.backend_used == "cli"
        assert task_res.error_details is None

    def test_execution_result_to_chat_result(self) -> None:
        """Verify ExecutionResult converts accurately to ChatResult."""
        usage = TokenUsage(input_tokens=50, output_tokens=30, total_tokens=80)
        res = ExecutionResult(
            success=True,
            status="success",
            response_text="Analysis complete.",
            token_usage=usage,
            backend_used=BackendType.SDK,
            duration_seconds=0.85,
            session_id="chat-456",
        )

        chat_res = res.to_chat_result()
        assert chat_res.status == "success"
        assert chat_res.conversation_id == "chat-456"
        assert chat_res.response == "Analysis complete."
        assert chat_res.duration_seconds == 0.85
        assert chat_res.tokens_used.total_tokens == 80
        assert chat_res.backend_used == "sdk"
        assert chat_res.error_details is None

    def test_token_usage_defaults_and_backward_compatibility(self) -> None:
        """Verify TokenUsage backward compatibility with zero-default initialization."""
        t = TokenUsage()
        assert t.input_tokens == 0
        assert t.output_tokens == 0
        assert t.thinking_tokens == 0
        assert t.cache_read_tokens == 0
        assert t.total_tokens == 0
        assert t.execution_time_ms == 0.0
        assert t.cost_estimate == 0.0

    def test_stream_event_construction(self) -> None:
        """Verify StreamEvent initialization and properties."""
        event = StreamEvent(
            event_type=StreamEventType.INIT,
            data={"version": "1.0"},
            conversation_id="conv-1",
        )
        assert event.event_type == StreamEventType.INIT
        assert event.conversation_id == "conv-1"
        assert event.data == {"version": "1.0"}
        assert event.timestamp > 0


# ==============================================================================
# 3. Subprocess CLI Backend Tests
# ==============================================================================

class TestSubprocessCLIBackend:
    """Tests for SubprocessCLIBackend, command construction, and NDJSON streaming parser."""

    def test_find_agy_executable_custom_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Verify AGY_BIN_PATH environment variable takes discovery precedence."""
        fake_exe = tmp_path / "agy.exe"
        fake_exe.write_text("fake binary", encoding="utf-8")

        monkeypatch.setenv("AGY_BIN_PATH", str(fake_exe))
        found = find_agy_executable()
        assert found == str(fake_exe)

    def test_build_command_flags(self) -> None:
        """Verify command flags constructed by SubprocessCLIBackend."""
        backend = SubprocessCLIBackend(executable_path="C:/fake/agy.exe")
        with patch("os.path.isfile", return_value=True):
            cmd = backend._build_command(
                prompt="Implement user auth",
                workspace_path="E:/repo",
                auto_approve=True,
                mode="accept-edits",
                conversation_id="conv-101",
                model="gemini-3.6-pro",
                effort="high",
                timeout_seconds=300,
            )

        assert cmd[0] == "C:/fake/agy.exe"
        assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "accept-edits"
        assert "--dangerously-skip-permissions" in cmd
        assert "--add-dir" in cmd and cmd[cmd.index("--add-dir") + 1] == "E:/repo"
        assert "--conversation" in cmd and cmd[cmd.index("--conversation") + 1] == "conv-101"
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gemini-3.6-pro"
        assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "high"
        assert "--print-timeout" in cmd and cmd[cmd.index("--print-timeout") + 1] == "300s"
        assert cmd[-2:] == ["--print", "Implement user auth"]

    @pytest.mark.asyncio
    async def test_ndjson_stream_parsing_full_lifecycle(self) -> None:
        """Verify NDJSON parser parses init, step_update, tool calls, file edits, and result."""
        ndjson_lines = [
            json.dumps({
                "event": "init",
                "conversation_id": "test-conv-123",
                "init": {"cwd": "E:/repo", "tools": ["write_to_file", "view_file"]},
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "conversation_id": "test-conv-123",
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "tool_info": {"name": "view_file", "parameters": {"path": "src/main.py"}},
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "conversation_id": "test-conv-123",
                    "step_index": 1,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "duration_seconds": 0.05,
                    "tool_info": {"name": "view_file", "output": "content..."},
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "conversation_id": "test-conv-123",
                    "step_index": 2,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "write_to_file",
                    "duration_seconds": 0.1,
                    "tool_info": {
                        "name": "write_to_file",
                        "parameters": {"TargetFile": "E:/repo/src/auth.py"},
                    },
                },
            }),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "conversation_id": "test-conv-123",
                    "step_index": 3,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "Auth module implemented successfully.\n",
                },
            }),
            json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": "test-conv-123",
                    "status": "SUCCESS",
                    "response": "Auth module implemented successfully.\n",
                    "duration_seconds": 4.5,
                    "usage": {
                        "input_tokens": 1200,
                        "output_tokens": 150,
                        "thinking_tokens": 80,
                        "cache_read_tokens": 500,
                        "total_tokens": 1350,
                    },
                },
            }),
        ]

        async def _mock_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
            for line in ndjson_lines:
                yield line

        backend = SubprocessCLIBackend(executable_path="C:/fake/agy.exe")
        with patch.object(backend, "is_available", return_value=True), \
             patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=_mock_stream):
            result = await backend.execute_task(
                workspace_path="E:/repo",
                prompt="Implement auth",
                auto_approve=True,
                mode="accept-edits",
                timeout_seconds=60,
            )

        assert result.status == "success"
        assert result.conversation_id == "test-conv-123"
        assert "Auth module implemented successfully" in result.response
        assert result.modified_files == ["src/auth.py"]
        assert result.tokens_used.input_tokens == 1200
        assert result.tokens_used.output_tokens == 150
        assert result.tokens_used.thinking_tokens == 80
        assert result.tokens_used.cache_read_tokens == 500
        assert result.tokens_used.total_tokens == 1350
        assert result.backend_used == "cli"

    @pytest.mark.asyncio
    async def test_ndjson_stream_handles_non_json_lines_safely(self) -> None:
        """Verify parser skips non-JSON diagnostic lines without crashing."""
        mixed_lines = [
            "WARNING: Antigravity background telemetry active",
            "DEBUG: Starting local harness",
            json.dumps({
                "event": "init",
                "conversation_id": "conv-diag",
            }),
            "INFO: Tool executed",
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Done with diagnostic output.",
                    "usage": {"total_tokens": 42},
                },
            }),
        ]

        async def _mock_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
            for line in mixed_lines:
                yield line

        backend = SubprocessCLIBackend(executable_path="C:/fake/agy.exe")
        with patch.object(backend, "is_available", return_value=True), \
             patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=_mock_stream):
            chat_res = await backend.chat(
                prompt="Hello",
                workspace_path="E:/repo",
                conversation_id="conv-diag",
            )

        assert chat_res.status == "success"
        assert chat_res.response == "Done with diagnostic output."
        assert chat_res.tokens_used.total_tokens == 42
        assert chat_res.backend_used == "cli"

    @pytest.mark.asyncio
    async def test_cli_backend_unavailable_error_handling(self) -> None:
        """Verify SubprocessCLIBackend returns structured error when executable is missing."""
        backend = SubprocessCLIBackend(executable_path=None)
        with patch("mcp_agy.core.cli_backend.find_agy_executable", return_value=None):
            assert not backend.is_available()
            res = await backend.execute_task(
                workspace_path="E:/repo",
                prompt="Implement feature",
            )
            assert res.status == "error"
            assert "executable not found" in (res.error_details or "")

    @pytest.mark.asyncio
    async def test_cli_backend_timeout_handling(self) -> None:
        """Verify SubprocessCLIBackend catches timeout and returns status='timeout'."""
        async def _mock_stream_timeout(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
            if False:
                yield ""
            raise asyncio.TimeoutError("Process timed out")

        backend = SubprocessCLIBackend(executable_path="C:/fake/agy.exe")
        with patch.object(backend, "is_available", return_value=True), \
             patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=_mock_stream_timeout):
            res = await backend.execute_task(
                workspace_path="E:/repo",
                prompt="Long task",
                timeout_seconds=5,
            )

        assert res.status == "timeout"
        assert "timed out" in (res.error_details or "")
        assert res.backend_used == "cli"

    @pytest.mark.asyncio
    async def test_cli_backend_execute_stream_yields_events(self) -> None:
        """Verify execute_stream yields StreamEvent objects for each NDJSON event."""
        ndjson_lines = [
            json.dumps({"event": "init", "conversation_id": "stream-101", "init": {"cwd": "E:/repo"}}),
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "conversation_id": "stream-101",
                    "step_type": "agent_response",
                    "text_delta": "Chunk 1",
                },
            }),
            json.dumps({
                "event": "result",
                "result": {"status": "SUCCESS", "response": "Chunk 1", "usage": {"total_tokens": 10}},
            }),
        ]

        async def _mock_stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
            for line_item in ndjson_lines:
                yield line_item

        backend = SubprocessCLIBackend(executable_path="C:/fake/agy.exe")
        req = ExecutionRequest(prompt="Test streaming", session_id="stream-101")
        events: List[StreamEvent] = []

        with patch.object(backend, "is_available", return_value=True), \
             patch("mcp_agy.core.cli_backend.stream_subprocess_lines", side_effect=_mock_stream):
            async for ev in backend.execute_stream(req):
                events.append(ev)

        assert len(events) == 3
        assert events[0].event_type == StreamEventType.INIT
        assert events[1].event_type == StreamEventType.TEXT_DELTA
        assert events[1].text_delta == "Chunk 1"
        assert events[2].event_type == StreamEventType.RESULT


# ==============================================================================
# 4. Python SDK Backend Tests
# ==============================================================================

class TestPythonSDKBackend:
    """Tests for PythonSDKBackend, defensive imports, and mock agent sessions."""

    def test_is_sdk_available_returns_bool_safely(self) -> None:
        """Verify is_sdk_available returns bool without raising unhandled exceptions."""
        result = is_sdk_available()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_sdk_backend_unavailable_error(self) -> None:
        """Verify PythonSDKBackend returns structured error when SDK is unavailable."""
        backend = PythonSDKBackend()
        with patch("mcp_agy.core.sdk_backend.is_sdk_available", return_value=False):
            assert not backend.is_available()
            res = await backend.execute_task(workspace_path="E:/repo", prompt="Implement auth")
            assert res.status == "error"
            assert "google.antigravity SDK is not available" in (res.error_details or "")

    @pytest.mark.asyncio
    async def test_sdk_backend_mocked_success_session(self) -> None:
        """Verify PythonSDKBackend executes through mocked Agent session and extracts tokens."""
        # Create mock response object
        class MockResponse:
            def __init__(self) -> None:
                self.usage_metadata = MagicMock(
                    prompt_token_count=100,
                    candidates_token_count=50,
                    thoughts_token_count=20,
                    cached_content_token_count=10,
                    total_token_count=160,
                )

            def __aiter__(self) -> AsyncIterator[str]:
                async def _gen() -> AsyncIterator[str]:
                    yield "SDK "
                    yield "response "
                    yield "completed."
                return _gen()

        class MockAgent:
            def __init__(self, config: Any) -> None:
                self.config = config
                self.conversation_id = "sdk-conv-777"

            async def __aenter__(self) -> "MockAgent":
                return self

            async def __aexit__(self, *args: Any) -> None:
                pass

            async def chat(self, prompt: str) -> MockResponse:
                return MockResponse()

        mock_module = MagicMock()
        mock_module.Agent = MockAgent
        mock_module.CapabilitiesConfig = MagicMock()
        mock_module.LocalAgentConfig = MagicMock()
        mock_module.types = MagicMock()
        mock_module.hooks.policy.allow_all = MagicMock(return_value="allow_all")

        backend = PythonSDKBackend()
        with patch("mcp_agy.core.sdk_backend.is_sdk_available", return_value=True), \
             patch.dict("sys.modules", {
                 "google": MagicMock(),
                 "google.antigravity": mock_module,
                 "google.antigravity.hooks": mock_module.hooks,
                 "google.antigravity.hooks.policy": mock_module.hooks.policy,
             }):
            res = await backend.execute_task(
                workspace_path="E:/repo",
                prompt="Write code",
                auto_approve=True,
            )

        assert res.status == "success"
        assert res.conversation_id == "sdk-conv-777"
        assert res.response == "SDK response completed."
        assert res.tokens_used.input_tokens == 100
        assert res.tokens_used.output_tokens == 50
        assert res.tokens_used.thinking_tokens == 20
        assert res.tokens_used.cache_read_tokens == 10
        assert res.tokens_used.total_tokens == 160
        assert res.backend_used == "sdk"

    @pytest.mark.asyncio
    async def test_sdk_backend_chat_read_only(self) -> None:
        """Verify PythonSDKBackend chat configures read-only capabilities."""
        class MockChatResponse:
            def __init__(self) -> None:
                self.usage_metadata = MagicMock(
                    prompt_token_count=30,
                    candidates_token_count=20,
                    thoughts_token_count=10,
                    total_token_count=60,
                )

            def __aiter__(self) -> AsyncIterator[str]:
                async def _gen() -> AsyncIterator[str]:
                    yield "Analytical guidance from SDK."
                return _gen()

        class MockAgent:
            def __init__(self, config: Any) -> None:
                self.config = config
                self.conversation_id = "sdk-chat-888"

            async def __aenter__(self) -> "MockAgent":
                return self

            async def __aexit__(self, *args: Any) -> None:
                pass

            async def chat(self, prompt: str) -> MockChatResponse:
                return MockChatResponse()

        mock_module = MagicMock()
        mock_module.Agent = MockAgent
        mock_module.CapabilitiesConfig = MagicMock()
        mock_module.LocalAgentConfig = MagicMock()
        mock_module.types.BuiltinTools.read_only.return_value = ["view_file", "list_dir"]

        backend = PythonSDKBackend()
        with patch("mcp_agy.core.sdk_backend.is_sdk_available", return_value=True), \
             patch.dict("sys.modules", {
                 "google": MagicMock(),
                 "google.antigravity": mock_module,
                 "google.antigravity.hooks": mock_module.hooks,
                 "google.antigravity.hooks.policy": mock_module.hooks.policy,
             }):
            chat_res = await backend.chat(
                prompt="How does auth work?",
                workspace_path="E:/repo",
                conversation_id="sdk-chat-888",
            )

        assert chat_res.status == "success"
        assert chat_res.response == "Analytical guidance from SDK."
        assert chat_res.tokens_used.total_tokens == 60
        assert chat_res.backend_used == "sdk"


# ==============================================================================
# 5. Mock AGY Backend Tests
# ==============================================================================

class TestMockAGYBackend:
    """Tests for MockAGYBackend simulation engine, file synthesis, invariants, and failure injection."""

    def test_mock_backend_is_available(self) -> None:
        """Verify MockAGYBackend is always available."""
        backend = MockAGYBackend()
        assert backend.is_available() is True

    @pytest.mark.asyncio
    async def test_mock_backend_file_synthesis_in_accept_edits(self, tmp_path: Path) -> None:
        """Verify MockAGYBackend creates synthetic files in accept-edits mode."""
        backend = MockAGYBackend()
        res = await backend.execute_task(
            workspace_path=str(tmp_path),
            prompt="Please create calculator module for basic arithmetic",
            mode="accept-edits",
        )

        assert res.status == "success"
        assert "calculator.py" in res.modified_files
        calc_file = tmp_path / "calculator.py"
        assert calc_file.exists()
        assert "def add" in calc_file.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_mock_backend_plan_mode_read_only_invariant(self, tmp_path: Path) -> None:
        """Verify MockAGYBackend never creates files when mode='plan'."""
        backend = MockAGYBackend()
        res = await backend.execute_task(
            workspace_path=str(tmp_path),
            prompt="Please create calculator module",
            mode="plan",
        )

        assert res.status == "success"
        assert len(res.modified_files) == 0
        calc_file = tmp_path / "calculator.py"
        assert not calc_file.exists()

    @pytest.mark.asyncio
    async def test_mock_backend_chat_read_only_invariant(self, tmp_path: Path) -> None:
        """Verify MockAGYBackend chat never creates files."""
        backend = MockAGYBackend()
        res = await backend.chat(
            prompt="Please create auth module in src/auth.py",
            workspace_path=str(tmp_path),
        )

        assert res.status == "success"
        auth_file = tmp_path / "src" / "auth.py"
        assert not auth_file.exists()

    @pytest.mark.asyncio
    async def test_mock_backend_failure_injection(self) -> None:
        """Verify failure injection controls: force_timeout, force_error, simulated_delay."""
        backend = MockAGYBackend()

        # Test force_error
        backend.force_error = "Synthetic failure injected"
        res = await backend.execute_task(workspace_path="", prompt="Task")
        assert res.status == "error"
        assert res.error_details == "Synthetic failure injected"

        # Test force_timeout
        backend.force_error = None
        backend.force_timeout = True
        res_timeout = await backend.execute_task(workspace_path="", prompt="Task")
        assert res_timeout.status == "timeout"

        # Test reset
        backend.reset()
        assert backend.force_timeout is False
        assert backend.force_error is None
        res_ok = await backend.execute_task(workspace_path="", prompt="Task")
        assert res_ok.status == "success"

    @pytest.mark.asyncio
    async def test_mock_backend_streaming_events(self) -> None:
        """Verify execute_stream yields simulated events."""
        backend = MockAGYBackend()
        req = ExecutionRequest(prompt="Build app", mode="accept-edits")
        events: List[StreamEvent] = []

        async for ev in backend.execute_stream(req):
            events.append(ev)

        assert len(events) >= 4
        types = [e.event_type for e in events]
        assert StreamEventType.INIT in types
        assert StreamEventType.TOOL_CALL in types
        assert StreamEventType.TEXT_DELTA in types
        assert StreamEventType.RESULT in types


# ==============================================================================
# 6. Backend Manager & 3-Tier Fallback Tests
# ==============================================================================

class TestBackendManager:
    """Tests for BackendManager 3-tier fallback hierarchy (SDK -> CLI -> Mock)."""

    def test_resolution_hierarchy_sdk_first(self) -> None:
        """Verify BackendManager prioritizes SDK when SDK is available."""
        manager = BackendManager(preferred_backend="auto")
        with patch.object(manager.sdk_backend, "is_available", return_value=True):
            backend, b_type = manager.get_best_available_backend()
            assert b_type == BackendType.SDK
            assert backend is manager.sdk_backend

    def test_resolution_hierarchy_cli_fallback_when_sdk_unavailable(self) -> None:
        """Verify BackendManager falls back to CLI when SDK is unavailable."""
        manager = BackendManager(preferred_backend="auto")
        with patch.object(manager.sdk_backend, "is_available", return_value=False), \
             patch.object(manager.cli_backend, "is_available", return_value=True):
            backend, b_type = manager.get_best_available_backend()
            assert b_type == BackendType.CLI
            assert backend is manager.cli_backend

    def test_resolution_hierarchy_mock_fallback_when_both_unavailable(self) -> None:
        """Verify BackendManager falls back to Mock when SDK and CLI are unavailable."""
        manager = BackendManager(preferred_backend="auto")
        with patch.object(manager.sdk_backend, "is_available", return_value=False), \
             patch.object(manager.cli_backend, "is_available", return_value=False):
            backend, b_type = manager.get_best_available_backend()
            assert b_type == BackendType.MOCK
            assert backend is manager.mock_backend

    def test_explicit_preference_override(self) -> None:
        """Verify explicit preferred_backend overrides auto hierarchy."""
        manager = BackendManager(preferred_backend="mock")
        backend, b_type = manager.get_best_available_backend()
        assert b_type == BackendType.MOCK
        assert backend is manager.mock_backend

    def test_explicit_preference_unavailable_with_fallback_disabled_raises(self) -> None:
        """Verify RuntimeError is raised when preferred backend is unavailable and auto_fallback=False."""
        manager = BackendManager(preferred_backend="cli", auto_fallback=False)
        with patch.object(manager.cli_backend, "is_available", return_value=False):
            with pytest.raises(RuntimeError, match="Preferred backend 'cli' is unavailable"):
                manager.get_best_available_backend()

    @pytest.mark.asyncio
    async def test_execution_error_triggers_mock_fallback(self) -> None:
        """Verify that when primary backend encounters execution error, auto-fallback to Mock occurs."""
        manager = BackendManager(preferred_backend="auto", auto_fallback=True)

        # Make CLI available, but fail during execution
        with patch.object(manager.sdk_backend, "is_available", return_value=False), \
             patch.object(manager.cli_backend, "is_available", return_value=True):

            async def _failing_execute(req: ExecutionRequest) -> ExecutionResult:
                return ExecutionResult(
                    success=False,
                    status="error",
                    response_text="",
                    backend_used=BackendType.CLI,
                    error_message="Subprocess crashed with fatal SIGSEGV",
                )

            with patch.object(manager.cli_backend, "execute", side_effect=_failing_execute):
                result = await manager.execute_task(
                    workspace_path="",
                    prompt="Do something important",
                )

        assert result.status == "success"
        assert "[Note: Fallback to mock backend occurred" in result.response
        assert result.backend_used == "mock"

    def test_backend_manager_singleton_lifecycle(self) -> None:
        """Verify get_backend_manager, set_backend_manager, and reset_backend_manager."""
        reset_backend_manager()
        m1 = get_backend_manager()
        m2 = get_backend_manager()
        assert m1 is m2

        custom = BackendManager(preferred_backend="mock")
        set_backend_manager(custom)
        assert get_backend_manager() is custom

        reset_backend_manager()
        m3 = get_backend_manager()
        assert m3 is not custom


# ==============================================================================
# 7. Server & Global Integration Tests
# ==============================================================================

class TestServerBackendIntegration:
    """Tests verifying server.py and get_backend() seamlessly use BackendManager."""

    def setup_method(self) -> None:
        reset_backend()
        reset_backend_manager()

    def teardown_method(self) -> None:
        reset_backend()
        reset_backend_manager()

    def test_default_get_backend_returns_backend_manager(self) -> None:
        """Verify get_backend() defaults to BackendManager."""
        backend = get_backend()
        assert isinstance(backend, BackendManager)

    def test_set_backend_overrides_get_backend(self) -> None:
        """Verify set_backend() overrides get_backend()."""
        mock = MockAGYBackend()
        set_backend(mock)
        assert get_backend() is mock
        reset_backend()
        assert isinstance(get_backend(), BackendManager)

    @pytest.mark.asyncio
    async def test_server_tools_execute_through_backend_manager(self, tmp_path: Path) -> None:
        """Verify FastMCP server tools execute through the live BackendManager."""
        server = create_mcp_server()

        # In standard environment (mock backend fallback), execute task
        task_res = await server.call_tool(
            "agy_execute_task",
            {"workspace_path": str(tmp_path), "prompt": "Hello test prompt"},
        )
        task_data = json.loads(task_res[0][0].text)
        assert task_data["status"] == "success"
        assert len(task_data["response"]) > 0

        # Execute chat
        chat_res = await server.call_tool(
            "agy_chat",
            {"prompt": "Architectural inquiry", "workspace_path": str(tmp_path)},
        )
        chat_data = json.loads(chat_res[0][0].text)
        assert chat_data["status"] == "success"
        assert len(chat_data["response"]) > 0
