"""Tests for the background job registry and the four job tools.

The registry exists because of a measured client constraint: an MCP client caps a single tool
call - Claude Code at 60 seconds, documented as a hard wall-clock limit that progress
notifications do not extend - while a real AGY run takes minutes. These tests hold the line
that starting a run never blocks on the run, that a result survives to be collected afterwards,
and that a failure is reported as a failure rather than lost with the call that started it.

Driven through a real MCP ClientSession, so the JSON the architect actually receives is what
is asserted on.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from mcp_agy.core.jobs import JobManager, reset_job_manager
from mcp_agy.core.models import TaskExecutionResult, TokenUsage
from mcp_agy.server import create_mcp_server
from mcp_agy.utils.workspace import WorkspaceLockManager, WorkspaceLockTimeoutError


@pytest.fixture(autouse=True)
def _clean_job_registry():
    """Each test gets an empty registry: job ids and listings must not leak between tests."""
    reset_job_manager()
    yield
    reset_job_manager()


def _payload(call_res: Any) -> Dict[str, Any]:
    """Decode the JSON body of a tool result."""
    return json.loads(call_res.content[0].text)


class _ControllableExecutor:
    """A backend executor the test releases by hand, to hold a run open on purpose."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[Dict[str, Any]] = []
        self.cancelled = False

    async def __call__(self, **kwargs: Any) -> TaskExecutionResult:
        self.calls.append(kwargs)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return TaskExecutionResult(
            status="success",
            conversation_id="conv-job-1",
            response="Refactored the gateway teardown path.",
            modified_files=["src/composables/useGateway.ts"],
            diff_summary="1 file(s) modified",
            duration_seconds=123.4,
            tokens_used=TokenUsage(total_tokens=4242),
            backend_used="cli",
        )


@pytest.mark.anyio
class TestBackgroundJobTools:
    """The start / collect / cancel loop, driven over MCP."""

    async def test_start_returns_immediately_while_the_run_is_still_going(
        self, client_factory, git_workspace
    ):
        """The whole point: the call that starts a long run does not wait for it.

        The executor here never finishes until the test releases it, so if `agy_start_task`
        waited on the run this call would hang until the test timed out.
        """
        executor = _ControllableExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            started = await session.call_tool(
                "agy_start_task",
                {"workspace_path": str(git_workspace), "prompt": "Fix the teardown path"},
            )
            handle = _payload(started)

            assert handle["status"] == "running"
            assert handle["job_id"], "a job id is the only way to collect the result later"
            assert handle["kind"] == "task"

            # Still running, and saying so - not a fabricated success.
            status = _payload(
                await session.call_tool("agy_job_status", {"job_id": handle["job_id"]})
            )
            assert status["status"] == "running"
            assert status["is_done"] is False
            assert status["result"] is None

            executor.release.set()

            done = _payload(
                await session.call_tool(
                    "agy_job_status", {"job_id": handle["job_id"], "wait_seconds": 5}
                )
            )
            assert done["status"] == "completed"
            assert done["is_done"] is True
            assert done["result"]["status"] == "success"
            assert done["result"]["modified_files"] == ["src/composables/useGateway.ts"]
            assert done["result"]["tokens_used"]["total_tokens"] == 4242

    async def test_wait_returns_the_moment_the_job_finishes(self, client_factory, git_workspace):
        """A bounded wait is not a sleep: it returns on completion, not at the deadline."""
        executor = _ControllableExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            handle = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "Do the thing"},
                )
            )
            await executor.started.wait()

            async def _release_soon() -> None:
                await asyncio.sleep(0.2)
                executor.release.set()

            releaser = asyncio.create_task(_release_soon())
            loop = asyncio.get_running_loop()
            began = loop.time()
            done = _payload(
                await session.call_tool(
                    "agy_job_status", {"job_id": handle["job_id"], "wait_seconds": 30}
                )
            )
            elapsed = loop.time() - began
            await releaser

            assert done["status"] == "completed"
            assert elapsed < 10, f"waited {elapsed:.1f}s for a job that finished in 0.2s"

    async def test_a_failed_run_is_reported_not_swallowed(self, client_factory, git_workspace):
        """A job that raises must surface as a failed job carrying the reason.

        Without this the exception dies with the background task and the architect sees a job
        that is simply never done.
        """
        async def _explode(**_: Any) -> TaskExecutionResult:
            raise RuntimeError("agy executable vanished mid-run")

        server = create_mcp_server(backend_executor=_explode)

        async with client_factory(server) as session:
            handle = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "Break please"},
                )
            )
            status = _payload(
                await session.call_tool(
                    "agy_job_status", {"job_id": handle["job_id"], "wait_seconds": 5}
                )
            )

            assert status["status"] == "failed"
            assert status["is_done"] is True
            assert "vanished mid-run" in (status["error_details"] or "")

    async def test_cancel_stops_the_run(self, client_factory, git_workspace):
        """Cancelling propagates into the run itself, not just the bookkeeping."""
        executor = _ControllableExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            handle = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "Run forever"},
                )
            )
            await executor.started.wait()

            cancelled = _payload(
                await session.call_tool("agy_cancel_job", {"job_id": handle["job_id"]})
            )
            assert cancelled["status"] == "cancelled"
            assert cancelled["is_done"] is True
            assert executor.cancelled, "cancellation never reached the running coroutine"

    async def test_unknown_job_id_says_so(self, client_factory, git_workspace):
        """After a server restart the architect holds an id this process never issued."""
        server = create_mcp_server(backend_executor=_ControllableExecutor())

        async with client_factory(server) as session:
            status = _payload(
                await session.call_tool("agy_job_status", {"job_id": "00000000-dead-beef"})
            )
            assert status["status"] == "not_found"
            assert "restart" in (status["error_details"] or "").lower()

    async def test_listing_recovers_ids_and_counts_what_is_running(
        self, client_factory, git_workspace
    ):
        """The listing is the recovery path when a job id has fallen out of context."""
        executor = _ControllableExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            first = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "First job"},
                )
            )
            second = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {
                        "workspace_path": str(git_workspace),
                        "prompt": "Second job, read only",
                        "mode": "plan",
                    },
                )
            )

            listing = _payload(await session.call_tool("agy_list_jobs", {}))
            ids = [j["job_id"] for j in listing["jobs"]]

            assert listing["running_count"] == 2
            assert ids[0] == second["job_id"], "newest job must come first"
            assert set(ids) == {first["job_id"], second["job_id"]}
            assert listing["jobs"][0]["kind"] == "plan"
            assert listing["jobs"][0]["prompt_preview"].startswith("Second job")

            executor.release.set()

    async def test_listing_leaves_results_for_the_status_call_to_carry(
        self, client_factory, git_workspace
    ):
        """A listing answers "what is running?" - it must not re-send every finished run.

        The registry keeps up to 100 completed jobs, each holding AGY's full prose response.
        Embedding them here made a one-line question cost the architect its context, which is
        the resource the whole background-job design exists to protect.
        """
        executor = _ControllableExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            handle = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "Fix the teardown path"},
                )
            )
            executor.release.set()
            done = _payload(
                await session.call_tool(
                    "agy_job_status", {"job_id": handle["job_id"], "wait_seconds": 5}
                )
            )
            assert done["status"] == "completed"
            assert done["result"] is not None, "the status call is where the result lives"

            listed = _payload(await session.call_tool("agy_list_jobs", {}))
            entry = listed["jobs"][0]

            assert entry["job_id"] == handle["job_id"]
            assert entry["result"] is None, "the listing must not carry finished results"
            # Everything needed to decide whether to collect it is still there.
            assert entry["status"] == "completed"
            assert entry["is_done"] is True
            assert entry["prompt_preview"].startswith("Fix the teardown")
            assert entry["duration_seconds"] >= 0.0

    async def test_two_jobs_on_one_workspace_do_not_run_at_once(
        self, client_factory, git_workspace
    ):
        """The workspace lock still serializes, but it is the jobs that queue - not the caller.

        Both start calls must return promptly; the second run must not begin while the first
        holds the workspace.
        """
        executor = _ControllableExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            await session.call_tool(
                "agy_start_task", {"workspace_path": str(git_workspace), "prompt": "First"}
            )
            await executor.started.wait()

            second = _payload(
                await session.call_tool(
                    "agy_start_task", {"workspace_path": str(git_workspace), "prompt": "Second"}
                )
            )
            assert second["status"] == "running", "the second start blocked on the first job"

            await asyncio.sleep(0.2)
            assert len(executor.calls) == 1, "two runs entered the same workspace concurrently"

            executor.release.set()

    async def test_bad_input_is_rejected_without_creating_a_job(
        self, client_factory, git_workspace
    ):
        """A refused start must not leave a phantom job behind for the architect to poll."""
        server = create_mcp_server(backend_executor=_ControllableExecutor())

        async with client_factory(server) as session:
            empty_prompt = _payload(
                await session.call_tool(
                    "agy_start_task", {"workspace_path": str(git_workspace), "prompt": "   "}
                )
            )
            assert empty_prompt["status"] == "error"
            assert empty_prompt["job_id"] == ""

            missing_ws = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": "E:/definitely/not/here", "prompt": "Work"},
                )
            )
            assert missing_ws["status"] == "error"
            assert "workspace" in (missing_ws["error_details"] or "").lower()

            listing = _payload(await session.call_tool("agy_list_jobs", {}))
            assert listing["jobs"] == []

    async def test_completed_job_writes_marker_and_matches_handle_path(
        self, client_factory, git_workspace
    ):
        """A completed job writes 'completed' to the marker path returned in JobHandle."""
        executor = _ControllableExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            handle = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "Marker completed test"},
                )
            )
            marker_path = Path(handle["done_marker_path"])
            assert handle["done_marker_path"], "JobHandle must populate done_marker_path"
            assert not marker_path.exists(), "Marker must not exist while job is running"

            executor.release.set()
            done = _payload(
                await session.call_tool(
                    "agy_job_status", {"job_id": handle["job_id"], "wait_seconds": 5}
                )
            )
            assert done["status"] == "completed"
            assert marker_path.is_file(), "Marker file must appear at the path in JobHandle"
            assert marker_path.read_text(encoding="utf-8").strip() == "completed"

    async def test_failed_job_writes_marker_containing_failed(
        self, client_factory, git_workspace
    ):
        """A failed job writes 'failed' so silence never means 'crashed'."""
        async def _explode(**_: Any) -> TaskExecutionResult:
            raise RuntimeError("agy subprocess crashed unexpectedly")

        server = create_mcp_server(backend_executor=_explode)

        async with client_factory(server) as session:
            handle = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "Marker failed test"},
                )
            )
            marker_path = Path(handle["done_marker_path"])
            assert handle["done_marker_path"]

            status = _payload(
                await session.call_tool(
                    "agy_job_status", {"job_id": handle["job_id"], "wait_seconds": 5}
                )
            )
            assert status["status"] == "failed"
            assert marker_path.is_file(), "Marker file must appear when the job fails"
            assert marker_path.read_text(encoding="utf-8").strip() == "failed"

    async def test_cancelled_job_writes_marker_containing_cancelled(
        self, client_factory, git_workspace
    ):
        """A cancelled job writes 'cancelled' into its marker file."""
        executor = _ControllableExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            handle = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "Marker cancelled test"},
                )
            )
            marker_path = Path(handle["done_marker_path"])
            assert handle["done_marker_path"]
            await executor.started.wait()

            cancelled = _payload(
                await session.call_tool("agy_cancel_job", {"job_id": handle["job_id"]})
            )
            assert cancelled["status"] == "cancelled"
            assert marker_path.is_file(), "Marker file must appear when the job is cancelled"
            assert marker_path.read_text(encoding="utf-8").strip() == "cancelled"

    async def test_wait_seconds_schema_boundary(self, client_factory, git_workspace):
        """wait_seconds=600 is accepted by the schema and 601 is rejected."""
        server = create_mcp_server(backend_executor=_ControllableExecutor())

        async with client_factory(server) as session:
            # 600 is accepted (returns valid status result, here not_found for dummy id)
            call_600 = await session.call_tool(
                "agy_job_status", {"job_id": "00000000-dead-beef", "wait_seconds": 600}
            )
            assert not getattr(call_600, "isError", False)
            res_600 = _payload(call_600)
            assert res_600["status"] == "not_found"

            # 601 is rejected by schema validation
            call_601 = await session.call_tool(
                "agy_job_status", {"job_id": "00000000-dead-beef", "wait_seconds": 601}
            )
            assert getattr(call_601, "isError", False) is True
            assert "600" in call_601.content[0].text

        # Directly calling the FastMCP server tool raises on 601
        with pytest.raises(Exception):
            await server.call_tool(
                "agy_job_status", {"job_id": "00000000-dead-beef", "wait_seconds": 601}
            )


@pytest.mark.anyio
class TestJobManagerHousekeeping:
    """Registry behaviour that the tools depend on but do not expose directly."""

    async def test_finished_jobs_are_pruned_once_there_are_too_many(self):
        """A long-lived server must not accumulate every run it has ever performed."""
        manager = JobManager()

        async def _instant() -> TaskExecutionResult:
            return TaskExecutionResult(status="success", response="ok")

        for _ in range(120):
            job = manager.start(_instant, kind="task", workspace_path="E:/ws", prompt="p")
            assert job.task is not None
            await job.task

        # The prune runs on the next start, so trigger one more.
        manager.start(_instant, kind="task", workspace_path="E:/ws", prompt="p")
        assert len(manager.list_jobs()) <= 101

    async def test_a_running_job_is_never_pruned(self):
        """Pruning must not be able to lose a job that is still working."""
        manager = JobManager()
        release = asyncio.Event()

        async def _slow() -> TaskExecutionResult:
            await release.wait()
            return TaskExecutionResult(status="success", response="ok")

        async def _instant() -> TaskExecutionResult:
            return TaskExecutionResult(status="success", response="ok")

        long_job = manager.start(_slow, kind="task", workspace_path="E:/ws", prompt="slow one")

        for _ in range(120):
            job = manager.start(_instant, kind="task", workspace_path="E:/ws", prompt="p")
            assert job.task is not None
            await job.task

        assert manager.get(long_job.job_id) is not None, "a running job was pruned"
        release.set()
        assert long_job.task is not None
        await long_job.task

    async def test_pruning_finished_job_removes_marker_file(self):
        """When _prune() drops an old finished job, its marker file is removed from disk."""
        manager = JobManager()

        async def _instant() -> TaskExecutionResult:
            return TaskExecutionResult(status="success", response="ok")

        job = manager.start(_instant, kind="task", workspace_path="E:/ws", prompt="job 0")
        assert job.task is not None
        await job.task

        marker = Path(job.done_marker_path)
        assert marker.is_file(), "Marker file must exist after job completes"
        assert marker.read_text(encoding="utf-8").strip() == "completed"

        # Create excess jobs to force the first job to be pruned
        for i in range(110):
            j = manager.start(_instant, kind="task", workspace_path="E:/ws", prompt=f"job {i+1}")
            assert j.task is not None
            await j.task

        # Trigger prune via one more start
        manager.start(_instant, kind="task", workspace_path="E:/ws", prompt="trigger")

        assert manager.get(job.job_id) is None, "The first job should have been pruned"
        assert not marker.exists(), "The pruned job's marker file must be deleted from disk"


class TestWorkspaceReadWriteLock:
    """The per-workspace lock lets reads share and still keeps writes exclusive.

    Before this, every operation on a workspace took the same mutex, so two `mode:"plan"`
    investigations of one repo queued behind each other while reporting `running_count: 2`.
    The queueing was invisible from outside, which is the worst kind: the architecture promises
    "delegate and keep working" and quietly did not.
    """

    @pytest.mark.asyncio
    async def test_two_readers_hold_the_same_workspace_at_once(self, git_workspace):
        mgr = WorkspaceLockManager()
        both_inside = asyncio.Event()
        first_may_leave = asyncio.Event()

        async def reader(is_first: bool) -> None:
            async with mgr.lock(git_workspace.str_path, shared=True):
                if is_first:
                    await asyncio.wait_for(both_inside.wait(), timeout=5)
                else:
                    both_inside.set()
                    await asyncio.wait_for(first_may_leave.wait(), timeout=5)

        first = asyncio.create_task(reader(True))
        second = asyncio.create_task(reader(False))
        await asyncio.wait_for(both_inside.wait(), timeout=5)
        first_may_leave.set()
        await asyncio.gather(first, second)

    @pytest.mark.asyncio
    async def test_a_writer_still_excludes_a_reader(self, git_workspace):
        mgr = WorkspaceLockManager()
        writer_inside = asyncio.Event()
        reader_got_in = False

        async def writer() -> None:
            async with mgr.lock(git_workspace.str_path):
                writer_inside.set()
                await asyncio.sleep(0.25)

        async def reader() -> None:
            nonlocal reader_got_in
            await asyncio.wait_for(writer_inside.wait(), timeout=5)
            async with mgr.lock(git_workspace.str_path, shared=True):
                reader_got_in = True

        w = asyncio.create_task(writer())
        r = asyncio.create_task(reader())
        await asyncio.sleep(0.05)
        assert not reader_got_in, "a reader must not enter while a writer holds the workspace"
        await asyncio.gather(w, r)
        assert reader_got_in, "the reader must get in once the writer leaves"

    @pytest.mark.asyncio
    async def test_a_waiting_writer_is_not_starved_by_arriving_readers(self, git_workspace):
        """The failure mode of a naive reader-preferring lock, and worse here than contention.

        A steady trickle of investigations must not hold an edit off forever, so a reader that
        arrives while a writer waits queues behind it instead of joining the batch in progress.
        """
        mgr = WorkspaceLockManager()
        order: list[str] = []
        first_reader_in = asyncio.Event()
        release_first = asyncio.Event()

        async def first_reader() -> None:
            async with mgr.lock(git_workspace.str_path, shared=True):
                first_reader_in.set()
                await asyncio.wait_for(release_first.wait(), timeout=5)
                order.append("reader-1")

        async def waiting_writer() -> None:
            await asyncio.wait_for(first_reader_in.wait(), timeout=5)
            await asyncio.sleep(0.05)
            async with mgr.lock(git_workspace.str_path):
                order.append("writer")

        async def late_reader() -> None:
            await asyncio.wait_for(first_reader_in.wait(), timeout=5)
            await asyncio.sleep(0.15)  # arrives after the writer is already queued
            async with mgr.lock(git_workspace.str_path, shared=True):
                order.append("reader-2")

        tasks = [
            asyncio.create_task(first_reader()),
            asyncio.create_task(waiting_writer()),
            asyncio.create_task(late_reader()),
        ]
        await asyncio.sleep(0.3)
        release_first.set()
        await asyncio.gather(*tasks)

        assert order.index("writer") < order.index("reader-2"), (
            f"a reader arriving after the writer queued jumped ahead of it: {order}"
        )

    @pytest.mark.asyncio
    async def test_a_timed_out_writer_does_not_block_later_readers(self, git_workspace):
        """The cancellation path: a writer that gives up must stop being counted as waiting.

        `_writers_waiting` is decremented in a `finally` precisely so this holds - leave it out
        and one timeout wedges every future reader on that workspace for the process lifetime.
        """
        mgr = WorkspaceLockManager()
        holder_in = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder() -> None:
            async with mgr.lock(git_workspace.str_path):
                holder_in.set()
                await asyncio.wait_for(release_holder.wait(), timeout=5)

        h = asyncio.create_task(holder())
        await asyncio.wait_for(holder_in.wait(), timeout=5)

        with pytest.raises(WorkspaceLockTimeoutError):
            async with mgr.lock(git_workspace.str_path, timeout=0.1):
                pass

        release_holder.set()
        await h

        async with mgr.lock(git_workspace.str_path, shared=True, timeout=2):
            pass
