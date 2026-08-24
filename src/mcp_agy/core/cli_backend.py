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
from mcp_agy.utils.process import SubprocessOutcome, run_subprocess_async, stream_subprocess_lines

logger = get_logger("mcp_agy.core.cli_backend")

# Tool names whose parameters name a file the run is about to write. This is a *hint*, not the
# record: it is an allow-list of another program's tool names, so it goes stale the moment agy
# renames one or adds another, and it never sees a write that arrives some other way - a shell
# redirect, `sed -i` inside `run_command`, a patch applied by a tool not listed here. Ground
# truth comes from `_detect_changed_files` below; this set only makes the common case cheap.
FILE_MODIFICATION_TOOLS: Set[str] = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "sed_file",
    "notebook_edit",
}


async def _detect_changed_files(workspace_path: str, since_wall_ts: float) -> Set[str]:
    """Ask git what this run actually touched, whatever tool did the touching.

    Why this exists: reporting `modified_files: []` for a run that rewrote four files is worse
    than reporting nothing at all, because the caller acts on it - it concludes the workspace is
    clean and moves on. That happened: a timed-out run reported zero while +203 lines sat on
    disk, because every write had gone through a tool absent from FILE_MODIFICATION_TOOLS.

    Method: everything git currently calls dirty or untracked, narrowed to entries whose mtime
    is at or after the run's start. The mtime filter is what separates "this run wrote it" from
    "it was already dirty when we got here" - a workspace mid-edit is the normal case, not the
    exception, and claiming its pre-existing changes would be its own kind of lie.

    Never raises: a workspace that is not a repo, a missing git, a hostile path - all resolve to
    the empty set, leaving the stream-derived hint as the only evidence. Silence here degrades
    the report; an exception would fail the run.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return set()

    # `or "git"` matters: `shutil.which` searches PATH, and this server is routinely launched by
    # a client that hands it almost no environment. Falling back to the bare name lets the OS
    # resolve it the way `diff_engine` already does - the first version of this function bailed
    # out on a None from `which` and reported "no files changed" for a run that had changed some.
    git_cmd = shutil.which("git") or "git"
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # never block waiting for credentials
    env["GIT_OPTIONAL_LOCKS"] = "0"  # read-only: do not fight a concurrent git for the index lock
    env["LC_ALL"] = "C"  # stable, parseable output regardless of the user's locale

    try:
        code, raw_out, _ = await run_subprocess_async(
            [git_cmd, "status", "--porcelain=v1", "-uall"],
            cwd=workspace_path,
            env=env,
            timeout_seconds=20.0,
        )
    except Exception as exc:  # not a repo, git missing, cwd vanished, timeout
        logger.debug(f"Change detection skipped for '{workspace_path}': {exc}")
        return set()

    if code != 0:
        return set()

    changed: Set[str] = set()
    for line in raw_out.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        # Renames arrive as `old -> new`; the new path is the one that exists on disk.
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip('"')
        if not entry or entry.endswith("/"):
            continue
        try:
            if os.path.getmtime(os.path.join(workspace_path, entry)) + 1.0 >= since_wall_ts:
                changed.add(entry.replace("\\", "/"))
        except OSError:
            # Deleted during the run: git still lists it, the stat fails. A deletion is a change,
            # and its timing cannot be recovered, so report it rather than drop it.
            changed.add(entry.replace("\\", "/"))
    return changed


def _relativize_to_workspace(target: str, workspace_path: str) -> str:
    """Render a path agy reported as workspace-relative, whichever OS reported it.

    The previous version asked `os.path.isabs` and `os.path.relpath`, both of which answer for
    the host running *this* server rather than the host that produced the telemetry line. A
    Windows-side run reporting `E:\\repo\\src\\auth.py` against workspace `E:\\repo` therefore
    came back as `E:/repo/src/auth.py` from a POSIX server: `isabs` says False for a drive
    letter, so the relativize branch was never entered and the caller was handed an absolute
    foreign path where `modified_files` promises a workspace-relative one.

    Folding both sides to forward slashes and stripping the prefix textually answers the same
    question without asking the local OS about a foreign path. A target that is not under the
    workspace keeps its full path - it is still the truest thing we can say about it.
    """
    normalized = target.replace("\\", "/")
    if not workspace_path:
        return normalized

    prefix = workspace_path.replace("\\", "/").rstrip("/") + "/"
    if normalized.startswith(prefix):
        return normalized[len(prefix) :]
    return normalized


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
        # Wall clock too: monotonic cannot be compared against a file's mtime.
        wall_start = time.time()
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
                                    modified_files.add(
                                        _relativize_to_workspace(target, request.workspace_path)
                                    )

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
            # The path that most needs the truth: agy was killed mid-run, so the stream stopped
            # wherever it stopped, and whatever it had already written is still on disk.
            modified_files |= await _detect_changed_files(request.workspace_path, wall_start)
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
            modified_files |= await _detect_changed_files(request.workspace_path, wall_start)
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
        # Also on the success path: a run can finish cleanly having written through a tool this
        # parser does not recognise, and "No files modified" would be just as wrong there.
        modified_files |= await _detect_changed_files(request.workspace_path, wall_start)
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
