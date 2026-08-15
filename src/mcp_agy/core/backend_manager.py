"""Backend Manager orchestrating the 3-Tier Fallback Hierarchy for AGY.

Hierarchy:
  Tier 1: PythonSDKBackend (in-process via `google.antigravity`)
  Tier 2: SubprocessCLIBackend (native `agy.EXE` subprocess with NDJSON streaming)
  Tier 3: MockAGYBackend (high-fidelity simulated engine)
"""

from __future__ import annotations

import os
from typing import AsyncIterator, Optional, Tuple

from mcp_agy.core.backend import AGYBackend
from mcp_agy.core.cli_backend import SubprocessCLIBackend
from mcp_agy.core.mock_backend import MockAGYBackend
from mcp_agy.core.models import (
    BackendType,
    ChatResult,
    ExecutionRequest,
    ExecutionResult,
    StreamEvent,
    StreamEventType,
    TaskExecutionResult,
)
from mcp_agy.core.sdk_backend import PythonSDKBackend
from mcp_agy.utils.logger import get_logger

logger = get_logger("mcp_agy.backend_manager")


class BackendManager(AGYBackend):
    """Coordinates backend selection, probing, and 3-tier fallback execution."""

    def __init__(
        self,
        preferred_backend: Optional[str] = None,
        auto_fallback: bool = False,
        cli_executable_path: Optional[str] = None,
        default_model: Optional[str] = None,
        default_effort: Optional[str] = None,
    ) -> None:
        env_pref = os.environ.get("MCP_AGY_BACKEND")
        self.preferred_backend = (preferred_backend or env_pref or "auto").lower()

        # Defaults to OFF, deliberately. With fallback on, a real backend that errors is
        # replaced by MockAGYBackend and the caller receives a fabricated `status="success"`
        # with the failure demoted to a note appended to the response text. For an architect
        # agent driving this over MCP that is the worst possible failure mode: it reports work
        # that never happened. Opt in with MCP_AGY_AUTO_FALLBACK=1 when a simulated answer is
        # genuinely better than an error.
        env_fallback = os.environ.get("MCP_AGY_AUTO_FALLBACK")
        if env_fallback is not None:
            self.auto_fallback = env_fallback.strip().lower() in ("1", "true", "yes", "on")
        else:
            self.auto_fallback = auto_fallback

        # MCP_AGY_DEFAULT_MODEL is the name the README documents and the one users copy into
        # their client config; MCP_AGY_MODEL is what this code originally read. Both are
        # accepted so a documented config is not silently answered by a different model.
        self.default_model = (
            default_model
            or os.environ.get("MCP_AGY_MODEL")
            or os.environ.get("MCP_AGY_DEFAULT_MODEL")
            or None
        )
        self.default_effort = default_effort or os.environ.get("MCP_AGY_EFFORT") or None

        self.sdk_backend = PythonSDKBackend(default_model=self.default_model)
        self.cli_backend = SubprocessCLIBackend(
            executable_path=cli_executable_path,
            default_model=self.default_model,
            default_effort=self.default_effort,
        )
        self.mock_backend = MockAGYBackend()

    def get_best_available_backend(
        self,
        override_pref: Optional[BackendType] = None,
    ) -> Tuple[AGYBackend, BackendType]:
        """Probe and return the highest-priority functional backend instance and type."""
        pref_str = (
            override_pref.value
            if isinstance(override_pref, BackendType)
            else (override_pref or self.preferred_backend)
        )
        pref = pref_str.lower()

        # Explicit preference override handling
        if pref == "sdk":
            if self.sdk_backend.is_available():
                return self.sdk_backend, BackendType.SDK
            if not self.auto_fallback:
                raise RuntimeError("Preferred backend 'sdk' is unavailable and auto_fallback is disabled.")
            logger.warning("Preferred backend 'sdk' is unavailable; falling back to CLI or Mock.")

        elif pref == "cli":
            if self.cli_backend.is_available():
                return self.cli_backend, BackendType.CLI
            if not self.auto_fallback:
                raise RuntimeError("Preferred backend 'cli' is unavailable and auto_fallback is disabled.")
            logger.warning("Preferred backend 'cli' is unavailable; falling back to Mock.")

        elif pref == "mock":
            return self.mock_backend, BackendType.MOCK

        # Standard 3-tier resolution hierarchy: SDK -> CLI -> Mock
        if self.sdk_backend.is_available():
            logger.debug("Selected Tier 1 backend: PythonSDKBackend")
            return self.sdk_backend, BackendType.SDK

        if self.cli_backend.is_available():
            logger.debug("Selected Tier 2 backend: SubprocessCLIBackend")
            return self.cli_backend, BackendType.CLI

        # Loud, not debug: reaching here means neither the SDK nor the agy binary was found,
        # so every result from now on is simulated. At debug level that fact never reaches the
        # caller's log and a fully mocked session is indistinguishable from a real one.
        logger.warning(
            "No real AGY backend available (SDK missing, agy executable not found) - falling "
            "back to the SIMULATED MockAGYBackend. Results will be fabricated, not real. Set "
            "AGY_BIN_PATH or MCP_AGY_CLI_PATH to point at agy, or MCP_AGY_BACKEND=cli with "
            "MCP_AGY_AUTO_FALLBACK=0 to make this an error instead."
        )
        return self.mock_backend, BackendType.MOCK

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute request with 3-tier fallback resilience."""
        pref = request.backend_preference if request.backend_preference != BackendType.AUTO else None
        backend, backend_type = self.get_best_available_backend(pref)

        logger.info(f"Executing request with backend={backend_type.value}")
        if hasattr(backend, "execute"):
            result = await backend.execute(request)
        else:
            task_res = await backend.execute_task(
                workspace_path=request.workspace_path,
                prompt=request.prompt,
                auto_approve=request.auto_approve,
                mode=request.mode,
                timeout_seconds=request.timeout_seconds,
            )
            result = ExecutionResult(
                success=(task_res.status == "success"),
                status=task_res.status,
                response_text=task_res.response,
                token_usage=task_res.tokens_used,
                backend_used=backend_type,
                duration_seconds=task_res.duration_seconds,
                session_id=task_res.conversation_id,
                modified_files=task_res.modified_files,
                diff_summary=task_res.diff_summary,
                error_message=task_res.error_details,
            )

        # Fallback handling on fatal error if enabled and not already on mock
        if result.status == "error" and self.auto_fallback and backend_type != BackendType.MOCK:
            logger.warning(
                f"Backend '{backend_type.value}' returned error ({result.error_message}); "
                f"falling back to Mock engine."
            )
            fallback_res = await self.mock_backend.execute(request)
            note = (
                f"\n\n[Note: Fallback to mock backend occurred after {backend_type.value} "
                f"error: {result.error_message}]"
            )
            fallback_res.response_text += note
            return fallback_res

        return result

    async def execute_stream(self, request: ExecutionRequest) -> AsyncIterator[StreamEvent]:
        """Stream execution events from the best available backend."""
        pref = request.backend_preference if request.backend_preference != BackendType.AUTO else None
        backend, backend_type = self.get_best_available_backend(pref)

        if hasattr(backend, "execute_stream"):
            async for event in backend.execute_stream(request):
                yield event
        else:
            # Fallback stream adapter
            res = await self.execute(request)
            yield StreamEvent(
                event_type=StreamEventType.INIT,
                conversation_id=res.session_id,
                data={"backend": backend_type.value},
            )
            yield StreamEvent(
                event_type=StreamEventType.RESULT,
                conversation_id=res.session_id,
                text_delta=res.response_text,
                token_usage=res.token_usage,
            )

    async def execute_task(
        self,
        workspace_path: str,
        prompt: str,
        auto_approve: bool = True,
        mode: str = "accept-edits",
        timeout_seconds: int = 600,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> TaskExecutionResult:
        """Execute autonomous task on the best available backend with fallback resilience."""
        # `model`/`effort` are forwarded, not dropped. They used to be absent from this
        # ExecutionRequest entirely, so a caller naming a model got agy's default instead and
        # nothing said so.
        request = ExecutionRequest(
            prompt=prompt,
            workspace_path=workspace_path,
            auto_approve=auto_approve,
            mode=mode if mode in ("accept-edits", "plan") else "accept-edits",
            timeout_seconds=timeout_seconds,
            model=model or self.default_model,
            effort=effort or self.default_effort,
        )
        result = await self.execute(request)
        return result.to_task_execution_result()

    async def chat(
        self,
        prompt: str,
        workspace_path: str = "",
        conversation_id: str = "",
        timeout_seconds: int = 300,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> ChatResult:
        """Execute chat consultation on the best available backend with fallback resilience."""
        request = ExecutionRequest(
            prompt=prompt,
            workspace_path=workspace_path,
            auto_approve=True,
            mode="plan",
            session_id=conversation_id,
            timeout_seconds=timeout_seconds,
            model=model or self.default_model,
            effort=effort or self.default_effort,
        )
        result = await self.execute(request)
        return result.to_chat_result()


# Global singleton manager
_global_backend_manager: Optional[BackendManager] = None


def get_backend_manager() -> BackendManager:
    """Retrieve or initialize the global BackendManager singleton."""
    global _global_backend_manager
    if _global_backend_manager is None:
        _global_backend_manager = BackendManager()
    return _global_backend_manager


def set_backend_manager(manager: BackendManager) -> None:
    """Set the global BackendManager singleton instance."""
    global _global_backend_manager
    _global_backend_manager = manager


def reset_backend_manager() -> None:
    """Reset the global BackendManager singleton."""
    global _global_backend_manager
    _global_backend_manager = None
