"""Python SDK execution backend for Google Antigravity (AGY).

Integrates with the official `google.antigravity` SDK using dynamic, defensive imports.
Wraps `Agent`, `LocalAgentConfig`, `CapabilitiesConfig`, and `policy.allow_all()` in an
async execution context with streaming token and thought collection.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional
import uuid

from mcp_agy.core.backend import AGYBackend
from mcp_agy.core.models import (
    BackendType,
    ChatResult,
    ExecutionRequest,
    ExecutionResult,
    TaskExecutionResult,
    TokenUsage,
)
from mcp_agy.utils.logger import get_logger

logger = get_logger("mcp_agy.backend.sdk")


def is_sdk_available() -> bool:
    """Probe whether the google.antigravity Python SDK can be imported and initialized safely.

    Catches all import errors, Protobuf runtime mismatches, and C-extension failures.
    """
    try:
        import google.antigravity  # noqa: F401
        from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig  # noqa: F401
        from google.antigravity.hooks import policy  # noqa: F401
        return True
    except Exception as e:
        logger.debug(f"google.antigravity SDK probe failed (expected in environments without SDK): {e}")
        return False


class PythonSDKBackend(AGYBackend):
    """Execution backend utilizing the official `google.antigravity` Python SDK."""

    def __init__(self, default_model: Optional[str] = None) -> None:
        self.default_model = default_model

    def is_available(self) -> bool:
        """Check if SDK backend is functional in the current Python environment."""
        return is_sdk_available()

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute request using google.antigravity Agent."""
        if not is_sdk_available():
            return ExecutionResult(
                success=False,
                status="error",
                response_text="",
                backend_used=BackendType.SDK,
                session_id=request.session_id,
                error_message="google.antigravity SDK is not available or failed runtime initialization.",
            )

        from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig, types
        policies = []
        try:
            from google.antigravity.hooks import policy
            policies = [policy.allow_all()] if request.auto_approve else [policy.confirm_run_command()]
        except Exception:
            pass

        start_time = time.monotonic()
        conv_id = request.session_id or f"sdk-session-{uuid.uuid4().hex[:8]}"

        # Configure capabilities based on mode
        if request.mode == "plan":
            capabilities = CapabilitiesConfig(enabled_tools=types.BuiltinTools.read_only())
        else:
            capabilities = CapabilitiesConfig()

        workspaces = [request.workspace_path] if request.workspace_path else []
        config = LocalAgentConfig(
            workspaces=workspaces,
            capabilities=capabilities,
            policies=policies,
            conversation_id=request.session_id or None,
            model=request.model or self.default_model,
        )

        try:
            async def _run_agent() -> ExecutionResult:
                async with Agent(config) as agent:
                    response_obj = await agent.chat(request.prompt)

                    # Consume streamed tokens
                    tokens: List[str] = []
                    async for token in response_obj:
                        tokens.append(str(token))
                    full_response = "".join(tokens)

                    # Extract usage metadata if available
                    usage = getattr(response_obj, "usage_metadata", None)
                    token_usage = TokenUsage(
                        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                        thinking_tokens=getattr(usage, "thoughts_token_count", 0) or 0,
                        cache_read_tokens=getattr(usage, "cached_content_token_count", 0) or 0,
                        total_tokens=getattr(usage, "total_token_count", 0) or 0,
                    )

                    duration = time.monotonic() - start_time
                    assigned_conv_id = agent.conversation_id or conv_id

                    return ExecutionResult(
                        success=True,
                        status="success",
                        response_text=full_response,
                        token_usage=token_usage,
                        backend_used=BackendType.SDK,
                        duration_seconds=round(duration, 4),
                        duration_ms=round(duration * 1000.0, 2),
                        session_id=assigned_conv_id,
                        modified_files=[],
                        diff_summary="",
                    )

            return await asyncio.wait_for(_run_agent(), timeout=float(request.timeout_seconds))

        except asyncio.TimeoutError:
            duration = time.monotonic() - start_time
            logger.error(f"SDK execution timed out after {request.timeout_seconds}s")
            return ExecutionResult(
                success=False,
                status="timeout",
                response_text="",
                token_usage=TokenUsage(),
                backend_used=BackendType.SDK,
                duration_seconds=round(duration, 4),
                duration_ms=round(duration * 1000.0, 2),
                session_id=conv_id,
                error_message=f"SDK execution timed out after {request.timeout_seconds} seconds.",
            )
        except Exception as e:
            duration = time.monotonic() - start_time
            logger.exception(f"SDK execution failed: {e}")
            return ExecutionResult(
                success=False,
                status="error",
                response_text="",
                token_usage=TokenUsage(),
                backend_used=BackendType.SDK,
                duration_seconds=round(duration, 4),
                duration_ms=round(duration * 1000.0, 2),
                session_id=conv_id,
                error_message=f"SDK execution error: {str(e)}",
            )

    async def execute_task(
        self,
        workspace_path: str,
        prompt: str,
        auto_approve: bool = True,
        mode: str = "accept-edits",
        timeout_seconds: int = 600,
    ) -> TaskExecutionResult:
        """Execute autonomous task via SDK Agent."""
        request = ExecutionRequest(
            prompt=prompt,
            workspace_path=workspace_path,
            auto_approve=auto_approve,
            mode=mode if mode in ("accept-edits", "plan") else "accept-edits",
            timeout_seconds=timeout_seconds,
            backend_preference=BackendType.SDK,
        )
        result = await self.execute(request)
        return result.to_task_execution_result()

    async def chat(
        self,
        prompt: str,
        workspace_path: str = "",
        conversation_id: str = "",
        timeout_seconds: int = 300,
    ) -> ChatResult:
        """Execute read-only consultation via SDK Agent."""
        request = ExecutionRequest(
            prompt=prompt,
            workspace_path=workspace_path,
            auto_approve=True,
            mode="plan",
            session_id=conversation_id,
            timeout_seconds=timeout_seconds,
            backend_preference=BackendType.SDK,
        )
        result = await self.execute(request)
        return result.to_chat_result()
