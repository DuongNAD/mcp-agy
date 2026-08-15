"""Tier 1: Feature Coverage Test Suite for FastMCP AGY Server.

Validates the 4 primary core MCP tools across requirement-driven happy-path scenarios:
- Tool 1: agy_execute_task (>= 5 tests: basic task execution, auto_approve toggle, file creation tracking, token usage parsing, backend telemetry).
- Tool 2: agy_chat (>= 5 tests: analytical query, model insights response, token telemetry, read-only filesystem check, conversation continuity).
- Tool 3: agy_get_diff (>= 5 tests: clean repo has_changes=False, unstaged modifications, staged additions, untracked files detection, file diff stat breakdown).
- Tool 4: agy_run_tests (>= 5 tests: pytest passing suite, pytest failing suite with failure diagnostics, python unittest framework detection, npm test passing, custom test command execution).

Uses ClientSession over in-memory AnyIO stream transport via client_factory and isolated EphemeralWorkspace fixtures.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

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


# ============================================================================
# Genuine Executors for Tier 1 Diff Inspection and Test Runner
# ============================================================================

async def genuine_diff_executor(workspace_path: str) -> DiffResult:
    """Inspects real Git repository state and generates structured diff metrics."""
    if not os.path.exists(workspace_path):
        return DiffResult(
            status="error",
            has_changes=False,
            summary="Workspace directory does not exist.",
            error_details=f"Directory not found: {workspace_path}",
        )

    git_dir = os.path.join(workspace_path, ".git")
    if not os.path.exists(git_dir):
        return DiffResult(
            status="not_a_git_repo",
            has_changes=False,
            summary="Directory is not a git repository.",
            error_details=None,
        )

    git_cmd = shutil.which("git") or "git"

    # git status --porcelain=v1
    p_status = subprocess.run(
        [git_cmd, "-C", workspace_path, "status", "--porcelain=v1", "-uall"],
        capture_output=True,
        text=True,
        check=False,
    )
    if p_status.returncode != 0:
        return DiffResult(
            status="error",
            has_changes=False,
            summary="Failed to run git status",
            error_details=p_status.stderr,
        )

    lines = p_status.stdout.splitlines()
    changed_files: List[FileDiffStat] = []
    untracked_files: List[str] = []

    # Get numstats for unstaged and staged
    p_numstat = subprocess.run(
        [git_cmd, "-C", workspace_path, "diff", "--numstat"],
        capture_output=True,
        text=True,
        check=False,
    )
    p_cached_numstat = subprocess.run(
        [git_cmd, "-C", workspace_path, "diff", "--cached", "--numstat"],
        capture_output=True,
        text=True,
        check=False,
    )

    stats_map: Dict[str, Tuple[int, int]] = {}
    for out in [p_numstat.stdout, p_cached_numstat.stdout]:
        for s_line in out.splitlines():
            parts = s_line.strip().split("\t")
            if len(parts) == 3:
                ins = int(parts[0]) if parts[0].isdigit() else 0
                dels = int(parts[1]) if parts[1].isdigit() else 0
                fpath = parts[2].replace("\\", "/")
                prev_ins, prev_dels = stats_map.get(fpath, (0, 0))
                stats_map[fpath] = (prev_ins + ins, prev_dels + dels)

    for s_line in lines:
        if len(s_line) < 3:
            continue
        code = s_line[:2]
        fpath = s_line[3:].strip().replace("\\", "/").strip('"')
        if code == "??":
            untracked_files.append(fpath)
        else:
            ins, dels = stats_map.get(fpath, (0, 0))
            changed_files.append(
                FileDiffStat(
                    path=fpath,
                    status=code.strip(),
                    insertions=ins,
                    deletions=dels,
                )
            )

    # Combined unified diff
    p_diff = subprocess.run(
        [git_cmd, "-C", workspace_path, "diff", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if p_diff.returncode != 0:
        p_diff_cached = subprocess.run(
            [git_cmd, "-C", workspace_path, "diff", "--cached"],
            capture_output=True,
            text=True,
            check=False,
        )
        p_diff_unstaged = subprocess.run(
            [git_cmd, "-C", workspace_path, "diff"],
            capture_output=True,
            text=True,
            check=False,
        )
        unified_diff = (p_diff_cached.stdout + "\n" + p_diff_unstaged.stdout).strip()
    else:
        unified_diff = p_diff.stdout.strip()

    has_changes = bool(changed_files or untracked_files)
    summary = (
        f"{len(changed_files)} changed file(s), {len(untracked_files)} untracked file(s)"
        if has_changes
        else "No uncommitted changes detected in repository."
    )

    return DiffResult(
        status="success",
        has_changes=has_changes,
        unified_diff=unified_diff,
        changed_files=changed_files,
        untracked_files=untracked_files,
        summary=summary,
        error_details=None,
    )


async def genuine_test_executor(
    workspace_path: str,
    test_command: str = "",
    timeout_seconds: int = 300,
) -> TestRunResult:
    """Executes test suites with auto-detection for pytest, unittest, npm, and custom runners."""
    if not os.path.exists(workspace_path):
        return TestRunResult(
            status="error",
            exit_code=1,
            framework="custom" if test_command else "none",
            test_command_executed=test_command,
            output="",
            summary=TestSummary(errors=1),
            error_details=f"Workspace directory not found: {workspace_path}",
        )

    start_time = time.monotonic()

    if test_command:
        framework = "custom"
        cmd = test_command
    elif os.path.exists(os.path.join(workspace_path, "tests", "test_suite.py")):
        framework = "unittest"
        cmd = f'"{sys.executable}" -m unittest discover -s tests'
    elif (
        os.path.exists(os.path.join(workspace_path, "pytest.ini"))
        or os.path.exists(os.path.join(workspace_path, "pyproject.toml"))
        or (
            os.path.exists(os.path.join(workspace_path, "tests"))
            and any(f.startswith("test_") for f in os.listdir(os.path.join(workspace_path, "tests")))
        )
    ):
        framework = "pytest"
        cmd = f'"{sys.executable}" -m pytest'
    elif os.path.exists(os.path.join(workspace_path, "package.json")):
        framework = "npm"
        with open(os.path.join(workspace_path, "package.json"), "r", encoding="utf-8") as f:
            pkg = json.load(f)
            cmd = pkg.get("scripts", {}).get("test", "node test.js")
    elif os.path.exists(os.path.join(workspace_path, "Cargo.toml")):
        framework = "cargo"
        cmd = "cargo test"
    else:
        return TestRunResult(
            status="no_framework_detected",
            exit_code=0,
            framework="none",
            test_command_executed="",
            output="No recognized test framework configuration found.",
            summary=TestSummary(),
            failures=[],
            duration_seconds=0.0,
            error_details=None,
        )

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        duration = time.monotonic() - start_time
        out = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
        exit_code = proc.returncode

        passed = 0
        failed = 0
        skipped = 0
        errors = 0
        failures: List[TestFailure] = []

        if framework == "pytest":
            pass_m = re.search(r"(\d+)\s+passed", out)
            fail_m = re.search(r"(\d+)\s+failed", out)
            skip_m = re.search(r"(\d+)\s+skipped", out)
            err_m = re.search(r"(\d+)\s+error", out)
            if pass_m:
                passed = int(pass_m.group(1))
            if fail_m:
                failed = int(fail_m.group(1))
            if skip_m:
                skipped = int(skip_m.group(1))
            if err_m:
                errors = int(err_m.group(1))

            for m in re.finditer(r"FAILED\s+([^\s:]+::\w+)(?:\s+-\s+(.*))?", out):
                test_id = m.group(1)
                msg = m.group(2) or "Test failed"
                failures.append(TestFailure(test_id=test_id, message=msg, location=test_id.split("::")[0]))

        elif framework == "unittest":
            ran_m = re.search(r"Ran\s+(\d+)\s+test", out)
            total_ran = int(ran_m.group(1)) if ran_m else (1 if exit_code == 0 else 0)
            if exit_code == 0:
                passed = total_ran
            else:
                fail_m = re.search(r"failures=(\d+)", out)
                err_m = re.search(r"errors=(\d+)", out)
                failed = int(fail_m.group(1)) if fail_m else 1
                errors = int(err_m.group(1)) if err_m else 0
                passed = max(0, total_ran - failed - errors)
                failures.append(TestFailure(test_id="unittest_suite", message="Unittest execution failed"))

        elif framework == "npm":
            if exit_code == 0:
                passed = 1
            else:
                failed = 1
                failures.append(TestFailure(test_id="npm_test", message="NPM test script failed"))

        elif framework == "custom":
            if exit_code == 0:
                passed = 1
            else:
                failed = 1
                failures.append(TestFailure(test_id="custom_test", message="Custom command failed"))

        total = passed + failed + skipped + errors
        if total == 0:
            total = 1 if exit_code == 0 else 0
            if exit_code == 0:
                passed = 1
            else:
                failed = 1

        status = "passed" if exit_code == 0 else "failed"

        return TestRunResult(
            status=status,
            exit_code=exit_code,
            framework=framework,
            test_command_executed=cmd,
            output=out.strip(),
            summary=TestSummary(
                total=total,
                passed=passed,
                failed=failed,
                skipped=skipped,
                errors=errors,
            ),
            failures=failures,
            duration_seconds=round(duration, 4),
            error_details=None if exit_code == 0 else f"Test run exited with code {exit_code}",
        )

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start_time
        return TestRunResult(
            status="timeout",
            exit_code=-1,
            framework=framework,
            test_command_executed=cmd,
            output="Execution timed out.",
            summary=TestSummary(errors=1),
            failures=[TestFailure(test_id="timeout", message=f"Timed out after {timeout_seconds}s")],
            duration_seconds=round(duration, 4),
            error_details=f"Test execution timed out after {timeout_seconds}s",
        )


@pytest.fixture
def tier1_server(mock_backend: ProgrammableMockAGYBackend) -> Any:
    """Provides a FastMCP server configured with genuine diff and test executors for Tier 1."""
    return create_mcp_server(
        backend=mock_backend,
        diff_executor=genuine_diff_executor,
        test_executor=genuine_test_executor,
    )


# ============================================================================
# Tool 1: agy_execute_task Tests (>= 5 tests)
# ============================================================================

@pytest.mark.tier1
@pytest.mark.anyio
class TestExecuteTaskFeatureCoverage:
    """Feature coverage test cases for agy_execute_task."""

    async def test_execute_task_basic_success(self, tier1_server, client_factory, git_workspace):
        """Validates basic successful coding task execution and response schema."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(git_workspace),
                    "prompt": "Implement calculation helper functions",
                    "auto_approve": True,
                },
            )
            assert call_res is not None
            assert not call_res.isError
            data = json.loads(call_res.content[0].text)
            result = TaskExecutionResult.model_validate(data)

            assert result.status == "success"
            assert result.backend_used == "mock"
            assert result.duration_seconds > 0
            assert "calculation helper functions" in result.response
            assert result.error_details is None

    async def test_execute_task_auto_approve_toggle(self, tier1_server, client_factory, git_workspace, mock_backend):
        """Validates auto_approve boolean flag toggling and backend parameter reception."""
        async with client_factory(tier1_server) as session:
            # Test auto_approve=True
            res_true = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(git_workspace),
                    "prompt": "Task with auto_approve true",
                    "auto_approve": True,
                },
            )
            data_true = json.loads(res_true.content[0].text)
            assert data_true["status"] == "success"
            assert mock_backend.call_history[-1]["auto_approve"] is True

            # Test auto_approve=False
            res_false = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(git_workspace),
                    "prompt": "Task with auto_approve false",
                    "auto_approve": False,
                },
            )
            data_false = json.loads(res_false.content[0].text)
            assert data_false["status"] == "success"
            assert mock_backend.call_history[-1]["auto_approve"] is False

    async def test_execute_task_file_creation_tracking(self, tier1_server, client_factory, git_workspace):
        """Validates file synthesis on disk and modified_files tracking list."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(git_workspace),
                    "prompt": "Create calculator.py with add and subtract functions",
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TaskExecutionResult.model_validate(data)

            assert result.status == "success"
            assert "calculator.py" in result.modified_files
            assert "1 file" in result.diff_summary or "calculator.py" in result.diff_summary
            assert git_workspace.exists("calculator.py")
            content = git_workspace.read_file("calculator.py")
            assert "def add" in content
            assert "def subtract" in content

    async def test_execute_task_token_usage_parsing_and_override(self, tier1_server, client_factory, git_workspace, mock_backend):
        """Validates structured TokenUsage parsing and telemetry override handling."""
        mock_backend.token_usage_override = TokenUsage(
            input_tokens=220,
            output_tokens=110,
            thinking_tokens=35,
            cache_read_tokens=15,
            total_tokens=380,
        )

        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(git_workspace),
                    "prompt": "Implement math functions with telemetry",
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TaskExecutionResult.model_validate(data)

            assert result.tokens_used.input_tokens == 220
            assert result.tokens_used.output_tokens == 110
            assert result.tokens_used.thinking_tokens == 35
            assert result.tokens_used.cache_read_tokens == 15
            assert result.tokens_used.total_tokens == 380

    async def test_execute_task_backend_telemetry_and_metadata(self, tier1_server, client_factory, git_workspace):
        """Validates conversation_id formatting, duration measurement, and metadata fields."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(git_workspace),
                    "prompt": "Perform architecture refactoring in src/app.py",
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TaskExecutionResult.model_validate(data)

            assert result.status == "success"
            assert re.match(r"^mock-task-[a-f0-9]+$", result.conversation_id)
            assert result.duration_seconds > 0.0
            assert result.backend_used == "mock"
            assert result.error_details is None

    async def test_execute_task_mode_plan_read_only(self, tier1_server, client_factory, clean_git_workspace):
        """Validates that mode='plan' generates analysis without creating files on disk."""
        initial_snapshot = clean_git_workspace.snapshot_hashes()

        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(clean_git_workspace),
                    "prompt": "Plan implementation of calculator.py",
                    "mode": "plan",
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TaskExecutionResult.model_validate(data)

            assert result.status == "success"
            assert not clean_git_workspace.exists("calculator.py")
            unchanged, desc = clean_git_workspace.verify_snapshot_unchanged(initial_snapshot)
            assert unchanged, f"Plan mode unexpectedly modified workspace: {desc}"

    async def test_execute_task_custom_handler_injection(self, tier1_server, client_factory, git_workspace, mock_backend):
        """Validates custom callback handler injection for execute_task."""
        def custom_handler(**kwargs):
            return TaskExecutionResult(
                status="success",
                conversation_id="custom-conv-handler-99",
                response="Custom task execution completed perfectly.",
                modified_files=["custom_output.py"],
                backend_used="custom-mock",
            )

        mock_backend.register_task_handler(custom_handler)

        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_execute_task",
                {
                    "workspace_path": str(git_workspace),
                    "prompt": "Trigger custom handler",
                },
            )
            data = json.loads(call_res.content[0].text)
            assert data["conversation_id"] == "custom-conv-handler-99"
            assert data["response"] == "Custom task execution completed perfectly."
            assert data["backend_used"] == "custom-mock"


# ============================================================================
# Tool 2: agy_chat Tests (>= 5 tests)
# ============================================================================

@pytest.mark.tier1
@pytest.mark.anyio
class TestChatFeatureCoverage:
    """Feature coverage test cases for agy_chat."""

    async def test_chat_analytical_query_success(self, tier1_server, client_factory):
        """Validates analytical / architectural question submission and ChatResult structure."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "What is the optimal concurrency model for FastMCP AGY server?",
                },
            )
            assert call_res is not None
            assert not call_res.isError
            data = json.loads(call_res.content[0].text)
            result = ChatResult.model_validate(data)

            assert result.status == "success"
            assert result.backend_used == "mock"
            assert result.duration_seconds > 0
            assert "optimal concurrency model" in result.response or "FastMCP" in result.response
            assert result.error_details is None

    async def test_chat_model_insights_with_workspace_context(self, tier1_server, client_factory, clean_git_workspace, mock_backend):
        """Validates passing workspace_path for codebase contextual analysis."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Review architecture and design in src/app.py",
                    "workspace_path": str(clean_git_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = ChatResult.model_validate(data)

            assert result.status == "success"
            assert mock_backend.chat_history[-1]["workspace_path"] == str(clean_git_workspace.path)
            assert "src/app.py" in result.response

    async def test_chat_token_telemetry_scaling_and_override(self, tier1_server, client_factory, mock_backend):
        """Validates token usage telemetry in chat responses."""
        mock_backend.token_usage_override = TokenUsage(
            input_tokens=75,
            output_tokens=45,
            thinking_tokens=15,
            cache_read_tokens=0,
            total_tokens=135,
        )

        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Evaluate pros and cons of microservices vs monolith.",
                },
            )
            data = json.loads(call_res.content[0].text)
            result = ChatResult.model_validate(data)

            assert result.tokens_used.input_tokens == 75
            assert result.tokens_used.output_tokens == 45
            assert result.tokens_used.thinking_tokens == 15
            assert result.tokens_used.total_tokens == 135

    async def test_chat_read_only_filesystem_invariant(self, tier1_server, client_factory, clean_git_workspace):
        """Validates strict read-only guarantee of agy_chat using SHA-256 snapshots."""
        initial_snapshot = clean_git_workspace.snapshot_hashes()

        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Please delete all files and write a new main.py",
                    "workspace_path": str(clean_git_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            assert data["status"] == "success"

        unchanged, desc = clean_git_workspace.verify_snapshot_unchanged(initial_snapshot)
        assert unchanged, f"Chat violated read-only invariant: {desc}"

    async def test_chat_conversation_continuity_multiturn(self, tier1_server, client_factory, mock_backend):
        """Validates multi-turn conversation session continuity across successive calls."""
        async with client_factory(tier1_server) as session:
            # Turn 1: initial query
            res1 = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Turn 1: Propose database schema design.",
                },
            )
            data1 = json.loads(res1.content[0].text)
            conv_id = data1["conversation_id"]
            assert conv_id.startswith("mock-chat-")

            # Turn 2: followup referencing conversation_id
            res2 = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Turn 2: Add migration script for proposed schema.",
                    "conversation_id": conv_id,
                },
            )
            data2 = json.loads(res2.content[0].text)
            assert data2["conversation_id"] == conv_id

            assert len(mock_backend.chat_history) == 2
            assert mock_backend.chat_history[0]["conversation_id"] == conv_id
            assert mock_backend.chat_history[1]["conversation_id"] == conv_id

    async def test_chat_general_consultation_without_workspace(self, tier1_server, client_factory):
        """Validates general consultation mode when workspace_path is omitted."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "How does python async AnyIO memory object streams work?",
                },
            )
            data = json.loads(call_res.content[0].text)
            result = ChatResult.model_validate(data)

            assert result.status == "success"
            assert result.conversation_id != ""
            assert "AnyIO" in result.response

    async def test_chat_custom_chat_handler(self, tier1_server, client_factory, mock_backend):
        """Validates custom callback handler injection for chat."""
        def custom_handler(**kwargs):
            return ChatResult(
                status="success",
                conversation_id="custom-chat-session-42",
                response="Custom analytical response injected by test.",
                backend_used="custom-chat-mock",
            )

        mock_backend.register_chat_handler(custom_handler)

        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_chat",
                {
                    "prompt": "Trigger custom chat",
                },
            )
            data = json.loads(call_res.content[0].text)
            assert data["conversation_id"] == "custom-chat-session-42"
            assert data["response"] == "Custom analytical response injected by test."
            assert data["backend_used"] == "custom-chat-mock"


# ============================================================================
# Tool 3: agy_get_diff Tests (>= 5 tests)
# ============================================================================

@pytest.mark.tier1
@pytest.mark.anyio
class TestGetDiffFeatureCoverage:
    """Feature coverage test cases for agy_get_diff."""

    async def test_get_diff_clean_repo_has_changes_false(self, tier1_server, client_factory, clean_git_workspace):
        """Validates that a pristine git repo returns has_changes=False with empty diff collections."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_get_diff",
                {
                    "workspace_path": str(clean_git_workspace),
                },
            )
            assert call_res is not None
            assert not call_res.isError
            data = json.loads(call_res.content[0].text)
            result = DiffResult.model_validate(data)

            assert result.status == "success"
            assert result.has_changes is False
            assert result.changed_files == []
            assert result.untracked_files == []
            assert result.unified_diff == ""
            assert "No uncommitted changes" in result.summary

    async def test_get_diff_unstaged_modifications(self, tier1_server, client_factory, unstaged_changes_workspace):
        """Validates detection of unstaged modifications in tracked files."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_get_diff",
                {
                    "workspace_path": str(unstaged_changes_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = DiffResult.model_validate(data)

            assert result.status == "success"
            assert result.has_changes is True
            paths = {f.path for f in result.changed_files}
            assert "src/app.py" in paths
            assert "unstaged_edit" in result.unified_diff

    async def test_get_diff_staged_additions(self, tier1_server, client_factory, staged_changes_workspace):
        """Validates detection of staged additions and staged modifications."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_get_diff",
                {
                    "workspace_path": str(staged_changes_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = DiffResult.model_validate(data)

            assert result.status == "success"
            assert result.has_changes is True
            paths = {f.path for f in result.changed_files}
            assert "src/app.py" in paths
            assert "src/utils.py" in paths
            assert "helper" in result.unified_diff

    async def test_get_diff_untracked_files_detection(self, tier1_server, client_factory, untracked_files_workspace):
        """Validates detection of new untracked files and subdirectories."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_get_diff",
                {
                    "workspace_path": str(untracked_files_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = DiffResult.model_validate(data)

            assert result.status == "success"
            assert result.has_changes is True
            assert "new_feature.py" in result.untracked_files
            assert "docs/guide.md" in result.untracked_files

    async def test_get_diff_file_diff_stat_breakdown(self, tier1_server, client_factory, dirty_git_workspace):
        """Validates structured FileDiffStat breakdown (paths, statuses, insertions, deletions)."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_get_diff",
                {
                    "workspace_path": str(dirty_git_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = DiffResult.model_validate(data)

            assert result.status == "success"
            assert result.has_changes is True
            assert len(result.changed_files) >= 2
            for stat in result.changed_files:
                assert isinstance(stat.path, str)
                assert isinstance(stat.status, str)
                assert isinstance(stat.insertions, int)
                assert isinstance(stat.deletions, int)

    async def test_get_diff_non_git_repo(self, tier1_server, client_factory, non_git_workspace):
        """Validates that a non-git directory returns status='not_a_git_repo' and has_changes=False."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_get_diff",
                {
                    "workspace_path": str(non_git_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = DiffResult.model_validate(data)

            assert result.status == "not_a_git_repo"
            assert result.has_changes is False


# ============================================================================
# Tool 4: agy_run_tests Tests (>= 5 tests)
# ============================================================================

@pytest.mark.tier1
@pytest.mark.anyio
class TestRunTestsFeatureCoverage:
    """Feature coverage test cases for agy_run_tests."""

    async def test_run_tests_pytest_passing_suite(self, tier1_server, client_factory, pytest_passing_workspace):
        """Validates pytest auto-detection and execution for a passing test suite."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(pytest_passing_workspace),
                },
            )
            assert call_res is not None
            assert not call_res.isError
            data = json.loads(call_res.content[0].text)
            result = TestRunResult.model_validate(data)

            assert result.status == "passed"
            assert result.exit_code == 0
            assert result.framework == "pytest"
            assert result.summary.passed == 2
            assert result.summary.failed == 0
            assert result.summary.total == 2
            assert result.failures == []

    async def test_run_tests_pytest_failing_suite_with_failure_diagnostics(self, tier1_server, client_factory, pytest_failing_workspace):
        """Validates pytest failure parsing, non-zero exit code, and TestFailure diagnostics."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(pytest_failing_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TestRunResult.model_validate(data)

            assert result.status == "failed"
            assert result.exit_code != 0
            assert result.framework == "pytest"
            assert result.summary.failed >= 1
            assert len(result.failures) >= 1
            assert any("test_division_by_zero" in f.test_id for f in result.failures)

    async def test_run_tests_python_unittest_framework(self, tier1_server, client_factory, unittest_workspace):
        """Validates python standard library unittest discovery and execution."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(unittest_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TestRunResult.model_validate(data)

            assert result.framework == "unittest"
            assert result.summary.total >= 1

    async def test_run_tests_npm_test_passing(self, tier1_server, client_factory, npm_passing_workspace):
        """Validates npm / package.json test script execution."""
        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(npm_passing_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TestRunResult.model_validate(data)

            assert result.status == "passed"
            assert result.exit_code == 0
            assert result.framework == "npm"
            assert result.summary.passed >= 1

    async def test_run_tests_custom_test_command_execution(self, tier1_server, client_factory, custom_script_workspace):
        """Validates explicit custom test command string execution."""
        custom_cmd = f'"{sys.executable}" run_custom_tests.py'

        async with client_factory(tier1_server) as session:
            call_res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(custom_script_workspace),
                    "test_command": custom_cmd,
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TestRunResult.model_validate(data)

            assert result.status == "passed"
            assert result.exit_code == 0
            assert result.framework == "custom"
            assert result.test_command_executed == custom_cmd
            assert "Running custom test suite" in result.output

    async def test_run_tests_default_server_baseline(self, test_server, client_factory, clean_workspace):
        """Validates default server baseline test execution on standard workspace."""
        async with client_factory(test_server) as session:
            call_res = await session.call_tool(
                "agy_run_tests",
                {
                    "workspace_path": str(clean_workspace),
                },
            )
            data = json.loads(call_res.content[0].text)
            result = TestRunResult.model_validate(data)

            assert result.status in ("passed", "no_framework_detected")
            assert result.exit_code == 0
