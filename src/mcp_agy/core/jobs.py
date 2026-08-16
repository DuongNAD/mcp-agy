"""Background job registry for AGY runs that outlive a single MCP tool call.

Every MCP client caps how long it waits for one tool call, and the cap is short: Claude Code
gives a call 60 seconds by default and documents that as a hard wall-clock limit. A real
autonomous coding task runs for minutes, so the synchronous tools can only ever carry the
short ones - and when the cap fires the client drops the call and the work is thrown away.

This registry breaks the tie between "how long AGY works" and "how long one call blocks".
`start` hands back a job id immediately; the run continues in the server's event loop and is
collected later by id. The architect stays responsive: it can do other work, or ask again with
a short bounded wait, instead of holding one request open for twenty minutes.

Jobs live in this process. A server restart loses them, which is why `describe` reports enough
for the architect to notice and re-issue rather than wait forever on an id that no longer exists.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp_agy.utils.logger import get_logger

logger = get_logger("mcp_agy.core.jobs")

# Finished jobs are kept so the architect can collect a result it has not asked for yet, and
# pruned so a long-lived server does not accumulate every run it has ever performed.
COMPLETED_JOB_TTL_SECONDS = 3600.0
MAX_COMPLETED_JOBS = 100

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"


def _marker_dir() -> Path:
    """Where completion markers land. `MCP_AGY_JOB_MARKER_DIR` redirects it.

    The override exists because `reset()` cannot catch every marker: a job that finishes *after*
    the registry is cleared still writes one, and the test suite resets between cases. Cleaning
    on reset took a full run from ~700 stray files down to 317 - better, but 317 a run into a
    directory shared by every run on the machine is still litter. Pointing the tests at their
    own directory makes it zero, which is the only number that stays true over time.
    """
    override = os.environ.get("MCP_AGY_JOB_MARKER_DIR")
    d = Path(override) if override else Path(tempfile.gettempdir()) / "mcp_agy_jobs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(f"Could not create marker directory {d}: {exc}")
    return d


class JobRecord:
    """One AGY run and everything known about it, before and after it finishes."""

    def __init__(
        self,
        job_id: str,
        kind: str,
        workspace_path: str,
        prompt: str,
        done_marker_path: str = "",
    ) -> None:
        self.job_id = job_id
        self.kind = kind
        self.workspace_path = workspace_path
        # The prompt can be thousands of words; a preview is enough to tell two jobs apart in
        # a listing without copying the whole thing into the architect's context again.
        self.prompt_preview = prompt[:160] + ("..." if len(prompt) > 160 else "")
        self.status = RUNNING
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.result: Any = None
        self.error_details: Optional[str] = None
        self.task: Optional[asyncio.Task[Any]] = None
        self.done_marker_path = done_marker_path

    @property
    def is_done(self) -> bool:
        return self.status != RUNNING

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.started_at, 4)


class JobManager:
    """Owns the running jobs. One instance per server process."""

    def __init__(self) -> None:
        self._jobs: Dict[str, JobRecord] = {}

    def start(
        self,
        runner: Callable[[], Awaitable[Any]],
        kind: str,
        workspace_path: str,
        prompt: str,
    ) -> JobRecord:
        """Launch `runner` in the background and return its record immediately."""
        self._prune()
        job_id = str(uuid.uuid4())
        done_marker_path = str(_marker_dir() / f"{job_id}.done")
        job = JobRecord(
            job_id=job_id,
            kind=kind,
            workspace_path=workspace_path,
            prompt=prompt,
            done_marker_path=done_marker_path,
        )
        self._jobs[job.job_id] = job

        async def _execute() -> None:
            try:
                job.result = await runner()
                job.status = COMPLETED
            except asyncio.CancelledError:
                # Reached both when the architect cancels and when the server shuts down. The
                # backend has already torn down the agy process tree by the time this runs.
                job.status = CANCELLED
                job.error_details = "Job was cancelled before it finished."
                job.finished_at = time.time()
                raise
            except Exception as exc:
                logger.exception(f"Background job {job.job_id} failed: {exc}")
                job.status = FAILED
                job.error_details = f"{type(exc).__name__}: {exc}"
            finally:
                if job.finished_at is None:
                    job.finished_at = time.time()
                if job.done_marker_path:
                    try:
                        p = Path(job.done_marker_path)
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(f"{job.status}\n", encoding="utf-8")
                    except Exception as exc:
                        logger.warning(
                            f"Failed to write done marker for job {job.job_id} at {job.done_marker_path}: {exc}"
                        )

        # The registry holds the only strong reference to this task. Without it the event loop
        # may garbage-collect a running task mid-flight.
        job.task = asyncio.create_task(_execute())
        logger.info(f"Started background job {job.job_id} ({kind}) for workspace '{workspace_path}'")
        return job

    def get(self, job_id: str) -> Optional[JobRecord]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> List[JobRecord]:
        """Newest first, so a listing opens on what the architect most likely wants."""
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    async def wait(self, job_id: str, timeout_seconds: float) -> Optional[JobRecord]:
        """Wait up to `timeout_seconds` for a job to finish. Returns as soon as it does.

        A bounded wait, never an open-ended one: the point of this registry is that no single
        call outlives the client's cap. Timing out here is not an error - the job keeps running
        and the next call collects it.
        """
        job = self._jobs.get(job_id)
        if job is None or job.is_done or job.task is None or timeout_seconds <= 0:
            return job
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            # The job was cancelled underneath us; its own handler recorded the outcome.
            pass
        except Exception:
            # The job's exception is recorded on the record itself - shielded here so a failed
            # job reports as a failed job rather than raising out of a status query.
            pass
        return job

    async def cancel(self, job_id: str) -> Optional[JobRecord]:
        """Cancel a running job and wait briefly for its teardown to complete."""
        job = self._jobs.get(job_id)
        if job is None or job.is_done or job.task is None:
            return job
        job.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        if not job.is_done:
            job.status = CANCELLED
            job.error_details = "Job was cancelled before it finished."
            job.finished_at = time.time()
        return job

    def _delete_marker(self, job: JobRecord) -> None:
        if job.done_marker_path:
            try:
                Path(job.done_marker_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _prune(self) -> None:
        """Drop finished jobs that are old or in excess. Running jobs are never dropped."""
        now = time.time()
        finished = [j for j in self._jobs.values() if j.is_done]
        for job in finished:
            if job.finished_at is not None and (now - job.finished_at) > COMPLETED_JOB_TTL_SECONDS:
                self._jobs.pop(job.job_id, None)
                self._delete_marker(job)

        finished = sorted(
            (j for j in self._jobs.values() if j.is_done),
            key=lambda j: j.finished_at or 0.0,
        )
        excess = len(finished) - MAX_COMPLETED_JOBS
        for job in finished[: max(0, excess)]:
            self._jobs.pop(job.job_id, None)
            self._delete_marker(job)

    def reset(self) -> None:
        """Drop every job without cancelling. For tests.

        Deletes the markers too. `_prune` already does this on the paths it owns, but `reset`
        is the path the test suite takes between cases, and it used to clear the registry while
        leaving the files behind: one full run left ~700 markers in a temp directory shared by
        every run on the machine, and 2 217 had piled up before anyone counted. A cleanup that
        only fires in production is not cleanup.
        """
        for job in list(self._jobs.values()):
            self._delete_marker(job)
        self._jobs.clear()


_job_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    """Return the process-wide job registry, creating it on first use."""
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager


def reset_job_manager() -> None:
    """Discard the process-wide registry. For tests."""
    global _job_manager
    _job_manager = None
