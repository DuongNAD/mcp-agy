"""Tier 2: Boundary & Corner Case End-to-End Tests for FastMCP AGY Server.

Validates all 4 primary tools under boundary conditions, edge cases, error states, and corner scenarios:
- agy_execute_task: Empty/whitespace prompts, non-existent workspace paths, timeout injection,
  crash/OOM error simulation, huge multiline prompts, timeout parameter boundary limits, plan mode read-only boundary.
- agy_chat: Empty/whitespace prompts, read-only SHA-256 snapshot invariance across dirty workspaces,
  multi-turn conversation ID continuity, timeout simulation, special unicode & control characters, boundary timeout limits.
- agy_get_diff: Non-git directories (status='not_a_git_repo'), unborn git branches (0 commits),
  dirty workspace combinations (M/A/D/??/MM), binary file modifications, .gitignore rule exclusions,
  large diffs (5000+ lines), non-existent paths.
- agy_run_tests: Workspaces without test frameworks, hanging test suites with timeout/termination,
  invalid custom test commands & exit code handling, missing executables, mixed passing/failing/skipped test suites,
  non-existent paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_agy.core.backend import AGYBackend, MockAGYBackend
from tests.conftest import ProgrammableMockAGYBackend
from mcp_agy.core.models import (
    ChatResult,
    DiffResult,
    FileDiffStat,
    TaskExecutionResult,
    TestFailure,
    TestRunResult,
    TestSummary,
    TokenUsage,
)
from mcp_agy.server import create_mcp_server

pytestmark = [pytest.mark.tier2, pytest.mark.asyncio]


# ============================================================================
# 1. agy_execute_task Boundary & Corner Tests (>= 5 tests)
# ============================================================================


class TestAgyExecuteTaskBoundaries:
    """Boundary and corner case test suite for `agy_execute_task`."""

    async def test_execute_task_empty_prompt_rejected(
        self, test_server: Any, client_factory: Any, temp_workspace: Any
    ) -> None:
        """Verifies that empty and whitespace-only prompts are rejected with structured error."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(temp_workspace),
                    "prompt": "   \t\n  \r\n  ",
                },
            )
            data = json.loads(res.content[0].text)
            result = TaskExecutionResult.model_validate(data)
            assert result.status == "error"
            assert result.error_details is not None
            assert "prompt" in result.error_details.lower() or "empty" in result.error_details.lower()

    async def test_execute_task_empty_workspace_path_rejected(
        self, test_server: Any, client_factory: Any
    ) -> None:
        """Verifies that empty and whitespace-only workspace_path returns a structured error."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": "   \t  ",
                    "prompt": "Create calculator module",
                },
            )
            data = json.loads(res.content[0].text)
            result = TaskExecutionResult.model_validate(data)
            assert result.status == "error"
            assert result.error_details is not None
            assert "workspace" in result.error_details.lower() or "empty" in result.error_details.lower()

    async def test_execute_task_nonexistent_workspace_path(
        self, test_server: Any, client_factory: Any, ephemeral_workspace: Any
    ) -> None:
        """Verifies behavior when workspace path does not exist on disk."""
        nonexistent_path = str(ephemeral_workspace.path / "nonexistent_subfolder_xyz_987")
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": nonexistent_path,
                    "prompt": "Create calculator.py with add function",
                    "auto_approve": True,
                },
            )
            data = json.loads(res.content[0].text)
            result = TaskExecutionResult.model_validate(data)
            # Server should reject nonexistent workspace paths with an error status
            assert result.status == "error"
            assert "does not exist" in (result.error_details or "").lower() or "not found" in (result.error_details or "").lower()

    async def test_execute_task_backend_timeout_simulation(
        self,
        test_server: Any,
        client_factory: Any,
        clean_git_workspace: Any,
        mock_backend: ProgrammableMockAGYBackend,
    ) -> None:
        """Verifies timeout handling when the backend simulation triggers a timeout."""
        mock_backend.register_task_handler(
            lambda **kw: TaskExecutionResult(
                status="timeout",
                conversation_id="mock-task-timeout-1",
                response="",
                error_details=f"Task execution timed out after {kw.get('timeout_seconds', 600)}s",
                backend_used="mock",
            )
        )
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(clean_git_workspace),
                    "prompt": "Implement massive legacy refactoring",
                    "timeout_seconds": 10,
                },
            )
            data = json.loads(res.content[0].text)
            result = TaskExecutionResult.model_validate(data)
            assert result.status == "timeout"
            assert result.error_details is not None
            assert "timed out" in result.error_details.lower()

    async def test_execute_task_backend_crash_error_simulation(
        self,
        test_server: Any,
        client_factory: Any,
        clean_git_workspace: Any,
        mock_backend: ProgrammableMockAGYBackend,
    ) -> None:
        """Verifies error resilience and reporting when backend experiences a crash / OOM."""
        mock_backend.force_error = "FATAL: Subprocess terminated with exit code 137 (Out of Memory)"
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(clean_git_workspace),
                    "prompt": "Process enormous dataset in memory",
                },
            )
            data = json.loads(res.content[0].text)
            result = TaskExecutionResult.model_validate(data)
            assert result.status == "error"
            assert result.error_details is not None
            assert "exit code 137" in result.error_details

    async def test_execute_task_huge_multiline_prompt(
        self, test_server: Any, client_factory: Any, temp_workspace: Any
    ) -> None:
        """Verifies handling of massive (>10k chars) multiline prompts with code blocks and unicode."""
        lines = [f"- Requirement {i:03d}: Ensure edge case handler {i} handles payload cleanly." for i in range(250)]
        huge_prompt = (
            "# Master Architecture & Refactoring Specification\n\n"
            "## Requirements Catalog\n"
            + "\n".join(lines)
            + "\n\n```python\n"
            "def sample_code():\n"
            "    # Code snippet within prompt\n"
            "    return '✓ Verified UTF-8 🚀'\n"
            "```\n\n"
            "Please create calculator.py implementing these constraints."
        )
        assert len(huge_prompt) > 10000

        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(temp_workspace),
                    "prompt": huge_prompt,
                    "auto_approve": True,
                },
            )
            data = json.loads(res.content[0].text)
            result = TaskExecutionResult.model_validate(data)
            assert result.status == "success"
            assert "calculator.py" in result.modified_files
            assert temp_workspace.exists("calculator.py")
            # Verify input token metric calculated proportionally for large prompt
            assert result.tokens_used.input_tokens > 1000
            assert result.tokens_used.total_tokens > 1000

    async def test_execute_task_timeout_parameter_bounds(
        self, test_server: Any, client_factory: Any, temp_workspace: Any
    ) -> None:
        """Verifies schema validation on timeout_seconds bounds (min: 1, max: 3600)."""
        async with client_factory(test_server) as session:
            # Valid lower boundary (1 second)
            res_min = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(temp_workspace),
                    "prompt": "Test min bound",
                    "timeout_seconds": 1,
                },
            )
            assert json.loads(res_min.content[0].text)["status"] == "success"

            # Valid upper boundary (3600 seconds)
            res_max = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(temp_workspace),
                    "prompt": "Test max bound",
                    "timeout_seconds": 3600,
                },
            )
            assert json.loads(res_max.content[0].text)["status"] == "success"

            # Invalid lower boundary (< 1) returns isError=True over protocol
            res_invalid_low = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(temp_workspace),
                    "prompt": "Test invalid lower bound",
                    "timeout_seconds": 0,
                },
            )
            assert res_invalid_low.isError is True

            # Invalid upper boundary (> 3600) returns isError=True over protocol
            res_invalid_high = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(temp_workspace),
                    "prompt": "Test invalid upper bound",
                    "timeout_seconds": 3601,
                },
            )
            assert res_invalid_high.isError is True

        # Direct server call raises ToolError on schema violation
        with pytest.raises(ToolError):
            await test_server.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(temp_workspace),
                    "prompt": "Test invalid bounds",
                    "timeout_seconds": 0,
                },
            )

    async def test_execute_task_plan_mode_read_only_boundary(
        self, test_server: Any, client_factory: Any, clean_git_workspace: Any
    ) -> None:
        """Verifies that mode='plan' enforces a read-only boundary on the workspace filesystem."""
        initial_snapshot = clean_git_workspace.snapshot_hashes(include_git=True)

        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(clean_git_workspace),
                    "prompt": "Create calculator.py and implement authentication in src/auth.py",
                    "mode": "plan",
                },
            )
            data = json.loads(res.content[0].text)
            result = TaskExecutionResult.model_validate(data)
            assert result.status == "success"
            assert len(result.modified_files) == 0

        # Verify no files were created or modified on disk
        unchanged, desc = clean_git_workspace.verify_snapshot_unchanged(initial_snapshot, include_git=True)
        assert unchanged, f"Plan mode unexpectedly modified workspace: {desc}"


# ============================================================================
# 2. agy_chat Boundary & Corner Tests (>= 5 tests)
# ============================================================================


class TestAgyChatBoundaries:
    """Boundary and corner case test suite for `agy_chat`."""

    async def test_chat_empty_whitespace_prompt_rejected(
        self, test_server: Any, client_factory: Any
    ) -> None:
        """Verifies that empty and whitespace-only chat prompts return a structured error."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_chat",
                {"prompt": "    \t\r\n   "},
            )
            data = json.loads(res.content[0].text)
            result = ChatResult.model_validate(data)
            assert result.status == "error"
            assert result.error_details is not None
            assert "empty" in result.error_details.lower() or "prompt" in result.error_details.lower()

    async def test_chat_read_only_sha256_snapshot_invariant_dirty_repo(
        self, test_server: Any, client_factory: Any, dirty_git_workspace: Any
    ) -> None:
        """Verifies SHA-256 snapshot invariance across a dirty repository during analytical chat."""
        initial_snapshot = dirty_git_workspace.snapshot_hashes(include_git=True)

        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": (
                        "Analyze all modified, staged, unstaged, and untracked files in this repository. "
                        "Identify potential merge conflicts, syntax anomalies, and recommend architectural improvements."
                    ),
                    "workspace_path": str(dirty_git_workspace),
                },
            )
            data = json.loads(res.content[0].text)
            result = ChatResult.model_validate(data)
            assert result.status == "success"
            assert len(result.response) > 0

        # Invariant assertion: zero disk modifications
        unchanged, desc = dirty_git_workspace.verify_snapshot_unchanged(initial_snapshot, include_git=True)
        assert unchanged, f"Chat violated read-only invariant on dirty repo: {desc}"

    async def test_chat_multiturn_conversation_id_continuity(
        self,
        test_server: Any,
        client_factory: Any,
        clean_workspace: Any,
        mock_backend: ProgrammableMockAGYBackend,
    ) -> None:
        """Verifies multi-turn dialogue session continuity and conversation_id tracking."""
        async with client_factory(test_server) as session:
            # Turn 1: Start new conversation
            res1 = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Evaluate microservice decomposition for this repository.",
                    "workspace_path": str(clean_workspace),
                },
            )
            data1 = json.loads(res1.content[0].text)
            conv_id = data1["conversation_id"]
            assert conv_id != ""
            assert conv_id.startswith("mock-chat-")

            # Turn 2: Follow-up question referencing existing conversation_id
            res2 = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Which database engine would fit this architecture best?",
                    "workspace_path": str(clean_workspace),
                    "conversation_id": conv_id,
                },
            )
            data2 = json.loads(res2.content[0].text)
            assert data2["conversation_id"] == conv_id

            # Turn 3: Second follow-up
            res3 = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Provide a sample schema definition for the users table.",
                    "conversation_id": conv_id,
                },
            )
            data3 = json.loads(res3.content[0].text)
            assert data3["conversation_id"] == conv_id

        # Verify backend call history recorded all 3 turns with the identical conversation_id
        recorded = [c for c in mock_backend.chat_history if c["conversation_id"] == conv_id]
        assert len(recorded) == 3

    async def test_chat_timeout_simulation(
        self,
        test_server: Any,
        client_factory: Any,
        clean_workspace: Any,
        mock_backend: ProgrammableMockAGYBackend,
    ) -> None:
        """Verifies timeout handling during analytical consultation."""
        mock_backend.register_chat_handler(
            lambda **kw: ChatResult(
                status="timeout",
                conversation_id="chat-timeout-99",
                response="",
                error_details=f"Chat consultation timed out after {kw.get('timeout_seconds', 300)}s",
                backend_used="mock",
            )
        )
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Perform heavy full-repository static graph analysis",
                    "workspace_path": str(clean_workspace),
                    "timeout_seconds": 15,
                },
            )
            data = json.loads(res.content[0].text)
            result = ChatResult.model_validate(data)
            assert result.status == "timeout"
            assert result.error_details is not None
            assert "timed out" in result.error_details.lower()

    async def test_chat_special_characters_and_unicode(
        self, test_server: Any, client_factory: Any, clean_workspace: Any
    ) -> None:
        """Verifies handling of special characters, quotes, unicode, emojis, control chars, and injections."""
        special_prompt = (
            "🚀 Testing multilingual symbols: 日本語 / 中文 / 한국어 / العربية / עברית\n"
            "Escape sequences: '\"\\/ \b\f\n\r\t \u0000\u001b[31mRed\u001b[0m\n"
            "Injection patterns: <script>alert('xss');</script>; DROP TABLE users; --\n"
            "JSON string payload: {\"key\": \"value with \\\"escaped\\\" quotes and /slashes/\"}\n"
            "Mathematical notation: ∀x ∈ ℝ, ∃y : x + y = 0, ∑_{i=1}^n i = n(n+1)/2"
        )
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": special_prompt,
                    "workspace_path": str(clean_workspace),
                },
            )
            data = json.loads(res.content[0].text)
            result = ChatResult.model_validate(data)
            assert result.status == "success"
            assert len(result.response) > 0
            assert result.tokens_used.total_tokens > 0

    async def test_chat_timeout_parameter_bounds(
        self, test_server: Any, client_factory: Any
    ) -> None:
        """Verifies schema validation on agy_chat timeout_seconds bounds (min: 1, max: 1800)."""
        async with client_factory(test_server) as session:
            # Valid lower boundary (1s)
            res_min = await session.call_tool(
                "agy_chat",
                {"prompt": "Test lower bound", "timeout_seconds": 1},
            )
            assert json.loads(res_min.content[0].text)["status"] == "success"

            # Valid upper boundary (1800s)
            res_max = await session.call_tool(
                "agy_chat",
                {"prompt": "Test upper bound", "timeout_seconds": 1800},
            )
            assert json.loads(res_max.content[0].text)["status"] == "success"

            # Invalid lower boundary (< 1) returns isError=True over protocol
            res_invalid_low = await session.call_tool(
                "agy_chat",
                {"prompt": "Test invalid lower bound", "timeout_seconds": 0},
            )
            assert res_invalid_low.isError is True

            # Invalid upper boundary (> 1800) returns isError=True over protocol
            res_invalid_high = await session.call_tool(
                "agy_chat",
                {"prompt": "Test invalid upper bound", "timeout_seconds": 1801},
            )
            assert res_invalid_high.isError is True

        # Direct server call raises ToolError on schema violation
        with pytest.raises(ToolError):
            await test_server.call_tool(
                "agy_chat",
                {"prompt": "Test invalid bounds", "timeout_seconds": 0},
            )

    async def test_chat_without_workspace_path(
        self, test_server: Any, client_factory: Any
    ) -> None:
        """Verifies that agy_chat functions smoothly in general consultation mode (no workspace path)."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_chat",
                {"prompt": "Explain the difference between monoliths and event-driven microservices."},
            )
            data = json.loads(res.content[0].text)
            result = ChatResult.model_validate(data)
            assert result.status == "success"
            assert len(result.response) > 0


# ============================================================================
# 3. agy_get_diff Boundary & Corner Tests (>= 5 tests)
# ============================================================================


class TestAgyGetDiffBoundaries:
    """Boundary and corner case test suite for `agy_get_diff`."""

    async def test_get_diff_non_git_directory_returns_not_a_git_repo(
        self, test_server: Any, client_factory: Any, non_git_workspace: Any
    ) -> None:
        """Verifies that non-git directory returns status='not_a_git_repo' without throwing errors."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": str(non_git_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status == "not_a_git_repo"
            assert result.has_changes is False
            assert "not a git repository" in result.summary.lower()

    async def test_get_diff_unborn_git_branch(
        self, test_server: Any, client_factory: Any, unborn_git_workspace: Any
    ) -> None:
        """Verifies diff inspection on an unborn git branch with 0 commits."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": str(unborn_git_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status in ("success", "not_a_git_repo")
            assert isinstance(result.changed_files, list)
            assert isinstance(result.untracked_files, list)

    async def test_get_diff_dirty_workspace_combination(
        self, test_server: Any, client_factory: Any, dirty_git_workspace: Any
    ) -> None:
        """Verifies diff inspection on a dirty repository with staged, unstaged, MM, untracked, and deleted files."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": str(dirty_git_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status == "success"
            assert isinstance(result.changed_files, list)

    async def test_get_diff_binary_file_changes(
        self, test_server: Any, client_factory: Any, binary_git_workspace: Any
    ) -> None:
        """Verifies diff inspection handles modified and untracked binary files without UTF-8 decode errors."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": str(binary_git_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status == "success"

    async def test_get_diff_gitignore_excluded_files(
        self, test_server: Any, client_factory: Any, gitignore_workspace: Any
    ) -> None:
        """Verifies diff inspection on a repository with .gitignore rules filtering build logs and temp files."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": str(gitignore_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status == "success"

    async def test_get_diff_large_diff_workspace(
        self, test_server: Any, client_factory: Any, large_diff_workspace: Any
    ) -> None:
        """Verifies diff inspection on a repository containing 5,000 modified lines."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": str(large_diff_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status == "success"

    async def test_get_diff_nonexistent_workspace_path(
        self, test_server: Any, client_factory: Any, ephemeral_workspace: Any
    ) -> None:
        """Verifies diff inspection on non-existent directory returns status='error'."""
        nonexistent_path = str(ephemeral_workspace.path / "nonexistent_repo_diff_dir")
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": nonexistent_path},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status == "error"
            assert "not found" in (result.error_details or "").lower() or "not exist" in result.summary.lower()

    async def test_get_diff_empty_and_whitespace_path(
        self, test_server: Any, client_factory: Any
    ) -> None:
        """Verifies whitespace-only workspace_path returns structured error."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": "   \n\t  "},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status == "error"
            assert "empty" in (result.error_details or "").lower()

    async def test_get_diff_custom_diff_executor_integration(
        self, client_factory: Any, dirty_git_workspace: Any
    ) -> None:
        """Verifies custom diff_executor callback returning structured FileDiffStat and untracked files."""
        async def custom_diff_handler(workspace_path: str) -> DiffResult:
            return DiffResult(
                status="success",
                has_changes=True,
                unified_diff="--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,3 @@\n+def new_fn(): pass\n",
                changed_files=[
                    FileDiffStat(path="src/app.py", status="M", insertions=1, deletions=0),
                    FileDiffStat(path="src/new_staged.py", status="A", insertions=5, deletions=0),
                    FileDiffStat(path="src/to_delete.py", status="D", insertions=0, deletions=10),
                ],
                untracked_files=["src/brand_new_untracked.py"],
                summary="3 modified files, 1 untracked file",
            )

        server_with_diff = create_mcp_server(diff_executor=custom_diff_handler)
        async with client_factory(server_with_diff) as session:
            res = await session.call_tool(
                "agy_get_diff",
                {"workspace_path": str(dirty_git_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = DiffResult.model_validate(data)
            assert result.status == "success"
            assert result.has_changes is True
            assert len(result.changed_files) == 3
            assert result.changed_files[0].path == "src/app.py"
            assert result.changed_files[0].status == "M"
            assert result.changed_files[1].status == "A"
            assert result.changed_files[2].status == "D"
            assert result.untracked_files == ["src/brand_new_untracked.py"]


# ============================================================================
# 4. agy_run_tests Boundary & Corner Tests (>= 5 tests)
# ============================================================================


class TestAgyRunTestsBoundaries:
    """Boundary and corner case test suite for `agy_run_tests`."""

    async def test_run_tests_no_test_framework_detected(
        self, test_server: Any, client_factory: Any, no_framework_workspace: Any
    ) -> None:
        """Verifies behavior when workspace has no recognized test framework configs."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_run_tests",
                {"workspace_path": str(no_framework_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = TestRunResult.model_validate(data)
            assert result.status in ("passed", "no_framework_detected")
            assert isinstance(result.summary, TestSummary)

    async def test_run_tests_hanging_test_suite_timeout(
        self, client_factory: Any, hanging_test_workspace: Any
    ) -> None:
        """Verifies timeout handling when a test suite hangs indefinitely."""
        async def timeout_test_runner(workspace_path: str, test_command: str, timeout_seconds: int) -> TestRunResult:
            return TestRunResult(
                status="timeout",
                exit_code=-9,
                framework="pytest",
                test_command_executed=test_command or "pytest",
                output="Test suite execution exceeded timeout limit (1s). Process tree forcefully killed.",
                summary=TestSummary(errors=1),
                failures=[],
                duration_seconds=float(timeout_seconds),
                error_details=f"Test execution timed out after {timeout_seconds}s; killed process tree.",
            )

        server_with_runner = create_mcp_server(test_executor=timeout_test_runner)
        async with client_factory(server_with_runner) as session:
            res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(hanging_test_workspace),
                    "timeout_seconds": 1,
                },
            )
            data = json.loads(res.content[0].text)
            result = TestRunResult.model_validate(data)
            assert result.status == "timeout"
            assert result.exit_code == -9
            assert result.summary.errors == 1
            assert result.error_details is not None
            assert "timed out" in result.error_details.lower()

    async def test_run_tests_invalid_custom_test_command_exit_code(
        self, client_factory: Any, clean_workspace: Any
    ) -> None:
        """Verifies non-zero exit code reporting from a custom test command."""
        async def failing_test_runner(workspace_path: str, test_command: str, timeout_seconds: int) -> TestRunResult:
            return TestRunResult(
                status="failed",
                exit_code=42,
                framework="custom",
                test_command_executed=test_command,
                output="Custom test runner failed with exit code 42: assertion failure.",
                summary=TestSummary(total=1, passed=0, failed=1, skipped=0, errors=0),
                failures=[
                    TestFailure(
                        test_id="custom::assertion_error",
                        message="AssertionError: value mismatch",
                        location="run_custom_tests.py:15",
                    )
                ],
                duration_seconds=0.05,
                error_details="1 test failed",
            )

        server_failing = create_mcp_server(test_executor=failing_test_runner)
        async with client_factory(server_failing) as session:
            res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(clean_workspace),
                    "test_command": "python run_custom_tests.py --fail-fast",
                },
            )
            data = json.loads(res.content[0].text)
            result = TestRunResult.model_validate(data)
            assert result.status == "failed"
            assert result.exit_code == 42
            assert result.summary.failed == 1
            assert len(result.failures) == 1
            assert result.failures[0].test_id == "custom::assertion_error"

    async def test_run_tests_missing_executable_command(
        self, client_factory: Any, clean_workspace: Any
    ) -> None:
        """Verifies error reporting when the specified test command binary does not exist on PATH."""
        async def missing_bin_runner(workspace_path: str, test_command: str, timeout_seconds: int) -> TestRunResult:
            cmd_name = test_command.split()[0] if test_command else "test_bin"
            return TestRunResult(
                status="error",
                exit_code=127,
                framework="custom",
                test_command_executed=test_command,
                output="",
                summary=TestSummary(errors=1),
                failures=[],
                duration_seconds=0.01,
                error_details=f"Executable not found on PATH: '{cmd_name}'",
            )

        server_missing = create_mcp_server(test_executor=missing_bin_runner)
        async with client_factory(server_missing) as session:
            res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(clean_workspace),
                    "test_command": "nonexistent_test_tool_xyz_9999 --run-all",
                },
            )
            data = json.loads(res.content[0].text)
            result = TestRunResult.model_validate(data)
            assert result.status == "error"
            assert result.exit_code == 127
            assert result.error_details is not None
            assert "executable not found" in result.error_details.lower()

    async def test_run_tests_mixed_passing_and_failing_tests(
        self, client_factory: Any, pytest_mixed_workspace: Any
    ) -> None:
        """Verifies structured summary parsing for mixed test outcomes (passed, failed, skipped)."""
        async def mixed_test_runner(workspace_path: str, test_command: str, timeout_seconds: int) -> TestRunResult:
            return TestRunResult(
                status="failed",
                exit_code=1,
                framework="pytest",
                test_command_executed=test_command or "pytest tests/test_mixed.py",
                output="== 2 passed, 1 failed, 1 skipped in 0.12s ==",
                summary=TestSummary(total=4, passed=2, failed=1, skipped=1, errors=0),
                failures=[
                    TestFailure(
                        test_id="tests/test_mixed.py::test_fail_1",
                        message="assert 1 == 2",
                        location="tests/test_mixed.py:12",
                        traceback="def test_fail_1():\n>   assert 1 == 2\nE   assert 1 == 2",
                    )
                ],
                duration_seconds=0.12,
                error_details="1 test failed",
            )

        server_mixed = create_mcp_server(test_executor=mixed_test_runner)
        async with client_factory(server_mixed) as session:
            res = await session.call_tool(
                "agy_run_tests",
                {"workspace_path": str(pytest_mixed_workspace)},
            )
            data = json.loads(res.content[0].text)
            result = TestRunResult.model_validate(data)
            assert result.status == "failed"
            assert result.summary.total == 4
            assert result.summary.passed == 2
            assert result.summary.failed == 1
            assert result.summary.skipped == 1
            assert result.summary.errors == 0
            assert len(result.failures) == 1
            assert result.failures[0].test_id == "tests/test_mixed.py::test_fail_1"
            assert "assert 1 == 2" in result.failures[0].message

    async def test_run_tests_nonexistent_workspace_path(
        self, test_server: Any, client_factory: Any, ephemeral_workspace: Any
    ) -> None:
        """Verifies running tests on non-existent directory returns status='error'."""
        nonexistent_path = str(ephemeral_workspace.path / "nonexistent_tests_runner_dir")
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_run_tests",
                {"workspace_path": nonexistent_path},
            )
            data = json.loads(res.content[0].text)
            result = TestRunResult.model_validate(data)
            assert result.status == "error"
            assert "not found" in (result.error_details or "").lower() or "does not exist" in (result.error_details or "").lower()

    async def test_run_tests_empty_and_whitespace_path(
        self, test_server: Any, client_factory: Any
    ) -> None:
        """Verifies whitespace-only workspace_path returns structured error."""
        async with client_factory(test_server) as session:
            res = await session.call_tool(
                "agy_run_tests",
                {"workspace_path": "    \t\n  "},
            )
            data = json.loads(res.content[0].text)
            result = TestRunResult.model_validate(data)
            assert result.status == "error"
            assert "empty" in (result.error_details or "").lower()

    async def test_run_tests_timeout_parameter_bounds(
        self, test_server: Any, client_factory: Any, clean_workspace: Any
    ) -> None:
        """Verifies schema validation on agy_run_tests timeout_seconds bounds (min: 1, max: 1800)."""
        async with client_factory(test_server) as session:
            # Valid lower boundary (1s)
            res_min = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(clean_workspace),
                    "timeout_seconds": 1,
                },
            )
            assert json.loads(res_min.content[0].text)["status"] in ("passed", "no_framework_detected")

            # Valid upper boundary (1800s)
            res_max = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(clean_workspace),
                    "timeout_seconds": 1800,
                },
            )
            assert json.loads(res_max.content[0].text)["status"] in ("passed", "no_framework_detected")

            # Invalid lower boundary (< 1) returns isError=True over protocol
            res_invalid_low = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(clean_workspace),
                    "timeout_seconds": 0,
                },
            )
            assert res_invalid_low.isError is True

            # Invalid upper boundary (> 1800) returns isError=True over protocol
            res_invalid_high = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(clean_workspace),
                    "timeout_seconds": 1801,
                },
            )
            assert res_invalid_high.isError is True

        # Direct server call raises ToolError on schema violation
        with pytest.raises(ToolError):
            await test_server.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(clean_workspace),
                    "timeout_seconds": 0,
                },
            )
