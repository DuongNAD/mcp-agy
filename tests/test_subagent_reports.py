"""Tests for the subagent loop: the REPORT contract, agy_wait, the channel announcement, and the
guidance any architect model reads before it starts.

The architect splits work, dispatches it with agy_start_task and needs a signal for "this
subagent is back". agy_wait is that signal on every client; the channel announcement is the push
version for Claude Code. These tests hold that each report is handed out exactly once, that a
wait returns on the first completion rather than at its deadline, and that the push is only sent
for a report nobody has read.

Driven through a real MCP ClientSession, so what is asserted is what the architect receives.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import time
from typing import Any, AsyncGenerator, Dict, List

import anyio
import pytest
from mcp.client.session import ClientSession

from mcp_agy.core import channel
from mcp_agy.core.jobs import reset_job_manager
from mcp_agy.core.models import TaskExecutionResult
from mcp_agy.core.report import parse_report, with_report_contract
from mcp_agy.server import PLAYBOOK, SERVER_INSTRUCTIONS, create_mcp_server

REPORTED_RESPONSE = (
    "Refactored the teardown path.\n\n"
    "REPORT\n"
    "STATUS: done\n"
    "SUMMARY: Teardown now releases the socket before the timer.\n"
    "CHANGED: src/gateway.ts, tests/gateway.test.ts\n"
    "CHECKS: npm test -> 14 passed\n"
    "BLOCKERS: none"
)


@pytest.fixture(autouse=True)
def _clean_job_registry(tmp_path, monkeypatch):
    marker_d = tmp_path / "mcp_agy_jobs"
    marker_d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_AGY_JOB_MARKER_DIR", str(marker_d))
    monkeypatch.delenv(channel.CHANNEL_ENV, raising=False)
    reset_job_manager()
    yield
    reset_job_manager()


def _payload(call_res: Any) -> Dict[str, Any]:
    return json.loads(call_res.content[0].text)


class _GatedExecutor:
    """Each run waits on its own gate, keyed by the first line of its prompt."""

    def __init__(self, response: str = REPORTED_RESPONSE) -> None:
        self.response = response
        self.gates: Dict[str, asyncio.Event] = {}
        self.prompts: List[str] = []

    def gate(self, key: str) -> asyncio.Event:
        return self.gates.setdefault(key, asyncio.Event())

    async def __call__(self, **kwargs: Any) -> TaskExecutionResult:
        prompt = kwargs["prompt"]
        self.prompts.append(prompt)
        await self.gate(prompt.splitlines()[0]).wait()
        return TaskExecutionResult(status="success", response=self.response)


async def _start(session: ClientSession, workspace: Any, prompt: str, mode: str = "plan") -> str:
    handle = _payload(
        await session.call_tool(
            "agy_start_task",
            {"workspace_path": str(workspace), "prompt": prompt, "mode": mode},
        )
    )
    assert handle["status"] == "running"
    return handle["job_id"]


@asynccontextmanager
async def _tapped_session(server: Any) -> AsyncGenerator[tuple[ClientSession, Any, list], None]:
    """A client session with a tap on the server's outbound stream that records channel
    notifications - the SDK client would only log them as unknown and drop them."""
    server_send, tap_recv = anyio.create_memory_object_stream(50)
    tap_send, client_recv = anyio.create_memory_object_stream(50)
    client_send, server_recv = anyio.create_memory_object_stream(50)
    announced: list = []
    lowlevel = server._mcp_server

    async with anyio.create_task_group() as tg:

        async def run_server() -> None:
            async with server_send, server_recv:
                await lowlevel.run(server_recv, server_send, lowlevel.create_initialization_options())

        async def relay() -> None:
            async with tap_recv, tap_send:
                async for msg in tap_recv:
                    root = getattr(getattr(msg, "message", None), "root", None)
                    if getattr(root, "method", None) == channel.CHANNEL_METHOD:
                        announced.append(root.params)
                        continue
                    await tap_send.send(msg)

        tg.start_soon(run_server)
        tg.start_soon(relay)
        async with client_send, client_recv:
            async with ClientSession(client_recv, client_send) as session:
                init = await session.initialize()
                yield session, init, announced
        tg.cancel_scope.cancel()


# ============================================================================
# 1. The REPORT contract
# ============================================================================


class TestReportContract:
    def test_a_full_block_is_parsed(self):
        report = parse_report(REPORTED_RESPONSE)
        assert report is not None
        assert report.status == "done"
        assert report.summary.startswith("Teardown now releases")
        assert report.changed == ["src/gateway.ts", "tests/gateway.test.ts"]
        assert report.checks == "npm test -> 14 passed"
        assert report.blockers == ""

    def test_markdown_decoration_is_tolerated(self):
        report = parse_report(
            "Body.\n\n**REPORT**\n- **STATUS:** blocked\n- SUMMARY: Needs a schema decision.\n"
            "- CHANGED: `src/db.py`\n- BLOCKERS: which column type to use"
        )
        assert report is not None
        assert report.status == "blocked"
        assert report.changed == ["src/db.py"]
        assert report.blockers == "which column type to use"

    def test_the_last_block_wins(self):
        report = parse_report("REPORT\nSTATUS: partial\n\nlater...\nREPORT\nSTATUS: done")
        assert report is not None and report.status == "done"

    def test_a_block_without_its_header_still_counts(self):
        report = parse_report("All done.\nSTATUS: partial\nSUMMARY: two of three endpoints")
        assert report is not None
        assert report.status == "partial"
        assert report.summary == "two of three endpoints"

    def test_an_echoed_template_is_not_read_as_a_verdict(self):
        """A reply that pastes the template has chosen nothing; 'done' would be a lie."""
        report = parse_report(with_report_contract("Fix it", "accept-edits"))
        assert report is not None
        assert report.status == "unknown"

    def test_no_block_means_no_report(self):
        assert parse_report("I changed some files.") is None
        assert parse_report("") is None

    def test_plan_runs_are_told_their_findings_are_the_deliverable(self):
        plan = with_report_contract("Audit auth", "plan")
        edit = with_report_contract("Audit auth", "accept-edits")
        assert "findings" in plan and "brief" not in plan
        assert "brief" in edit
        assert plan.startswith("Audit auth") and edit.startswith("Audit auth")


# ============================================================================
# 2. agy_wait - the "subagent returned" signal
# ============================================================================


@pytest.mark.anyio
class TestAgyWait:
    async def test_a_finished_job_is_reported_once_with_its_parsed_report(
        self, client_factory, git_workspace
    ):
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            job_id = await _start(session, git_workspace, "Fix the teardown", mode="accept-edits")
            executor.gate("Fix the teardown").set()

            first = _payload(await session.call_tool("agy_wait", {"wait_seconds": 10}))
            assert first["status"] == "ready"
            assert [j["job_id"] for j in first["finished"]] == [job_id]
            report = first["finished"][0]["result"]["report"]
            assert report["status"] == "done"
            assert report["changed"] == ["src/gateway.ts", "tests/gateway.test.ts"]
            assert first["running_count"] == 0

            # The worker was asked for the block - the architect never had to.
            assert executor.prompts[0].endswith(with_report_contract("", "accept-edits"))

            second = _payload(await session.call_tool("agy_wait", {"wait_seconds": 1}))
            assert second["status"] == "idle"
            assert second["finished"] == []

    async def test_returns_on_the_first_finish_not_at_the_deadline(
        self, client_factory, git_workspace
    ):
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            fast = await _start(session, git_workspace, "Investigate A")
            slow = await _start(session, git_workspace, "Investigate B")

            async def _release_soon() -> None:
                await asyncio.sleep(0.2)
                executor.gate("Investigate A").set()

            releaser = asyncio.create_task(_release_soon())
            began = time.monotonic()
            res = _payload(await session.call_tool("agy_wait", {"wait_seconds": 30}))
            elapsed = time.monotonic() - began
            await releaser

            assert elapsed < 10, f"agy_wait sat out its deadline ({elapsed:.1f}s)"
            assert res["status"] == "ready"
            assert [j["job_id"] for j in res["finished"]] == [fast]
            assert [j["job_id"] for j in res["still_running"]] == [slow]
            assert res["still_running"][0]["result"] is None

            executor.gate("Investigate B").set()
            rest = _payload(await session.call_tool("agy_wait", {"wait_seconds": 10}))
            assert [j["job_id"] for j in rest["finished"]] == [slow]

    async def test_a_wait_that_times_out_leaves_the_job_running(self, client_factory, git_workspace):
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            job_id = await _start(session, git_workspace, "Long investigation")
            res = _payload(await session.call_tool("agy_wait", {"wait_seconds": 1}))
            assert res["status"] == "running"
            assert res["finished"] == []
            assert [j["job_id"] for j in res["still_running"]] == [job_id]

            executor.gate("Long investigation").set()
            done = _payload(
                await session.call_tool("agy_wait", {"job_ids": [job_id], "wait_seconds": 10})
            )
            assert done["status"] == "ready"
            assert done["finished"][0]["status"] == "completed"

    async def test_named_ids_scope_the_wait_and_unknown_ids_are_reported(
        self, client_factory, git_workspace
    ):
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            mine = await _start(session, git_workspace, "Investigate mine")
            other = await _start(session, git_workspace, "Investigate other")
            executor.gate("Investigate other").set()

            res = _payload(
                await session.call_tool(
                    "agy_wait", {"job_ids": [mine, "no-such-job"], "wait_seconds": 1}
                )
            )
            # `other` finished, but it was not asked about.
            assert res["status"] == "running"
            assert res["unknown_job_ids"] == ["no-such-job"]
            assert [j["job_id"] for j in res["still_running"]] == [mine]

            everything = _payload(await session.call_tool("agy_wait", {"wait_seconds": 0}))
            assert [j["job_id"] for j in everything["finished"]] == [other]
            executor.gate("Investigate mine").set()

    async def test_a_result_read_through_job_status_is_not_handed_out_again(
        self, client_factory, git_workspace
    ):
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with client_factory(server) as session:
            job_id = await _start(session, git_workspace, "Investigate C")
            executor.gate("Investigate C").set()
            status = _payload(
                await session.call_tool("agy_job_status", {"job_id": job_id, "wait_seconds": 10})
            )
            assert status["status"] == "completed"

            res = _payload(await session.call_tool("agy_wait", {"wait_seconds": 0}))
            assert res["status"] == "idle"

    async def test_a_failed_job_is_reported_as_a_failure(self, client_factory, git_workspace):
        async def _boom(**kwargs: Any) -> TaskExecutionResult:
            raise RuntimeError("agy crashed")

        server = create_mcp_server(backend_executor=_boom)
        async with client_factory(server) as session:
            job_id = await _start(session, git_workspace, "Crash", mode="accept-edits")
            res = _payload(await session.call_tool("agy_wait", {"wait_seconds": 10}))
            assert res["status"] == "ready"
            assert res["finished"][0]["job_id"] == job_id
            assert res["finished"][0]["status"] == "failed"
            assert "agy crashed" in res["finished"][0]["error_details"]

    async def test_nothing_started_is_idle_at_once(self, client_factory):
        server = create_mcp_server(backend_executor=_GatedExecutor())
        async with client_factory(server) as session:
            began = time.monotonic()
            res = _payload(await session.call_tool("agy_wait", {"wait_seconds": 30}))
            assert res["status"] == "idle"
            assert time.monotonic() - began < 5


# ============================================================================
# 3. The channel announcement
# ============================================================================


@pytest.mark.anyio
class TestChannelAnnouncement:
    async def test_the_capability_is_declared(self):
        server = create_mcp_server(backend_executor=_GatedExecutor())
        async with _tapped_session(server) as (_, init, _announced):
            assert channel.CHANNEL_CAPABILITY in (init.capabilities.experimental or {})

    async def test_an_unread_finish_is_announced_with_its_job_id(
        self, git_workspace, monkeypatch
    ):
        monkeypatch.setattr(channel, "ANNOUNCE_GRACE_SECONDS", 0.05)
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with _tapped_session(server) as (session, _, announced):
            job_id = await _start(session, git_workspace, "Investigate D")
            executor.gate("Investigate D").set()
            for _ in range(100):
                if announced:
                    break
                await asyncio.sleep(0.05)

            assert len(announced) == 1
            params = announced[0]
            assert params["meta"]["job_id"] == job_id
            assert params["meta"]["outcome"] == "done"
            assert all(k.isidentifier() for k in params["meta"]), "Claude Code drops other keys"
            assert job_id in params["content"] and "agy_wait" in params["content"]
            assert "Teardown now releases" in params["content"]

            # Announcing is not collecting: the report is still there to be read.
            res = _payload(await session.call_tool("agy_wait", {"wait_seconds": 0}))
            assert [j["job_id"] for j in res["finished"]] == [job_id]

    async def test_a_report_collected_in_time_is_not_announced(self, git_workspace, monkeypatch):
        monkeypatch.setattr(channel, "ANNOUNCE_GRACE_SECONDS", 0.5)
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with _tapped_session(server) as (session, _, announced):
            await _start(session, git_workspace, "Investigate E")

            async def _release_soon() -> None:
                await asyncio.sleep(0.1)
                executor.gate("Investigate E").set()

            releaser = asyncio.create_task(_release_soon())
            res = _payload(await session.call_tool("agy_wait", {"wait_seconds": 10}))
            await releaser
            assert res["status"] == "ready"

            await asyncio.sleep(0.8)
            assert announced == []

    async def test_switched_off_means_no_capability_and_no_announcement(
        self, git_workspace, monkeypatch
    ):
        monkeypatch.setenv(channel.CHANNEL_ENV, "0")
        monkeypatch.setattr(channel, "ANNOUNCE_GRACE_SECONDS", 0.05)
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with _tapped_session(server) as (session, init, announced):
            assert channel.CHANNEL_CAPABILITY not in (init.capabilities.experimental or {})
            await _start(session, git_workspace, "Investigate F")
            executor.gate("Investigate F").set()
            await asyncio.sleep(0.5)
            assert announced == []


# ============================================================================
# 4. What a model reads before it starts
# ============================================================================


@pytest.mark.anyio
class TestGuidance:
    async def test_instructions_describe_the_whole_loop(self):
        server = create_mcp_server(backend_executor=_GatedExecutor())
        assert server._mcp_server.create_initialization_options().instructions == SERVER_INSTRUCTIONS
        for step in ("agy_start_task", "agy_wait", "agy_get_diff", "agy_run_tests", "idle"):
            assert step in SERVER_INSTRUCTIONS

    async def test_the_delegate_prompt_carries_the_playbook_and_the_goal(self, client_factory):
        server = create_mcp_server(backend_executor=_GatedExecutor())
        async with client_factory(server) as session:
            prompts = await session.list_prompts()
            assert [p.name for p in prompts.prompts] == ["delegate"]
            got = await session.get_prompt(
                "delegate", {"goal": "Add rate limiting", "workspace_path": "E:/proj"}
            )
            text = got.messages[0].content.text
            assert "agy_wait" in text
            assert "Goal: Add rate limiting" in text
            assert "Workspace: E:/proj" in text

    async def test_the_playbook_resource_reads_back(self, client_factory):
        server = create_mcp_server(backend_executor=_GatedExecutor())
        async with client_factory(server) as session:
            resources = await session.list_resources()
            assert [str(r.uri) for r in resources.resources] == ["agy://playbook"]
            read = await session.read_resource("agy://playbook")
            assert read.contents[0].text == PLAYBOOK

    async def test_start_task_says_what_to_do_next(self, client_factory, git_workspace):
        executor = _GatedExecutor()
        server = create_mcp_server(backend_executor=executor)
        async with client_factory(server) as session:
            handle = _payload(
                await session.call_tool(
                    "agy_start_task",
                    {"workspace_path": str(git_workspace), "prompt": "Investigate G", "mode": "plan"},
                )
            )
            assert "agy_wait" in handle["next_step"]
            executor.gate("Investigate G").set()
