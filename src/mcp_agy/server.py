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
- agy_wait: Block until the next job finishes and hand back every report not yet collected
- agy_job_status: Collect one job's result, optionally waiting a bounded number of seconds
- agy_cancel_job: Stop a run and kill its process tree
- agy_list_jobs: Recover job ids and see what is still running

A finished job is also announced on Claude Code's channel (see core/channel.py), and the
`delegate` prompt and `agy://playbook` resource carry the full delegation protocol.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from mcp_agy import __version__
from mcp_agy.core import channel
from mcp_agy.core.backend import AGYBackend, get_backend
from mcp_agy.core.diff_engine import GitDiffEngine, inspect_git_diff
from mcp_agy.core.jobs import JobRecord, get_job_manager
from mcp_agy.core.models import (
    ChatResult,
    DiffResult,
    JobHandle,
    JobListResult,
    JobStatusResult,
    JobWaitResult,
    ReasoningProfile,
    TaskExecutionResult,
    TestRunResult,
    TestSummary,
)
from mcp_agy.core import reasoning_toolkit
from mcp_agy.core.report import parse_report, with_report_contract
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
    "Google Antigravity (AGY) is your coding subagent: you plan and review, it edits files and runs "
    "commands. Loop:\n"
    "1. Split the work into small tasks. AGY cannot see this chat, so each prompt stands alone, in "
    "English: GOAL (one sentence), FILES (exact paths), RULES (constraints, what not to touch), "
    "DONE WHEN (checkable criteria). The server asks AGY to end with a REPORT block; do not ask.\n"
    "2. Start each with agy_start_task (returns a job_id at once). Writes to one workspace run one at "
    "a time: give parallel write tasks their own git worktree. mode='plan' is read-only and may share.\n"
    "3. Call agy_wait: it returns when a job finishes, with its report - each report once. Repeat "
    "until status is 'idle'; 'running' only means nothing finished yet.\n"
    "4. Verify each report with agy_get_diff and agy_run_tests before accepting; send a corrective "
    "task if needed.\n"
    "agy_execute_task / agy_chat block until AGY is done - only for work under ~30s. A finished job "
    "may also arrive as a <channel> event carrying its job_id: collect it with agy_wait."
)

PLAYBOOK = """# Delegating to AGY (mcp-agy)

You are the architect. AGY is a coding subagent: it edits files, runs commands and reports back.
You decide what to build, split it up, review what comes back and decide what is accepted.

## 1. Split
Break the goal into tasks that are small (one concern, a handful of files) and independent.
Tasks that must happen in order are separate rounds, not one big prompt.

## 2. Write each prompt so it stands alone
AGY cannot see this conversation. Write in English:
- GOAL: one sentence - the behaviour wanted, not the edit.
- FILES: exact paths to touch, and any that must not change.
- RULES: project constraints - style, test command, forbidden dependencies.
- DONE WHEN: criteria AGY can check itself, e.g. "pytest tests/test_auth.py passes".
The server appends a REPORT instruction; AGY's reply ends with STATUS / SUMMARY / CHANGED /
CHECKS / BLOCKERS, parsed into `result.report`.

## 3. Dispatch
- agy_start_task(workspace_path, prompt) for each task; it returns a job_id at once.
- Edits to one workspace run one at a time. For parallel edits give each task its own git
  worktree; mode='plan' (read-only) tasks may share a workspace.
- agy_execute_task / agy_chat block until done: only for work that ends in under ~30s.

## 4. Collect - the "subagent returned" signal
- agy_wait() blocks until the next job finishes and returns every report not yet collected,
  each exactly once. Call it again until status is 'idle'.
- 'running' means nothing finished inside the wait; the jobs are unharmed - call it again, or do
  other work first. Keep wait_seconds under your client's per-call timeout.
- In Claude Code with channels on for this server, a job nobody collected also arrives on its own
  as a <channel> event - then call agy_wait(job_ids=[...]) for the full result.
- Clients with a background shell can instead wait on the job's `done_marker_path` file.

## 5. Verify, then decide
Never accept a report on its word: agy_get_diff shows what actually changed, agy_run_tests
shows whether it still works. Accept, or send one corrective task naming exactly what was wrong.
agy_cancel_job stops a run that is going the wrong way; files already written stay.
"""


RIGOR_DESCRIPTION = (
    "Reasoning protocol (needs MCP_AGY_TOOLKIT_PATH). 'standard' (default): load the toolkit's "
    "rules into the workspace. 'deep': also run the deep-verify skill - only for tasks with two "
    "or more genuinely different designs where a wrong choice is costly; on a fully specified "
    "task it adds ~37% more code for the same score. 'off': touch nothing."
)


def _prepare_reasoning(
    workspace: Path, prompt: str, rigor: str
) -> tuple[str, Optional[ReasoningProfile]]:
    """Provision the toolkit and, for 'deep', name the skill in the prompt.

    Returns the prompt to actually send and the profile to stamp on the result.

    A profile comes back whenever the toolkit is configured, and also when the caller asked for
    'deep' and it is not - that request must not fail silently, or the architect reads a plain
    AGY answer as the output of a verification pipeline that never ran.

    Never raises. A checkout that cannot be copied downgrades the run to plain AGY and says so
    in `notes`: failing a coding task over a rules file would be the worse trade.
    """
    root = reasoning_toolkit.toolkit_root()
    if root is None:
        if rigor != "deep":
            return prompt, None
        return prompt, ReasoningProfile(
            rigor="off",
            notes=(
                f"rigor='deep' requested but {reasoning_toolkit.TOOLKIT_ENV} is not set to a "
                f"valid checkout, so the deep-verify skill was not available. Clone "
                f"{reasoning_toolkit.TOOLKIT_URL} and point that variable at it."
            ),
        )

    if rigor == "off":
        return prompt, ReasoningProfile(rigor="off", toolkit_source=str(root), notes="not installed by request")

    try:
        installed, notes = reasoning_toolkit.provision(workspace, root)
    except OSError as exc:
        logger.warning(f"Reasoning toolkit could not be installed into '{workspace}': {exc}")
        return prompt, ReasoningProfile(
            rigor="off",
            toolkit_source=str(root),
            notes=f"install failed, run proceeded without the toolkit: {exc}",
        )

    profile = ReasoningProfile(
        rigor="deep" if rigor == "deep" else "standard",
        toolkit_active=True,
        toolkit_source=str(root),
        toolkit_revision=reasoning_toolkit.toolkit_revision(root),
        installed=installed,
        notes=notes,
    )
    if rigor == "deep":
        return reasoning_toolkit.deep_verify_preamble() + prompt, profile
    return prompt, profile


def _stamp_reasoning(
    result: TaskExecutionResult, profile: Optional[ReasoningProfile]
) -> TaskExecutionResult:
    """Attach the profile to a finished result, filling in what AGY reported back."""
    if profile is None:
        return result

    if profile.toolkit_active:
        profile.gate_line, profile.deep_verify_declined = reasoning_toolkit.read_protocol_report(
            result.response
        )
    result.reasoning = profile
    return result


def _finish_result(
    result: TaskExecutionResult, profile: Optional[ReasoningProfile]
) -> TaskExecutionResult:
    """Everything the server adds to a finished run: the reasoning profile and the parsed report."""
    result = _stamp_reasoning(result, profile)
    if isinstance(result, TaskExecutionResult) and result.report is None:
        result.report = parse_report(result.response)
    return result


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
    """Create and configure the FastMCP server with all 9 tools, the `delegate` prompt and the
    `agy://playbook` resource.

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

    if channel.channel_enabled():
        channel.advertise(server._mcp_server)

    @server.prompt(
        name="delegate",
        description="Plan a goal as AGY subagent tasks: split, dispatch with agy_start_task, collect with agy_wait, verify.",
    )
    def delegate(goal: str, workspace_path: str = "") -> str:
        where = f"Workspace: {workspace_path}\n" if workspace_path.strip() else ""
        return f"{PLAYBOOK}\n---\n\nGoal: {goal}\n{where}\nSplit this goal into AGY tasks and run the loop above."

    @server.resource(
        "agy://playbook",
        name="playbook",
        description="How to delegate work to AGY through this server, end to end.",
        mime_type="text/markdown",
    )
    def playbook() -> str:
        return PLAYBOOK

    def _resolve_backend() -> AGYBackend:
        if backend is not None:
            return backend
        return get_backend()

    @server.tool(
        name="agy_execute_task",
        description="Run an autonomous coding task in a workspace with AGY and wait for it to finish (blocks).\n\nArchitect Guidance:\n- Use only for work that ends in under ~30s; anything longer belongs in agy_start_task, because clients drop long calls.\n- mode='plan' is read-only analysis. Edits to one workspace run one at a time.\n- Confirm what changed with agy_get_diff; do not rely on AGY's own summary.",
    )
    async def agy_execute_task(
        workspace_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Existing directory AGY works in.",
            ),
        ],
        prompt: Annotated[
            str,
            Field(
                min_length=1,
                description="Self-contained task: goal, files, rules, done-when. The server adds the REPORT format.",
            ),
        ],
        auto_approve: Annotated[
            bool,
            Field(
                description="Auto-approve AGY's tool calls and edits.",
            ),
        ] = True,
        mode: Annotated[
            Literal["accept-edits", "plan"],
            Field(
                description="'accept-edits' edits files; 'plan' is read-only.",
            ),
        ] = "accept-edits",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=3600,
                description="Max run time in seconds.",
            ),
        ] = 600,
        model: Annotated[
            str,
            Field(
                description="Model id, e.g. 'gemini-3.8-flash-high'. Empty uses MCP_AGY_MODEL. List: `agy models`.",
            ),
        ] = "",
        effort: Annotated[
            str,
            Field(
                description="'low'|'medium'|'high'. Leave empty when the model id already carries an effort; the CLI rejects both.",
            ),
        ] = "",
        rigor: Annotated[
            Literal["standard", "deep", "off"],
            Field(description=RIGOR_DESCRIPTION),
        ] = "standard",
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
            # Inside the lock: provisioning writes into the workspace, and the prompt it may
            # extend has to be the one the backend actually receives.
            effective_prompt, profile = _prepare_reasoning(validated_ws, prompt, rigor)
            effective_prompt = with_report_contract(effective_prompt, mode)

            if backend_executor is not None:
                return _finish_result(
                    await backend_executor(
                        workspace_path=normalized_workspace,
                        prompt=effective_prompt,
                        auto_approve=auto_approve,
                        mode=mode,
                        timeout_seconds=timeout_seconds,
                        **extra,
                    ),
                    profile,
                )

            active_backend = _resolve_backend()
            return _finish_result(
                await active_backend.execute_task(
                    workspace_path=normalized_workspace,
                    prompt=effective_prompt,
                    auto_approve=auto_approve,
                    mode=mode,
                    timeout_seconds=timeout_seconds,
                    **extra,
                ),
                profile,
            )

    @server.tool(
        name="agy_chat",
        description="Ask AGY a read-only question about a codebase or a design. It never edits files.\n\nArchitect Guidance:\n- Pass workspace_path so AGY can read the code.\n- Pass conversation_id to continue an earlier thread.\n- Use model='gemini-3.1-pro-high' for hard design questions.",
    )
    async def agy_chat(
        prompt: Annotated[
            str,
            Field(
                min_length=1,
                description="Question or analysis request.",
            ),
        ],
        workspace_path: Annotated[
            str,
            Field(
                description="Optional directory for code context.",
            ),
        ] = "",
        conversation_id: Annotated[
            str,
            Field(
                description="Optional id of a conversation to continue.",
            ),
        ] = "",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=1800,
                description="Max seconds.",
            ),
        ] = 300,
        model: Annotated[
            str,
            Field(
                description="Model id, e.g. 'gemini-3.8-flash-high'. Empty uses MCP_AGY_MODEL. List: `agy models`.",
            ),
        ] = "",
        effort: Annotated[
            str,
            Field(
                description="'low'|'medium'|'high'. Leave empty when the model id already carries an effort; the CLI rejects both.",
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
        description="Show what changed in a git workspace: staged, unstaged and untracked files, per-file stats, unified diff.\n\nArchitect Guidance:\n- Run it after a task to verify AGY's work before accepting it.\n- A folder that is not a git repo returns status 'not_a_git_repo'.",
    )
    async def agy_get_diff(
        workspace_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Git workspace directory.",
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
        description="Run a workspace's tests and return pass/fail counts and failure traces.\n\nArchitect Guidance:\n- Omit test_command to auto-detect the framework (pytest, npm, cargo, go, maven, gradle, dotnet...).\n- Run it after changes to catch regressions.",
    )
    async def agy_run_tests(
        workspace_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Workspace directory.",
            ),
        ],
        test_command: Annotated[
            str,
            Field(
                description="Optional command, e.g. 'pytest -k auth'. Empty auto-detects.",
            ),
        ] = "",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=1800,
                description="Max seconds.",
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
        description="Start an AGY subagent task in the background and return a job_id at once. Prefer this to agy_execute_task for anything over ~30s.\n\nArchitect Guidance:\n- Start every independent task first, then call agy_wait: it returns each job's report as the job finishes.\n- Other ways to wait: a <channel> event (Claude Code with --channels), or the `done_marker_path` file appearing.\n- mode='plan' is read-only and plan jobs may share a workspace; write jobs on one workspace queue behind each other.\n- After a server restart, finished results are recovered from disk; running jobs are lost - re-issue them.\n- MCP_AGY_MAX_CONCURRENCY caps simultaneous AGY processes; extra jobs wait their turn.",
    )
    async def agy_start_task(
        workspace_path: Annotated[
            str,
            Field(
                min_length=1,
                description="Existing directory AGY works in.",
            ),
        ],
        prompt: Annotated[
            str,
            Field(
                min_length=1,
                description="Self-contained task: goal, files, rules, done-when. The server adds the REPORT format.",
            ),
        ],
        auto_approve: Annotated[
            bool,
            Field(
                description="Auto-approve AGY's tool calls and edits.",
            ),
        ] = True,
        mode: Annotated[
            Literal["accept-edits", "plan"],
            Field(
                description="'accept-edits' edits files; 'plan' is read-only.",
            ),
        ] = "accept-edits",
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=3600,
                description="Max run time in seconds; bounds AGY, not this call.",
            ),
        ] = 600,
        model: Annotated[
            str,
            Field(
                description="Model id, e.g. 'gemini-3.8-flash-high'. Empty uses MCP_AGY_MODEL. List: `agy models`.",
            ),
        ] = "",
        effort: Annotated[
            str,
            Field(
                description="'low'|'medium'|'high'. Leave empty when the model id already carries an effort; the CLI rejects both.",
            ),
        ] = "",
        rigor: Annotated[
            Literal["standard", "deep", "off"],
            Field(description=RIGOR_DESCRIPTION),
        ] = "standard",
        ctx: Optional[Context] = None,
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
                effective_prompt, profile = _prepare_reasoning(validated_ws, prompt, rigor)
                effective_prompt = with_report_contract(effective_prompt, mode)

                if backend_executor is not None:
                    return _finish_result(
                        await backend_executor(
                            workspace_path=normalized_workspace,
                            prompt=effective_prompt,
                            auto_approve=auto_approve,
                            mode=mode,
                            timeout_seconds=timeout_seconds,
                            **extra,
                        ),
                        profile,
                    )
                active_backend = _resolve_backend()
                return _finish_result(
                    await active_backend.execute_task(
                        workspace_path=normalized_workspace,
                        prompt=effective_prompt,
                        auto_approve=auto_approve,
                        mode=mode,
                        timeout_seconds=timeout_seconds,
                        **extra,
                    ),
                    profile,
                )

        manager = get_job_manager()
        # The session that started the job is the one to tell when it ends.
        announce = (
            channel.make_announcer(ctx.session, manager)
            if ctx is not None and channel.channel_enabled()
            else None
        )
        job = manager.start(
            runner=_run,
            kind="plan" if mode == "plan" else "task",
            workspace_path=normalized_workspace,
            prompt=prompt,
            on_finish=announce,
        )
        return JobHandle(
            status="running",
            job_id=job.job_id,
            kind=job.kind,
            workspace_path=job.workspace_path,
            started_at=job.started_at,
            done_marker_path=job.done_marker_path,
            next_step=(
                "Start any other independent tasks now, then call agy_wait to receive each "
                "report as its job finishes."
            ),
        )

    @server.tool(
        name="agy_wait",
        description="Wait for AGY subagents to report back: blocks until a background job finishes, then returns every finished job's full result not yet collected (each exactly once) plus what is still running.\n\nArchitect Guidance:\n- Call it after starting jobs, and again after handling each report, until status is 'idle'.\n- 'running' means nothing finished within wait_seconds; nothing is lost - call again.\n- Omit job_ids to wait on every job; pass ids to wait on just those.\n- Verify each finished job with agy_get_diff / agy_run_tests before accepting it.",
    )
    async def agy_wait(
        job_ids: Annotated[
            list[str],
            Field(description="Jobs to wait on. Empty: every job this server started."),
        ] = [],
        wait_seconds: Annotated[
            int,
            Field(
                ge=0,
                le=600,
                description="Block up to this many seconds (0 checks now). Keep it below your client's per-call timeout (Claude Code: 60s).",
            ),
        ] = 45,
    ) -> JobWaitResult:
        """Blocks until a background AGY job finishes and returns the reports not yet collected."""
        finished, running, unknown = await get_job_manager().wait_any(
            [j for j in job_ids if j.strip()], float(wait_seconds)
        )

        if finished:
            state = "ready"
            more = (
                f" {len(running)} job(s) still running: call agy_wait again after handling these."
                if running
                else " Nothing else is running."
            )
            next_step = "Verify each finished job with agy_get_diff / agy_run_tests before accepting it." + more
        elif running:
            state = "running"
            next_step = (
                f"Nothing finished within {wait_seconds}s; {len(running)} job(s) still running and "
                "unharmed. Call agy_wait again, or do other work first."
            )
        else:
            state = "idle"
            next_step = "No job is running and no report is waiting to be collected."
        if unknown:
            next_step += " Unknown job ids: re-issue those tasks with agy_start_task."

        return JobWaitResult(
            status=state,  # type: ignore[arg-type]
            finished=[_describe_job(j) for j in finished],
            still_running=[_describe_job(j, include_result=False) for j in running],
            running_count=len(running),
            unknown_job_ids=unknown,
            next_step=next_step,
        )

    @server.tool(
        name="agy_job_status",
        description="Get one background job's state and, once finished, its full result. To wait on several jobs, use agy_wait.\n\nArchitect Guidance:\n- wait_seconds blocks until the job ends or the deadline; keep it below your client's per-call timeout (Claude Code default: 60s).\n- status 'completed' means the job ran; read result.status for AGY's own outcome and result.modified_files for what changed.\n- 'not_found': unknown or expired id (finished jobs are kept 1h) - re-issue the run.",
    )
    async def agy_job_status(
        job_id: Annotated[
            str,
            Field(min_length=1, description="Id returned by agy_start_task."),
        ],
        wait_seconds: Annotated[
            int,
            Field(
                ge=0,
                le=600,
                description="Block up to this many seconds for the job to finish (0 reports now, max 600). Keep it below your client's per-call timeout.",
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
        if job.is_done:
            job.collected = True
        return _describe_job(job)

    @server.tool(
        name="agy_cancel_job",
        description="Stop a running job and kill its process tree.\n\nArchitect Guidance:\n- Files already written stay; run agy_get_diff to see them.\n- Cancel before re-running a task on the same workspace.",
    )
    async def agy_cancel_job(
        job_id: Annotated[
            str,
            Field(min_length=1, description="Id returned by agy_start_task."),
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
        if job.is_done:
            job.collected = True
        return _describe_job(job)

    @server.tool(
        name="agy_list_jobs",
        description="List background jobs, newest first, with running_count. Results are omitted; fetch one with agy_job_status.\n\nArchitect Guidance:\n- Use it to recover a lost job_id or to see what is running.\n- Jobs waiting on MCP_AGY_MAX_CONCURRENCY also count as running.",
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
