"""Tier 4: Real-World Scenarios and Transport End-to-End Test Suite.

Verifies end-to-end real-world scenarios across FastMCP AGY:
- Scenario 1: Full Subprocess Stdio Lifecycle using `stdio_client_factory` (exercising real Python stdio JSON-RPC 2.0 transport).
- Scenario 2: External Architect (Claude Desktop/Cursor simulation) dispatching module creation + test suite execution.
- Scenario 3: Multi-Language Project Verification (Python + Node.js / NPM hybrid workspace).
- Scenario 4: Failure Diagnostics & Auto-Remediation Loop.
- Scenario 5: Timeout Recovery, Mutex Lock Release, and Workspace Reusability.
- Scenario 6: Legacy Code Refactoring with SHA-256 Snapshot Integrity and Regression Verification.
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
from tests.test_tier3_cross_feature import real_git_diff_executor, real_test_runner_executor


@pytest.fixture
def integrated_server(mock_backend: ProgrammableMockAGYBackend) -> Any:
    """Provides a FastMCP server instance configured with mock backend, real git diff, and real test runner."""
    return create_server(
        backend=mock_backend,
        diff_executor=real_git_diff_executor,
        test_executor=real_test_runner_executor,
    )


# ============================================================================
# Tier 4 Real-World Scenario Tests
# ============================================================================

@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario1_full_subprocess_stdio_lifecycle(
    stdio_client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
) -> None:
    """Scenario 1: Exercises complete subprocess stdio JSON-RPC 2.0 lifecycle with tool calls."""
    ws = clean_git_workspace

    async with stdio_client_factory() as session:
        # Step 1: Tool Discovery
        tools_response = await session.list_tools()
        assert tools_response is not None
        tool_names = {t.name for t in tools_response.tools}
        assert {
            "agy_execute_task",
            "agy_chat",
            "agy_get_diff",
            "agy_run_tests",
        }.issubset(tool_names)

        # Step 2: agy_chat over stdio
        chat_call = await session.call_tool(
            "agy_chat",
            {"prompt": "Architectural status query over stdio", "workspace_path": str(ws)},
        )
        assert chat_call is not None and not chat_call.isError
        chat_data = json.loads(chat_call.content[0].text)
        chat_model = ChatResult.model_validate(chat_data)
        assert chat_model.status == "success"
        assert chat_model.conversation_id != ""
        assert chat_model.tokens_used.total_tokens > 0

        # Step 3: agy_execute_task over stdio
        task_call = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Create utils helper for data serialization",
                "auto_approve": True,
            },
        )
        assert task_call is not None and not task_call.isError
        task_data = json.loads(task_call.content[0].text)
        task_model = TaskExecutionResult.model_validate(task_data)
        assert task_model.status == "success"
        assert task_model.tokens_used.total_tokens > 0

        # Step 4: agy_get_diff over stdio
        diff_call = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        assert diff_call is not None and not diff_call.isError
        diff_data = json.loads(diff_call.content[0].text)
        diff_model = DiffResult.model_validate(diff_data)
        assert diff_model.status in ("success", "not_a_git_repo")

        # Step 5: agy_run_tests over stdio
        test_call = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws), "test_command": "echo test_ok"},
        )
        assert test_call is not None and not test_call.isError
        test_data = json.loads(test_call.content[0].text)
        test_model = TestRunResult.model_validate(test_data)
        assert test_model.status == "passed"


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario2_external_architect_simulation(
    integrated_server: Any,
    client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 2: Simulates Claude Desktop / Cursor Architect Agent dispatching module creation + test suite execution."""
    ws = clean_git_workspace
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")

    # Configure backend rules for LRU cache implementation and tests
    mock_backend.add_task_rule(
        "implement lru cache",
        "cache/lru.py",
        "from collections import OrderedDict\n"
        "from typing import Any, Optional\n\n"
        "class LRUCache:\n"
        "    def __init__(self, capacity: int = 100):\n"
        "        self.capacity = capacity\n"
        "        self.cache: OrderedDict[str, Any] = OrderedDict()\n\n"
        "    def get(self, key: str) -> Optional[Any]:\n"
        "        if key not in self.cache:\n"
        "            return None\n"
        "        self.cache.move_to_end(key)\n"
        "        return self.cache[key]\n\n"
        "    def put(self, key: str, value: Any) -> None:\n"
        "        if key in self.cache:\n"
        "            self.cache.move_to_end(key)\n"
        "        self.cache[key] = value\n"
        "        if len(self.cache) > self.capacity:\n"
        "            self.cache.popitem(last=False)\n",
    )

    mock_backend.add_task_rule(
        "create lru cache unit tests",
        "tests/test_lru.py",
        "import pytest\n"
        "from cache.lru import LRUCache\n\n"
        "def test_lru_put_and_get():\n"
        "    cache = LRUCache(capacity=2)\n"
        "    cache.put('a', 1)\n"
        "    cache.put('b', 2)\n"
        "    assert cache.get('a') == 1\n"
        "    assert cache.get('b') == 2\n\n"
        "def test_lru_eviction():\n"
        "    cache = LRUCache(capacity=2)\n"
        "    cache.put('a', 1)\n"
        "    cache.put('b', 2)\n"
        "    cache.get('a')  # 'a' becomes most recently used\n"
        "    cache.put('c', 3)  # 'b' should be evicted\n"
        "    assert cache.get('b') is None\n"
        "    assert cache.get('a') == 1\n"
        "    assert cache.get('c') == 3\n",
    )

    async with client_factory(integrated_server) as session:
        # Phase 1: Architect conducts design query via agy_chat
        chat_res = await session.call_tool(
            "agy_chat",
            {
                "prompt": "Architectural recommendation for LRU cache module with OrderedDict",
                "workspace_path": str(ws),
            },
        )
        chat = ChatResult.model_validate(json.loads(chat_res.content[0].text))
        assert chat.status == "success"

        # Phase 2: Architect dispatches implementation task
        task1_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Implement LRU cache in cache/lru.py with capacity management",
                "auto_approve": True,
            },
        )
        task1 = TaskExecutionResult.model_validate(json.loads(task1_res.content[0].text))
        assert task1.status == "success"
        assert ws.exists("cache/lru.py")

        # Phase 3: Architect dispatches test creation task
        task2_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Create LRU cache unit tests in tests/test_lru.py",
                "auto_approve": True,
            },
        )
        task2 = TaskExecutionResult.model_validate(json.loads(task2_res.content[0].text))
        assert task2.status == "success"
        assert ws.exists("tests/test_lru.py")

        # Phase 4: Architect reviews unified git diff
        diff_res = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        diff = DiffResult.model_validate(json.loads(diff_res.content[0].text))
        assert diff.status == "success"
        assert diff.has_changes is True

        # Phase 5: Architect commands test runner execution
        test_res = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        test = TestRunResult.model_validate(json.loads(test_res.content[0].text))
        assert test.status == "passed"
        assert test.summary.passed >= 2
        assert test.summary.failed == 0

        # Phase 6: Architect requests completion summary
        summary_res = await session.call_tool(
            "agy_chat",
            {
                "prompt": f"Summarize test execution: {test.summary.passed} tests passed. All criteria met.",
                "workspace_path": str(ws),
            },
        )
        summary_chat = ChatResult.model_validate(json.loads(summary_res.content[0].text))
        assert summary_chat.status == "success"


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario3_multilanguage_hybrid_workspace(
    integrated_server: Any,
    client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 3: Multi-Language Project Verification (Python + Node.js / NPM hybrid workspace)."""
    ws = clean_git_workspace

    # Setup Python backend component
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")
    ws.write_file(
        "backend/service.py",
        "def process_payload(data: dict) -> dict:\n"
        "    return {'status': 'ok', 'count': len(data)}\n",
    )
    ws.write_file(
        "backend/test_service.py",
        "from backend.service import process_payload\n\n"
        "def test_process_payload():\n"
        "    res = process_payload({'a': 1, 'b': 2})\n"
        "    assert res['status'] == 'ok'\n"
        "    assert res['count'] == 2\n",
    )

    # Setup Node.js frontend / CLI component
    node_test_script = (
        "const payload = { a: 1, b: 2 };\n"
        "if (Object.keys(payload).length === 2) {\n"
        "  console.log('PASS: Node client payload test');\n"
        "  process.exit(0);\n"
        "} else {\n"
        "  console.error('FAIL: Invalid payload count');\n"
        "  process.exit(1);\n"
        "}\n"
    )
    ws.write_file("frontend/test.js", node_test_script)
    pkg = {
        "name": "hybrid-frontend",
        "version": "1.0.0",
        "scripts": {"test": "node frontend/test.js"},
    }
    ws.write_file("package.json", json.dumps(pkg, indent=2))

    async with client_factory(integrated_server) as session:
        # Step 1: Run Python test suite
        py_test_res = await session.call_tool(
            "agy_run_tests",
            {
                "workspace_path": str(ws),
                "test_command": f"{sys.executable} -m pytest backend/test_service.py -v",
            },
        )
        py_test = TestRunResult.model_validate(json.loads(py_test_res.content[0].text))
        assert py_test.status == "passed"
        assert py_test.summary.passed >= 1

        # Step 2: Run Node.js test suite
        node_test_res = await session.call_tool(
            "agy_run_tests",
            {
                "workspace_path": str(ws),
                "test_command": "node frontend/test.js",
            },
        )
        node_test = TestRunResult.model_validate(json.loads(node_test_res.content[0].text))
        assert node_test.status == "passed"
        assert node_test.exit_code == 0

        # Step 3: Execute cross-stack feature task
        mock_backend.add_task_rule(
            "update api version",
            "backend/version.py",
            "API_VERSION = '2.0.0'\n",
        )
        mock_backend.add_task_rule(
            "update api version",
            "frontend/version.json",
            '{"version": "2.0.0"}\n',
        )

        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Update API version to 2.0.0 across backend and frontend",
                "auto_approve": True,
            },
        )
        assert not exec_res.isError

        # Step 4: Inspect unified diff across entire hybrid repo
        diff_res = await session.call_tool(
            "agy_get_diff",
            {"workspace_path": str(ws)},
        )
        diff = DiffResult.model_validate(json.loads(diff_res.content[0].text))
        assert diff.status == "success"
        assert diff.has_changes is True


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario4_failure_diagnostics_and_autoremediation_loop(
    integrated_server: Any,
    client_factory: Any,
    ephemeral_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 4: Failure Diagnostics & Auto-Remediation Loop."""
    ws = ephemeral_workspace
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")

    # Faulty pipeline
    ws.write_file(
        "pipeline.py",
        "def sanitize_input(text: str) -> str:\n"
        "    if text is None:\n"
        "        raise ValueError('text cannot be None')\n"
        "    return text.strip().lower()\n\n"
        "def transform_records(records: list) -> list:\n"
        "    # Faulty implementation: crashes on empty record list\n"
        "    if not records:\n"
        "        raise IndexError('Empty records list')\n"
        "    return [sanitize_input(r.get('name', '')) for r in records]\n",
    )
    ws.write_file(
        "tests/test_pipeline.py",
        "import pytest\n"
        "from pipeline import transform_records\n\n"
        "def test_transform_valid_records():\n"
        "    records = [{'name': ' Alice '}, {'name': 'BOB'}]\n"
        "    assert transform_records(records) == ['alice', 'bob']\n\n"
        "def test_transform_empty_records():\n"
        "    assert transform_records([]) == []\n",
    )

    async with client_factory(integrated_server) as session:
        # Step 1: Initial test run fails on empty records
        test_run1_res = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        test_run1 = TestRunResult.model_validate(json.loads(test_run1_res.content[0].text))
        assert test_run1.status == "failed"
        assert test_run1.summary.failed >= 1
        assert len(test_run1.failures) >= 1 or "IndexError" in test_run1.output

        # Step 2: Configure remediation rule
        mock_backend.add_task_rule(
            "fix transform_records",
            "pipeline.py",
            "def sanitize_input(text: str) -> str:\n"
            "    if text is None:\n"
            "        return ''\n"
            "    return text.strip().lower()\n\n"
            "def transform_records(records: list) -> list:\n"
            "    if not records:\n"
            "        return []\n"
            "    return [sanitize_input(r.get('name', '')) for r in records]\n",
        )

        # Step 3: Dispatch remediation task
        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Fix transform_records to gracefully return empty list [] when records is empty",
                "auto_approve": True,
            },
        )
        task_res = TaskExecutionResult.model_validate(json.loads(exec_res.content[0].text))
        assert task_res.status == "success"

        # Step 4: Re-run tests and confirm 100% pass
        test_run2_res = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        test_run2 = TestRunResult.model_validate(json.loads(test_run2_res.content[0].text))
        assert test_run2.status == "passed"
        assert test_run2.summary.failed == 0
        assert test_run2.summary.passed == 2
        assert test_run2.exit_code == 0


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario5_timeout_recovery_and_workspace_reusability(
    integrated_server: Any,
    client_factory: Any,
    ephemeral_workspace: EphemeralWorkspace,
) -> None:
    """Scenario 5: Timeout Recovery, Process Tree Cleanup, and Workspace Reusability."""
    ws = ephemeral_workspace
    ws.write_file("pytest.ini", "[pytest]\n")

    # Script with hanging infinite sleep
    ws.write_file("hang.py", "import time; time.sleep(60)\n")

    async with client_factory(integrated_server) as session:
        # Step 1: Execute hanging command with short 2s timeout
        start_t = time.monotonic()
        test_timeout_res = await session.call_tool(
            "agy_run_tests",
            {
                "workspace_path": str(ws),
                "test_command": f"{sys.executable} hang.py",
                "timeout_seconds": 2,
            },
        )
        elapsed = time.monotonic() - start_t
        assert elapsed < 10.0, f"Timeout took too long ({elapsed}s)"

        test_timeout = TestRunResult.model_validate(json.loads(test_timeout_res.content[0].text))
        assert test_timeout.status == "timeout"
        assert test_timeout.exit_code == -1

        # Step 2: Immediately execute subsequent valid command on same workspace
        # Proves workspace is unlocked and no hanging locks remain
        valid_test_res = await session.call_tool(
            "agy_run_tests",
            {
                "workspace_path": str(ws),
                "test_command": f"{sys.executable} -c \"print('PASS: recovery test'); import sys; sys.exit(0)\"",
                "timeout_seconds": 10,
            },
        )
        valid_test = TestRunResult.model_validate(json.loads(valid_test_res.content[0].text))
        assert valid_test.status == "passed"
        assert valid_test.exit_code == 0


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario6_legacy_refactoring_with_snapshot_integrity(
    integrated_server: Any,
    client_factory: Any,
    clean_git_workspace: EphemeralWorkspace,
    mock_backend: ProgrammableMockAGYBackend,
) -> None:
    """Scenario 6: Legacy Code Refactoring with SHA-256 Snapshot Integrity and Regression Verification."""
    ws = clean_git_workspace

    # Setup non-target files that must remain untouched
    ws.write_file("config/database.json", '{"host": "localhost", "port": 5432}\n')
    ws.write_file("docs/architecture.md", "# Architecture\nLegacy service v1.\n")
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")

    # Legacy code to refactor
    ws.write_file(
        "legacy/engine.py",
        "def compute_interest(principal: float, rate: float, time_years: float) -> float:\n"
        "    return principal * (1 + rate * time_years)\n",
    )
    ws.write_file(
        "tests/test_legacy.py",
        "from legacy.engine import compute_interest\n\n"
        "def test_interest():\n"
        "    assert compute_interest(1000, 0.05, 1) == 1050\n\n"
        "def test_zero_time():\n"
        "    assert compute_interest(500, 0.10, 0) == 500\n",
    )

    # Capture initial SHA-256 snapshot of non-target files
    initial_snapshots = ws.snapshot_hashes()
    non_target_keys = {"config/database.json", "docs/architecture.md", "pytest.ini"}

    mock_backend.add_task_rule(
        "refactor engine with compound interest",
        "legacy/engine.py",
        "def compute_interest(principal: float, rate: float, time_years: float, compound_periods: int = 1) -> float:\n"
        "    if compound_periods <= 1:\n"
        "        return principal * (1 + rate * time_years)\n"
        "    return principal * ((1 + rate / compound_periods) ** (compound_periods * time_years))\n",
    )

    async with client_factory(integrated_server) as session:
        # Step 1: Execute refactoring task
        exec_res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(ws),
                "prompt": "Refactor engine with compound interest support in legacy/engine.py",
                "auto_approve": True,
            },
        )
        task = TaskExecutionResult.model_validate(json.loads(exec_res.content[0].text))
        assert task.status == "success"

        # Step 2: Run tests to ensure no regressions
        test_res = await session.call_tool(
            "agy_run_tests",
            {"workspace_path": str(ws)},
        )
        test = TestRunResult.model_validate(json.loads(test_res.content[0].text))
        assert test.status == "passed"
        assert test.summary.passed == 2
        assert test.summary.failed == 0

        # Step 3: Verify non-target files are 100% unchanged
        current_snapshots = ws.snapshot_hashes()
        for key in non_target_keys:
            assert key in current_snapshots, f"File {key} was deleted!"
            assert (
                current_snapshots[key] == initial_snapshots[key]
            ), f"Side-effect detected: Non-target file {key} was unexpectedly modified!"
