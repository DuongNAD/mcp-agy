"""FastMCP Server for Google Antigravity (AGY).

Exposes Google Antigravity as an autonomous coding worker for external AI architect agents
(Claude Desktop, Cursor, Cline, Roo Code) communicating over clean stdio JSON-RPC 2.0.
"""

from __future__ import annotations

__version__ = "0.1.0"
__title__ = "mcp_agy"
__description__ = (
    "FastMCP server exposing Google Antigravity (AGY) as an autonomous coding worker "
    "for AI architect agents."
)

from mcp_agy.cli import main as cli_main
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
from mcp_agy.server import (
    create_mcp_server,
    create_server,
    mcp,
    server,
)
from mcp_agy.utils.logger import (
    configure_logging,
    get_logger,
    setup_logger,
    setup_logging,
)
from mcp_agy.utils.workspace import (
    POSIX_SENSITIVE_ROOTS,
    WIN_SENSITIVE_ROOTS,
    WorkspaceError,
    WorkspaceLockManager,
    WorkspaceLockTimeoutError,
    WorkspaceNotADirectoryError,
    WorkspaceNotFoundError,
    WorkspaceSecurityError,
    canonicalize_workspace_path,
    get_workspace_key,
    get_workspace_lock_manager,
    is_filesystem_root,
    is_system_critical_path,
    reset_workspace_lock_manager,
    validate_workspace_path,
)

__all__ = [
    "__version__",
    "__title__",
    "__description__",
    # FastMCP server instances & factories
    "mcp",
    "server",
    "create_mcp_server",
    "create_server",
    "cli_main",
    # Core Pydantic models
    "TokenUsage",
    "TaskExecutionResult",
    "ChatResult",
    "FileDiffStat",
    "DiffResult",
    "TestFailure",
    "TestSummary",
    "TestRunResult",
    # Backend interfaces and registry
    "AGYBackend",
    "MockAGYBackend",
    "get_backend",
    "set_backend",
    "reset_backend",
    # Stderr logging utilities
    "setup_logger",
    "get_logger",
    "configure_logging",
    "setup_logging",
    # Workspace Safety & Locking
    "WIN_SENSITIVE_ROOTS",
    "POSIX_SENSITIVE_ROOTS",
    "WorkspaceError",
    "WorkspaceSecurityError",
    "WorkspaceNotFoundError",
    "WorkspaceNotADirectoryError",
    "WorkspaceLockTimeoutError",
    "canonicalize_workspace_path",
    "get_workspace_key",
    "is_filesystem_root",
    "is_system_critical_path",
    "validate_workspace_path",
    "WorkspaceLockManager",
    "get_workspace_lock_manager",
    "reset_workspace_lock_manager",
]
