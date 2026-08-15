"""FastMCP Server implementation exposing Google Antigravity (AGY) tools.

This module registers the 4 primary tools for external AI architect agents:
- agy_execute_task: Autonomous coding execution in workspace
- agy_chat: Analytical / architectural consultation in read-only mode
- agy_get_diff: Git status and unified diff inspection
- agy_run_tests: Workspace test suite execution and diagnostics
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from mcp_agy import __version__
from mcp_agy.core.backend import AGYBackend, get_backend
from mcp_agy.core.diff_engine import GitDiffEngine, inspect_git_diff
from mcp_agy.core.models import (
    ChatResult,
    DiffResult,
    TaskExecutionResult,
    TestRunResult,
    TestSummary,
)
from mcp_agy.core.test_runner import MultiEcosystemTestRunner, execute_workspace_tests
from mcp_agy.utils.logger import configure_logging, get_logger
from mcp_agy.utils.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
    get_workspace_lock_manager,
    validate_workspace_path,
)

logger = get_logger("mcp_agy.server")

SERVER_NAME = "mcp-agy"
SERVER_INSTRUCTIONS = (
    "FastMCP server exposing Google Antigravity (AGY) as an autonomous coding worker "
    "for external AI architect agents (Claude Desktop, Cursor, Cline, Roo Code). "
    "Enables executing coding tasks, analytical discussions, git diff inspections, "
    "and automated test runner execution within specified project workspaces."
)


def create_mcp_server(
    backend: Optional[AGYBackend] = None,
    backend_executor: Optional[Callable[..., Any]] = None,
    chat_executor: Optional[Callable[..., Any]] = None,
    diff_executor: Optional[Callable[..., Any]] = None,
    test_executor: Optional[Callable[..., Any]] = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    debug: bool = False,
    log_level: str = "INFO",
) -> FastMCP:
    """Create and configure the FastMCP server with all 4 primary tools.

    Args:
        backend: Optional explicit AGYBackend instance to use.
        backend_executor: Optional custom override for task execution.
        chat_executor: Optional custom override for chat execution.
        diff_executor: Optional custom override for diff inspection.
        test_executor: Optional custom override for test running.
        host: Host interface binding (default: 127.0.0.1).
        port: Port number for network transports (default: 8000).
        debug: Enable debug mode on FastMCP instance (default: False).
        log_level: FastMCP logging verbosity level (default: "INFO").

    Returns:
        FastMCP: Configured FastMCP server instance.
    """
    server = FastMCP(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        debug=debug,
        log_level=log_level,
        host=host,
        port=port,
    )

    # FastMCP 1.x takes no `version`, so the low-level server falls back to the installed mcp
    # SDK version and every client reports this server as "mcp-agy v1.29.0". Stamping the real
    # package version makes the handshake say which mcp-agy build is answering — the only
    # version a user can act on when triaging a client-side bug report.
    server._mcp_server.version = __version__

    def _resolve_backend() -> AGYBackend:
        if backend is not None:
            return backend
        return get_backend()

    @server.tool(
        name="agy_execute_task",
        description=(
            "Executes an autonomous multi-step coding task in a target workspace using Google Antigravity (AGY).\n\n"
            "Spawns a specialized AGY autonomous coding agent inside the specified repository or project "
            "directory. AGY investigates the codebase, writes new files, modifies existing code, refactors "
            "modules, executes build or setup commands, and returns structured telemetry and execution results.\n\n"
            "Architect Guidance:\n"
            "- Use this tool when you need AGY to implement concrete features, fix bugs, refactor code, "
            "or perform multi-file code modifications in a workspace.\n"
            "- For read-only planning or architectural queries without modifying files, set mode='plan' "
            "or use the dedicated `agy_chat` tool.\n"
            "- Always provide clear, self-contained prompts with explicit requirements, constraints, "
            "file paths, and acceptance criteria.\n\n"
            "Args:\n"
            "    workspace_path: Absolute or relative filesystem path to the target repository. Must exist.\n"
            "    prompt: Clear, detailed instructions for AGY (e.g. 'Implement auth in src/auth.py and add pytest tests').\n"
            "    auto_approve: Automatically approve tool calls and file edits (default: True).\n"
            "    mode: Agent mode: 'accept-edits' for full coding/editing (default), or 'plan' for read-only plan generation.\n"
            "    timeout_seconds: Maximum execution time in seconds (default: 600s, min: 1, max: 3600).\n\n"
            "Returns:\n"
            "    TaskExecutionResult containing status, conversation_id, response, modified_files, diff_summary, duration_seconds, tokens_used, backend_used, and error_details."
        ),
    )
    async def agy_execute_task(
        workspace_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Absolute or relative filesystem path to the target workspace/repository where AGY will execute coding operations. Must be an existing directory.",
            ),
        ],
        prompt: Annotated[
            str,
            Field(
                min_length=1,
                description="Comprehensive natural language instructions detailing the coding task, requirements, expected file changes, architecture rules, or terminal commands for AGY to execute.",
            ),
        ],
        auto_approve: Annotated[
            bool,
            Field(
                description="When True (default), automatically approves all AGY tool executions (file creation, edits, terminal commands) without blocking for interactive user confirmation.",
            ),
        ] = True,
        mode: Annotated[
            Literal["accept-edits", "plan"],
            Field(
                description="Execution mode for AGY: 'accept-edits' (default) enables full autonomous coding with file creation and modification; 'plan' runs read-only analysis without making changes.",
            ),
        ] = "accept-edits",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=3600,
                description="Maximum execution duration in seconds before the task is cancelled (default: 600 seconds / 10 minutes). Minimum: 1, Maximum: 3600.",
            ),
        ] = 600,
        model: Annotated[
            str,
            Field(
                description="Optional model id for this task (e.g. 'gemini-3.7-flash-high', 'gemini-3.1-pro-high'). Empty uses MCP_AGY_MODEL, else the agy CLI default. Run `agy models` to list valid ids.",
            ),
        ] = "",
        effort: Annotated[
            str,
            Field(
                description="Optional reasoning effort for this task: 'low', 'medium', or 'high'. Empty uses MCP_AGY_EFFORT, else the agy CLI default.",
            ),
        ] = "",
    ) -> TaskExecutionResult:
        """Executes an autonomous multi-step coding task in a target workspace using Google Antigravity (AGY)."""
        logger.info(f"agy_execute_task invoked for workspace='{workspace_path}', mode='{mode}'")

        # Domain-level validation
        if not prompt.strip():
            logger.warning("agy_execute_task rejected: prompt is empty or whitespace only.")
            return TaskExecutionResult(
                status="error",
                response="",
                error_details="Prompt cannot be empty or whitespace only.",
            )

        if not workspace_path.strip():
            logger.warning("agy_execute_task rejected: workspace_path is empty or whitespace only.")
            return TaskExecutionResult(
                status="error",
                response="",
                error_details="Workspace path cannot be empty or whitespace only.",
            )

        try:
            validated_ws = validate_workspace_path(workspace_path)
        except WorkspaceError as exc:
            logger.warning(f"Workspace validation failed for '{workspace_path}': {exc}")
            return TaskExecutionResult(
                status="error",
                response="",
                error_details=f"Workspace validation failed: {exc}",
            )

        normalized_workspace = str(validated_ws)
        lock_mgr = get_workspace_lock_manager()

        # `model`/`effort` are added only when the caller named one, so a backend written
        # against the original signature keeps working unchanged. When one *is* named and the
        # backend cannot take it, the resulting TypeError is the right outcome: the alternative
        # is running the task on a different model than the architect asked for and saying
        # nothing.
        extra: dict[str, Any] = {}
        if model.strip():
            extra["model"] = model.strip()
        if effort.strip():
            extra["effort"] = effort.strip()

        async with lock_mgr.lock(validated_ws):
            if backend_executor is not None:
                return await backend_executor(
                    workspace_path=normalized_workspace,
                    prompt=prompt,
                    auto_approve=auto_approve,
                    mode=mode,
                    timeout_seconds=timeout_seconds,
                    **extra,
                )

            active_backend = _resolve_backend()
            return await active_backend.execute_task(
                workspace_path=normalized_workspace,
                prompt=prompt,
                auto_approve=auto_approve,
                mode=mode,
                timeout_seconds=timeout_seconds,
                **extra,
            )

    @server.tool(
        name="agy_chat",
        description=(
            "Submits an analytical, planning, or architectural question to Google Antigravity (AGY) without modifying files.\n\n"
            "Consults AGY as an expert reasoning partner. Ideal for repository exploration, code review, "
            "design discussions, architecture planning, and troubleshooting without making any edits to disk.\n\n"
            "Architect Guidance:\n"
            "- Use this tool when you want AGY's insights, code explanations, or architectural recommendations "
            "before commanding code modifications.\n"
            "- Pass `workspace_path` if the query pertains to a specific codebase on disk so AGY can inspect files. "
            "(In chat mode, AGY will strictly NOT edit or create files).\n"
            "- Pass `conversation_id` to continue an ongoing multi-turn dialogue session.\n\n"
            "Args:\n"
            "    prompt: The question, review request, or planning prompt.\n"
            "    workspace_path: Optional path to workspace directory for context.\n"
            "    conversation_id: Optional UUID of previous conversation to resume.\n"
            "    timeout_seconds: Maximum time to wait for response (default: 300s, min: 1, max: 1800).\n\n"
            "Returns:\n"
            "    ChatResult containing status, conversation_id, response, duration_seconds, tokens_used, backend_used, and error_details."
        ),
    )
    async def agy_chat(
        prompt: Annotated[
            str,
            Field(
                min_length=1,
                description="Natural language question, planning query, codebase analysis prompt, or architectural consultation for AGY.",
            ),
        ],
        workspace_path: Annotated[
            str,
            Field(
                description="Optional filesystem path to a workspace/repository to provide codebase context for analysis. If empty, AGY operates in general consultation mode.",
            ),
        ] = "",
        conversation_id: Annotated[
            str,
            Field(
                description="Optional conversation ID to resume or continue an ongoing multi-turn dialogue session with AGY. If empty, a new conversation is started.",
            ),
        ] = "",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=1800,
                description="Maximum consultation duration in seconds before timing out (default: 300 seconds / 5 minutes). Minimum: 1, Maximum: 1800.",
            ),
        ] = 300,
        model: Annotated[
            str,
            Field(
                description="Optional model id for this consultation (e.g. 'gemini-3.1-pro-high' for hard architectural questions). Empty uses MCP_AGY_MODEL, else the agy CLI default.",
            ),
        ] = "",
        effort: Annotated[
            str,
            Field(
                description="Optional reasoning effort: 'low', 'medium', or 'high'. Empty uses MCP_AGY_EFFORT, else the agy CLI default.",
            ),
        ] = "",
    ) -> ChatResult:
        """Submits an analytical, planning, or architectural question to Google Antigravity (AGY) without modifying files."""
        logger.info(f"agy_chat invoked with conversation_id='{conversation_id}'")

        if not prompt.strip():
            logger.warning("agy_chat rejected: prompt is empty or whitespace only.")
            return ChatResult(
                status="error",
                response="",
                error_details="Prompt cannot be empty or whitespace only.",
            )

        if workspace_path.strip():
            try:
                validated_ws = validate_workspace_path(workspace_path)
                normalized_workspace = str(validated_ws)
            except WorkspaceError as exc:
                logger.warning(f"Workspace validation failed for '{workspace_path}': {exc}")
                return ChatResult(
                    status="error",
                    response="",
                    error_details=f"Workspace validation failed: {exc}",
                )
        else:
            normalized_workspace = ""

        # Added only when named — see the note in agy_execute_task.
        extra: dict[str, Any] = {}
        if model.strip():
            extra["model"] = model.strip()
        if effort.strip():
            extra["effort"] = effort.strip()

        if chat_executor is not None:
            return await chat_executor(
                prompt=prompt,
                workspace_path=normalized_workspace,
                conversation_id=conversation_id,
                timeout_seconds=timeout_seconds,
                **extra,
            )

        active_backend = _resolve_backend()
        return await active_backend.chat(
            prompt=prompt,
            workspace_path=normalized_workspace,
            conversation_id=conversation_id,
            timeout_seconds=timeout_seconds,
            **extra,
        )

    @server.tool(
        name="agy_get_diff",
        description=(
            "Inspects git status and generates a unified diff of all changes in the target workspace.\n\n"
            "Captures all modifications made in the repository, including staged changes, unstaged edits "
            "in tracked files, and newly created untracked files. Provides structured file diff statistics "
            "(insertions, deletions, change types) and a complete unified diff.\n\n"
            "Architect Guidance:\n"
            "- Call this tool after `agy_execute_task` to verify exact code changes made by AGY before approving or proceeding.\n"
            "- Handles empty/unborn git repositories, untracked new files, and clean workspaces gracefully.\n"
            "- If the target folder is not a git repository, returns status 'not_a_git_repo' with a list of workspace files.\n\n"
            "Args:\n"
            "    workspace_path: Path to the workspace directory. Must exist.\n\n"
            "Returns:\n"
            "    DiffResult containing status ('success', 'error', 'not_a_git_repo'), has_changes, unified_diff, changed_files, untracked_files, summary, and error_details."
        ),
    )
    async def agy_get_diff(
        workspace_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Absolute or relative path to the git workspace directory to inspect for changes.",
            ),
        ],
    ) -> DiffResult:
        """Inspects git status and generates a unified diff of all changes in the target workspace."""
        logger.info(f"agy_get_diff invoked for workspace='{workspace_path}'")

        if not workspace_path.strip():
            logger.warning("agy_get_diff rejected: workspace_path is empty or whitespace only.")
            return DiffResult(
                status="error",
                summary="",
                error_details="Workspace path cannot be empty or whitespace only.",
            )

        try:
            validated_ws = validate_workspace_path(workspace_path)
        except WorkspaceNotFoundError as exc:
            return DiffResult(
                status="error",
                summary="Workspace directory does not exist",
                error_details=f"Workspace path '{workspace_path}' not found on disk: {exc}",
            )
        except WorkspaceError as exc:
            return DiffResult(
                status="error",
                summary="Workspace validation failed",
                error_details=f"Workspace validation failed: {exc}",
            )

        normalized_workspace = str(validated_ws)
        lock_mgr = get_workspace_lock_manager()

        async with lock_mgr.lock(validated_ws):
            if diff_executor is not None:
                return await diff_executor(workspace_path=normalized_workspace)

            diff_engine = GitDiffEngine()
            return await diff_engine.get_diff(workspace_path=normalized_workspace)

    @server.tool(
        name="agy_run_tests",
        description=(
            "Runs test suites in the target workspace and returns structured outcomes, summary counts, and failure diagnostics.\n\n"
            "Automatically detects installed test frameworks (pytest, unittest, npm/jest/vitest, cargo, "
            "go test, maven, gradle, dotnet, etc.) or runs a custom user-specified test command. "
            "Captures exit codes, total/passed/failed counts, test durations, and failure tracebacks.\n\n"
            "Architect Guidance:\n"
            "- Call this tool after AGY makes code changes to verify test pass rates and identify any regressions.\n"
            "- If `test_command` is omitted, the runner checks workspace configuration files (e.g. `pytest.ini`, "
            "`pyproject.toml`, `package.json`, `Cargo.toml`, etc.) and workspace virtualenvs to auto-run tests.\n"
            "- Sanitizes terminal ANSI color codes and captures failure locations for easy LLM debugging.\n\n"
            "Args:\n"
            "    workspace_path: Path to the workspace repository. Must exist.\n"
            "    test_command: Optional custom command string to run tests (e.g. 'pytest -k test_auth').\n"
            "    timeout_seconds: Maximum test run duration before killing test process tree (default: 300s, min: 1, max: 1800).\n\n"
            "Returns:\n"
            "    TestRunResult containing status ('passed', 'failed', 'error', 'timeout', 'no_framework_detected'), exit_code, framework, test_command_executed, output, summary, failures, duration_seconds, and error_details."
        ),
    )
    async def agy_run_tests(
        workspace_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Absolute or relative path to the target workspace directory where tests will be executed.",
            ),
        ],
        test_command: Annotated[
            str,
            Field(
                description="Optional explicit test command to execute (e.g. 'pytest tests/ -v', 'npm test', 'cargo test'). If empty, the test runner automatically detects the framework.",
            ),
        ] = "",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=1800,
                description="Maximum test suite execution time in seconds (default: 300 seconds / 5 minutes). Minimum: 1, Maximum: 1800.",
            ),
        ] = 300,
    ) -> TestRunResult:
        """Runs test suites in the target workspace and returns structured outcomes, summary counts, and failure diagnostics."""
        logger.info(f"agy_run_tests invoked for workspace='{workspace_path}', command='{test_command}'")

        if not workspace_path.strip():
            logger.warning("agy_run_tests rejected: workspace_path is empty or whitespace only.")
            return TestRunResult(
                status="error",
                summary=TestSummary(),
                error_details="Workspace path cannot be empty or whitespace only.",
            )

        try:
            validated_ws = validate_workspace_path(workspace_path)
        except WorkspaceError as exc:
            return TestRunResult(
                status="error",
                summary=TestSummary(),
                error_details=f"Workspace validation failed: {exc}",
            )

        normalized_workspace = str(validated_ws)
        lock_mgr = get_workspace_lock_manager()

        async with lock_mgr.lock(validated_ws):
            if test_executor is not None:
                return await test_executor(
                    workspace_path=normalized_workspace,
                    test_command=test_command,
                    timeout_seconds=timeout_seconds,
                )

            runner = MultiEcosystemTestRunner(default_timeout_seconds=timeout_seconds)
            return await runner.run_tests(
                workspace_path=normalized_workspace,
                test_command=test_command,
                timeout_seconds=timeout_seconds,
            )

    return server


# Module-level server instance
mcp = create_mcp_server()
server = mcp
create_server = create_mcp_server


def main() -> None:
    """CLI entrypoint for running the FastMCP server over stdio."""
    configure_logging()
    logger.info("Starting mcp-agy FastMCP server on stdio transport...")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
