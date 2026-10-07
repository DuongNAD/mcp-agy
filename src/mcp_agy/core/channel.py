"""Push a finished job into the architect's session instead of waiting to be asked.

Claude Code's channels (research preview, CLI only) let an MCP server that declares the
experimental `claude/channel` capability send `notifications/claude/channel`; when the session was
started with `--channels server:<name>` (or `--dangerously-load-development-channels
server:<name>` for a server outside the approved list), the notification arrives in the
conversation as a `<channel source="<name>" ...>` event. That is the closest thing MCP offers to a
subagent handing its result back: the architect can end its turn and be told when the work lands.

Every other client ignores the unknown notification, as JSON-RPC lets it, so the announcement is
best-effort by design. agy_wait stays the path that works everywhere; this only saves the polling.
`MCP_AGY_CHANNEL=0` switches it off.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from pydantic import BaseModel

from mcp_agy.core.jobs import JobManager, JobRecord
from mcp_agy.core.models import TaskExecutionResult
from mcp_agy.utils.logger import get_logger

logger = get_logger("mcp_agy.core.channel")

CHANNEL_CAPABILITY = "claude/channel"
CHANNEL_METHOD = "notifications/claude/channel"
CHANNEL_ENV = "MCP_AGY_CHANNEL"

# How long a finished job may sit unread before it is announced. An architect that asks for the
# result within this window - the usual case when it calls agy_wait right after starting jobs -
# gets it from the call and is not told twice.
ANNOUNCE_GRACE_SECONDS = 2.0


class ChannelNotification(BaseModel):
    """`notifications/claude/channel`. Not in the MCP SDK's notification union, so built here;
    the session only needs something with `method` and `params` to serialise."""

    method: Literal["notifications/claude/channel"] = CHANNEL_METHOD
    params: dict[str, Any]


def channel_enabled() -> bool:
    return (os.environ.get(CHANNEL_ENV) or "").strip().lower() not in ("0", "false", "no", "off")


def advertise(lowlevel_server: Any) -> None:
    """Declare the channel capability in every initialize result this server sends.

    Every transport - and the test harness - builds its handshake through
    `create_initialization_options`, so wrapping that one method covers them all.
    """
    original = lowlevel_server.create_initialization_options

    def _with_channel(notification_options: Any = None, experimental_capabilities: Any = None) -> Any:
        capabilities = dict(experimental_capabilities or {})
        capabilities.setdefault(CHANNEL_CAPABILITY, {})
        return original(notification_options, capabilities)

    lowlevel_server.create_initialization_options = _with_channel


def describe_finish(job: JobRecord) -> tuple[str, dict[str, str]]:
    """The event body and its tag attributes. Kept short: the report itself comes from agy_wait."""
    result = job.result if isinstance(job.result, TaskExecutionResult) else None
    report = result.report if result is not None else None
    outcome = report.status if report is not None else (result.status if result is not None else job.status)

    lines = [f"AGY job {job.job_id} ({job.kind}) finished: {outcome}.", f"Task: {job.prompt_preview}"]
    if report is not None and report.summary:
        lines.append(f"Summary: {report.summary}")
    elif job.error_details:
        lines.append(f"Error: {job.error_details[:300]}")
    lines.append(f'Collect the full report with agy_wait(job_ids=["{job.job_id}"]), then verify it with agy_get_diff.')

    # Attribute keys must be identifiers; Claude Code drops any other key silently.
    meta = {"job_id": job.job_id, "kind": job.kind, "status": job.status, "outcome": str(outcome)}
    return "\n".join(lines), meta


def make_announcer(session: Any, manager: JobManager):
    """Build the `on_finish` callback that announces a job to `session` unless it was read first."""

    async def _announce(job: JobRecord) -> None:
        await asyncio.sleep(ANNOUNCE_GRACE_SECONDS)
        if job.collected or manager.is_awaited(job.job_id):
            return
        content, meta = describe_finish(job)
        try:
            await session.send_notification(
                ChannelNotification(params={"content": content, "meta": meta})
            )
        except Exception as exc:
            # The session that started the job may be gone; the result is still on disk and in
            # the registry for agy_wait to hand out.
            logger.debug(f"Could not announce job {job.job_id} on the channel: {exc}")

    return _announce
