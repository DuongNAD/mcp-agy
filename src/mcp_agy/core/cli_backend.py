"""Subprocess CLI execution backend for Google Antigravity (agy.EXE).

Spawns standalone agy CLI process, constructs command-line arguments,
and parses streaming NDJSON telemetry into structured execution results.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Set

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
from mcp_agy.utils.process import SubprocessOutcome, stream_subprocess_lines

logger = get_logger("mcp_agy.core.cli_backend")

FILE_MODIFICATION_TOOLS: Set[str] = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "sed_file",
    "notebook_edit",
}


def find_agy_executable() -> Optional[str]:
    """Locate agy executable binary across environment variables, PATH, and standard directories."""
    # 1. Custom environment variable override
    custom_path = os.environ.get("AGY_BIN_PATH") or os.environ.get("MCP_AGY_CLI_PATH")
    if custom_path and os.path.isfile(custom_path):
        return custom_path

    # 2. System PATH lookup
    for name in ("agy", "agy.exe", "agy.EXE"):
        which_path = shutil.which(name)
        if which_path and os.path.isfile(which_path):
            return which_path

    # 3. Windows standard AppData locations
    win_paths = [
        os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.EXE"),
        os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe"),
        r"C:\Users\Admin\AppData\Local\agy\bin\agy.EXE",
        r"C:\Users\Admin\AppData\Local\agy\bin\agy.exe",
    ]
    for p in win_paths:
        if os.path.isfile(p):
            return p

    # 4. Standard Gemini / Antigravity user config paths
    gemini_paths = [
        os.path.expanduser("~/.gemini/antigravity-cli/bin/agy.EXE"),
        os.path.expanduser("~/.gemini/antigravity-cli/bin/agy.exe"),
        os.path.expanduser("~/.gemini/antigravity-cli/bin/agy"),
        os.path.expanduser("~/AppData/Local/agy/bin/agy.EXE"),
    ]
    for p in gemini_paths:
        if os.path.isfile(p):
            return p

    return None


def _safe_dict(val: Any) -> Dict[str, Any]:
    """Ensure value is a dict, returning empty dict if None or not a dict."""
    return val if isinstance(val, dict) else {}


def _safe_int(val: Any, default: int = 0) -> int:
    """Safely convert value to non-negative int, returning default if None or invalid."""
    if val is None:
        return default
    try:
        res = int(val)
        return max(0, res)
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert value to non-negative float, returning default if None or invalid."""
    if val is None:
        return default
    try:
        res = float(val)
        return max(0.0, res)
    except (ValueError, TypeError):
        return default


def _parse_token_usage(u: Any, fallback: Optional[TokenUsage] = None) -> Optional[TokenUsage]:
    """Safely parse TokenUsage from arbitrary dict or object, defaulting nullable fields to 0 or fallback."""
    if not isinstance(u, dict):
        return fallback
    fb = fallback or TokenUsage()
    return TokenUsage(
        input_tokens=_safe_int(u.get("input_tokens"), fb.input_tokens),
        output_tokens=_safe_int(u.get("output_tokens"), fb.output_tokens),
        thinking_tokens=_safe_int(u.get("thinking_tokens"), fb.thinking_tokens),
        cache_read_tokens=_safe_int(u.get("cache_read_tokens"), fb.cache_read_tokens),
        total_tokens=_safe_int(u.get("total_tokens"), fb.total_tokens),
        execution_time_ms=_safe_float(u.get("execution_time_ms"), fb.execution_time_ms),
        cost_estimate=_safe_float(u.get("cost_estimate"), fb.cost_estimate),
    )


class SubprocessCLIBackend(AGYBackend):
    """Execution backend utilizing standalone agy CLI via asynchronous NDJSON streaming."""

    def __init__(
        self,
        executable_path: Optional[str] = None,
        default_model: Optional[str] = None,
        default_effort: Optional[str] = None,
    ) -> None:
        self._custom_path = executable_path
        self._cached_path: Optional[str] = None
        # Per-request `model`/`effort` win; these are the fallback when a caller does not
        # name one. Previously only PythonSDKBackend accepted a default model, so a model
        # chosen for a CLI run was dropped without a word and agy silently used its own
        # default — the run looked correct and was answered by a different model.
        self.default_model = default_model
        self.default_effort = default_effort

    @property
    def executable_path(self) -> Optional[str]:
        """Resolves the executable path dynamically or uses custom path."""
        if self._custom_path and os.path.isfile(self._custom_path):
            return self._custom_path
        if self._cached_path and os.path.isfile(self._cached_path):
            return self._cached_path
        found = find_agy_executable()
        if found:
            self._cached_path = found
        return found

    def is_available(self) -> bool:
        """Check if agy binary is present and executable."""
        path = self.executable_path
        return bool(path and os.path.isfile(path))

    def _build_command(
        self,
        prompt: str,
        workspace_path: str = "",
        auto_approve: bool = True,
        mode: str = "accept-edits",
        conversation_id: str = "",
        model: Optional[str] = None,
        effort: Optional[str] = None,
        timeout_seconds: int = 600,
    ) -> List[str]:
        """Build command line arguments list for agy executable invocation."""
        exe = self.executable_path
        if not exe:
            raise FileNotFoundError("agy executable not found in PATH or standard installation paths.")

        cmd = [
            exe,
            "--output-format",
            "stream-json",
            "--mode",
            mode,
        ]
        if auto_approve:
            cmd.append("--dangerously-skip-permissions")
        if workspace_path:
            cmd.extend(["--add-dir", workspace_path])
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])
        cmd.extend(["--print-timeout", f"{timeout_seconds}s"])
        cmd.extend(["--print", prompt])
        return cmd

    async def execute_stream(
        self,
        request: ExecutionRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Stream execution events from agy CLI in real-time as NDJSON lines arrive."""
        if not self.is_available():
            yield StreamEvent(
                event_type=StreamEventType.ERROR,
                data={"error": "agy executable not found."},
                conversation_id=request.session_id,
            )
            return

        cmd = self._build_command(
            prompt=request.prompt,
            workspace_path=request.workspace_path,
            auto_approve=request.auto_approve,
            mode=request.mode,
            conversation_id=request.session_id,
            model=request.model or self.default_model,
            effort=request.effort or self.default_effort,
            timeout_seconds=request.timeout_seconds,
        )

        conv_id = request.session_id
        valid_cwd = (
            request.workspace_path
            if (request.workspace_path and os.path.isdir(request.workspace_path))
            else None
        )
        async for line in stream_subprocess_lines(
            cmd,
            cwd=valid_cwd,
            timeout_seconds=float(request.timeout_seconds),
        ):
            try:
                event_data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if not isinstance(event_data, dict):
                continue

            try:
                raw_event = str(event_data.get("event") or "").strip()
                if raw_event == "init":
                    conv_id = str(event_data.get("conversation_id") or conv_id)
                    init_data = _safe_dict(event_data.get("init"))
                    yield StreamEvent(
                        event_type=StreamEventType.INIT,
                        data=init_data,
                        conversation_id=conv_id,
                    )

                elif raw_event == "step_update":
                    step = _safe_dict(event_data.get("step_update"))
                    if not conv_id:
                        conv_id = str(step.get("conversation_id") or "")

                    step_type = str(step.get("step_type") or "")
                    state = str(step.get("state") or "ACTIVE")

                    # Handle tool invocation
                    if step_type == "tool":
                        tool_info = _safe_dict(step.get("tool_info"))
                        tool_name = str(step.get("tool_name") or tool_info.get("name") or "unknown_tool")
                        tool_status = ToolStatus.DONE if state == "DONE" else ToolStatus.ACTIVE
                        tool_params = _safe_dict(tool_info.get("parameters"))
                        raw_output = tool_info.get("output")
                        tool_output = str(raw_output) if isinstance(raw_output, str) else None

                        tool_event = ToolEvent(
                            tool_name=tool_name,
                            tool_call_id=str(step.get("step_index") or ""),
                            status=tool_status,
                            arguments=tool_params,
                            output=tool_output,
                            duration_seconds=_safe_float(step.get("duration_seconds")),
                        )
                        yield StreamEvent(
                            event_type=StreamEventType.TOOL_CALL,
                            data=step,
                            conversation_id=conv_id,
                            tool_event=tool_event,
                        )

                    # Handle text delta
                    elif step_type == "agent_response" and "text_delta" in step:
                        usage_obj = _parse_token_usage(step.get("usage")) if "usage" in step else None
                        raw_delta = step.get("text_delta")
                        text_delta = str(raw_delta) if raw_delta is not None else ""
                        yield StreamEvent(
                            event_type=StreamEventType.TEXT_DELTA,
                            data=step,
                            conversation_id=conv_id,
                            text_delta=text_delta,
                            token_usage=usage_obj,
                        )

                    # Handle checkpoint
                    elif step_type == "checkpoint":
                        usage_obj = _parse_token_usage(step.get("usage")) if "usage" in step else None
                        yield StreamEvent(
                            event_type=StreamEventType.CHECKPOINT,
                            data=step,
                            conversation_id=conv_id,
                            token_usage=usage_obj,
                        )

                    else:
                        yield StreamEvent(
                            event_type=StreamEventType.STEP_UPDATE,
                            data=step,
                            conversation_id=conv_id,
                        )

                elif raw_event == "result":
                    res = _safe_dict(event_data.get("result"))
                    if not conv_id:
                        conv_id = str(res.get("conversation_id") or "")

                    usage_obj = _parse_token_usage(res.get("usage")) if "usage" in res else None
                    raw_resp = res.get("response")
                    text_delta = str(raw_resp) if raw_resp is not None else ""

                    yield StreamEvent(
                        event_type=StreamEventType.RESULT,
                        data=res,
                        conversation_id=conv_id,
                        text_delta=text_delta,
                        token_usage=usage_obj,
                    )
            except Exception as ev_err:
                logger.warning(f"Error parsing streamed NDJSON event line: {ev_err}")
                continue

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute a full autonomous request to completion via agy CLI."""
        start_time = time.monotonic()
        conv_id = request.session_id
        final_response_chunks: List[str] = []
        status = "success"
        tokens_used = TokenUsage()
        modified_files: Set[str] = set()
        tool_calls: List[ToolEvent] = []
        error_details: Optional[str] = None
        tool_errors: List[str] = []
        saw_result_event = False

        if not self.is_available():
            return ExecutionResult(
                success=False,
                status="error",
                response_text="",
                backend_used=BackendType.CLI,
                session_id=conv_id,
                error_message="agy executable not found in PATH or standard install directories.",
            )

        cmd = self._build_command(
            prompt=request.prompt,
            workspace_path=request.workspace_path,
            auto_approve=request.auto_approve,
            mode=request.mode,
            conversation_id=request.session_id,
            model=request.model or self.default_model,
            effort=request.effort or self.default_effort,
            timeout_seconds=request.timeout_seconds,
        )

        valid_cwd = (
            request.workspace_path
            if (request.workspace_path and os.path.isdir(request.workspace_path))
            else None
        )
        outcome = SubprocessOutcome()
        try:
            async for line in stream_subprocess_lines(
                cmd,
                cwd=valid_cwd,
                timeout_seconds=float(request.timeout_seconds),
                outcome=outcome,
            ):
                try:
                    event_data = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if not isinstance(event_data, dict):
                    continue

                try:
                    event_type = str(event_data.get("event") or "").strip()

                    if event_type == "init":
                        conv_id = str(event_data.get("conversation_id") or conv_id)

                    elif event_type == "step_update":
                        step = _safe_dict(event_data.get("step_update"))
                        if not conv_id:
                            conv_id = str(step.get("conversation_id") or "")

                        step_type = str(step.get("step_type") or "")
                        state = str(step.get("state") or "")

                        # Track file modifications and tool executions
                        if step_type == "tool":
                            tool_info = _safe_dict(step.get("tool_info"))
                            tool_name = str(step.get("tool_name") or tool_info.get("name") or "unknown_tool")

                            # Track file modifications
                            if tool_name in FILE_MODIFICATION_TOOLS:
                                params = _safe_dict(tool_info.get("parameters"))
                                target = params.get("TargetFile") or params.get("file_path") or params.get("path")
                                if target and isinstance(target, str):
                                    if request.workspace_path and os.path.isabs(target):
                                        try:
                                            rel = os.path.relpath(target, request.workspace_path)
                                            if not rel.startswith(".."):
                                                modified_files.add(rel.replace("\\", "/"))
                                            else:
                                                modified_files.add(target.replace("\\", "/"))
                                        except ValueError:
                                            modified_files.add(target.replace("\\", "/"))
                                    else:
                                        modified_files.add(str(target).replace("\\", "/"))

                            if state == "DONE":
                                params = _safe_dict(tool_info.get("parameters"))
                                raw_output = tool_info.get("output")
                                tool_output = str(raw_output) if isinstance(raw_output, str) else None
                                tool_calls.append(
                                    ToolEvent(
                                        tool_name=tool_name,
                                        tool_call_id=str(step.get("step_index") or ""),
                                        status=ToolStatus.DONE,
                                        arguments=params,
                                        output=tool_output,
                                        duration_seconds=_safe_float(step.get("duration_seconds")),
                                    )
                                )

                            elif state in ("ERROR", "FAILED"):
                                # A tool that fails is reported here and nowhere else: the run
                                # can still end on `result.status: SUCCESS` with an empty
                                # response, which is how a permission denial reached the
                                # architect as a successful call that simply said nothing.
                                params = _safe_dict(tool_info.get("parameters"))
                                err_msg = str(
                                    _safe_dict(tool_info.get("error")).get("message") or ""
                                ).strip()
                                tool_calls.append(
                                    ToolEvent(
                                        tool_name=tool_name,
                                        tool_call_id=str(step.get("step_index") or ""),
                                        status=ToolStatus.FAILED,
                                        arguments=params,
                                        output=None,
                                        duration_seconds=_safe_float(step.get("duration_seconds")),
                                        error=err_msg or None,
                                    )
                                )
                                tool_errors.append(
                                    f"{tool_name}: {err_msg}" if err_msg else f"{tool_name}: failed"
                                )

                        elif step_type == "agent_response" and "text_delta" in step:
                            raw_delta = step.get("text_delta")
                            if raw_delta is not None:
                                final_response_chunks.append(str(raw_delta))

                        if "usage" in step and state == "DONE":
                            parsed_u = _parse_token_usage(step.get("usage"), fallback=tokens_used)
                            if parsed_u:
                                tokens_used = parsed_u

                    elif event_type == "result":
                        res = _safe_dict(event_data.get("result"))
                        saw_result_event = True
                        if not conv_id:
                            conv_id = str(res.get("conversation_id") or "")
                        raw_status = str(res.get("status") or "SUCCESS").upper()
                        status = "success" if raw_status == "SUCCESS" else "error"
                        if status == "error":
                            # The CLI puts the reason in `result.error` - an invalid model or
                            # effort selection, a refused workspace, an auth failure. Dropping
                            # it left the architect holding status="error" and error_details
                            # null: a failure with nothing to act on.
                            error_details = (
                                str(res.get("error") or "").strip()
                                or f"agy CLI reported status {raw_status} without an error message."
                            )
                        if "response" in res and res["response"] is not None:
                            final_response_chunks = [str(res["response"])]
                        if "usage" in res:
                            parsed_u = _parse_token_usage(res.get("usage"), fallback=tokens_used)
                            if parsed_u:
                                tokens_used = parsed_u
                except Exception as line_err:
                    logger.warning(f"Error processing NDJSON line in execute: {line_err}")
                    continue

        except asyncio.TimeoutError:
            duration = time.monotonic() - start_time
            return ExecutionResult(
                success=False,
                status="timeout",
                response_text="".join(final_response_chunks),
                tool_calls=tool_calls,
                token_usage=tokens_used,
                backend_used=BackendType.CLI,
                duration_seconds=round(duration, 4),
                duration_ms=round(duration * 1000.0, 2),
                session_id=conv_id,
                modified_files=sorted(list(modified_files)),
                diff_summary=f"{len(modified_files)} file(s) modified before timeout",
                error_message=(
                    f"Subprocess execution timed out after {request.timeout_seconds} seconds"
                    + (f". agy stderr: {outcome.stderr_tail}" if outcome.stderr_tail else "")
                ),
            )
        except Exception as e:
            duration = time.monotonic() - start_time
            logger.exception(f"CLI backend execution failed: {e}")
            return ExecutionResult(
                success=False,
                status="error",
                response_text="".join(final_response_chunks),
                tool_calls=tool_calls,
                token_usage=tokens_used,
                backend_used=BackendType.CLI,
                duration_seconds=round(duration, 4),
                duration_ms=round(duration * 1000.0, 2),
                session_id=conv_id,
                modified_files=sorted(list(modified_files)),
                diff_summary=f"{len(modified_files)} file(s) modified",
                error_message=f"CLI subprocess execution error: {str(e)}",
            )

        duration = time.monotonic() - start_time
        response_text = "".join(final_response_chunks)
        diff_summary = f"Modified {len(modified_files)} file(s)" if modified_files else "No files modified"

        # A run that answered nothing, changed nothing and reported SUCCESS is not a success -
        # it is a failure whose evidence landed somewhere this parser used to ignore. Seen live:
        # agy denied its own `run_command` under headless permissions, wrote the reason to
        # stderr, then emitted `result.status: SUCCESS` with an empty response. Only claim an
        # error when there is evidence for one; an empty answer with nothing else wrong stays
        # a success, as it always was.
        if status == "success" and not response_text.strip() and not modified_files:
            evidence: List[str] = []
            if tool_errors:
                evidence.append("Tools that failed during the run: " + "; ".join(tool_errors[:5]))
            if outcome.stderr_tail:
                evidence.append(f"agy stderr: {outcome.stderr_tail}")
            if outcome.returncode not in (None, 0):
                evidence.append(f"agy exited with code {outcome.returncode}")
            elif not saw_result_event:
                evidence.append("agy produced no result event")
            if evidence:
                status = "error"
                error_details = "AGY returned an empty response. " + " | ".join(evidence)

        return ExecutionResult(
            success=(status == "success"),
            status=status,
            response_text=response_text,
            tool_calls=tool_calls,
            token_usage=tokens_used,
            backend_used=BackendType.CLI,
            duration_seconds=round(duration, 4),
            duration_ms=round(duration * 1000.0, 2),
            session_id=conv_id,
            modified_files=sorted(list(modified_files)),
            diff_summary=diff_summary,
            error_message=error_details,
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
        """Execute autonomous task via CLI subprocess."""
        request = ExecutionRequest(
            prompt=prompt,
            workspace_path=workspace_path,
            auto_approve=auto_approve,
            mode=mode if mode in ("accept-edits", "plan") else "accept-edits",
            timeout_seconds=timeout_seconds,
            backend_preference=BackendType.CLI,
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
        """Execute read-only consultation via CLI subprocess."""
        request = ExecutionRequest(
            prompt=prompt,
            workspace_path=workspace_path,
            auto_approve=True,
            mode="plan",
            session_id=conversation_id,
            timeout_seconds=timeout_seconds,
            backend_preference=BackendType.CLI,
            model=model or self.default_model,
            effort=effort or self.default_effort,
        )
        result = await self.execute(request)
        return result.to_chat_result()
