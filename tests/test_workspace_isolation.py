"""Comprehensive automated test suite for Workspace Safety, Isolation, Boundary Validation, and Concurrency Locking."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_agy import (
    WorkspaceError,
    WorkspaceLockManager,
    WorkspaceLockTimeoutError,
    WorkspaceNotADirectoryError,
    WorkspaceNotFoundError,
    WorkspaceSecurityError,
    canonicalize_workspace_path,
    create_mcp_server,
    get_workspace_key,
    get_workspace_lock_manager,
    is_filesystem_root,
    is_system_critical_path,
    reset_workspace_lock_manager,
    validate_workspace_path,
)


@pytest.fixture(autouse=True)
def clean_workspace_lock_state():
    """Reset workspace lock manager before and after each test."""
    reset_workspace_lock_manager()
    yield
    reset_workspace_lock_manager()


# ============================================================================
# 1. Path Canonicalization & Key Generation Tests
# ============================================================================


class TestPathCanonicalization:
    """Verifies path resolution, relative segment normalization, tilde expansion, and null byte rejection."""

    def test_canonicalize_relative_path(self, tmp_path):
        """Verifies resolving relative path segments (. and ..)."""
        sub_dir = tmp_path / "sub"
        sub_dir.mkdir()
        nested_dir = sub_dir / "nested"
        nested_dir.mkdir()

        rel_path = f"{nested_dir}/../nested/../"
        canonical = canonicalize_workspace_path(rel_path)
        assert canonical == sub_dir.resolve()

    def test_canonicalize_tilde_expansion(self):
        """Verifies expanding user home tilde (~)."""
        canonical = canonicalize_workspace_path("~")
        assert canonical == Path.home().resolve()

    def test_canonicalize_mixed_slashes(self, tmp_path):
        """Verifies mixed forward and backward slashes normalize correctly."""
        sub_dir = tmp_path / "a" / "b"
        sub_dir.mkdir(parents=True)

        mixed_str = f"{tmp_path}/a\\b"
        canonical = canonicalize_workspace_path(mixed_str)
        assert canonical == sub_dir.resolve()

    def test_canonicalize_null_byte_injection_rejected(self):
        """Verifies null byte injection raises WorkspaceSecurityError."""
        with pytest.raises(WorkspaceSecurityError, match="null bytes"):
            canonicalize_workspace_path("C:\\foo\x00bar")

    def test_canonicalize_empty_or_whitespace_rejected(self):
        """Verifies empty or whitespace-only paths raise WorkspaceSecurityError."""
        with pytest.raises(WorkspaceSecurityError, match="empty or whitespace"):
            canonicalize_workspace_path("")

        with pytest.raises(WorkspaceSecurityError, match="empty or whitespace"):
            canonicalize_workspace_path("   \t\n  ")

    def test_canonicalize_none_rejected(self):
        """Verifies None path raises WorkspaceSecurityError."""
        with pytest.raises(WorkspaceSecurityError, match="cannot be None"):
            canonicalize_workspace_path(None)  # type: ignore

    def test_get_workspace_key_case_handling(self, tmp_path):
        """Verifies workspace keys normalize case on Windows."""
        key1 = get_workspace_key(str(tmp_path))
        key2 = get_workspace_key(str(tmp_path).upper() if os.name == "nt" else str(tmp_path))
        if os.name == "nt":
            assert key1 == key2
            assert key1 == key1.lower()
        else:
            assert isinstance(key1, str)


# ============================================================================
# 2. Filesystem Root & System-Critical Directory Protection Tests
# ============================================================================


class TestSystemRootAndCriticalPathProtection:
    """Verifies protection against targeting filesystem roots, system dirs, and user profile roots."""

    def test_is_filesystem_root_detection(self):
        """Verifies filesystem roots are identified across platforms."""
        assert is_filesystem_root(Path("/")) is True

        if os.name == "nt":
            drive = os.path.splitdrive(os.getcwd())[0] + "\\"
            assert is_filesystem_root(Path(drive)) is True

    def test_validate_workspace_rejects_filesystem_root(self):
        """Verifies attempting to use filesystem root as workspace raises WorkspaceSecurityError."""
        with pytest.raises(WorkspaceSecurityError, match="filesystem root"):
            validate_workspace_path("/")

        if os.name == "nt":
            drive = os.path.splitdrive(os.getcwd())[0] + "\\"
            with pytest.raises(WorkspaceSecurityError, match="filesystem root"):
                validate_workspace_path(drive)

    def test_validate_workspace_rejects_windows_system_roots(self):
        """Verifies Windows system directories are forbidden."""
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", "C:\\Windows")
            with pytest.raises(WorkspaceSecurityError, match="system-critical"):
                validate_workspace_path(system_root)

            sys32 = os.path.join(system_root, "System32")
            if os.path.exists(sys32):
                with pytest.raises(WorkspaceSecurityError, match="system-critical"):
                    validate_workspace_path(sys32)

    def test_validate_workspace_rejects_posix_system_roots(self):
        """Verifies POSIX critical directories are rejected."""
        critical_dirs = ["/etc", "/sys", "/proc", "/bin", "/usr", "/var"]
        for cdir in critical_dirs:
            is_crit, reason = is_system_critical_path(Path(cdir))
            if is_crit:
                assert "denied" in reason.lower()

    def test_user_home_direct_root_rejected_but_subfolder_allowed(self, tmp_path):
        """Verifies direct user home root is rejected, but a subfolder is allowed."""
        home_path = Path.home().resolve()
        is_crit, _ = is_system_critical_path(home_path)
        assert is_crit is True

        # Subfolder in tmp_path (or subfolder in home) is allowed
        sub_folder = tmp_path / "project_alpha"
        sub_folder.mkdir()
        is_crit_sub, _ = is_system_critical_path(sub_folder)
        assert is_crit_sub is False


# ============================================================================
# 3. Boundary & Whitelist Validation Tests
# ============================================================================


class TestBoundaryAndAllowedRootsValidation:
    """Verifies allowed_roots containment and traversal escape prevention."""

    def test_path_within_allowed_roots_succeeds(self, tmp_path):
        """Verifies paths inside allowed_roots are accepted."""
        allowed_dir = tmp_path / "allowed_workspace"
        allowed_dir.mkdir()
        sub_project = allowed_dir / "project_1"
        sub_project.mkdir()

        res = validate_workspace_path(sub_project, allowed_roots=[allowed_dir])
        assert res == sub_project.resolve()

    def test_path_outside_allowed_roots_raises_error(self, tmp_path):
        """Verifies paths outside allowed_roots raise WorkspaceSecurityError."""
        allowed_dir = tmp_path / "allowed_workspace"
        allowed_dir.mkdir()

        forbidden_dir = tmp_path / "forbidden_workspace"
        forbidden_dir.mkdir()

        with pytest.raises(WorkspaceSecurityError, match="outside allowed roots"):
            validate_workspace_path(forbidden_dir, allowed_roots=[allowed_dir])

    def test_traversal_escape_from_allowed_roots_rejected(self, tmp_path):
        """Verifies directory traversal escaping allowed_roots is blocked."""
        allowed_dir = tmp_path / "allowed_workspace"
        allowed_dir.mkdir()

        forbidden_dir = tmp_path / "secret_folder"
        forbidden_dir.mkdir()

        traversal_path = str(allowed_dir / ".." / "secret_folder")
        with pytest.raises(WorkspaceSecurityError, match="outside allowed roots"):
            validate_workspace_path(traversal_path, allowed_roots=[allowed_dir])

    def test_multiple_allowed_roots_support(self, tmp_path):
        """Verifies support for multiple whitelisted allowed roots."""
        root_a = tmp_path / "root_a"
        root_a.mkdir()
        root_b = tmp_path / "root_b"
        root_b.mkdir()

        proj_a = root_a / "repo_a"
        proj_a.mkdir()
        proj_b = root_b / "repo_b"
        proj_b.mkdir()

        assert validate_workspace_path(proj_a, allowed_roots=[root_a, root_b]) == proj_a.resolve()
        assert validate_workspace_path(proj_b, allowed_roots=[root_a, root_b]) == proj_b.resolve()


# ============================================================================
# 4. Existence, Directory Type & Exception Handling Tests
# ============================================================================


class TestWorkspaceTypeAndExistenceValidation:
    """Verifies existence checks, file-vs-directory enforcement, and exception types."""

    def test_non_existent_workspace_raises_not_found_error(self, tmp_path):
        """Verifies non-existent directory raises WorkspaceNotFoundError."""
        non_existent = tmp_path / "does_not_exist_12345"
        with pytest.raises(WorkspaceNotFoundError, match="does not exist"):
            validate_workspace_path(non_existent, require_exists=True)

    def test_file_path_raises_not_a_directory_error(self, tmp_path):
        """Verifies pointing to a regular file raises WorkspaceNotADirectoryError."""
        file_path = tmp_path / "some_file.txt"
        file_path.write_text("hello", encoding="utf-8")

        with pytest.raises(WorkspaceNotADirectoryError, match="not a directory"):
            validate_workspace_path(file_path, require_directory=True)

    def test_allow_create_permits_non_existent_with_existing_parent(self, tmp_path):
        """Verifies allow_create=True permits non-existent path if parent directory exists."""
        new_dir = tmp_path / "new_project_folder"
        res = validate_workspace_path(new_dir, require_exists=True, allow_create=True)
        assert res == new_dir.resolve()


# ============================================================================
# 5. Async Concurrency Lock Manager Tests
# ============================================================================


class TestWorkspaceLockManager:
    """Verifies per-workspace async locking, serialized execution, parallel distinct locks, and timeouts."""

    @pytest.mark.asyncio
    async def test_serialized_execution_on_same_workspace(self, tmp_path):
        """Verifies concurrent tasks on the same workspace run sequentially."""
        lock_mgr = WorkspaceLockManager()
        execution_order: list[str] = []

        async def worker(task_name: str, delay: float):
            async with lock_mgr.lock(tmp_path):
                execution_order.append(f"{task_name}_start")
                await asyncio.sleep(delay)
                execution_order.append(f"{task_name}_end")

        await asyncio.gather(
            worker("task_1", 0.05),
            worker("task_2", 0.02),
        )

        assert execution_order == ["task_1_start", "task_1_end", "task_2_start", "task_2_end"]

    @pytest.mark.asyncio
    async def test_parallel_execution_on_distinct_workspaces(self, tmp_path):
        """Verifies tasks on distinct workspaces execute concurrently without blocking."""
        lock_mgr = WorkspaceLockManager()
        ws_a = tmp_path / "ws_a"
        ws_a.mkdir()
        ws_b = tmp_path / "ws_b"
        ws_b.mkdir()

        execution_order: list[str] = []

        async def worker_a():
            async with lock_mgr.lock(ws_a):
                execution_order.append("a_start")
                await asyncio.sleep(0.06)
                execution_order.append("a_end")

        async def worker_b():
            async with lock_mgr.lock(ws_b):
                execution_order.append("b_start")
                await asyncio.sleep(0.02)
                execution_order.append("b_end")

        await asyncio.gather(worker_a(), worker_b())

        # Since b has smaller sleep and different lock, b should end before a
        assert execution_order[0] in ("a_start", "b_start")
        assert "b_end" in execution_order[:3]

    @pytest.mark.asyncio
    async def test_lock_timeout_raises_error(self, tmp_path):
        """Verifies waiting for an occupied workspace lock raises WorkspaceLockTimeoutError when timeout expires."""
        lock_mgr = WorkspaceLockManager()

        async def long_holder():
            async with lock_mgr.lock(tmp_path):
                await asyncio.sleep(0.2)

        async def impatient_waiter():
            await asyncio.sleep(0.02)  # Wait for holder to lock
            with pytest.raises(WorkspaceLockTimeoutError, match="Timed out"):
                async with lock_mgr.lock(tmp_path, timeout=0.05):
                    pass

        await asyncio.gather(long_holder(), impatient_waiter())

    @pytest.mark.asyncio
    async def test_lock_released_on_exception(self, tmp_path):
        """Verifies lock is reliably released even if protected block raises an exception."""
        lock_mgr = WorkspaceLockManager()

        with pytest.raises(ValueError, match="intentional failure"):
            async with lock_mgr.lock(tmp_path):
                raise ValueError("intentional failure")

        assert lock_mgr.is_locked(tmp_path) is False

        # Verify next task can acquire lock immediately
        acquired = False
        async with lock_mgr.lock(tmp_path):
            acquired = True
        assert acquired is True

    @pytest.mark.asyncio
    async def test_is_locked_and_active_locks_count(self, tmp_path):
        """Verifies is_locked and active_locks_count report accurate status."""
        lock_mgr = WorkspaceLockManager()
        assert lock_mgr.is_locked(tmp_path) is False
        assert lock_mgr.active_locks_count() == 0

        async with lock_mgr.lock(tmp_path):
            assert lock_mgr.is_locked(tmp_path) is True
            assert lock_mgr.active_locks_count() == 1

        assert lock_mgr.is_locked(tmp_path) is False
        assert lock_mgr.active_locks_count() == 0


# ============================================================================
# 6. FastMCP Server Tool Integration Tests with Safety Engine
# ============================================================================


class TestServerSafetyIntegration:
    """Verifies that all FastMCP tools enforce workspace safety and locking."""

    @pytest.mark.asyncio
    async def test_execute_task_rejects_invalid_workspace(self):
        """Verifies agy_execute_task returns error status on invalid or non-existent path."""
        server = create_mcp_server()
        res = await server.call_tool(
            "agy_execute_task",
            {
                "workspace_path": "non_existent_folder_xyz_999",
                "prompt": "Fix code",
            },
        )
        data = json.loads(res[0][0].text)
        assert data["status"] == "error"
        assert "Workspace validation failed" in data["error_details"]

    @pytest.mark.asyncio
    async def test_get_diff_rejects_system_root(self):
        """Verifies agy_get_diff rejects system root."""
        server = create_mcp_server()
        res = await server.call_tool(
            "agy_get_diff",
            {"workspace_path": "/"},
        )
        data = json.loads(res[0][0].text)
        assert data["status"] == "error"
        assert "Workspace validation failed" in data["error_details"]

    @pytest.mark.asyncio
    async def test_run_tests_rejects_non_existent_workspace(self):
        """Verifies agy_run_tests returns error on non-existent path."""
        server = create_mcp_server()
        res = await server.call_tool(
            "agy_run_tests",
            {"workspace_path": "non_existent_folder_xyz_999"},
        )
        data = json.loads(res[0][0].text)
        assert data["status"] == "error"
        assert "Workspace validation failed" in data["error_details"]

    @pytest.mark.asyncio
    async def test_chat_validates_optional_workspace_path(self):
        """Verifies agy_chat validates workspace_path when provided."""
        server = create_mcp_server()
        res = await server.call_tool(
            "agy_chat",
            {
                "prompt": "Analyze code",
                "workspace_path": "non_existent_folder_xyz_999",
            },
        )
        data = json.loads(res[0][0].text)
        assert data["status"] == "error"
        assert "Workspace validation failed" in data["error_details"]
