"""Core domain models, backend interfaces, and schema definitions for mcp_agy."""

from mcp_agy.core.backend import (
    AGYBackend,
    MockAGYBackend,
    get_backend,
    reset_backend,
    set_backend,
)
from mcp_agy.core.jobs import (
    JobManager,
    JobRecord,
    get_job_manager,
    reset_job_manager,
)
from mcp_agy.core.models import (
    ChatResult,
    DiffResult,
    FileDiffStat,
    JobHandle,
    JobListResult,
    JobStatusResult,
    TaskExecutionResult,
    TestFailure,
    TestRunResult,
    TestSummary,
    TokenUsage,
)

__all__ = [
    "AGYBackend",
    "MockAGYBackend",
    "get_backend",
    "set_backend",
    "reset_backend",
    "TokenUsage",
    "TaskExecutionResult",
    "ChatResult",
    "FileDiffStat",
    "DiffResult",
    "TestFailure",
    "TestSummary",
    "TestRunResult",
    "JobManager",
    "JobRecord",
    "get_job_manager",
    "reset_job_manager",
    "JobHandle",
    "JobStatusResult",
    "JobListResult",
]
