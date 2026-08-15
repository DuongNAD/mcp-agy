"""Pydantic v2 data models for FastMCP AGY server.

This module defines all request, response, telemetry, diff, test execution schemas,
and low-level backend execution models used across the mcp_agy package.
"""

from __future__ import annotations

from enum import Enum
import time
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class BackendType(str, Enum):
    """Execution backend engine identifier."""

    SDK = "sdk"
    CLI = "cli"
    MOCK = "mock"
    AUTO = "auto"


class ExecutionMode(str, Enum):
    """AGY execution mode."""

    ACCEPT_EDITS = "accept-edits"
    PLAN = "plan"


class ToolStatus(str, Enum):
    """Execution status for an individual tool invocation."""

    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"


class StreamEventType(str, Enum):
    """Type of streamed NDJSON telemetry event."""

    INIT = "init"
    STEP_UPDATE = "step_update"
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    CHECKPOINT = "checkpoint"
    RESULT = "result"
    ERROR = "error"


class TokenUsage(BaseModel):
    """Token usage metrics captured during AGY execution."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int = Field(
        default=0,
        ge=0,
        description="Prompt and context tokens consumed",
    )
    output_tokens: int = Field(
        default=0,
        ge=0,
        description="Completion and response tokens produced",
    )
    thinking_tokens: int = Field(
        default=0,
        ge=0,
        description="Reasoning and thought tokens consumed",
    )
    cache_read_tokens: int = Field(
        default=0,
        ge=0,
        description="Prompt cache tokens read",
    )
    total_tokens: int = Field(
        default=0,
        ge=0,
        description="Total tokens consumed across all phases",
    )
    execution_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Backend execution time in milliseconds",
    )
    cost_estimate: float = Field(
        default=0.0,
        ge=0.0,
        description="Estimated USD cost of the execution",
    )


class ToolEvent(BaseModel):
    """Captures a tool invocation emitted by the agent."""

    model_config = ConfigDict(extra="ignore")

    tool_name: str = Field(..., description="Name of the invoked tool (e.g. view_file, write_to_file)")
    tool_call_id: str = Field(default="", description="Unique tool call or step identifier")
    status: ToolStatus = Field(default=ToolStatus.ACTIVE, description="Current execution state of the tool")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Parameters passed to the tool")
    output: Optional[str] = Field(default=None, description="Output or response returned by the tool")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Execution duration of the tool in seconds")
    error: Optional[str] = Field(default=None, description="Error message if tool execution failed")


class StreamEvent(BaseModel):
    """Real-time streaming event emitted during AGY execution."""

    model_config = ConfigDict(extra="ignore")

    event_type: StreamEventType = Field(..., description="Category of the streamed event")
    data: Dict[str, Any] = Field(default_factory=dict, description="Raw event payload data")
    timestamp: float = Field(default_factory=time.time, description="Unix timestamp of the event")
    conversation_id: str = Field(default="", description="Associated conversation or session UUID")
    text_delta: Optional[str] = Field(default=None, description="Incremental text fragment if applicable")
    tool_event: Optional[ToolEvent] = Field(default=None, description="Associated tool event details if applicable")
    token_usage: Optional[TokenUsage] = Field(default=None, description="Incremental or final token usage metrics")


class ExecutionRequest(BaseModel):
    """Standardized request payload for AGY backend execution."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(..., min_length=1, description="Instructions or prompt for AGY")
    workspace_path: str = Field(default="", description="Absolute path to target workspace directory")
    mode: Literal["accept-edits", "plan"] = Field(
        default="accept-edits",
        description="Execution mode: 'accept-edits' for coding/modifications, 'plan' for read-only analysis",
    )
    auto_approve: bool = Field(default=True, description="Auto-approve tool and command permissions")
    timeout_seconds: int = Field(default=600, ge=1, le=3600, description="Max execution time in seconds")
    model: Optional[str] = Field(default=None, description="Optional LLM model override")
    session_id: str = Field(default="", description="Conversation / session ID for multi-turn continuity")
    backend_preference: BackendType = Field(default=BackendType.AUTO, description="Preferred backend engine")
    effort: Optional[str] = Field(default=None, description="Reasoning effort: 'low', 'medium', or 'high'")
    extra_args: Dict[str, Any] = Field(default_factory=dict, description="Additional backend-specific arguments")


class TaskExecutionResult(BaseModel):
    """Result returned from an autonomous task execution in the target workspace."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["success", "error", "timeout"] = Field(
        description="Outcome status of the task execution ('success', 'error', 'timeout')"
    )
    conversation_id: str = Field(
        default="",
        description="AGY conversation / session identifier",
    )
    response: str = Field(
        default="",
        description="Final text summary or response message from AGY",
    )
    modified_files: list[str] = Field(
        default_factory=list,
        description="List of file paths created, modified, or deleted",
    )
    diff_summary: str = Field(
        default="",
        description="Summary of git or file changes made during execution",
    )
    duration_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Total execution duration in seconds",
    )
    tokens_used: TokenUsage = Field(
        default_factory=TokenUsage,
        description="Detailed token consumption metrics",
    )
    backend_used: str = Field(
        default="cli",
        description="Backend engine used ('cli', 'sdk', 'mock')",
    )
    error_details: Optional[str] = Field(
        default=None,
        description="Detailed error trace or message if status != success",
    )


class ChatResult(BaseModel):
    """Result returned from read-only architectural or planning consultation."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["success", "error", "timeout"] = Field(
        description="Outcome status of the chat interaction ('success', 'error', 'timeout')"
    )
    conversation_id: str = Field(
        default="",
        description="AGY conversation / session identifier",
    )
    response: str = Field(
        default="",
        description="Analytical or architectural guidance from AGY",
    )
    duration_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Total consultation duration in seconds",
    )
    tokens_used: TokenUsage = Field(
        default_factory=TokenUsage,
        description="Detailed token consumption metrics",
    )
    backend_used: str = Field(
        default="cli",
        description="Backend engine used ('cli', 'sdk', 'mock')",
    )
    error_details: Optional[str] = Field(
        default=None,
        description="Detailed error trace or message if status != success",
    )


class ExecutionResult(BaseModel):
    """Comprehensive result returned from backend execution."""

    model_config = ConfigDict(extra="ignore")

    success: bool = Field(..., description="True if execution completed without fatal error")
    status: Literal["success", "error", "timeout"] = Field(
        default="success",
        description="Outcome status ('success', 'error', 'timeout')",
    )
    response_text: str = Field(default="", description="Final text response from AGY")
    tool_calls: List[ToolEvent] = Field(default_factory=list, description="All tool invocations during execution")
    token_usage: TokenUsage = Field(default_factory=TokenUsage, description="Total token metrics")
    backend_used: BackendType = Field(default=BackendType.MOCK, description="Backend engine that executed the task")
    duration_ms: float = Field(default=0.0, ge=0.0, description="Execution duration in milliseconds")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Execution duration in seconds")
    session_id: str = Field(default="", description="Conversation or session identifier")
    modified_files: List[str] = Field(default_factory=list, description="List of modified/created files")
    diff_summary: str = Field(default="", description="Summary of changes")
    error_message: Optional[str] = Field(default=None, description="Error message if execution failed")

    def to_task_execution_result(self) -> TaskExecutionResult:
        """Convert to Milestone 1 TaskExecutionResult for FastMCP tool compatibility."""
        dur = self.duration_seconds if self.duration_seconds > 0 else (self.duration_ms / 1000.0)
        default_diff = f"{len(self.modified_files)} file(s) modified" if self.modified_files else ""
        backend_str = (
            self.backend_used.value
            if isinstance(self.backend_used, BackendType)
            else str(self.backend_used)
        )
        return TaskExecutionResult(
            status=self.status,
            conversation_id=self.session_id,
            response=self.response_text,
            modified_files=self.modified_files,
            diff_summary=self.diff_summary or default_diff,
            duration_seconds=round(dur, 4),
            tokens_used=self.token_usage,
            backend_used=backend_str,
            error_details=self.error_message,
        )

    def to_chat_result(self) -> ChatResult:
        """Convert to Milestone 1 ChatResult for FastMCP tool compatibility."""
        dur = self.duration_seconds if self.duration_seconds > 0 else (self.duration_ms / 1000.0)
        backend_str = (
            self.backend_used.value
            if isinstance(self.backend_used, BackendType)
            else str(self.backend_used)
        )
        return ChatResult(
            status=self.status,
            conversation_id=self.session_id,
            response=self.response_text,
            duration_seconds=round(dur, 4),
            tokens_used=self.token_usage,
            backend_used=backend_str,
            error_details=self.error_message,
        )


class FileDiffStat(BaseModel):
    """Change statistics for an individual file."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(
        description="Relative path of the modified or created file"
    )
    status: str = Field(
        description="Git porcelain status code ('M', 'A', 'D', 'R', '??', etc.)"
    )
    insertions: int = Field(
        default=0,
        ge=0,
        description="Number of inserted lines",
    )
    deletions: int = Field(
        default=0,
        ge=0,
        description="Number of deleted lines",
    )


class DiffResult(BaseModel):
    """Result returned from repository diff inspection."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["success", "error", "not_a_git_repo"] = Field(
        description="Outcome status of the diff inspection ('success', 'error', 'not_a_git_repo')"
    )
    has_changes: bool = Field(
        default=False,
        description="True if workspace contains modifications or untracked files",
    )
    unified_diff: str = Field(
        default="",
        description="Complete unified diff (git diff + untracked files)",
    )
    changed_files: list[FileDiffStat] = Field(
        default_factory=list,
        description="Per-file status and line change statistics",
    )
    untracked_files: list[str] = Field(
        default_factory=list,
        description="List of untracked files in the workspace",
    )
    summary: str = Field(
        default="",
        description="Human-readable summary of repository changes",
    )
    error_details: Optional[str] = Field(
        default=None,
        description="Detailed error trace or message if status == 'error'",
    )


class TestFailure(BaseModel):
    """Detailed diagnostics for a failed or errored test case."""

    __test__ = False

    model_config = ConfigDict(extra="ignore")

    test_id: str = Field(
        description="Identifier or name of the failed test (e.g., 'tests/test_a.py::test_fn')"
    )
    message: str = Field(
        description="Failure or exception message"
    )
    location: Optional[str] = Field(
        default=None,
        description="Source file and line number if available",
    )
    traceback: Optional[str] = Field(
        default=None,
        description="Stack trace or failure trace if available",
    )


class TestSummary(BaseModel):
    """Aggregate statistics for a test execution run."""

    __test__ = False

    model_config = ConfigDict(extra="ignore")

    total: int = Field(
        default=0,
        ge=0,
        description="Total number of tests executed or collected",
    )
    passed: int = Field(
        default=0,
        ge=0,
        description="Number of passing tests",
    )
    failed: int = Field(
        default=0,
        ge=0,
        description="Number of failing tests",
    )
    skipped: int = Field(
        default=0,
        ge=0,
        description="Number of skipped tests",
    )
    errors: int = Field(
        default=0,
        ge=0,
        description="Number of test execution errors or crashes",
    )


class TestRunResult(BaseModel):
    """Result returned from automated test suite execution."""

    __test__ = False

    model_config = ConfigDict(extra="ignore")

    status: Literal["passed", "failed", "error", "timeout", "no_framework_detected"] = Field(
        description="Overall test suite outcome ('passed', 'failed', 'error', 'timeout', 'no_framework_detected')"
    )
    exit_code: int = Field(
        default=0,
        description="Process exit code from test command",
    )
    framework: str = Field(
        default="custom",
        description="Detected or specified test framework ('pytest', 'npm', 'cargo', etc.)",
    )
    test_command_executed: str = Field(
        default="",
        description="Exact shell command executed to run the tests",
    )
    output: str = Field(
        default="",
        description="Raw standard output and standard error from test runner",
    )
    summary: TestSummary = Field(
        default_factory=TestSummary,
        description="Aggregate test count statistics",
    )
    failures: list[TestFailure] = Field(
        default_factory=list,
        description="Detailed list of test failures",
    )
    duration_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Total execution duration in seconds",
    )
    error_details: Optional[str] = Field(
        default=None,
        description="Detailed error trace or message if status == 'error'",
    )
