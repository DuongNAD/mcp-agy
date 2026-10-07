"""Comprehensive Protocol, Tool Registration, Schema, and Stdio Safety Tests for FastMCP AGY Server."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_agy import (
    AGYBackend,
    ChatResult,
    DiffResult,
    FileDiffStat,
    MockAGYBackend,
    TaskExecutionResult,
    TestFailure,
    TestRunResult,
    TestSummary,
    TokenUsage,
    configure_logging,
    create_mcp_server,
    create_server,
    get_backend,
    get_logger,
    mcp as default_mcp,
    reset_backend,
    server as default_server,
    set_backend,
    setup_logger,
    setup_logging,
)


@pytest.fixture(autouse=True)
def clean_backend_state():
    """Reset backend state before and after each test."""
    reset_backend()
    yield
    reset_backend()


@pytest.fixture
def mcp_app():
    """Provides a freshly instantiated FastMCP server instance for isolation."""
    return create_mcp_server()


# ============================================================================
# 1. Server Initialization & Protocol Tests
# ============================================================================


class TestServerInitialization:
    """Verifies server initialization, metadata, and JSON-RPC protocol handshakes."""

    def test_default_server_instances(self):
        """Verifies default module-level server is instantiated and configured."""
        assert default_mcp is not None
        assert default_server is not None
        assert default_mcp is default_server
        assert default_mcp.name == "mcp-agy"

    def test_server_factory_instances(self, mcp_app):
        """Verifies create_mcp_server and create_server create fresh instances."""
        assert mcp_app is not None
        assert mcp_app.name == "mcp-agy"
        server2 = create_server()
        assert server2 is not mcp_app

    @pytest.mark.asyncio
    async def test_protocol_client_session_handshake(self, mcp_app):
        """Verifies in-memory JSON-RPC 2.0 protocol handshake with MCP ClientSession."""
        async with create_connected_server_and_client_session(mcp_app._mcp_server) as client:
            init_result = await client.initialize()
            assert init_result is not None
            assert init_result.serverInfo.name == "mcp-agy"
            assert init_result.protocolVersion is not None
            assert init_result.capabilities is not None

    @pytest.mark.asyncio
    async def test_handshake_reports_package_version_not_sdk_version(self, mcp_app):
        """Verifies serverInfo.version is the mcp-agy version, not the installed mcp SDK version.

        FastMCP 1.x takes no `version`, so the low-level server otherwise reports the SDK
        version and every client shows this server as "mcp-agy v1.29.x" - the one number a
        user cannot act on when triaging a client-side bug report.
        """
        import mcp as mcp_sdk

        from mcp_agy import __version__

        async with create_connected_server_and_client_session(mcp_app._mcp_server) as client:
            init_result = await client.initialize()
            assert init_result.serverInfo.version == __version__
            sdk_version = getattr(mcp_sdk, "__version__", None)
            if sdk_version and sdk_version != __version__:
                assert init_result.serverInfo.version != sdk_version

    @pytest.mark.asyncio
    async def test_protocol_client_session_list_and_call_tools(self, mcp_app, tmp_path):
        """Verifies listing and calling tools via in-memory ClientSession JSON-RPC transport."""
        async with create_connected_server_and_client_session(mcp_app._mcp_server) as client:
            await client.initialize()
            tools_list = await client.list_tools()
            tool_names = {t.name for t in tools_list.tools}
            assert "agy_execute_task" in tool_names
            assert "agy_chat" in tool_names
            assert "agy_get_diff" in tool_names
            assert "agy_run_tests" in tool_names

            # Call tool over client session
            call_res = await client.call_tool(
                "agy_chat",
                arguments={"prompt": "Explain FastMCP protocol."},
            )
            assert call_res is not None
            assert not call_res.isError
            assert len(call_res.content) > 0
            text_content = call_res.content[0].text
            assert "Explain FastMCP protocol." in text_content


# ============================================================================
# 2. Tool Registration & Listing Tests
# ============================================================================


class TestToolRegistrationAndListing:
    """Verifies that all 4 core tools are properly registered with rich docstrings."""

    @pytest.mark.asyncio
    async def test_exactly_the_expected_tools_are_registered(self, mcp_app):
        """Verifies exactly the required tools are registered - the 4 synchronous ones plus
        the 5 that run AGY as a background job, and nothing else."""
        tools = await mcp_app.list_tools()
        tool_names = {t.name for t in tools}
        expected_names = {
            "agy_execute_task",
            "agy_chat",
            "agy_get_diff",
            "agy_run_tests",
            "agy_start_task",
            "agy_job_status",
            "agy_cancel_job",
            "agy_list_jobs",
            "agy_wait",
        }
        assert tool_names == expected_names
        assert len(tools) == len(expected_names)

    @pytest.mark.asyncio
    async def test_tool_docstrings_are_rich(self, mcp_app):
        """Verifies tool docstrings are detailed (>50 chars) and contain architect guidance."""
        tools = await mcp_app.list_tools()
        for tool in tools:
            assert tool.description is not None
            desc = tool.description.strip()
            assert len(desc) >= 50, f"Tool '{tool.name}' docstring is too short ({len(desc)} chars)"
            assert "Architect Guidance" in desc or "Args:" in desc


# ============================================================================
# 3. Tool Schemas & Parameter Annotations Tests
# ============================================================================


class TestToolSchemasAndAnnotations:
    """Verifies JSON Schema parameters, types, required fields, and constraints."""

    @pytest.mark.asyncio
    async def test_agy_execute_task_schema(self, mcp_app):
        """Verifies schema definition for agy_execute_task."""
        tools = await mcp_app.list_tools()
        tool = next(t for t in tools if t.name == "agy_execute_task")
        schema = tool.inputSchema

        assert schema["type"] == "object"
        assert "workspace_path" in schema["required"]
        assert "prompt" in schema["required"]

        props = schema["properties"]
        # workspace_path
        assert props["workspace_path"]["type"] == "string"
        assert props["workspace_path"].get("minLength") == 1
        assert "description" in props["workspace_path"]

        # prompt
        assert props["prompt"]["type"] == "string"
        assert props["prompt"].get("minLength") == 1
        assert "description" in props["prompt"]

        # auto_approve
        assert props["auto_approve"]["type"] == "boolean"
        assert props["auto_approve"]["default"] is True

        # mode
        assert props["mode"]["default"] == "accept-edits"
        assert props["mode"].get("enum") == ["accept-edits", "plan"]

        # timeout_seconds
        assert props["timeout_seconds"]["type"] == "integer"
        assert props["timeout_seconds"]["default"] == 600
        assert props["timeout_seconds"].get("minimum") == 1
        assert props["timeout_seconds"].get("maximum") == 3600

    @pytest.mark.asyncio
    async def test_agy_chat_schema(self, mcp_app):
        """Verifies schema definition for agy_chat."""
        tools = await mcp_app.list_tools()
        tool = next(t for t in tools if t.name == "agy_chat")
        schema = tool.inputSchema

        assert schema["type"] == "object"
        assert "prompt" in schema["required"]

        props = schema["properties"]
        assert props["prompt"]["type"] == "string"
        assert props["prompt"].get("minLength") == 1
        assert props["workspace_path"]["type"] == "string"
        assert props["workspace_path"]["default"] == ""
        assert props["conversation_id"]["type"] == "string"
        assert props["conversation_id"]["default"] == ""
        assert props["timeout_seconds"]["type"] == "integer"
        assert props["timeout_seconds"]["default"] == 300
        assert props["timeout_seconds"].get("minimum") == 1
        assert props["timeout_seconds"].get("maximum") == 1800

    @pytest.mark.asyncio
    async def test_agy_get_diff_schema(self, mcp_app):
        """Verifies schema definition for agy_get_diff."""
        tools = await mcp_app.list_tools()
        tool = next(t for t in tools if t.name == "agy_get_diff")
        schema = tool.inputSchema

        assert schema["type"] == "object"
        assert "workspace_path" in schema["required"]
        assert schema["properties"]["workspace_path"]["type"] == "string"
        assert schema["properties"]["workspace_path"].get("minLength") == 1

    @pytest.mark.asyncio
    async def test_agy_run_tests_schema(self, mcp_app):
        """Verifies schema definition for agy_run_tests."""
        tools = await mcp_app.list_tools()
        tool = next(t for t in tools if t.name == "agy_run_tests")
        schema = tool.inputSchema

        assert schema["type"] == "object"
        assert "workspace_path" in schema["required"]
        props = schema["properties"]
        assert props["workspace_path"]["type"] == "string"
        assert props["workspace_path"].get("minLength") == 1
        assert props["test_command"]["type"] == "string"
        assert props["test_command"]["default"] == ""
        assert props["timeout_seconds"]["type"] == "integer"
        assert props["timeout_seconds"]["default"] == 300
        assert props["timeout_seconds"].get("minimum") == 1


# ============================================================================
# 4. Tool Execution & Serialization Tests
# ============================================================================


class TestToolExecutionAndSerialization:
    """Verifies direct and in-memory execution of each tool and return deserialization."""

    @pytest.mark.asyncio
    async def test_agy_execute_task_execution(self, mcp_app, tmp_path):
        """Verifies executing agy_execute_task returns valid TaskExecutionResult."""
        res = await mcp_app.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(tmp_path),
                "prompt": "Implement calculation engine",
                "auto_approve": True,
                "mode": "accept-edits",
                "timeout_seconds": 120,
            },
        )
        assert res is not None
        text_payload = res[0][0].text
        data = json.loads(text_payload)
        model = TaskExecutionResult.model_validate(data)

        assert model.status == "success"
        assert model.backend_used == "mock"
        assert "calculation engine" in model.response
        assert model.tokens_used.total_tokens > 0

    @pytest.mark.asyncio
    async def test_agy_chat_execution(self, mcp_app):
        """Verifies executing agy_chat returns valid ChatResult."""
        res = await mcp_app.call_tool(
            "agy_chat",
            {
                "prompt": "Review system architecture",
                "conversation_id": "conv-test-123",
            },
        )
        text_payload = res[0][0].text
        data = json.loads(text_payload)
        model = ChatResult.model_validate(data)

        assert model.status == "success"
        assert model.conversation_id == "conv-test-123"
        assert "architecture" in model.response
        assert model.tokens_used.total_tokens > 0

    @pytest.mark.asyncio
    async def test_agy_get_diff_execution_non_git(self, mcp_app, tmp_path):
        """Verifies executing agy_get_diff on a regular non-git folder returns not_a_git_repo."""
        res = await mcp_app.call_tool(
            "agy_get_diff",
            {"workspace_path": str(tmp_path)},
        )
        text_payload = res[0][0].text
        data = json.loads(text_payload)
        model = DiffResult.model_validate(data)

        assert model.status == "not_a_git_repo"
        assert model.has_changes is False

    @pytest.mark.asyncio
    async def test_agy_get_diff_execution_non_existent_dir(self, mcp_app):
        """Verifies executing agy_get_diff on non-existent path returns error status."""
        non_existent_path = os.path.join("non_existent_dir_99999", "sub_path")
        res = await mcp_app.call_tool(
            "agy_get_diff",
            {"workspace_path": non_existent_path},
        )
        text_payload = res[0][0].text
        data = json.loads(text_payload)
        model = DiffResult.model_validate(data)

        assert model.status == "error"
        assert "not found" in (model.error_details or "").lower() or "not exist" in model.summary.lower()

    @pytest.mark.asyncio
    async def test_agy_run_tests_execution(self, mcp_app, tmp_path):
        """Verifies executing agy_run_tests returns valid TestRunResult."""
        test_file = tmp_path / "test_sample.py"
        test_file.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
        res = await mcp_app.call_tool(
            "agy_run_tests",
            {
                "workspace_path": str(tmp_path),
                "test_command": f'"{sys.executable}" -m pytest -v',
            },
        )
        text_payload = res[0][0].text
        data = json.loads(text_payload)
        model = TestRunResult.model_validate(data)

        assert model.status == "passed"
        assert model.exit_code == 0
        assert f'"{sys.executable}" -m pytest -v' in model.test_command_executed
        assert model.summary.total == 1
        assert model.summary.passed == 1

    @pytest.mark.asyncio
    async def test_custom_backend_injection(self, tmp_path):
        """Verifies custom AGYBackend injection into server factory."""
        class CustomBackend(AGYBackend):
            async def execute_task(self, workspace_path, prompt, **kwargs):
                return TaskExecutionResult(
                    status="success",
                    conversation_id="custom-conv-1",
                    response="Custom backend executed.",
                    backend_used="custom",
                )

            async def chat(self, prompt, **kwargs):
                return ChatResult(
                    status="success",
                    conversation_id="custom-chat-1",
                    response="Custom chat response.",
                    backend_used="custom",
                )

        custom_server = create_mcp_server(backend=CustomBackend())
        res = await custom_server.call_tool(
            "agy_execute_task",
            {"workspace_path": str(tmp_path), "prompt": "Test custom"},
        )
        data = json.loads(res[0][0].text)
        assert data["backend_used"] == "custom"
        assert data["response"] == "Custom backend executed."


# ============================================================================
# 5. Input Validation & Error Handling Tests
# ============================================================================


class TestInputValidationAndErrorHandling:
    """Verifies parameter validation, constraint enforcement, and error reporting."""

    @pytest.mark.asyncio
    async def test_execute_task_missing_required_arguments(self, mcp_app, tmp_path):
        """Verifies missing required parameters raise ToolError."""
        with pytest.raises(ToolError):
            await mcp_app.call_tool("agy_execute_task", {"workspace_path": str(tmp_path)})

        with pytest.raises(ToolError):
            await mcp_app.call_tool("agy_execute_task", {"prompt": "Do task"})

    @pytest.mark.asyncio
    async def test_execute_task_invalid_timeout_bounds(self, mcp_app, tmp_path):
        """Verifies timeout outside [1, 3600] raises ToolError."""
        with pytest.raises(ToolError):
            await mcp_app.call_tool(
                "agy_execute_task",
                {"workspace_path": str(tmp_path), "prompt": "Test", "timeout_seconds": 0},
            )

        with pytest.raises(ToolError):
            await mcp_app.call_tool(
                "agy_execute_task",
                {"workspace_path": str(tmp_path), "prompt": "Test", "timeout_seconds": 5000},
            )

    @pytest.mark.asyncio
    async def test_execute_task_invalid_mode(self, mcp_app, tmp_path):
        """Verifies invalid mode raises ToolError."""
        with pytest.raises(ToolError):
            await mcp_app.call_tool(
                "agy_execute_task",
                {"workspace_path": str(tmp_path), "prompt": "Test", "mode": "unsupported-mode"},
            )

    @pytest.mark.asyncio
    async def test_execute_task_empty_whitespace_prompt(self, mcp_app, tmp_path):
        """Verifies whitespace-only prompt returns structured error."""
        res = await mcp_app.call_tool(
            "agy_execute_task",
            {"workspace_path": str(tmp_path), "prompt": "   \t\n  "},
        )
        data = json.loads(res[0][0].text)
        assert data["status"] == "error"
        assert "empty" in data["error_details"].lower() or "prompt" in data["error_details"].lower()

    @pytest.mark.asyncio
    async def test_chat_empty_whitespace_prompt(self, mcp_app):
        """Verifies whitespace-only chat prompt returns structured error."""
        res = await mcp_app.call_tool(
            "agy_chat",
            {"prompt": "   "},
        )
        data = json.loads(res[0][0].text)
        assert data["status"] == "error"
        assert "empty" in data["error_details"].lower()

    @pytest.mark.asyncio
    async def test_get_diff_empty_whitespace_path(self, mcp_app):
        """Verifies whitespace-only workspace_path returns structured error."""
        res = await mcp_app.call_tool(
            "agy_get_diff",
            {"workspace_path": "   "},
        )
        data = json.loads(res[0][0].text)
        assert data["status"] == "error"
        assert "empty" in data["error_details"].lower()

    @pytest.mark.asyncio
    async def test_run_tests_empty_whitespace_path(self, mcp_app):
        """Verifies whitespace-only workspace_path for run_tests returns structured error."""
        res = await mcp_app.call_tool(
            "agy_run_tests",
            {"workspace_path": "   "},
        )
        data = json.loads(res[0][0].text)
        assert data["status"] == "error"
        assert "empty" in data["error_details"].lower()


# ============================================================================
# 6. Stdio Purity & Logging Tests
# ============================================================================


class TestStdioPurityAndLogging:
    """Verifies that all server logging routes exclusively to sys.stderr, leaving sys.stdout 100% clean."""

    def test_logger_writes_exclusively_to_stderr(self, capsys):
        """Verifies that logger messages never touch sys.stdout."""
        setup_logging(level="DEBUG")
        logger = get_logger("test_purity")

        logger.debug("Debug message on stderr")
        logger.info("Info message on stderr")
        logger.warning("Warning message on stderr")
        logger.error("Error message on stderr")

        captured = capsys.readouterr()
        assert captured.out == "", f"CRITICAL: sys.stdout polluted: {captured.out!r}"
        assert "Debug message on stderr" in captured.err
        assert "Info message on stderr" in captured.err
        assert "Warning message on stderr" in captured.err
        assert "Error message on stderr" in captured.err

    @pytest.mark.asyncio
    async def test_tool_invocations_preserve_clean_stdout(self, mcp_app, tmp_path, capsys):
        """Verifies that executing tools does not write anything to sys.stdout."""
        setup_logging(level="INFO")

        await mcp_app.call_tool("agy_chat", {"prompt": "Analyze something"})
        await mcp_app.call_tool("agy_execute_task", {"workspace_path": str(tmp_path), "prompt": "Code task"})
        await mcp_app.call_tool("agy_get_diff", {"workspace_path": str(tmp_path)})
        await mcp_app.call_tool("agy_run_tests", {"workspace_path": str(tmp_path)})

        captured = capsys.readouterr()
        assert captured.out == "", f"CRITICAL: sys.stdout was polluted during tool calls: {captured.out!r}"
        assert "agy_chat invoked" in captured.err
        assert "agy_execute_task invoked" in captured.err
        assert "agy_get_diff invoked" in captured.err
        assert "agy_run_tests invoked" in captured.err


# ============================================================================
# 7. Pydantic Models Serialization & Validation Tests
# ============================================================================


class TestPydanticModelSerialization:
    """Verifies Pydantic v2 serialization, deserialization, and schema integrity for all 8 models."""

    def test_token_usage_serialization(self):
        token = TokenUsage(
            input_tokens=100,
            output_tokens=50,
            thinking_tokens=25,
            cache_read_tokens=10,
            total_tokens=185,
        )
        data = token.model_dump()
        assert data["input_tokens"] == 100
        assert data["total_tokens"] == 185
        validated = TokenUsage.model_validate_json(token.model_dump_json())
        assert validated == token

    def test_task_execution_result_serialization(self):
        res = TaskExecutionResult(
            status="success",
            conversation_id="conv-101",
            response="Done",
            modified_files=["src/a.py", "tests/test_a.py"],
            diff_summary="2 files modified",
            duration_seconds=1.25,
            tokens_used=TokenUsage(total_tokens=200),
            backend_used="cli",
        )
        json_str = res.model_dump_json()
        validated = TaskExecutionResult.model_validate_json(json_str)
        assert validated.status == "success"
        assert len(validated.modified_files) == 2
        assert validated.tokens_used.total_tokens == 200

    def test_chat_result_serialization(self):
        chat = ChatResult(
            status="success",
            conversation_id="chat-101",
            response="Analytical insight",
            duration_seconds=0.5,
            tokens_used=TokenUsage(total_tokens=50),
            backend_used="cli",
        )
        validated = ChatResult.model_validate_json(chat.model_dump_json())
        assert validated.response == "Analytical insight"
        assert validated.backend_used == "cli"

    def test_diff_result_serialization(self):
        diff_stat = FileDiffStat(
            path="src/main.py",
            status="M",
            insertions=10,
            deletions=2,
        )
        diff = DiffResult(
            status="success",
            has_changes=True,
            unified_diff="--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n",
            changed_files=[diff_stat],
            untracked_files=["new_file.py"],
            summary="1 modified, 1 untracked",
        )
        validated = DiffResult.model_validate_json(diff.model_dump_json())
        assert validated.has_changes is True
        assert len(validated.changed_files) == 1
        assert validated.changed_files[0].status == "M"
        assert validated.untracked_files == ["new_file.py"]

    def test_test_run_result_serialization(self):
        failure = TestFailure(
            test_id="tests/test_auth.py::test_login",
            message="AssertionError: 401 != 200",
            location="tests/test_auth.py:42",
            traceback="Traceback (most recent call last):\n...",
        )
        summary = TestSummary(
            total=10,
            passed=9,
            failed=1,
            skipped=0,
            errors=0,
        )
        run_res = TestRunResult(
            status="failed",
            exit_code=1,
            framework="pytest",
            test_command_executed="pytest tests/",
            output="FAILED tests/test_auth.py::test_login",
            summary=summary,
            failures=[failure],
            duration_seconds=2.1,
            error_details="1 test failed",
        )
        validated = TestRunResult.model_validate_json(run_res.model_dump_json())
        assert validated.status == "failed"
        assert validated.summary.total == 10
        assert validated.summary.failed == 1
        assert len(validated.failures) == 1
        assert validated.failures[0].test_id == "tests/test_auth.py::test_login"

    def test_model_json_schemas_exist(self):
        """Verifies that all 8 models can generate valid JSON Schema dicts."""
        models = [
            TokenUsage,
            TaskExecutionResult,
            ChatResult,
            FileDiffStat,
            DiffResult,
            TestFailure,
            TestSummary,
            TestRunResult,
        ]
        for model in models:
            schema = model.model_json_schema()
            assert isinstance(schema, dict)
            assert "type" in schema or "properties" in schema
