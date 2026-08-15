"""Master Pytest Configuration and Test Infrastructure for FastMCP AGY E2E Test Suite.

Provides:
- EphemeralWorkspace: Isolated temporary directory with robust Windows leak-free teardown
  (handling read-only git files via stat.S_IWRITE), git initialization, and filesystem snapshotting.
- Pytest workspace fixtures for Git states (unborn branch, clean, staged, unstaged, dirty, binary, large diffs).
- Pytest workspace fixtures for multi-ecosystem test runners (pytest, unittest, npm, cargo, hanging/timeout, custom).
- ProgrammableMockAGYBackend with synthetic file edits, token telemetry, and SHA-256 snapshot comparisons.
- Async MCP client fixtures: in-memory AnyIO stream client_factory and subprocess stdio_client_factory.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, AsyncGenerator, Callable, Dict, Generator, List, Optional, Tuple, Union
import uuid

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

# Ensure `src` is in sys.path for direct imports without editable install
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Isolate test suite from live agy.EXE subprocesses by defaulting to mock backend
os.environ.setdefault("MCP_AGY_BACKEND", "mock")

from mcp_agy.core.backend import AGYBackend, MockAGYBackend, set_backend, reset_backend
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


# ============================================================================
# Pytest Markers & Configuration
# ============================================================================

def pytest_configure(config: pytest.Config) -> None:
    """Register custom pytest markers for E2E testing tiers and configure test environment."""
    os.environ.setdefault("MCP_AGY_BACKEND", "mock")
    config.addinivalue_line("markers", "tier1: Tier 1 Feature Coverage test cases")
    config.addinivalue_line("markers", "tier2: Tier 2 Boundary & Corner Case test cases")
    config.addinivalue_line("markers", "tier3: Tier 3 Cross-Feature Combination test cases")
    config.addinivalue_line("markers", "tier4: Tier 4 Real-World Scenario test cases")
    config.addinivalue_line("markers", "live: Live integration tests against real agy binary")
    config.addinivalue_line("markers", "slow: Tests that take more than 2 seconds to execute")


# ============================================================================
# Ephemeral Workspace Helper
# ============================================================================

class EphemeralWorkspace:
    """Encapsulates an isolated ephemeral workspace directory for E2E testing.

    Features:
    - Path normalization and canonical resolution.
    - Path protocol compatibility (__fspath__, __str__, __truediv__).
    - Text and binary file creation, reading, and deletion.
    - Git repository initialization and command execution.
    - SHA-256 filesystem snapshot capture and verification for read-only invariants.
    - Robust Windows leak-free teardown handling read-only git objects (stat.S_IWRITE).
    """

    def __init__(self, prefix: str = "mcp_agy_test_") -> None:
        self.raw_path = tempfile.mkdtemp(prefix=prefix)
        self.path = Path(self.raw_path).resolve()
        self.str_path = str(self.path)

    def __str__(self) -> str:
        return self.str_path

    def __fspath__(self) -> str:
        return self.str_path

    def __truediv__(self, other: Union[str, Path]) -> Path:
        return self.path / other

    def write_file(
        self,
        rel_path: Union[str, Path],
        content: Union[str, bytes],
        encoding: str = "utf-8",
    ) -> Path:
        """Write text or binary content to a relative path inside the workspace."""
        file_path = (self.path / rel_path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            file_path.write_bytes(content)
        else:
            file_path.write_text(content, encoding=encoding)
        return file_path

    def read_file(self, rel_path: Union[str, Path], encoding: str = "utf-8") -> str:
        """Read text content from a relative path inside the workspace."""
        file_path = (self.path / rel_path).resolve()
        return file_path.read_text(encoding=encoding)

    def read_bytes(self, rel_path: Union[str, Path]) -> bytes:
        """Read binary content from a relative path inside the workspace."""
        file_path = (self.path / rel_path).resolve()
        return file_path.read_bytes()

    def delete_file(self, rel_path: Union[str, Path]) -> None:
        """Delete a file inside the workspace if it exists."""
        file_path = (self.path / rel_path).resolve()
        if file_path.is_file() or file_path.is_symlink():
            try:
                os.chmod(file_path, stat.S_IWRITE)
            except Exception:
                pass
            file_path.unlink(missing_ok=True)
        elif file_path.is_dir():
            shutil.rmtree(file_path, ignore_errors=True)

    def exists(self, rel_path: Union[str, Path]) -> bool:
        """Check whether a relative path exists inside the workspace."""
        return (self.path / rel_path).exists()

    def list_files(self, include_git: bool = False) -> List[str]:
        """List all relative file paths currently existing inside the workspace."""
        rel_paths = []
        for root, dirs, files in os.walk(self.path):
            if not include_git and ".git" in dirs:
                dirs.remove(".git")
            for file in files:
                abs_f = Path(root) / file
                rel_paths.append(str(abs_f.relative_to(self.path)).replace("\\", "/"))
        return sorted(rel_paths)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run a git command inside the workspace."""
        git_cmd = shutil.which("git") or "git"
        return subprocess.run(
            [git_cmd, "-C", self.str_path] + list(args),
            capture_output=True,
            text=True,
            check=False,
        )

    def init_git(
        self,
        user_name: str = "Test User",
        user_email: str = "test@example.com",
        initial_commit: bool = True,
        initial_file: str = "README.md",
        initial_content: str = "# Ephemeral Workspace\n",
    ) -> None:
        """Initialize a git repository with local user identity and optional initial commit."""
        self.git("init")
        self.git("config", "user.name", user_name)
        self.git("config", "user.email", user_email)
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

        if initial_commit:
            self.write_file(initial_file, initial_content)
            self.git("add", initial_file)
            self.git("commit", "-m", "Initial commit")

    def stage(self, *rel_paths: str) -> None:
        """Stage one or more relative file paths in git."""
        self.git("add", *rel_paths)

    def commit(self, message: str) -> None:
        """Commit staged changes in git."""
        self.git("commit", "-m", message)

    def snapshot_hashes(self, include_git: bool = False) -> Dict[str, str]:
        """Compute SHA-256 hashes of all non-git files in the workspace.

        Returns:
            Dict mapping normalized relative file paths to their SHA-256 hex digests.
        """
        snapshots: Dict[str, str] = {}
        for root, dirs, files in os.walk(self.path):
            if not include_git and ".git" in dirs:
                dirs.remove(".git")
            for file in files:
                full_path = Path(root) / file
                rel_path = str(full_path.relative_to(self.path)).replace("\\", "/")
                try:
                    content = full_path.read_bytes()
                    snapshots[rel_path] = hashlib.sha256(content).hexdigest()
                except Exception:
                    pass
        return snapshots

    def verify_snapshot_unchanged(
        self, initial_snapshot: Dict[str, str], include_git: bool = False
    ) -> Tuple[bool, str]:
        """Verify that the current workspace state is identical to an earlier snapshot.

        Args:
            initial_snapshot: Output from earlier snapshot_hashes() call.
            include_git: Whether to include .git directory in verification.

        Returns:
            Tuple of (is_unchanged: bool, diff_description: str).
        """
        current_snapshot = self.snapshot_hashes(include_git=include_git)
        initial_keys = set(initial_snapshot.keys())
        current_keys = set(current_snapshot.keys())

        added = current_keys - initial_keys
        removed = initial_keys - current_keys
        common = initial_keys & current_keys
        modified = {k for k in common if initial_snapshot[k] != current_snapshot[k]}

        if not added and not removed and not modified:
            return True, "Workspace is completely unmodified."

        diffs = []
        if added:
            diffs.append(f"Added files: {sorted(added)}")
        if removed:
            diffs.append(f"Removed files: {sorted(removed)}")
        if modified:
            diffs.append(f"Modified files: {sorted(modified)}")

        return False, "; ".join(diffs)

    def cleanup(self) -> None:
        """Robustly delete the workspace directory, handling Windows read-only file locks and Git objects."""
        if not os.path.exists(self.raw_path):
            return

        def _handle_remove_readonly(func: Any, path_str: str, exc_info: Any) -> None:
            try:
                os.chmod(path_str, stat.S_IWRITE)
                func(path_str)
            except Exception:
                pass

        # Try up to 3 times to account for transient Windows file locks
        for attempt in range(3):
            try:
                if sys.version_info >= (3, 12):
                    def _onexc(func: Any, path_str: str, exc: Any) -> None:
                        try:
                            os.chmod(path_str, stat.S_IWRITE)
                            func(path_str)
                        except Exception:
                            pass
                    shutil.rmtree(self.raw_path, onexc=_onexc)
                else:
                    shutil.rmtree(self.raw_path, onerror=_handle_remove_readonly)
                break
            except Exception:
                if attempt < 2:
                    time.sleep(0.05)
                else:
                    shutil.rmtree(self.raw_path, ignore_errors=True)

    def __enter__(self) -> "EphemeralWorkspace":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cleanup()


# ============================================================================
# Programmable Mock AGY Backend
# ============================================================================

class ProgrammableMockAGYBackend(MockAGYBackend):
    """Rich, programmable in-memory AGY backend simulator for deterministic E2E testing.

    Features:
    - Synthetic file modifications on disk when mode='accept-edits' (allows agy_get_diff
      and agy_run_tests to verify real downstream effects).
    - Chat read-only invariant (never touches workspace files in chat mode).
    - Customizable rule table matching prompt keywords to synthetic file actions.
    - Controllable failure injection: force_error, force_timeout, simulated_delay.
    - Realistic token telemetry computation based on prompt length and actions.
    - Full call history recording for test assertions.
    """

    def __init__(
        self,
        default_response: str = "Mock AGY execution completed successfully.",
        simulated_delay: float = 0.0,
        token_usage_override: Optional[TokenUsage] = None,
        auto_synthesize_edits: bool = True,
    ) -> None:
        super().__init__(default_response=default_response, simulated_delay=simulated_delay)
        self.token_usage_override = token_usage_override
        self.auto_synthesize_edits = auto_synthesize_edits
        self.force_timeout: bool = False
        self.force_error: Optional[str] = None
        self.call_history: List[Dict[str, Any]] = []
        self.custom_task_handlers: List[Callable[..., Optional[TaskExecutionResult]]] = []
        self.custom_chat_handlers: List[Callable[..., Optional[ChatResult]]] = []
        self.synthetic_file_rules: List[Tuple[str, str, str]] = []  # (keyword, rel_path, content)

        # Default rules for standard coding tasks
        self.add_task_rule(
            "calculator",
            "calculator.py",
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def subtract(a: int, b: int) -> int:\n    return a - b\n",
        )
        self.add_task_rule(
            "auth",
            "src/auth.py",
            "class AuthService:\n    def authenticate(self, user: str, token: str) -> bool:\n        return token == 'valid_token'\n",
        )
        self.add_task_rule(
            "math",
            "math_utils.py",
            "def multiply(a: float, b: float) -> float:\n    return a * b\n\n"
            "def divide(a: float, b: float) -> float:\n    if b == 0:\n        raise ZeroDivisionError('division by zero')\n    return a / b\n",
        )

    def add_task_rule(self, keyword: str, rel_path: str, content: str) -> None:
        """Register a keyword-triggered synthetic file creation rule for task executions."""
        self.synthetic_file_rules.append((keyword.lower(), rel_path, content))

    def register_task_handler(self, handler: Callable[..., Optional[TaskExecutionResult]]) -> None:
        """Register a custom callback handler for execute_task."""
        self.custom_task_handlers.append(handler)

    def register_chat_handler(self, handler: Callable[..., Optional[ChatResult]]) -> None:
        """Register a custom callback handler for chat."""
        self.custom_chat_handlers.append(handler)

    def reset(self) -> None:
        """Reset history, custom handlers, and failure injection flags."""
        self.call_history.clear()
        self.executed_tasks.clear()
        self.chat_history.clear()
        self.custom_task_handlers.clear()
        self.custom_chat_handlers.clear()
        self.force_timeout = False
        self.force_error = None
        self.simulated_delay = 0.0

    async def execute_task(
        self,
        workspace_path: str,
        prompt: str,
        auto_approve: bool = True,
        mode: str = "accept-edits",
        timeout_seconds: int = 600,
    ) -> TaskExecutionResult:
        """Execute a simulated coding task with authentic file synthesis and telemetry."""
        start_time = time.monotonic()
        conv_id = f"mock-task-{uuid.uuid4().hex[:8]}"

        call_record = {
            "method": "execute_task",
            "conversation_id": conv_id,
            "workspace_path": workspace_path,
            "prompt": prompt,
            "auto_approve": auto_approve,
            "mode": mode,
            "timeout_seconds": timeout_seconds,
            "timestamp": time.time(),
        }
        self.call_history.append(call_record)
        self.executed_tasks.append(call_record)

        # Custom handler override
        for handler in self.custom_task_handlers:
            custom_res = handler(
                workspace_path=workspace_path,
                prompt=prompt,
                auto_approve=auto_approve,
                mode=mode,
                timeout_seconds=timeout_seconds,
            )
            if custom_res is not None:
                return custom_res

        if self.simulated_delay > 0:
            await asyncio.sleep(self.simulated_delay)

        if self.force_timeout:
            await asyncio.sleep(timeout_seconds + 1)
            return TaskExecutionResult(
                status="timeout",
                conversation_id=conv_id,
                response="",
                error_details=f"Task execution timed out after {timeout_seconds}s",
            )

        if self.force_error:
            return TaskExecutionResult(
                status="error",
                conversation_id=conv_id,
                response="",
                error_details=self.force_error,
            )

        modified_files: List[str] = []

        # File synthesis when mode is 'accept-edits' and workspace path exists
        if mode == "accept-edits" and self.auto_synthesize_edits and os.path.isdir(workspace_path):
            prompt_lower = prompt.lower()
            ws_path = Path(workspace_path)

            for kw, rel_path, content in self.synthetic_file_rules:
                if kw in prompt_lower:
                    target_file = ws_path / rel_path
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_text(content, encoding="utf-8")
                    norm_rel = str(rel_path).replace("\\", "/")
                    if norm_rel not in modified_files:
                        modified_files.append(norm_rel)

            # Fallback heuristic: If prompt explicitly asks to "create <filename>" or "write <filename>"
            matches = re.findall(r"(?:create|implement|write|add|generate)\s+([a-zA-Z0-9_\-/\\]+\.[a-zA-Z0-9]+)", prompt, re.IGNORECASE)
            for filename in matches:
                target_file = ws_path / filename
                if not target_file.exists():
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_text(f"# Generated by AGY for task: {prompt}\n", encoding="utf-8")
                    norm_rel = str(filename).replace("\\", "/")
                    if norm_rel not in modified_files:
                        modified_files.append(norm_rel)

        duration = time.monotonic() - start_time + (self.simulated_delay or 0.02)
        diff_summary = f"Modified {len(modified_files)} file(s)" if modified_files else "No files modified"
        response_text = f"Task completed: {prompt}\nWorkspace: {workspace_path}\nFiles modified: {modified_files}"

        words = len(prompt.split())
        tokens = self.token_usage_override or TokenUsage(
            input_tokens=max(15, words * 4),
            output_tokens=max(30, len(response_text.split()) * 2),
            thinking_tokens=20,
            cache_read_tokens=0,
            total_tokens=max(65, words * 4 + len(response_text.split()) * 2 + 20),
        )

        return TaskExecutionResult(
            status="success",
            conversation_id=conv_id,
            response=response_text,
            modified_files=modified_files,
            diff_summary=diff_summary,
            duration_seconds=round(duration, 4),
            tokens_used=tokens,
            backend_used="mock",
            error_details=None,
        )

    async def chat(
        self,
        prompt: str,
        workspace_path: str = "",
        conversation_id: str = "",
        timeout_seconds: int = 300,
    ) -> ChatResult:
        """Execute a simulated read-only chat consultation with strict filesystem isolation."""
        start_time = time.monotonic()
        conv_id = conversation_id or f"mock-chat-{uuid.uuid4().hex[:8]}"

        call_record = {
            "method": "chat",
            "conversation_id": conv_id,
            "workspace_path": workspace_path,
            "prompt": prompt,
            "timeout_seconds": timeout_seconds,
            "timestamp": time.time(),
        }
        self.call_history.append(call_record)
        self.chat_history.append(call_record)

        # Custom handler override
        for handler in self.custom_chat_handlers:
            custom_res = handler(
                prompt=prompt,
                workspace_path=workspace_path,
                conversation_id=conversation_id,
                timeout_seconds=timeout_seconds,
            )
            if custom_res is not None:
                return custom_res

        if self.simulated_delay > 0:
            await asyncio.sleep(self.simulated_delay)

        if self.force_timeout:
            await asyncio.sleep(timeout_seconds + 1)
            return ChatResult(
                status="timeout",
                conversation_id=conv_id,
                response="",
                error_details=f"Chat consultation timed out after {timeout_seconds}s",
            )

        if self.force_error:
            return ChatResult(
                status="error",
                conversation_id=conv_id,
                response="",
                error_details=self.force_error,
            )

        duration = time.monotonic() - start_time + (self.simulated_delay or 0.01)
        response_text = f"Mock AGY analytical guidance for query: '{prompt}'. Workspace context: '{workspace_path or 'none'}'."

        words = len(prompt.split())
        tokens = self.token_usage_override or TokenUsage(
            input_tokens=max(10, words * 3),
            output_tokens=max(20, len(response_text.split()) * 2),
            thinking_tokens=15,
            cache_read_tokens=0,
            total_tokens=max(45, words * 3 + len(response_text.split()) * 2 + 15),
        )

        return ChatResult(
            status="success",
            conversation_id=conv_id,
            response=response_text,
            duration_seconds=round(duration, 4),
            tokens_used=tokens,
            backend_used="mock",
            error_details=None,
        )


# ============================================================================
# Core Pytest Fixtures (Mock Backend & FastMCP Server)
# ============================================================================

@pytest.fixture
def mock_backend() -> Generator[ProgrammableMockAGYBackend, None, None]:
    """Provides a clean, programmable Mock AGY Backend for deterministic testing."""
    backend = ProgrammableMockAGYBackend()
    set_backend(backend)
    yield backend
    backend.reset()
    reset_backend()


@pytest.fixture
def test_server(mock_backend: ProgrammableMockAGYBackend) -> Any:
    """Provides a FastMCP server instance configured with the mock backend."""
    return create_server(backend=mock_backend)


@pytest.fixture
def fastmcp_server(test_server: Any) -> Any:
    """Alias for test_server."""
    return test_server


# ============================================================================
# FastMCP Test Client Fixtures (In-Memory AnyIO Stream & Subprocess Stdio)
# ============================================================================

@pytest.fixture
def client_factory():
    """Async context manager factory for in-memory AnyIO stream ClientSession.

    Usage:
        async with client_factory(server) as session:
            tools = await session.list_tools()
            result = await session.call_tool("agy_chat", {"prompt": "hello"})
    """
    @asynccontextmanager
    async def _create_client(server_instance: Any) -> AsyncGenerator[ClientSession, None]:
        server_send, client_recv = anyio.create_memory_object_stream(10)
        client_send, server_recv = anyio.create_memory_object_stream(10)
        server_lowlevel = server_instance._mcp_server

        async with anyio.create_task_group() as tg:
            async def run_server() -> None:
                async with server_send, server_recv:
                    await server_lowlevel.run(
                        server_recv,
                        server_send,
                        server_lowlevel.create_initialization_options(),
                    )

            tg.start_soon(run_server)

            async with client_send, client_recv:
                async with ClientSession(client_recv, client_send) as session:
                    await session.initialize()
                    yield session

            tg.cancel_scope.cancel()

    return _create_client


@pytest.fixture
def stdio_client_factory():
    """Async context manager factory for subprocess stdio ClientSession.

    Usage:
        async with stdio_client_factory() as session:
            result = await session.call_tool("agy_execute_task", {...})
    """
    @asynccontextmanager
    async def _create_stdio_client(
        env_overrides: Optional[Dict[str, str]] = None,
        server_args: Optional[List[str]] = None,
    ) -> AsyncGenerator[ClientSession, None]:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(SRC_DIR)
        if env_overrides:
            env.update(env_overrides)

        args = server_args or ["-m", "mcp_agy.server"]

        params = StdioServerParameters(
            command=sys.executable,
            args=args,
            env=env,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    return _create_stdio_client


@pytest.fixture
async def mcp_client(test_server: Any, client_factory: Any) -> AsyncGenerator[ClientSession, None]:
    """Provides an active in-memory ClientSession already connected and initialized."""
    async with client_factory(test_server) as session:
        yield session


# ============================================================================
# Ephemeral Workspace Fixtures
# ============================================================================

@pytest.fixture
def ephemeral_workspace() -> Generator[EphemeralWorkspace, None, None]:
    """Yields a clean, empty EphemeralWorkspace with automatic teardown."""
    ws = EphemeralWorkspace()
    try:
        yield ws
    finally:
        ws.cleanup()


@pytest.fixture
def temp_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Alias fixture for ephemeral_workspace."""
    return ephemeral_workspace


@pytest.fixture
def clean_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a clean workspace directory containing sample non-git files."""
    ephemeral_workspace.write_file("main.py", "print('hello world')\n")
    ephemeral_workspace.write_file("config.json", '{"name": "test_app", "version": "1.0.0"}\n')
    return ephemeral_workspace


@pytest.fixture
def non_git_workspace(clean_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Alias for clean_workspace (directory without .git folder)."""
    return clean_workspace


# ============================================================================
# Git State Workspace Fixtures (for agy_get_diff & agy_execute_task)
# ============================================================================

@pytest.fixture
def unborn_git_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a git repository initialized via git init with 0 commits (unborn HEAD)."""
    ephemeral_workspace.init_git(initial_commit=False)
    ephemeral_workspace.write_file("staged_unborn.txt", "staged content on unborn branch\n")
    ephemeral_workspace.stage("staged_unborn.txt")
    ephemeral_workspace.write_file("untracked_unborn.txt", "untracked content on unborn branch\n")
    return ephemeral_workspace


@pytest.fixture
def clean_git_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a pristine git repository with an initial commit and no working tree changes."""
    ephemeral_workspace.init_git(initial_commit=True, initial_file="README.md", initial_content="# Test Repo\n")
    ephemeral_workspace.write_file("src/app.py", "def run():\n    return 42\n")
    ephemeral_workspace.stage("src/app.py")
    ephemeral_workspace.commit("Add initial src/app.py")
    return ephemeral_workspace


@pytest.fixture
def git_workspace(clean_git_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Alias for clean_git_workspace."""
    return clean_git_workspace


@pytest.fixture
def staged_changes_workspace(clean_git_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a git workspace with staged modifications and staged new files."""
    # Staged modification
    clean_git_workspace.write_file("src/app.py", "def run():\n    return 100  # modified\n")
    clean_git_workspace.stage("src/app.py")
    # Staged new file
    clean_git_workspace.write_file("src/utils.py", "def helper():\n    return True\n")
    clean_git_workspace.stage("src/utils.py")
    return clean_git_workspace


@pytest.fixture
def staged_git_workspace(staged_changes_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Alias for staged_changes_workspace."""
    return staged_changes_workspace


@pytest.fixture
def unstaged_changes_workspace(clean_git_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a git workspace with unstaged modifications to tracked files."""
    clean_git_workspace.write_file("src/app.py", "def run():\n    return 'unstaged_edit'\n")
    return clean_git_workspace


@pytest.fixture
def untracked_files_workspace(clean_git_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a git workspace with new untracked files and directories."""
    clean_git_workspace.write_file("new_feature.py", "# untracked feature file\n")
    clean_git_workspace.write_file("docs/guide.md", "# Guide\nUntracked documentation\n")
    return clean_git_workspace


@pytest.fixture
def dirty_git_workspace(clean_git_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a comprehensive dirty git repository combining all change categories:
    - Modified & Staged (M)
    - Modified & Unstaged (M)
    - Modified both Staged and Unstaged (MM)
    - Staged new file (A)
    - Untracked file (??)
    - Deleted tracked file (D)
    """
    ws = clean_git_workspace
    ws.write_file("src/file_mm.py", "original MM content\n")
    ws.write_file("src/to_delete.py", "delete me\n")
    ws.stage("src/file_mm.py", "src/to_delete.py")
    ws.commit("Setup tracked files for dirty workspace")

    # 1. Staged modification
    ws.write_file("src/app.py", "def run():\n    return 'staged modification'\n")
    ws.stage("src/app.py")

    # 2. Both Staged and Unstaged modification (MM)
    ws.write_file("src/file_mm.py", "staged MM content\n")
    ws.stage("src/file_mm.py")
    ws.write_file("src/file_mm.py", "final unstaged MM content\n")

    # 3. Staged new file (A)
    ws.write_file("src/new_staged.py", "# staged new file\n")
    ws.stage("src/new_staged.py")

    # 4. Untracked new file (??)
    ws.write_file("src/brand_new_untracked.py", "# untracked file\n")

    # 5. Deleted tracked file (D)
    ws.delete_file("src/to_delete.py")

    return ws


@pytest.fixture
def binary_git_workspace(clean_git_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a git workspace containing binary file modifications and untracked binary files."""
    ws = clean_git_workspace
    ws.write_file("assets/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01")
    ws.stage("assets/logo.png")
    ws.commit("Add initial binary logo")

    # Modify binary file
    ws.write_file("assets/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xff\xff\xff")
    # Add untracked binary file
    ws.write_file("assets/icon.bin", b"\x00\x01\x02\x03\x04\x05\x06\x07")
    return ws


@pytest.fixture
def large_diff_workspace(clean_git_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a git workspace with 5,000 modified lines to test diff truncation and limits."""
    ws = clean_git_workspace
    initial_lines = "\n".join([f"line_{i} = {i}" for i in range(5000)]) + "\n"
    ws.write_file("large_module.py", initial_lines)
    ws.stage("large_module.py")
    ws.commit("Add large module")

    # Modify all 5,000 lines
    modified_lines = "\n".join([f"line_{i} = {i * 10}" for i in range(5000)]) + "\n"
    ws.write_file("large_module.py", modified_lines)
    return ws


@pytest.fixture
def gitignore_workspace(clean_git_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a git workspace with .gitignore rules filtering out logs and build artifacts."""
    ws = clean_git_workspace
    ws.write_file(".gitignore", "*.log\nbuild/\n__pycache__/\n*.tmp\n")
    ws.stage(".gitignore")
    ws.commit("Add .gitignore")

    # Create ignored files
    ws.write_file("debug.log", "some log info\n")
    ws.write_file("build/output.o", "binary artifact\n")
    ws.write_file("temp.tmp", "temp artifact\n")
    # Create non-ignored untracked file
    ws.write_file("src/valid.py", "# valid untracked\n")
    return ws


# ============================================================================
# Multi-Ecosystem Test Workspace Fixtures (for agy_run_tests)
# ============================================================================

@pytest.fixture(name="pytest_passing_workspace")
def fixture_pytest_passing_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a workspace configured for Pytest where all tests pass."""
    ws = ephemeral_workspace
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")
    ws.write_file(
        "tests/test_math.py",
        "def test_addition():\n"
        "    assert 1 + 1 == 2\n\n"
        "def test_multiplication():\n"
        "    assert 2 * 3 == 6\n",
    )
    return ws


@pytest.fixture(name="pytest_workspace")
def fixture_pytest_workspace(pytest_passing_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Alias for pytest_passing_workspace."""
    return pytest_passing_workspace


@pytest.fixture(name="pytest_failing_workspace")
def fixture_pytest_failing_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a workspace configured for Pytest containing a failing test."""
    ws = ephemeral_workspace
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")
    ws.write_file(
        "tests/test_failing.py",
        "def test_success():\n"
        "    assert True\n\n"
        "def test_division_by_zero():\n"
        "    assert 1 / 0 == 1\n",
    )
    return ws


@pytest.fixture(name="pytest_mixed_workspace")
def fixture_pytest_mixed_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a workspace configured for Pytest with passed, failed, and skipped tests."""
    ws = ephemeral_workspace
    ws.write_file("pytest.ini", "[pytest]\npython_files = test_*.py\n")
    ws.write_file(
        "tests/test_mixed.py",
        "import pytest\n\n"
        "def test_pass_1():\n"
        "    assert True\n\n"
        "def test_pass_2():\n"
        "    assert 10 > 5\n\n"
        "def test_fail_1():\n"
        "    assert 1 == 2\n\n"
        "@pytest.mark.skip(reason='Work in progress')\n"
        "def test_skipped():\n"
        "    pass\n",
    )
    return ws


@pytest.fixture
def unittest_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a workspace configured with Python standard library unittest."""
    ws = ephemeral_workspace
    ws.write_file(
        "tests/test_suite.py",
        "import unittest\n\n"
        "class TestSample(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(2 + 2, 4)\n\n"
        "    def test_fail(self):\n"
        "        self.assertEqual('a', 'b')\n",
    )
    return ws


@pytest.fixture
def npm_passing_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a Node.js workspace with package.json and a passing test script."""
    ws = ephemeral_workspace
    ws.write_file("test.js", "console.log('PASS: test_node_suite'); process.exit(0);\n")
    pkg = {
        "name": "npm-pass-project",
        "version": "1.0.0",
        "scripts": {
            "test": "node test.js",
        },
    }
    ws.write_file("package.json", json.dumps(pkg, indent=2))
    return ws


@pytest.fixture
def npm_workspace(npm_passing_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Alias for npm_passing_workspace."""
    return npm_passing_workspace


@pytest.fixture
def npm_failing_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a Node.js workspace with package.json and a failing test script."""
    ws = ephemeral_workspace
    ws.write_file(
        "test.js",
        "console.error('FAIL: assertion failed expected true to equal false'); process.exit(1);\n",
    )
    pkg = {
        "name": "npm-fail-project",
        "version": "1.0.0",
        "scripts": {
            "test": "node test.js",
        },
    }
    ws.write_file("package.json", json.dumps(pkg, indent=2))
    return ws


@pytest.fixture
def cargo_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a Rust / Cargo workspace structure."""
    ws = ephemeral_workspace
    cargo_toml = (
        "[package]\n"
        'name = "mock_cargo_project"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
    )
    lib_rs = (
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\n\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "    #[test]\n"
        "    fn it_works() {\n"
        "        assert_eq!(add(2, 2), 4);\n"
        "    }\n"
        "}\n"
    )
    ws.write_file("Cargo.toml", cargo_toml)
    ws.write_file("src/lib.rs", lib_rs)
    return ws


@pytest.fixture
def hanging_test_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a workspace with a hanging test script to verify timeout handling and process tree killing."""
    ws = ephemeral_workspace
    ws.write_file("hang.py", "import time; time.sleep(60)\n")
    ws.write_file("pytest.ini", "[pytest]\n")
    return ws


@pytest.fixture
def custom_script_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a workspace with a custom test execution script."""
    ws = ephemeral_workspace
    ws.write_file(
        "run_custom_tests.py",
        "import sys\n"
        "print('Running custom test suite: 5 passed, 0 failed')\n"
        "sys.exit(0)\n",
    )
    return ws


@pytest.fixture
def no_framework_workspace(ephemeral_workspace: EphemeralWorkspace) -> EphemeralWorkspace:
    """Yields a workspace containing plain files with no recognized test framework configs."""
    ws = ephemeral_workspace
    ws.write_file("notes.txt", "Architecture meeting notes.\n")
    ws.write_file("data.csv", "id,name,role\n1,Alice,architect\n2,Bob,worker\n")
    return ws
