"""FastMCP Server implementation exposing Google Antigravity (AGY) tools.

This module registers the tools available to external AI architect agents.

Synchronous - the call blocks until AGY is done, so they suit runs shorter than the client's
per-call timeout:
- agy_execute_task: Autonomous coding execution in workspace
- agy_chat: Analytical / architectural consultation in read-only mode
- agy_get_diff: Git status and unified diff inspection
- agy_run_tests: Workspace test suite execution and diagnostics

Background - the run outlives the call that started it, which is what makes a multi-minute
task possible at all against a client that caps a single call at 60 seconds:
- agy_start_task: Launch a run, return a job id immediately
- agy_job_status: Collect a job's result, optionally waiting a bounded number of seconds
- agy_cancel_job: Stop a run and kill its process tree
- agy_list_jobs: Recover job ids and see what is still running
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
from mcp_agy.core.jobs import JobRecord, get_job_manager
from mcp_agy.core.models import (
    ChatResult,
    DiffResult,
    JobHandle,
    JobListResult,
    JobStatusResult,
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
    """Create and configure the FastMCP server with all 8 tools.

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

        # `plan` cannot write, so it takes a reader lock: two investigations of one repo run
        # together instead of queueing. `accept-edits` stays exclusive.
        async with lock_mgr.lock(validated_ws, shared=(mode == "plan")):
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

        # Reading a diff cannot change the tree, so it shares: inspecting one repo while an
        # investigation runs against it is exactly the pairing an architect wants.
        async with lock_mgr.lock(validated_ws, shared=True):
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

    def _describe_job(job: JobRecord, include_result: bool = True) -> JobStatusResult:
        """Render a job record as the wire model, whatever state it is in.

        `include_result` is off for listings: a finished job's result carries AGY's full prose
        response, and the registry keeps up to 100 of them. Sending them all back to answer
        "what is running?" would spend more of the architect's context than the question is
        worth. The listing carries the metadata; `agy_job_status` carries the result.
        """
        return JobStatusResult(
            status=job.status,  # type: ignore[arg-type]
            job_id=job.job_id,
            kind=job.kind,
            workspace_path=job.workspace_path,
            prompt_preview=job.prompt_preview,
            is_done=job.is_done,
            started_at=job.started_at,
            finished_at=job.finished_at,
            duration_seconds=job.duration_seconds,
            result=(
                job.result
                if include_result and isinstance(job.result, TaskExecutionResult)
                else None
            ),
            error_details=job.error_details,
            recovered_from_disk=job.recovered_from_disk,
        )

    @server.tool(
        name="agy_start_task",
        description=(
            "Starts an AGY coding or analysis run in the background and returns a job id immediately.\n\n"
            "Use this instead of `agy_execute_task` for anything that takes more than about half a "
            "minute - which is most real work. Every MCP client caps how long it waits for a single "
            "tool call (Claude Code: 60 seconds by default, configurable higher; shipped configs use "
            "30 minutes), so a synchronous call to a task that runs for minutes is dropped by the "
            "client and its work is lost. This tool returns at once; AGY keeps working in the server; "
            "you collect the result later with `agy_job_status`.\n\n"
            "Architect Guidance:\n"
            "- Start the job, then do something else - inspect files, plan the next step, start another "
            "job in a different workspace - and collect the result when you need it.\n"
            "- The recommended way to wait on completion is checking the `done_marker_path` file on disk "
            "(e.g. `until [ -f <path> ]; do sleep 5; done`) rather than polling `agy_job_status` repeatedly, "
            "then calling `agy_job_status` once to collect the result.\n"
            "- Alternatively, pass `wait_seconds` to `agy_job_status` to block until the job finishes.\n"
            "- mode='plan' is the read-only form: AGY investigates and reports without touching files.\n"
            "- Jobs live in the server process. If the server restarts, `agy_job_status` reports "
            "'not_found' and the run must be re-issued.\n\n"
            "Args:\n"
            "    workspace_path: Absolute or relative path to the target repository. Must exist.\n"
            "    prompt: Clear, detailed, self-contained instructions for AGY.\n"
            "    auto_approve: Automatically approve tool calls and file edits (default: True).\n"
            "    mode: 'accept-edits' for full coding/editing (default), or 'plan' for read-only analysis.\n"
            "    timeout_seconds: Maximum execution time for the run itself (default: 600s, max: 3600).\n\n"
            "Returns:\n"
            "    JobHandle containing status, job_id, kind, workspace_path, started_at, done_marker_path, and error_details."
        ),
    )
    async def agy_start_task(
        workspace_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Absolute or relative filesystem path to the target workspace/repository where AGY will work. Must be an existing directory.",
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
                description="When True (default), automatically approves all AGY tool executions without blocking for interactive confirmation.",
            ),
        ] = True,
        mode: Annotated[
            Literal["accept-edits", "plan"],
            Field(
                description="Execution mode: 'accept-edits' (default) enables autonomous coding with file modification; 'plan' runs read-only analysis.",
            ),
        ] = "accept-edits",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=3600,
                description="Maximum duration of the background run in seconds before it is cancelled (default: 600). This bounds AGY, not this call - this call returns immediately.",
            ),
        ] = 600,
        model: Annotated[
            str,
            Field(
                description="Optional model id for this task (e.g. 'gemini-3.7-flash-high'). Empty uses MCP_AGY_MODEL, else the agy CLI default.",
            ),
        ] = "",
        effort: Annotated[
            str,
            Field(
                description="Optional reasoning effort: 'low', 'medium', or 'high'. Leave empty when the model id already encodes one - the CLI rejects the combination.",
            ),
        ] = "",
    ) -> JobHandle:
        """Starts an AGY run in the background and returns a job id immediately."""
        logger.info(f"agy_start_task invoked for workspace='{workspace_path}', mode='{mode}'")

        if not prompt.strip():
            return JobHandle(status="error", error_details="Prompt cannot be empty or whitespace only.")

        if not workspace_path.strip():
            return JobHandle(
                status="error", error_details="Workspace path cannot be empty or whitespace only."
            )

        try:
            validated_ws = validate_workspace_path(workspace_path)
        except WorkspaceError as exc:
            logger.warning(f"Workspace validation failed for '{workspace_path}': {exc}")
            return JobHandle(status="error", error_details=f"Workspace validation failed: {exc}")

        normalized_workspace = str(validated_ws)
        lock_mgr = get_workspace_lock_manager()

        extra: dict[str, Any] = {}
        if model.strip():
            extra["model"] = model.strip()
        if effort.strip():
            extra["effort"] = effort.strip()

        async def _run() -> TaskExecutionResult:
            # The lock is taken inside the job, not by the call that started it: two jobs against
            # the same workspace must still serialize, but the architect must not be made to wait
            # for that here - waiting is the thing this tool exists to avoid.
            #
            # `plan` takes the reader lock, so several background investigations of one repo
            # actually run at once. Under the old mutex they reported `running_count: 3` while
            # only one `agy.EXE` existed - the queueing was invisible from the outside.
            async with lock_mgr.lock(validated_ws, shared=(mode == "plan")):
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

        job = get_job_manager().start(
            runner=_run,
            kind="plan" if mode == "plan" else "task",
            workspace_path=normalized_workspace,
            prompt=prompt,
        )
        return JobHandle(
            status="running",
            job_id=job.job_id,
            kind=job.kind,
            workspace_path=job.workspace_path,
            started_at=job.started_at,
            done_marker_path=job.done_marker_path,
        )

    @server.tool(
        name="agy_job_status",
        description=(
            "Checks a background AGY job and returns its full result once it has finished.\n\n"
            "Architect Guidance:\n"
            "- Recommended waiting method: Monitor `done_marker_path` on disk (e.g. `until [ -f <path> ]; do sleep 5; done`), "
            "then call `agy_job_status(job_id, wait_seconds=0)` once to collect the result.\n"
            "- Pass `wait_seconds` to block until the job finishes instead of polling: the call returns "
            "the moment the job is done, or at the deadline with status still 'running'. Keep `wait_seconds` "
            "strictly below whatever per-call cap your client enforces (e.g. 60s default on Claude Code, although shipped "
            "configs may raise it to 30 minutes). Measured incident: a call with `wait_seconds=300` was cut by the client "
            "timeout, triggering a server process restart. Before result persistence was added, that restart completely "
            "wiped out a completed 255s / 864k-token job result.\n"
            "- `status: 'completed'` means the job ran to completion; read `result.status` for whether "
            "AGY itself succeeded, and `result.modified_files` for what it changed.\n"
            "- If the server process restarted after the job completed, `recovered_from_disk` will be True.\n"
            "- 'not_found' means the id is unknown to this server process and no unexpired result exists on disk.\n\n"
            "Args:\n"
            "    job_id: Identifier returned by agy_start_task.\n"
            "    wait_seconds: Seconds to wait for completion before returning (default: 0, max: 600).\n\n"
            "Returns:\n"
            "    JobStatusResult containing status, is_done, duration_seconds, result, recovered_from_disk, and error_details."
        ),
    )
    async def agy_job_status(
        job_id: Annotated[
            str,
            Field(min_length=1, description="Identifier of the job to inspect, as returned by agy_start_task."),
        ],
        wait_seconds: Annotated[
            int,
            Field(
                ge=0,
                le=600,
                description=(
                    "Block up to this many seconds waiting for the job to finish, returning early the "
                    "moment it does. 0 (default) reports the current state immediately. The ceiling exists "
                    "because the call must return inside whatever cap the client enforces; the shipped Claude "
                    "Code config raises that to 30 minutes, but a client left at its 60-second default will still "
                    "drop a long wait. Keep wait_seconds below your own configured cap."
                ),
            ),
        ] = 0,
    ) -> JobStatusResult:
        """Checks a background AGY job and returns its full result once it has finished."""
        manager = get_job_manager()
        job = await manager.wait(job_id, float(wait_seconds)) if wait_seconds else manager.get(job_id)
        if job is None:
            return JobStatusResult(
                status="not_found",
                job_id=job_id,
                error_details=(
                    "No job with that id in this server process or on disk. If the server restarted, "
                    "the job may have expired past TTL or never finished; re-issue the run with agy_start_task."
                ),
            )
        return _describe_job(job)

    @server.tool(
        name="agy_cancel_job",
        description=(
            "Stops a running background AGY job and terminates its process tree.\n\n"
            "Architect Guidance:\n"
            "- Use when a task is going the wrong way, or before starting a replacement run against the "
            "same workspace.\n"
            "- Cancellation is not a rollback: files AGY already wrote stay written. Call `agy_get_diff` "
            "afterwards to see what landed.\n\n"
            "Args:\n"
            "    job_id: Identifier returned by agy_start_task.\n\n"
            "Returns:\n"
            "    JobStatusResult describing the job after cancellation."
        ),
    )
    async def agy_cancel_job(
        job_id: Annotated[
            str,
            Field(min_length=1, description="Identifier of the job to cancel, as returned by agy_start_task."),
        ],
    ) -> JobStatusResult:
        """Stops a running background AGY job and terminates its process tree."""
        logger.info(f"agy_cancel_job invoked for job_id='{job_id}'")
        job = await get_job_manager().cancel(job_id)
        if job is None:
            return JobStatusResult(
                status="not_found",
                job_id=job_id,
                error_details="No job with that id in this server process.",
            )
        return _describe_job(job)

    @server.tool(
        name="agy_list_jobs",
        description=(
            "Lists every background AGY job this server knows about, newest first.\n\n"
            "Architect Guidance:\n"
            "- Use to recover a job id you no longer have, or to see what is still running before "
            "starting more work.\n"
            "- Results are omitted here to keep the listing small; collect a finished job's result "
            "with `agy_job_status(job_id)`.\n"
            "- Finished jobs are pruned an hour after they end.\n\n"
            "Returns:\n"
            "    JobListResult containing jobs (newest first) and running_count."
        ),
    )
    async def agy_list_jobs() -> JobListResult:
        """Lists every background AGY job this server knows about, newest first."""
        jobs = [_describe_job(j, include_result=False) for j in get_job_manager().list_jobs()]
        return JobListResult(
            jobs=jobs,
            running_count=sum(1 for j in jobs if not j.is_done),
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
