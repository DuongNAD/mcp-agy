"""Abstract AGY execution backend interface and backend registration.

This module defines the abstract AGYBackend interface implemented by all execution engines
(PythonSDKBackend, SubprocessCLIBackend, MockAGYBackend, BackendManager).
It provides global backend registration, retrieval, and lifecycle management.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from mcp_agy.core.models import (
    ChatResult,
    TaskExecutionResult,
)


class AGYBackend(ABC):
    """Abstract base class for Google Antigravity (AGY) execution backends."""

    def is_available(self) -> bool:
        """Probe the environment to check if this backend is available."""
        return True

    @abstractmethod
    async def execute_task(
        self,
        workspace_path: str,
        prompt: str,
        auto_approve: bool = True,
        mode: str = "accept-edits",
        timeout_seconds: int = 600,
    ) -> TaskExecutionResult:
        """Execute an autonomous multi-step coding task in the target workspace.

        Args:
            workspace_path: Absolute path to the target workspace directory.
            prompt: Detailed coding instructions, constraints, and requirements.
            auto_approve: If True, automatically approves tool and file actions.
            mode: Agent execution mode ('accept-edits' or 'plan').
            timeout_seconds: Maximum allowed runtime in seconds before aborting.

        Returns:
            TaskExecutionResult: Structured result with status, modified files, diff, and tokens.
        """
        raise NotImplementedError

    @abstractmethod
    async def chat(
        self,
        prompt: str,
        workspace_path: str = "",
        conversation_id: str = "",
        timeout_seconds: int = 300,
    ) -> ChatResult:
        """Submit an analytical, architectural, or planning query in read-only mode.

        Args:
            prompt: Question, code review prompt, or architectural inquiry.
            workspace_path: Optional path for contextual repository understanding.
            conversation_id: Optional existing conversation ID to continue dialogue.
            timeout_seconds: Maximum allowed runtime in seconds before aborting.

        Returns:
            ChatResult: Structured analytical response with duration and token usage.
        """
        raise NotImplementedError


# Import MockAGYBackend from mock_backend module for re-export and backward compatibility
from mcp_agy.core.mock_backend import MockAGYBackend  # noqa: E402, F401

_active_backend: Optional[AGYBackend] = None


def get_backend() -> AGYBackend:
    """Retrieve the globally registered AGY execution backend instance.

    If an explicit backend has been registered via `set_backend()`, returns it.
    Otherwise, returns the 3-tier `BackendManager` singleton.

    Returns:
        AGYBackend: The active backend or BackendManager instance.
    """
    if _active_backend is not None:
        return _active_backend

    from mcp_agy.core.backend_manager import get_backend_manager
    return get_backend_manager()


def set_backend(backend: AGYBackend) -> None:
    """Register an AGY execution backend instance globally.

    Args:
        backend: AGYBackend instance to register.
    """
    global _active_backend
    _active_backend = backend


def reset_backend() -> None:
    """Reset the global backend override to None (reverting to BackendManager on next get)."""
    global _active_backend
    _active_backend = None
