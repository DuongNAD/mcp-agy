"""Core domain models, backend interfaces, and schema definitions for mcp_agy."""

from mcp_agy.core.backend import (
    AGYBackend,
    MockAGYBackend,
    get_backend,
    reset_backend,
    set_backend,
)
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
]
