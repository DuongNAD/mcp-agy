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
import dataclasses
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

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
    """Where completion markers and result records land. `MCP_AGY_JOB_MARKER_DIR` redirects it.

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


def _serialize_job_result(result: Any) -> tuple[Any, Optional[str]]:
    """Serialize JobRecord.result to a JSON-safe structure.

    Returns (serialized_data, serialization_error_or_none).
    """
    if result is None:
        return None, None

    try:
        # Pydantic v2 model
        if hasattr(result, "model_dump") and callable(result.model_dump):
            dumped = result.model_dump(mode="json")
            json.dumps(dumped)
            return dumped, None

        # Pydantic v1 model
        if hasattr(result, "dict") and callable(result.dict):
            dumped = result.dict()
            json.dumps(dumped)
            return dumped, None

        # Dataclass
        if dataclasses.is_dataclass(result) and not isinstance(result, type):
            dumped = dataclasses.asdict(result)
            json.dumps(dumped)
            return dumped, None

        # Dict / list / primitive
        json.dumps(result)
        return result, None
    except Exception as exc:
        logger.warning(f"Failed to serialize job result: {exc}")
        return None, f"{type(exc).__name__}: {exc}"


def _load_job_from_disk(result_file: Path) -> Optional[JobRecord]:
    """Reconstruct a JobRecord from its on-disk result file.

    Returns None if the file is invalid, corrupted, or has expired past TTL.
    """
    try:
        if not result_file.is_file():
            return None
        text = result_file.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return None

        job_id = data.get("job_id")
        if not job_id:
            return None

        finished_at = data.get("finished_at")
        started_at = data.get("started_at", 0.0)
        reference_time = finished_at if finished_at is not None else started_at

        # An on-disk record older than TTL must not be revived into the registry; deleting the
        # expired files prevents unpruned runs from piling up indefinitely.
        if (time.time() - reference_time) > COMPLETED_JOB_TTL_SECONDS:
            try:
                result_file.unlink(missing_ok=True)
                (_marker_dir() / f"{job_id}.done").unlink(missing_ok=True)
                (_marker_dir() / f"{job_id}.result.json.tmp").unlink(missing_ok=True)
            except Exception:
                pass
            return None

        raw_result = data.get("result")
        result_obj = raw_result
        if isinstance(raw_result, dict):
            try:
                from mcp_agy.core.models import TaskExecutionResult
                result_obj = TaskExecutionResult.model_validate(raw_result)
            except Exception:
                result_obj = raw_result

        done_marker_path = str(_marker_dir() / f"{job_id}.done")
        job = JobRecord(
            job_id=job_id,
            kind=data.get("kind", "task"),
            workspace_path=data.get("workspace_path", ""),
            prompt=data.get("prompt_preview", ""),
            done_marker_path=done_marker_path,
            recovered_from_disk=True,
        )
        job.prompt_preview = data.get("prompt_preview", job.prompt_preview)
        job.status = data.get("status", COMPLETED)
        job.started_at = started_at
        job.finished_at = finished_at
        job.result = result_obj
        job.error_details = data.get("error_details")
        job.task = None
        return job
    except Exception as exc:
        logger.warning(f"Failed to load job from disk file {result_file}: {exc}")
        return None


class JobRecord:
    """One AGY run and everything known about it, before and after it finishes."""

    def __init__(
        self,
        job_id: str,
        kind: str,
        workspace_path: str,
        prompt: str,
        done_marker_path: str = "",
        recovered_from_disk: bool = False,
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
        self.recovered_from_disk = recovered_from_disk
        # Set once a finished result has been handed to the architect, so agy_wait returns each
        # report exactly once and a completion announcement is not sent for one already read.
        self.collected = False

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
        # job id -> how many waits are blocked on it right now. A job someone is waiting on will
        # be collected the moment it ends, so it needs no completion announcement.
        self._waiters: Dict[str, int] = {}
        self._callbacks: Set[asyncio.Task[Any]] = set()

    def start(
        self,
        runner: Callable[[], Awaitable[Any]],
        kind: str,
        workspace_path: str,
        prompt: str,
        on_finish: Optional[Callable[[JobRecord], Awaitable[None]]] = None,
    ) -> JobRecord:
        """Launch `runner` in the background and return its record immediately.

        `on_finish` runs after a job completes or fails - not after a cancel, which the architect
        asked for and already knows about. It runs as its own task, so a slow or failing
        callback can never hold up the job's own completion.
        """
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

                # Write .result.json BEFORE .done marker:
                # The documented contract for clients watching `done_marker_path` requires that
                # the moment `.done` appears on disk, the full result is already committed and
                # readable. Writing `.result.json` first ensures no reader ever encounters a
                # missing or partially written result payload.
                try:
                    serialized_result, ser_err = _serialize_job_result(job.result)
                    record_data: Dict[str, Any] = {
                        "job_id": job.job_id,
                        "kind": job.kind,
                        "workspace_path": job.workspace_path,
                        "prompt_preview": job.prompt_preview,
                        "status": job.status,
                        "started_at": job.started_at,
                        "finished_at": job.finished_at,
                        "duration_seconds": job.duration_seconds,
                        "error_details": job.error_details,
                        "result": serialized_result,
                    }
                    if ser_err is not None:
                        record_data["serialization_error"] = ser_err

                    result_path = _marker_dir() / f"{job.job_id}.result.json"
                    tmp_path = _marker_dir() / f"{job.job_id}.result.json.tmp"

                    tmp_path.write_text(
                        json.dumps(record_data, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    os.replace(str(tmp_path), str(result_path))
                except Exception as exc:
                    # Disk persistence failures must never turn an otherwise completed run into a failure.
                    logger.warning(
                        f"Failed to write result file for job {job.job_id}: {exc}"
                    )

                if job.done_marker_path:
                    try:
                        p = Path(job.done_marker_path)
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(f"{job.status}\n", encoding="utf-8")
                    except Exception as exc:
                        logger.warning(
                            f"Failed to write done marker for job {job.job_id} at {job.done_marker_path}: {exc}"
                        )

                if on_finish is not None and job.status in (COMPLETED, FAILED):
                    self._spawn_callback(on_finish, job)

        # The registry holds the only strong reference to this task. Without it the event loop
        # may garbage-collect a running task mid-flight.
        job.task = asyncio.create_task(_execute())
        logger.info(f"Started background job {job.job_id} ({kind}) for workspace '{workspace_path}'")
        return job

    def _spawn_callback(
        self, callback: Callable[[JobRecord], Awaitable[None]], job: JobRecord
    ) -> None:
        async def _guarded() -> None:
            try:
                await callback(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"on_finish callback for job {job.job_id} failed: {exc}")

        task = asyncio.create_task(_guarded())
        # Held here so the loop cannot garbage-collect it mid-flight.
        self._callbacks.add(task)
        task.add_done_callback(self._callbacks.discard)

    def is_awaited(self, job_id: str) -> bool:
        """True while some agy_wait / agy_job_status call is blocked waiting on this job."""
        return self._waiters.get(job_id, 0) > 0

    async def _wait_tasks(self, jobs: Sequence[JobRecord], timeout_seconds: float) -> None:
        """Block until any of `jobs` ends or the deadline passes, counting as a waiter on each."""
        tasks = {j.task for j in jobs if j.task is not None and not j.is_done}
        if not tasks or timeout_seconds <= 0:
            return
        ids = [j.job_id for j in jobs]
        for job_id in ids:
            self._waiters[job_id] = self._waiters.get(job_id, 0) + 1
        try:
            # asyncio.wait never cancels what it waits on, so a wait that times out - or a caller
            # that goes away - leaves every job running.
            await asyncio.wait(tasks, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for job_id in ids:
                remaining = self._waiters.get(job_id, 0) - 1
                if remaining > 0:
                    self._waiters[job_id] = remaining
                else:
                    self._waiters.pop(job_id, None)

    async def wait_any(
        self, job_ids: Sequence[str], timeout_seconds: float
    ) -> Tuple[List[JobRecord], List[JobRecord], List[str]]:
        """Wait for the next finished job and hand out every result not yet collected.

        Scope is `job_ids`, or - when empty - every job this process started. Jobs recovered
        from disk are only in scope when named: without ids there is no telling whether a
        previous server process already handed them out.

        Returns (finished, still_running, unknown_ids). `finished` is marked collected, so the
        same report is never returned twice. Returns at once when something is already waiting
        to be collected, or when nothing in scope is running.
        """
        unknown: List[str] = []
        if job_ids:
            scope: List[JobRecord] = []
            for job_id in dict.fromkeys(job_ids):
                job = self.get(job_id)
                if job is None:
                    unknown.append(job_id)
                else:
                    scope.append(job)
        else:
            scope = [j for j in self._jobs.values() if not j.recovered_from_disk]

        def _uncollected() -> List[JobRecord]:
            return [j for j in scope if j.is_done and not j.collected]

        if not _uncollected():
            await self._wait_tasks([j for j in scope if not j.is_done], timeout_seconds)

        finished = sorted(_uncollected(), key=lambda j: j.finished_at or 0.0)
        for job in finished:
            job.collected = True
        still_running = [j for j in scope if not j.is_done]
        return finished, still_running, unknown

    def get(self, job_id: str) -> Optional[JobRecord]:
        job = self._jobs.get(job_id)
        if job is not None:
            return job

        # Fall back to on-disk result: when a client times out and restarts the server process,
        # recovering the completed result from disk prevents throwing away hundreds of seconds
        # and hundreds of thousands of tokens of completed work.
        result_path = _marker_dir() / f"{job_id}.result.json"
        if result_path.is_file():
            recovered = _load_job_from_disk(result_path)
            if recovered is not None:
                self._jobs[job_id] = recovered
                return recovered
        return None

    def list_jobs(self) -> List[JobRecord]:
        """Newest first, so a listing opens on what the architect most likely wants.

        Merges in-memory jobs with unexpired on-disk records. In-memory records take precedence.
        """
        all_jobs: Dict[str, JobRecord] = dict(self._jobs)
        try:
            marker_d = _marker_dir()
            if marker_d.is_dir():
                for p in marker_d.glob("*.result.json"):
                    job_id = p.name[:-12]
                    if job_id not in all_jobs:
                        recovered = _load_job_from_disk(p)
                        if recovered is not None:
                            all_jobs[recovered.job_id] = recovered
        except Exception as exc:
            logger.warning(f"Failed to scan on-disk job results: {exc}")

        return sorted(all_jobs.values(), key=lambda j: j.started_at, reverse=True)

    async def wait(self, job_id: str, timeout_seconds: float) -> Optional[JobRecord]:
        """Wait up to `timeout_seconds` for a job to finish. Returns as soon as it does.

        A bounded wait, never an open-ended one: the point of this registry is that no single
        call outlives the client's cap. Timing out here is not an error - the job keeps running
        and the next call collects it.
        """
        job = self._jobs.get(job_id)
        if job is None or job.is_done or job.task is None or timeout_seconds <= 0:
            return job
        # A job's own exception or cancellation is recorded on the record, and asyncio.wait
        # neither raises it nor cancels the job when the wait times out.
        await self._wait_tasks([job], timeout_seconds)
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
        try:
            (_marker_dir() / f"{job.job_id}.result.json").unlink(missing_ok=True)
            (_marker_dir() / f"{job.job_id}.result.json.tmp").unlink(missing_ok=True)
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

        Deletes both the .done markers and the .result.json files. `_prune` already does
        this on the paths it owns, but `reset` is the path the test suite takes between cases,
        and it used to clear the registry while leaving the files behind: one full run left
        ~700 markers in a temp directory shared by every run on the machine, and 2 217 had
        piled up before anyone counted. A cleanup that only fires in production is not cleanup.
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
