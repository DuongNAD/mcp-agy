"""High-Fidelity Mock Backend for Google Antigravity (AGY).

Simulates realistic autonomous coding execution, NDJSON streaming events,
synthetic file creation, strict read-only invariants, and programmable test controls.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
import uuid

from mcp_agy.core.backend import AGYBackend
from mcp_agy.core.models import (
    BackendType,
    ChatResult,
    ExecutionRequest,
    ExecutionResult,
    StreamEvent,
    StreamEventType,
    TaskExecutionResult,
    TokenUsage,
    ToolEvent,
    ToolStatus,
)
from mcp_agy.utils.logger import get_logger

logger = get_logger("mcp_agy.backend.mock")


class MockAGYBackend(AGYBackend):
    """High-fidelity simulated AGY backend for offline testing, CI environments, and Tier 3 fallback."""

    def __init__(
        self,
        default_response: str = "Mock AGY execution completed successfully.",
        simulated_delay: float = 0.0,
        token_usage_override: Optional[TokenUsage] = None,
        auto_synthesize_edits: bool = True,
    ) -> None:
        self.default_response = default_response
        self.simulated_delay = simulated_delay
        self.token_usage_override = token_usage_override
        self.auto_synthesize_edits = auto_synthesize_edits
        self.force_timeout: bool = False
        self.force_error: Optional[str] = None
        self.call_history: List[Dict[str, Any]] = []
        self.executed_tasks: List[Dict[str, Any]] = []
        self.chat_history: List[Dict[str, Any]] = []
        self.custom_task_handlers: List[Callable[..., Optional[TaskExecutionResult]]] = []
        self.custom_chat_handlers: List[Callable[..., Optional[ChatResult]]] = []
        self.synthetic_file_rules: List[Tuple[str, str, str]] = []

        # Default rules for synthetic file generation in accept-edits mode
        self.add_task_rule(
            "calculator",
            "calculator.py",
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def subtract(a: int, b: int) -> int:\n    return a - b\n",
        )
        self.add_task_rule(
            "auth",
            "src/auth.py",
            "class AuthService:\n"
            "    def authenticate(self, user: str, token: str) -> bool:\n"
            "        return token == 'valid_token'\n",
        )
        self.add_task_rule(
            "math",
            "math_utils.py",
            "def multiply(a: float, b: float) -> float:\n    return a * b\n\n"
            "def divide(a: float, b: float) -> float:\n"
            "    if b == 0:\n"
            "        raise ZeroDivisionError('division by zero')\n"
            "    return a / b\n",
        )

    def is_available(self) -> bool:
        """Mock backend is always available."""
        return True

    def add_task_rule(self, keyword: str, rel_path: str, content: str) -> None:
        """Register a keyword-triggered synthetic file creation rule."""
        self.synthetic_file_rules.append((keyword.lower(), rel_path, content))

    def register_task_handler(self, handler: Callable[..., Optional[TaskExecutionResult]]) -> None:
        """Register custom task callback handler."""
        self.custom_task_handlers.append(handler)

    def register_chat_handler(self, handler: Callable[..., Optional[ChatResult]]) -> None:
        """Register custom chat callback handler."""
        self.custom_chat_handlers.append(handler)

    def reset(self) -> None:
        """Reset mock history, failure injection, and custom callbacks."""
        self.call_history.clear()
        self.executed_tasks.clear()
        self.chat_history.clear()
        self.custom_task_handlers.clear()
        self.custom_chat_handlers.clear()
        self.force_timeout = False
        self.force_error = None
        self.simulated_delay = 0.0

    async def execute_stream(
        self,
        request: ExecutionRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Yield simulated streaming events mimicking AGY NDJSON output."""
        session_id = request.session_id or f"mock-session-{uuid.uuid4().hex[:8]}"

        # 1. Init event
        yield StreamEvent(
            event_type=StreamEventType.INIT,
            data={"cwd": request.workspace_path, "model": request.model or "mock-gemini"},
            conversation_id=session_id,
        )

        # 2. Tool call: view_file
        tool_1 = ToolEvent(
            tool_name="view_file",
            tool_call_id="1",
            status=ToolStatus.ACTIVE,
            arguments={"path": "README.md"},
        )
        yield StreamEvent(
            event_type=StreamEventType.TOOL_CALL,
            conversation_id=session_id,
            tool_event=tool_1,
        )

        tool_1_done = ToolEvent(
            tool_name="view_file",
            tool_call_id="1",
            status=ToolStatus.DONE,
            arguments={"path": "README.md"},
            output="README content loaded.",
            duration_seconds=0.01,
        )
        yield StreamEvent(
            event_type=StreamEventType.TOOL_CALL,
            conversation_id=session_id,
            tool_event=tool_1_done,
        )

        # 3. Tool call: write_to_file (only if accept-edits)
        if request.mode == "accept-edits":
            tool_2 = ToolEvent(
                tool_name="write_to_file",
                tool_call_id="2",
                status=ToolStatus.ACTIVE,
                arguments={"TargetFile": "src/output.py"},
            )
            yield StreamEvent(
                event_type=StreamEventType.TOOL_CALL,
                conversation_id=session_id,
                tool_event=tool_2,
            )

            tool_2_done = ToolEvent(
                tool_name="write_to_file",
                tool_call_id="2",
                status=ToolStatus.DONE,
                arguments={"TargetFile": "src/output.py"},
                output="File created successfully.",
                duration_seconds=0.02,
            )
            yield StreamEvent(
                event_type=StreamEventType.TOOL_CALL,
                conversation_id=session_id,
                tool_event=tool_2_done,
            )

        # 4. Text delta
        text_chunk = f"Task completed successfully: {request.prompt[:60]}..."
        yield StreamEvent(
            event_type=StreamEventType.TEXT_DELTA,
            conversation_id=session_id,
            text_delta=text_chunk,
        )

        # 5. Result event
        token_usage = self.token_usage_override or TokenUsage(
            input_tokens=150,
            output_tokens=50,
            thinking_tokens=25,
            cache_read_tokens=0,
            total_tokens=225,
        )
        yield StreamEvent(
            event_type=StreamEventType.RESULT,
            data={"status": "SUCCESS", "response": text_chunk},
            conversation_id=session_id,
            text_delta=text_chunk,
            token_usage=token_usage,
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute request simulation with genuine state and file synthesis."""
        start_time = time.monotonic()
        conv_id = request.session_id or f"mock-session-{uuid.uuid4().hex[:8]}"

        call_record = {
            "method": "execute",
            "conversation_id": conv_id,
            "workspace_path": request.workspace_path,
            "prompt": request.prompt,
            "auto_approve": request.auto_approve,
            "mode": request.mode,
            "timeout_seconds": request.timeout_seconds,
            "timestamp": time.time(),
        }
        self.call_history.append(call_record)

        if self.simulated_delay > 0:
            await asyncio.sleep(self.simulated_delay)

        if self.force_timeout:
            duration = time.monotonic() - start_time
            return ExecutionResult(
                success=False,
                status="timeout",
                response_text="",
                backend_used=BackendType.MOCK,
                duration_seconds=round(duration, 4),
                duration_ms=round(duration * 1000.0, 2),
                session_id=conv_id,
                error_message=f"Mock execution timed out after {request.timeout_seconds}s",
            )

        if self.force_error:
            duration = time.monotonic() - start_time
            return ExecutionResult(
                success=False,
                status="error",
                response_text="",
                backend_used=BackendType.MOCK,
                duration_seconds=round(duration, 4),
                duration_ms=round(duration * 1000.0, 2),
                session_id=conv_id,
                error_message=self.force_error,
            )

        modified_files: List[str] = []
        # STRICT INVARIANT: Only modify files if mode == "accept-edits"
        can_synthesize = (
            request.mode == "accept-edits"
            and self.auto_synthesize_edits
            and bool(request.workspace_path)
            and os.path.isdir(request.workspace_path)
        )
        if can_synthesize:
            ws_path = Path(request.workspace_path)
            prompt_lower = request.prompt.lower()

            for kw, rel_path, content in self.synthetic_file_rules:
                if kw in prompt_lower:
                    target_file = ws_path / rel_path
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_text(content, encoding="utf-8")
                    norm_rel = str(rel_path).replace("\\", "/")
                    if norm_rel not in modified_files:
                        modified_files.append(norm_rel)

            matches = re.findall(
                r"(?:create|implement|write|add|generate)\s+([a-zA-Z0-9_\-/\\]+\.[a-zA-Z0-9]+)",
                request.prompt,
                re.IGNORECASE,
            )
            for filename in matches:
                target_file = ws_path / filename
                if not target_file.exists():
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_text(f"# Generated by AGY for task: {request.prompt}\n", encoding="utf-8")
                    norm_rel = str(filename).replace("\\", "/")
                    if norm_rel not in modified_files:
                        modified_files.append(norm_rel)

        duration = time.monotonic() - start_time + (self.simulated_delay or 0.02)
        diff_summary = f"Modified {len(modified_files)} file(s)" if modified_files else "No files modified"
        response_text = (
            f"Mock AGY execution completed successfully for task: '{request.prompt}'. "
            f"Workspace: '{request.workspace_path or 'none'}'. Files modified: {modified_files}."
        )

        words = len(request.prompt.split())
        tokens = self.token_usage_override or TokenUsage(
            input_tokens=max(15, words * 4),
            output_tokens=max(30, len(response_text.split()) * 2),
            thinking_tokens=20,
            cache_read_tokens=0,
            total_tokens=max(65, words * 4 + len(response_text.split()) * 2 + 20),
            execution_time_ms=round(duration * 1000.0, 2),
        )

        return ExecutionResult(
            success=True,
            status="success",
            response_text=response_text,
            token_usage=tokens,
            backend_used=BackendType.MOCK,
            duration_seconds=round(duration, 4),
            duration_ms=round(duration * 1000.0, 2),
            session_id=conv_id,
            modified_files=modified_files,
            diff_summary=diff_summary,
            error_message=None,
        )

    async def execute_task(
        self,
        workspace_path: str,
        prompt: str,
        auto_approve: bool = True,
        mode: str = "accept-edits",
        timeout_seconds: int = 600,
    ) -> TaskExecutionResult:
        """Simulates autonomous task execution with call tracking and custom handlers."""
        conv_id = f"mock-task-{uuid.uuid4().hex[:8]}"

        call_record = {
            "method": "execute_task",
            "conversation_id": conv_id,
            "workspace_path": workspace_path,
            "prompt": prompt,
            "auto_approve": auto_approve,
            "mode": mode,
            "timeout_seconds": timeout_seconds,
            "timestamp": time.time(),
        }
        self.call_history.append(call_record)
        self.executed_tasks.append(call_record)

        # Check custom handlers
        for handler in self.custom_task_handlers:
            res = handler(
                workspace_path=workspace_path,
                prompt=prompt,
                auto_approve=auto_approve,
                mode=mode,
                timeout_seconds=timeout_seconds,
            )
            if res is not None:
                return res

        request = ExecutionRequest(
            prompt=prompt,
            workspace_path=workspace_path,
            auto_approve=auto_approve,
            mode=mode if mode in ("accept-edits", "plan") else "accept-edits",
            session_id=conv_id,
            timeout_seconds=timeout_seconds,
            backend_preference=BackendType.MOCK,
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
        """Simulates analytical consultation in strict read-only mode."""
        conv_id = conversation_id or f"mock-chat-{uuid.uuid4().hex[:8]}"

        call_record = {
            "method": "chat",
            "conversation_id": conv_id,
            "workspace_path": workspace_path,
            "prompt": prompt,
            "timeout_seconds": timeout_seconds,
            "timestamp": time.time(),
        }
        self.call_history.append(call_record)
        self.chat_history.append(call_record)

        # Check custom chat handlers
        for handler in self.custom_chat_handlers:
            res = handler(
                prompt=prompt,
                workspace_path=workspace_path,
                conversation_id=conversation_id,
                timeout_seconds=timeout_seconds,
            )
            if res is not None:
                return res

        if self.simulated_delay > 0:
            await asyncio.sleep(self.simulated_delay)

        if self.force_timeout:
            return ChatResult(
                status="timeout",
                conversation_id=conv_id,
                response="",
                error_details=f"Chat consultation timed out after {timeout_seconds}s",
                backend_used="mock",
            )

        if self.force_error:
            return ChatResult(
                status="error",
                conversation_id=conv_id,
                response="",
                error_details=self.force_error,
                backend_used="mock",
            )

        start_time = time.monotonic()
        duration = time.monotonic() - start_time + (self.simulated_delay or 0.01)
        response_text = (
            f"Mock AGY analytical guidance for query: '{prompt}'. "
            f"Workspace context: '{workspace_path or 'none'}'."
        )

        words = len(prompt.split())
        tokens = self.token_usage_override or TokenUsage(
            input_tokens=max(10, words * 3),
            output_tokens=max(20, len(response_text.split()) * 2),
            thinking_tokens=15,
            cache_read_tokens=0,
            total_tokens=max(45, words * 3 + len(response_text.split()) * 2 + 15),
            execution_time_ms=round(duration * 1000.0, 2),
        )

        return ChatResult(
            status="success",
            conversation_id=conv_id,
            response=response_text,
            duration_seconds=round(duration, 4),
            tokens_used=tokens,
            backend_used="mock",
            error_details=None,
        )
