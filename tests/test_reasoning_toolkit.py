"""Tests for the AI Deep Reasoning Toolkit integration.

The integration's whole job is to put the toolkit where AGY will read it, name the deep-verify
skill when asked, and report back what the protocol said - without any of that leaking into the
user's repository or into the diffs this server reports. These tests hold each of those.

No real agy process is involved: the executor is stubbed, so what is under test is this
server's behaviour, not Gemini's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_agy.core import reasoning_toolkit
from mcp_agy.core.models import TaskExecutionResult
from mcp_agy.core.report import with_report_contract
from mcp_agy.server import create_mcp_server


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def isolated_job_markers(tmp_path: Path, monkeypatch):
    """Keep this module's background jobs out of the machine-wide marker directory.

    Job results are persisted so they survive a server restart, which means a job started here
    is recoverable by any other server process on the machine - including the stdio subprocess
    that tests/test_stream_purity_adversarial.py spawns, whose `agy_list_jobs` assertion counts
    exactly one job. Without this, the background test below made that unrelated test fail.
    """
    markers = tmp_path / "job_markers"
    markers.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_AGY_JOB_MARKER_DIR", str(markers))


@pytest.fixture
def fake_toolkit(tmp_path: Path) -> Path:
    """A checkout shaped like the real repo, small enough to assert on byte-for-byte."""
    root = tmp_path / "ai-deep-reasoning-toolkit"
    (root / ".agents" / "skills" / "deep-verify" / "examples").mkdir(parents=True)
    (root / "GEMINI.md").write_text("# SYSTEM PROTOCOL\n\n## 4. Simplicity Gate\n", encoding="utf-8")
    (root / ".agents" / "skills" / "deep-verify" / "SKILL.md").write_text(
        "---\nname: deep-verify\n---\n", encoding="utf-8"
    )
    (root / ".agents" / "skills" / "deep-verify" / "examples" / "workflow.md").write_text(
        "example\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    """A real git repo, because the git-exclusion behaviour is half of what is being tested."""
    ws = tmp_path / "project"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=ws,
        check=True,
    )
    return ws


@pytest.fixture
def toolkit_env(monkeypatch, fake_toolkit: Path):
    """Point the server at the fake checkout and clear the per-process revision cache."""
    monkeypatch.setenv(reasoning_toolkit.TOOLKIT_ENV, str(fake_toolkit))
    reasoning_toolkit._revision_cache.clear()
    yield fake_toolkit
    reasoning_toolkit._revision_cache.clear()


class PromptCapturingExecutor:
    """Stands in for a backend, recording the prompt it was handed.

    Every task prompt ends in the server's REPORT contract, which is not the toolkit's doing:
    it is checked here and recorded without it, so these tests see only what the toolkit changed.
    """

    def __init__(self, response: str = "done") -> None:
        self.response = response
        self.prompts: list[str] = []

    async def __call__(self, **kwargs) -> TaskExecutionResult:
        prompt = kwargs["prompt"]
        contract = with_report_contract("", kwargs.get("mode", "accept-edits"))
        assert prompt.endswith(contract), "every task prompt must end in the REPORT contract"
        self.prompts.append(prompt[: -len(contract)])
        return TaskExecutionResult(status="success", response=self.response)


def payload(result):
    """Read a tool result as a dict (the text block carries the full model JSON)."""
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    return {}


# ============================================================================
# 1. Provisioning
# ============================================================================


class TestProvisioning:
    """Verifies what lands in the workspace, and what is deliberately left alone."""

    def test_installs_both_artefacts(self, fake_toolkit, git_workspace):
        installed, notes = reasoning_toolkit.provision(git_workspace, fake_toolkit)

        assert (git_workspace / "GEMINI.md").is_file()
        assert (git_workspace / ".agents/skills/deep-verify/SKILL.md").is_file()
        # Copied whole, not just the entry point: the skill's examples are part of it.
        assert (git_workspace / ".agents/skills/deep-verify/examples/workflow.md").is_file()
        assert set(installed) == {"GEMINI.md", ".agents/skills/deep-verify"}
        assert notes == ""

    def test_never_overwrites_an_existing_rules_file(self, fake_toolkit, git_workspace):
        """A GEMINI.md already in the repo is the user's, and there is no way to merge two."""
        mine = "# my own rules, do not touch\n"
        (git_workspace / "GEMINI.md").write_text(mine, encoding="utf-8")

        installed, notes = reasoning_toolkit.provision(git_workspace, fake_toolkit)

        assert (git_workspace / "GEMINI.md").read_text(encoding="utf-8") == mine
        assert "GEMINI.md" not in installed
        assert "already present" in notes

    def test_never_writes_agents_md(self, fake_toolkit, git_workspace):
        """Claude Code reads AGENTS.md too; writing it would destroy the worker/architect split."""
        reasoning_toolkit.provision(git_workspace, fake_toolkit)

        assert not (git_workspace / "AGENTS.md").exists()
        assert not (git_workspace / ".claude").exists()

    def test_is_idempotent(self, fake_toolkit, git_workspace):
        """Two shared-lock plan jobs can provision the same workspace at once."""
        reasoning_toolkit.provision(git_workspace, fake_toolkit)
        installed, _ = reasoning_toolkit.provision(git_workspace, fake_toolkit)

        assert installed == []
        assert (git_workspace / ".agents/skills/deep-verify/SKILL.md").is_file()

    def test_a_non_git_workspace_is_not_an_error(self, fake_toolkit, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()

        installed, _ = reasoning_toolkit.provision(plain, fake_toolkit)

        assert set(installed) == {"GEMINI.md", ".agents/skills/deep-verify"}


class TestGitInvisibility:
    """The installed files must not show up as changes in the user's repo, or in ours."""

    def test_git_status_stays_clean(self, fake_toolkit, git_workspace):
        """This is what spares the diff engine a skip-list it would have to keep in sync."""
        reasoning_toolkit.provision(git_workspace, fake_toolkit)

        out = subprocess.run(
            ["git", "status", "--porcelain=v1", "-uall"],
            cwd=git_workspace,
            capture_output=True,
            text=True,
        ).stdout

        assert out.strip() == "", f"toolkit files leaked into git status:\n{out}"

    def test_exclusion_is_local_only(self, fake_toolkit, git_workspace):
        """`.git/info/exclude` is never committed; a .gitignore edit would be."""
        reasoning_toolkit.provision(git_workspace, fake_toolkit)

        assert not (git_workspace / ".gitignore").exists()
        exclude = (git_workspace / ".git/info/exclude").read_text(encoding="utf-8")
        assert "/GEMINI.md" in exclude
        assert "/.agents/skills/deep-verify/" in exclude

    def test_exclusion_is_not_duplicated(self, fake_toolkit, git_workspace):
        reasoning_toolkit.provision(git_workspace, fake_toolkit)
        reasoning_toolkit.provision(git_workspace, fake_toolkit)

        exclude = (git_workspace / ".git/info/exclude").read_text(encoding="utf-8")
        assert exclude.count("/GEMINI.md") == 1

    def test_preserves_existing_exclude_entries(self, fake_toolkit, git_workspace):
        exclude = git_workspace / ".git/info/exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("scratch/\n", encoding="utf-8")

        reasoning_toolkit.provision(git_workspace, fake_toolkit)

        content = exclude.read_text(encoding="utf-8")
        assert "scratch/" in content
        assert "/GEMINI.md" in content

    def test_a_linked_worktree_stays_clean_too(self, fake_toolkit, git_workspace, tmp_path):
        """In a worktree `.git` is a file, so there is no `.git/info/` to write into.

        Parallel tasks are meant to each run in their own worktree, so this is the layout the
        toolkit has to work in - not an edge case.
        """
        worktree = tmp_path / "task-1"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "task-1", str(worktree)],
            cwd=git_workspace,
            check=True,
        )
        assert (worktree / ".git").is_file(), "fixture is not a linked worktree"

        installed, _ = reasoning_toolkit.provision(worktree, fake_toolkit)

        assert "GEMINI.md" in installed
        out = subprocess.run(
            ["git", "status", "--porcelain=v1", "-uall"],
            cwd=worktree,
            capture_output=True,
            text=True,
        ).stdout
        assert out.strip() == "", f"toolkit files leaked into the worktree's git status:\n{out}"


# ============================================================================
# 2. Reading the protocol's mandated one-liners
# ============================================================================


class TestProtocolReport:
    """Only the two formats the toolkit actually mandates are parsed."""

    def test_reads_the_gate_line(self):
        gate, declined = reasoning_toolkit.read_protocol_report(
            "Here is the code.\n\nSimplicity gate: cut _helper, _fmt; kept parse (LOAD-BEARING: contract rule 3).\n"
        )

        assert gate == "Simplicity gate: cut _helper, _fmt; kept parse (LOAD-BEARING: contract rule 3)."
        assert declined is False

    def test_reads_the_gate_line_through_markdown_decoration(self):
        """Section 4.4 shows the line as a blockquote, and models bold it."""
        gate, _ = reasoning_toolkit.read_protocol_report("> **Simplicity gate: nothing to cut. Ledger:**")

        assert gate.startswith("Simplicity gate: nothing to cut.")

    def test_absent_gate_line_is_reported_as_absent(self):
        """4.4: a missing gate line means the gate did not run - so do not invent one."""
        gate, declined = reasoning_toolkit.read_protocol_report("I refactored the module. All tests pass.")

        assert gate == ""
        assert declined is False

    def test_reads_the_deep_verify_declination(self):
        _, declined = reasoning_toolkit.read_protocol_report(
            "deep-verify: not activated - contract is fully specified, single viable design.\n\nHere is the code."
        )

        assert declined is True

    def test_empty_response_is_safe(self):
        assert reasoning_toolkit.read_protocol_report("") == ("", False)


class TestToolkitRoot:
    """A misconfigured path must not look like a working install."""

    def test_unset_means_disabled(self, monkeypatch):
        monkeypatch.delenv(reasoning_toolkit.TOOLKIT_ENV, raising=False)

        assert reasoning_toolkit.toolkit_root() is None

    def test_a_directory_without_the_rules_file_is_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv(reasoning_toolkit.TOOLKIT_ENV, str(tmp_path))

        assert reasoning_toolkit.toolkit_root() is None

    def test_a_valid_checkout_is_accepted(self, monkeypatch, fake_toolkit):
        monkeypatch.setenv(reasoning_toolkit.TOOLKIT_ENV, str(fake_toolkit))

        assert reasoning_toolkit.toolkit_root() == fake_toolkit


# ============================================================================
# 3. End to end through the MCP tools
# ============================================================================


class TestRigorThroughTheServer:
    """What an architect actually sees when it passes `rigor`."""

    async def test_standard_installs_and_leaves_the_prompt_alone(self, toolkit_env, git_workspace):
        executor = PromptCapturingExecutor("Simplicity gate: cut _unused.")
        server = create_mcp_server(backend_executor=executor)

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            res = payload(
                await session.call_tool(
                    "agy_execute_task",
                    {"workspace_path": str(git_workspace), "prompt": "Add a health endpoint."},
                )
            )

        assert executor.prompts == ["Add a health endpoint."]
        assert res["reasoning"]["rigor"] == "standard"
        assert res["reasoning"]["toolkit_active"] is True
        assert res["reasoning"]["gate_line"] == "Simplicity gate: cut _unused."
        assert (git_workspace / "GEMINI.md").is_file()

    async def test_deep_names_the_skill_in_the_prompt(self, toolkit_env, git_workspace):
        """The measured failure this exists for: the skill is not discovered unless named."""
        executor = PromptCapturingExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            res = payload(
                await session.call_tool(
                    "agy_execute_task",
                    {
                        "workspace_path": str(git_workspace),
                        "prompt": "Design the event dispatcher.",
                        "rigor": "deep",
                    },
                )
            )

        sent = executor.prompts[0]
        assert "deep-verify" in sent
        assert "Comparative Matrix" in sent
        assert sent.endswith("Design the event dispatcher.")
        assert res["reasoning"]["rigor"] == "deep"

    async def test_deep_still_permits_declining(self, toolkit_env, git_workspace):
        """A matrix of one real design and two strawmen is a defect, so declining is reported."""
        executor = PromptCapturingExecutor(
            "deep-verify: not activated - contract is fully specified, single viable design.\n"
            "Simplicity gate: nothing to cut. Ledger: ..."
        )
        server = create_mcp_server(backend_executor=executor)

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            res = payload(
                await session.call_tool(
                    "agy_execute_task",
                    {"workspace_path": str(git_workspace), "prompt": "Add a getter.", "rigor": "deep"},
                )
            )

        assert res["reasoning"]["deep_verify_declined"] is True

    async def test_off_touches_nothing(self, toolkit_env, git_workspace):
        executor = PromptCapturingExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            res = payload(
                await session.call_tool(
                    "agy_execute_task",
                    {"workspace_path": str(git_workspace), "prompt": "Rename a variable.", "rigor": "off"},
                )
            )

        assert not (git_workspace / "GEMINI.md").exists()
        assert res["reasoning"]["toolkit_active"] is False
        assert executor.prompts == ["Rename a variable."]

    async def test_unconfigured_toolkit_leaves_results_untouched(self, monkeypatch, git_workspace):
        """With the integration off, a result carries no profile and nothing is written."""
        monkeypatch.delenv(reasoning_toolkit.TOOLKIT_ENV, raising=False)
        executor = PromptCapturingExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            res = payload(
                await session.call_tool(
                    "agy_execute_task",
                    {"workspace_path": str(git_workspace), "prompt": "Fix the typo."},
                )
            )

        assert res["reasoning"] is None
        assert not (git_workspace / "GEMINI.md").exists()

    async def test_deep_without_a_toolkit_says_so_rather_than_pretending(
        self, monkeypatch, git_workspace
    ):
        """Silently downgrading would let a plain answer be read as a verified one."""
        monkeypatch.delenv(reasoning_toolkit.TOOLKIT_ENV, raising=False)
        executor = PromptCapturingExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            res = payload(
                await session.call_tool(
                    "agy_execute_task",
                    {"workspace_path": str(git_workspace), "prompt": "Design it.", "rigor": "deep"},
                )
            )

        assert res["reasoning"]["toolkit_active"] is False
        assert reasoning_toolkit.TOOLKIT_ENV in res["reasoning"]["notes"]
        assert executor.prompts == ["Design it."]

    async def test_background_tasks_carry_the_profile_too(self, toolkit_env, git_workspace):
        executor = PromptCapturingExecutor("Simplicity gate: cut nothing. Ledger: ...")
        server = create_mcp_server(backend_executor=executor)

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            handle = payload(
                await session.call_tool(
                    "agy_start_task",
                    {
                        "workspace_path": str(git_workspace),
                        "prompt": "Wire the router.",
                        "rigor": "deep",
                    },
                )
            )
            status = payload(
                await session.call_tool(
                    "agy_job_status", {"job_id": handle["job_id"], "wait_seconds": 30}
                )
            )

        assert status["status"] == "completed"
        assert status["result"]["reasoning"]["rigor"] == "deep"
        assert status["result"]["reasoning"]["toolkit_active"] is True
        assert "deep-verify" in executor.prompts[0]


class TestDiffStaysClean:
    """The point of the git exclusion, asserted through the tool an architect actually calls."""

    async def test_get_diff_does_not_report_the_toolkit(self, toolkit_env, git_workspace):
        executor = PromptCapturingExecutor()
        server = create_mcp_server(backend_executor=executor)

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            await session.call_tool(
                "agy_execute_task",
                {"workspace_path": str(git_workspace), "prompt": "Do the thing."},
            )
            diff = payload(await session.call_tool("agy_get_diff", {"workspace_path": str(git_workspace)}))

        assert diff["has_changes"] is False, f"toolkit leaked into the diff: {diff['summary']}"
        assert diff["untracked_files"] == []
