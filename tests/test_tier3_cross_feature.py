"""Tier 3: Cross-Feature Combinations and Multi-Tool Integration Test Suite.

Verifies pairwise and multi-tool workflows across FastMCP AGY:
- Scenario 1: `agy_execute_task` (code creation) -> `agy_get_diff` (unified diff & changed files).
- Scenario 2: `agy_execute_task` (bug fixing) -> `agy_run_tests` (verifying test runner passes).
- Scenario 3: `agy_chat` (planning consultation) -> `agy_execute_task` (implementation) -> `agy_get_diff` (diff verification).
- Scenario 4: Closed-loop self-remediation: `agy_execute_task` -> `agy_run_tests` (fail) -> `agy_chat` (diagnose) -> `agy_execute_task` (fix) -> `agy_run_tests` (pass).
- Scenario 5: Multi-turn chat session preserving conversation_id while running diff in parallel.
- Scenario 6: Task execution modifying existing tracked file -> git diff shows modified status 'M' with correct insertions/deletions.
- Scenario 7: Staged and unstaged mixed git diff inspection following task execution.
- Scenario 8: Full 4-tool chain (`agy_chat` -> `agy_execute_task` -> `agy_get_diff` -> `agy_run_tests`) in a single session.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

import pytest
from mcp.client.session import ClientSession

from mcp_agy.core.backend import AGYBackend, get_backend, reset_backend, set_backend
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
from mcp_agy.server import create_mcp_server, create_server
from tests.conftest import EphemeralWorkspace, ProgrammableMockAGYBackend


# ============================================================================
# Git Diff & Test Runner Execution Helpers
# ============================================================================

async def real_git_diff_executor(workspace_path: str) -> DiffResult:
    """Authentic git diff inspection executor for E2E integration tests."""
    ws_path = Path(workspace_path).resolve()
    if not ws_path.exists():
        return DiffResult(
            status="error",
            has_changes=False,
            summary="Workspace directory does not exist.",
            error_details=f"Directory not found: {ws_path}",
        )
    git_dir = ws_path / ".git"
    if not git_dir.exists():
        return DiffResult(
            status="not_a_git_repo",
            has_changes=False,
            summary="Directory is not a git repository.",
            error_details=None,
        )

    git_cmd = shutil.which("git") or "git"

    def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [git_cmd, "-C", str(ws_path)] + list(args),
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )

    has_head_proc = _run_git("rev-parse", "--verify", "HEAD")
    has_head = has_head_proc.returncode == 0

    status_proc = _run_git("status", "--porcelain=v1", "-uall")
    status_lines = [line for line in status_proc.stdout.splitlines() if line.strip()]

    untracked_proc = _run_git("ls-files", "--others", "--exclude-standard")
    untracked_files = [
        str(Path(p)).replace("\\", "/")
        for p in untracked_proc.stdout.splitlines()
        if p.strip()
    ]

    if has_head:
        diff_proc = _run_git("diff", "HEAD")
        unified_diff = diff_proc.stdout
    else:
        diff_proc = _run_git("diff")
        cached_diff_proc = _run_git("diff", "--cached")
        unified_diff = (cached_diff_proc.stdout + "\n" + diff_proc.stdout).strip()

    numstat_map: Dict[str, Tuple[int, int]] = {}
    if has_head:
        numstat_proc = _run_git("diff", "--numstat", "HEAD")
        for line in numstat_proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                ins = int(parts[0]) if parts[0].isdigit() else 0
                dels = int(parts[1]) if parts[1].isdigit() else 0
                fpath = str(Path(parts[2])).replace("\\", "/")
                numstat_map[fpath] = (ins, dels)

    changed_files: List[FileDiffStat] = []
    for line in status_lines:
        code = line[:2]
        rel_f = line[3:].strip()
        if " -> " in rel_f:
            rel_f = rel_f.split(" -> ")[-1]
        norm_path = str(Path(rel_f)).replace("\\", "/")
        if code == "??":
            continue

        status_char = "M"
        if "A" in code:
            status_char = "A"
        elif "D" in code:
            status_char = "D"
        elif "R" in code:
            status_char = "R"
        elif "M" in code:
            status_char = "M"

        ins, dels = numstat_map.get(norm_path, (0, 0))
        changed_files.append(
            FileDiffStat(
                path=norm_path,
                status=status_char,
                insertions=ins,
                deletions=dels,
            )
        )

    has_changes = bool(changed_files or untracked_files)
    summary_parts = []
    if changed_files:
        summary_parts.append(f"{len(changed_files)} changed file(s)")
    if untracked_files:
        summary_parts.append(f"{len(untracked_files)} untracked file(s)")
    summary = ", ".join(summary_parts) if summary_parts else "No uncommitted changes detected"

    return DiffResult(
        status="success",
        has_changes=has_changes,
        unified_diff=unified_diff,
        changed_files=changed_files,
        untracked_files=untracked_files,
        summary=summary,
        error_details=None,
    )


async def real_test_runner_executor(
    workspace_path: str,
    test_command: str = "",
    timeout_seconds: int = 300,
) -> TestRunResult:
    """Authentic multi-ecosystem test runner executor for E2E integration tests."""
    start_time = time.monotonic()
    ws_path = Path(workspace_path).resolve()
    if not ws_path.exists():
        return TestRunResult(
            status="error",
            exit_code=1,
            framework="custom" if test_command else "none",
            test_command_executed=test_command,
            output="",
            summary=TestSummary(errors=1),
            error_details=f"Workspace directory not found: {ws_path}",
        )

    framework = "custom"
    cmd_to_run = test_command

    if not cmd_to_run:
        if (
            (ws_path / "pytest.ini").exists()
            or (ws_path / "pyproject.toml").exists()
            or list(ws_path.glob("**/test_*.py"))
        ):
            framework = "pytest"
            cmd_to_run = f"{sys.executable} -m pytest -v"
        elif (ws_path / "package.json").exists():
            framework = "npm"
            cmd_to_run = "npm test"
        elif (ws_path / "Cargo.toml").exists():
            framework = "cargo"
            cmd_to_run = "cargo test"
        else:
            return TestRunResult(
                status="no_framework_detected",
                exit_code=0,
                framework="none",
                test_command_executed="",
                output="No recognized test configuration found in workspace.",
                summary=TestSummary(),
                duration_seconds=0.0,
            )

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd_to_run,
            cwd=str(ws_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        check=False,
                    )
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return TestRunResult(
                status="timeout",
                exit_code=-1,
                framework=framework,
                test_command_executed=cmd_to_run,
                output="Test execution timed out.",
                summary=TestSummary(errors=1),
                duration_seconds=float(timeout_seconds),
                error_details=f"Process timed out after {timeout_seconds}s",
            )

        duration = time.monotonic() - start_time
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        combined_output = f"{stdout}\n{stderr}".strip()

        passed = 0
        failed = 0
        skipped = 0
        errors = 0
        failures: List[TestFailure] = []

        match = re.search(r"=+\s+([0-9a-zA-Z\s,]+)\s+in\s+[\d\.]+s\s*=+", combined_output)
        if match:
            summary_str = match.group(1)
            p_m = re.search(r"(\d+)\s+passed", summary_str)
            if p_m:
                passed = int(p_m.group(1))
            f_m = re.search(r"(\d+)\s+failed", summary_str)
            if f_m:
                failed = int(f_m.group(1))
            s_m = re.search(r"(\d+)\s+skipped", summary_str)
            if s_m:
                skipped = int(s_m.group(1))
            e_m = re.search(r"(\d+)\s+error", summary_str)
            if e_m:
                errors = int(e_m.group(1))

        if passed == 0 and failed == 0:
            if "PASS" in combined_output or proc.returncode == 0:
                passed = max(1, len(re.findall(r"PASS|passed|ok", combined_output, re.IGNORECASE)))
            if "FAIL" in combined_output or proc.returncode != 0:
                failed = max(1, len(re.findall(r"FAIL|failed", combined_output, re.IGNORECASE)))

        for fail_line in re.findall(r"FAILED\s+([^\s]+)(?:\s+-\s+(.+))?", combined_output):
            test_id = fail_line[0]
            msg = fail_line[1] if len(fail_line) > 1 and fail_line[1] else "Assertion or runtime error"
            failures.append(TestFailure(test_id=test_id, message=msg))

        total = passed + failed + skipped + errors
        status = "passed" if proc.returncode == 0 and failed == 0 and errors == 0 else "failed"

        return TestRunResult(
            status=status,
            exit_code=proc.returncode or 0,
            framework=framework,
            test_command_executed=cmd_to_run,
            output=combined_output,
            summary=TestSummary(
                total=total,
                passed=passed,
                failed=failed,
                skipped=skipped,
                errors=errors,
            ),
            failures=failures,
            duration_seconds=round(duration, 4),
            error_details=f"{failed} failure(s) encountered" if failed else None,
        )
    except Exception as e:
        return TestRunResult(
            status="error",
            exit_code=1,
            framework=framework,
            test_command_executed=cmd_to_run,
            output=str(e),
            summary=TestSummary(errors=1),
            duration_seconds=time.monotonic() - start_time,
            error_details=str(e),
        )


@pytest.fixture
def integrated_server(mock_backend: ProgrammableMockAGYBackend) -> Any:
    """Provides a FastMCP server instance configured with mock backend, real git diff, and real test runner."""
    return create_server(
        backend=mock_backend,
        diff_executor=real_git_diff_executor,
        test_executor=real_test_runner_executor,
    )


# ============================================================================
# Tier 3 Cross-Feature Combination Tests
# ============================================================================

@pytest.mark.tier3
@pytest.mark.asyncio
async def test_scenario1_execute_task_then_get_diff(
    integrated_server: Any,
    client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
) -> None:
    """Scenario 1: agy_execute_task (creates code) -> agy_get_diff (inspects unified diff)."""
    ws = clean_git_workspace

    async with client_factory(integrated_server) as session:
        # Step 1: Execute coding task to create calculator module
        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Create calculator module with add and subtract functions",
                "auto_approve": True,
                "mode": "accept-edits",
            },
        )
        assert exec_res is not None and not exec_res.isError
        task_data = json.loads(exec_res.content[0].text)
        task_result = TaskExecutionResult.model_validate(task_data)

        assert task_result.status == "success"
        assert "calculator.py" in task_result.modified_files
        assert ws.exists("calculator.py")
        assert "def add" in ws.read_file("calculator.py")

        # Step 2: Query git diff to inspect changes
        diff_res = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        assert diff_res is not None and not diff_res.isError
        diff_data = json.loads(diff_res.content[0].text)
        diff_result = DiffResult.model_validate(diff_data)

        assert diff_result.status == "success"
        assert diff_result.has_changes is True
        # calculator.py is untracked
        assert "calculator.py" in diff_result.untracked_files or any(
            f.path == "calculator.py" for f in diff_result.changed_files
        )
        assert "calculator" in diff_result.summary.lower() or "untracked" in diff_result.summary.lower() or "changed" in diff_result.summary.lower()


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_scenario2_execute_task_then_run_tests(
    integrated_server: Any,
    client_factory: Any,
    pytest_failing_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 2: agy_execute_task (implements bugfix) -> agy_run_tests (verifies test suite passes)."""
    ws = pytest_failing_workspace

    # Register rule to fix the failing test in tests/test_failing.py
    mock_backend.add_task_rule(
        "fix division",
        "tests/test_failing.py",
        "def test_success():\n"
        "    assert True\n\n"
        "def test_division_fixed():\n"
        "    assert 4 / 2 == 2\n",
    )

    async with client_factory(integrated_server) as session:
        # Step 1: Confirm initial test suite fails
        initial_test_res = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        initial_data = json.loads(initial_test_res.content[0].text)
        initial_run = TestRunResult.model_validate(initial_data)
        assert initial_run.status == "failed"
        assert initial_run.summary.failed >= 1

        # Step 2: Execute task to fix division bug
        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Fix division bug in tests/test_failing.py so that tests pass cleanly",
                "auto_approve": True,
            },
        )
        exec_data = json.loads(exec_res.content[0].text)
        task_result = TaskExecutionResult.model_validate(exec_data)
        assert task_result.status == "success"
        assert "tests/test_failing.py" in task_result.modified_files

        # Step 3: Run tests again and verify green test suite
        final_test_res = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        final_data = json.loads(final_test_res.content[0].text)
        final_run = TestRunResult.model_validate(final_data)

        assert final_run.status == "passed"
        assert final_run.exit_code == 0
        assert final_run.summary.failed == 0
        assert final_run.summary.passed >= 2


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_scenario3_chat_plan_then_execute_then_diff(
    integrated_server: Any,
    client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
) -> None:
    """Scenario 3: agy_chat (planning) -> agy_execute_task (implementation) -> agy_get_diff (diff inspection)."""
    ws = clean_git_workspace
    initial_snapshot = ws.snapshot_hashes()

    async with client_factory(integrated_server) as session:
        # Step 1: Analytical planning via agy_chat
        chat_res = await session.call_tool(
            "agy_chat",
            {
                "prompt": "Analyze repository and plan auth service architecture with token validation",
                "workspace_path": str(ws),
            },
        )
        chat_data = json.loads(chat_res.content[0].text)
        chat_result = ChatResult.model_validate(chat_data)

        assert chat_result.status == "success"
        assert "auth" in chat_result.response.lower()
        assert chat_result.conversation_id != ""

        # Verify chat read-only guarantee: 100% unchanged filesystem
        is_unchanged, desc = ws.verify_snapshot_unchanged(initial_snapshot)
        assert is_unchanged, f"Chat modified workspace: {desc}"

        # Step 2: Implementation via agy_execute_task
        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Implement auth service according to plan in src/auth.py",
                "auto_approve": True,
            },
        )
        exec_data = json.loads(exec_res.content[0].text)
        task_result = TaskExecutionResult.model_validate(exec_data)

        assert task_result.status == "success"
        assert "src/auth.py" in task_result.modified_files
        assert ws.exists("src/auth.py")

        # Step 3: Git diff inspection
        diff_res = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        diff_data = json.loads(diff_res.content[0].text)
        diff_result = DiffResult.model_validate(diff_data)

        assert diff_result.status == "success"
        assert diff_result.has_changes is True
        assert "src/auth.py" in diff_result.untracked_files or any(
            f.path == "src/auth.py" for f in diff_result.changed_files
        )


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_scenario4_execute_test_fail_chat_analyze_execute_fix_test_pass_loop(
    integrated_server: Any,
    client_factory: Any,
    ephemeral_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 4: Closed-loop self-remediation cycle: execute -> test fail -> chat analyze -> execute fix -> test pass."""
    ws = ephemeral_workspace
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")

    # Step 1: Initial implementation with intentional bug in math module
    ws.write_file(
        "math_utils.py",
        "def multiply(a: float, b: float) -> float:\n"
        "    return a + b  # Bug: adding instead of multiplying\n",
    )
    ws.write_file(
        "tests/test_math.py",
        "from math_utils import multiply\n\n"
        "def test_multiply():\n"
        "    assert multiply(3, 4) == 12\n\n"
        "def test_multiply_zero():\n"
        "    assert multiply(5, 0) == 0\n",
    )

    async with client_factory(integrated_server) as session:
        # Step 2: Run tests and detect failure
        test_res1 = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        test_data1 = json.loads(test_res1.content[0].text)
        test_run1 = TestRunResult.model_validate(test_data1)

        assert test_run1.status == "failed"
        assert test_run1.summary.failed >= 1
        failure_output = test_run1.output

        # Step 3: Consult AGY via chat to analyze failure
        chat_res = await session.call_tool(
            "agy_chat",
            {
                "prompt": f"Analyze test failure output: {failure_output[:200]} and explain what logic needs fixing.",
                "workspace_path": str(ws),
            },
        )
        chat_data = json.loads(chat_res.content[0].text)
        chat_result = ChatResult.model_validate(chat_data)
        assert chat_result.status == "success"
        conv_id = chat_result.conversation_id

        # Step 4: Fix math_utils.py using execute_task
        mock_backend.add_task_rule(
            "fix multiply",
            "math_utils.py",
            "def multiply(a: float, b: float) -> float:\n"
            "    return a * b\n",
        )

        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Fix multiply function in math_utils.py to correctly compute multiplication",
                "auto_approve": True,
            },
        )
        exec_data = json.loads(exec_res.content[0].text)
        task_result = TaskExecutionResult.model_validate(exec_data)
        assert task_result.status == "success"

        # Step 5: Re-run tests and verify 100% pass
        test_res2 = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        test_data2 = json.loads(test_res2.content[0].text)
        test_run2 = TestRunResult.model_validate(test_data2)

        assert test_run2.status == "passed"
        assert test_run2.summary.failed == 0
        assert test_run2.summary.passed == 2
        assert test_run2.exit_code == 0


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_scenario5_multiturn_chat_with_concurrent_diff(
    integrated_server: Any,
    client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
) -> None:
    """Scenario 5: Multi-turn chat session preserving conversation_id while running diff in parallel."""
    ws = clean_git_workspace

    async with client_factory(integrated_server) as session:
        # Turn 1: Start chat session
        chat1_res = await session.call_tool(
            "agy_chat",
            {
                "prompt": "Start architecture review for cloud storage sync",
                "workspace_path": str(ws),
            },
        )
        chat1_data = json.loads(chat1_res.content[0].text)
        chat1 = ChatResult.model_validate(chat1_data)
        conv_id = chat1.conversation_id
        assert conv_id != ""

        # Concurrently perform diff inspection while conversation is active
        diff_res = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        diff_data = json.loads(diff_res.content[0].text)
        diff_result = DiffResult.model_validate(diff_data)
        assert diff_result.status == "success"

        # Turn 2: Continue dialogue with same conversation_id
        chat2_res = await session.call_tool(
            "agy_chat",
            {
                "prompt": "What security considerations apply to token rotation?",
                "workspace_path": str(ws),
                "conversation_id": conv_id,
            },
        )
        chat2_data = json.loads(chat2_res.content[0].text)
        chat2 = ChatResult.model_validate(chat2_data)
        assert chat2.status == "success"
        assert chat2.conversation_id == conv_id

        # Turn 3: Final consultation turn
        chat3_res = await session.call_tool(
            "agy_chat",
            {
                "prompt": "Summarize design recommendations in 3 bullet points.",
                "workspace_path": str(ws),
                "conversation_id": conv_id,
            },
        )
        chat3_data = json.loads(chat3_res.content[0].text)
        chat3 = ChatResult.model_validate(chat3_data)
        assert chat3.status == "success"
        assert chat3.conversation_id == conv_id


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_scenario6_tracked_file_modification_git_diff_stats(
    integrated_server: Any,
    client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 6: Task execution modifying existing tracked file -> git diff shows modified status 'M' with correct stats."""
    ws = clean_git_workspace
    # clean_git_workspace has src/app.py committed

    mock_backend.add_task_rule(
        "refactor app",
        "src/app.py",
        "def run():\n"
        "    # Refactored implementation with enhancements\n"
        "    base = 40\n"
        "    extra = 60\n"
        "    return base + extra\n",
    )

    async with client_factory(integrated_server) as session:
        # Execute task modifying src/app.py
        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Refactor app in src/app.py to compute base + extra",
                "auto_approve": True,
            },
        )
        exec_data = json.loads(exec_res.content[0].text)
        task_result = TaskExecutionResult.model_validate(exec_data)
        assert task_result.status == "success"
        assert "src/app.py" in task_result.modified_files

        # Query git diff
        diff_res = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        diff_data = json.loads(diff_res.content[0].text)
        diff_result = DiffResult.model_validate(diff_data)

        assert diff_result.status == "success"
        assert diff_result.has_changes is True

        # Verify FileDiffStat entry for src/app.py
        app_diffs = [f for f in diff_result.changed_files if f.path == "src/app.py"]
        assert len(app_diffs) == 1
        stat = app_diffs[0]
        assert stat.status == "M"
        assert stat.insertions > 0

        # Verify unified diff content contains standard patch markers
        assert "--- a/src/app.py" in diff_result.unified_diff
        assert "+++ b/src/app.py" in diff_result.unified_diff
        assert "+    base = 40" in diff_result.unified_diff


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_scenario7_staged_and_unstaged_mixed_diff_workflow(
    integrated_server: Any,
    client_factory: Any,
    staged_changes_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 7: agy_execute_task with pre-existing staged changes -> unified diff captures staged + new edits."""
    ws = staged_changes_workspace
    # staged_changes_workspace has staged modifications to src/app.py and staged new file src/utils.py

    mock_backend.add_task_rule(
        "add logging",
        "src/logger.py",
        "import logging\ndef get_app_logger():\n    return logging.getLogger('app')\n",
    )

    async with client_factory(integrated_server) as session:
        # Perform task creating src/logger.py
        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Add logging helper in src/logger.py",
                "auto_approve": True,
            },
        )
        assert not exec_res.isError

        # Inspect diff
        diff_res = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        diff_data = json.loads(diff_res.content[0].text)
        diff_result = DiffResult.model_validate(diff_data)

        assert diff_result.status == "success"
        assert diff_result.has_changes is True

        changed_paths = {f.path for f in diff_result.changed_files}
        # Staged files should be present in changed_files
        assert "src/app.py" in changed_paths
        assert "src/utils.py" in changed_paths

        # Newly created untracked file
        assert "src/logger.py" in diff_result.untracked_files or "src/logger.py" in changed_paths


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_scenario8_full_four_tool_chain(
    integrated_server: Any,
    client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 8: Complete 4-tool pipeline (chat -> execute -> diff -> test) in a single session."""
    ws = clean_git_workspace
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")

    # Add rules for module creation and test creation
    mock_backend.add_task_rule(
        "build math library",
        "math_lib.py",
        "def power(base: float, exp: float) -> float:\n"
        "    return base ** exp\n",
    )
    ws.write_file(
        "tests/test_math_lib.py",
        "from math_lib import power\n\n"
        "def test_power():\n"
        "    assert power(2, 3) == 8\n"
        "    assert power(5, 2) == 25\n",
    )

    async with client_factory(integrated_server) as session:
        # Tool 1: Chat planning
        chat_res = await session.call_tool(
            "agy_chat",
            {"prompt": "Plan math library with power function", "workspace_path": str(ws)},
        )
        chat = ChatResult.model_validate(json.loads(chat_res.content[0].text))
        assert chat.status == "success"

        # Tool 2: Execute task
        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Build math library with power function in math_lib.py",
                "auto_approve": True,
            },
        )
        task = TaskExecutionResult.model_validate(json.loads(exec_res.content[0].text))
        assert task.status == "success"
        assert ws.exists("math_lib.py")

        # Tool 3: Get diff
        diff_res = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        diff = DiffResult.model_validate(json.loads(diff_res.content[0].text))
        assert diff.status == "success"
        assert diff.has_changes is True

        # Tool 4: Run tests
        test_res = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        test = TestRunResult.model_validate(json.loads(test_res.content[0].text))
        assert test.status == "passed"
        assert test.summary.passed >= 1
        assert test.summary.failed == 0
