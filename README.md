# `mcp-agy` — Google Antigravity FastMCP Server

<div align="center">

[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![FastMCP Protocol](https://img.shields.io/badge/MCP-FastMCP%202024--11--05-brightgreen.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()
[![Code Quality](https://img.shields.io/badge/tests-100%25%20pass-success.svg)]()

**Bridging High-Level AI Architect Agents with Autonomous Antigravity Coding Workers via Model Context Protocol (MCP)**

</div>

---

## 🌟 1. Overview & High-Level Vision

In contemporary AI-assisted software engineering, single-agent workflows frequently suffer from cognitive overload, context exhaustion, and execution drift. Large Language Models forced to simultaneously handle macro-level system architecture, cross-module requirements, and low-level line-by-line code editing often lose sight of overarching design invariants.

**`mcp-agy`** establishes a paradigm shift: the **Architect-Worker Division of Labor**.

```
+-----------------------------------------------------------------------------------+
|                           THE ARCHITECT-WORKER PARADIGM                           |
+-----------------------------------------------------------------------------------+
|                                                                                   |
|   STRATEGIC TIER (The Architect)                                                  |
|   - Claude Desktop, Cursor Composer, Cline, Roo Code                              |
|   - Responsibilities: System design, domain modeling, security audit, code review |
|                                                                                   |
|                                       │                                           |
|                                       │  Delegates via FastMCP JSON-RPC 2.0       |
|                                       ▼                                           |
|                                                                                   |
|   TACTICAL EXECUTION TIER (The Worker)                                            |
|   - Google Antigravity (AGY)                                                      |
|   - Responsibilities: Multi-file edits, build runs, test executions, git diffs    |
|                                                                                   |
+-----------------------------------------------------------------------------------+
```

### Key Pillars:
1. **Separation of Concerns**: The AI Architect focuses on reasoning, design trade-offs, and verification. Google Antigravity (AGY) operates as a high-speed execution sandbox performing atomic edits, shell commands, and automated test runs.
2. **Pristine Stdio Protocol Purity**: Standard output (`sys.stdout`) is strictly reserved for JSON-RPC 2.0 frames. All logging, startup banners, and diagnostic traces route exclusively to `sys.stderr`, preventing framing corruption in MCP clients.
3. **Resilient 3-Tier Backend Fallback**: Seamlessly transitions across Python SDK (`Tier 1`), Subprocess CLI (`Tier 2`), and High-Fidelity Simulation Mock (`Tier 3`) for continuous offline testing and CI workflows.
4. **Zero-Trust Workspace Isolation**: Path canonicalization, system-critical directory protection, and per-workspace asynchronous mutex concurrency locking prevent race conditions and unintended file modifications.
5. **Pluggable Worker Reasoning Protocol**: Optional delivery of the [AI Deep Reasoning Toolkit](https://github.com/DuongNAD/ai-deep-reasoning-toolkit) into each workspace, constraining how the worker writes code while leaving the architect's own behaviour untouched. See [§6](#-6-deep-reasoning-toolkit-integration).

---

## 🏗️ 2. System Architecture & Data Flow

```text
+-------------------------------------------------------------------------------+
|                       External AI Architect Agent                             |
|          (Claude Desktop, Cursor Composer, Cline, Roo Code, Custom)           |
+---------------------------------------+---------------------------------------+
                                        |
                                        | JSON-RPC 2.0 over Stdio (Pure Stream)
                                        | (Zero stdout pollution; logs to stderr)
                                        v
+---------------------------------------+---------------------------------------+
|                               mcp_agy (FastMCP)                               |
|                                                                               |
|   +--------------------+  +------------+  +--------------+  +-------------+   |
|   | agy_execute_task   |  |  agy_chat  |  | agy_get_diff |  |agy_run_tests|   |
|   +---------+----------+  +-----+------+  +-------+------+  +------+------+   |
|             |                   |                 |                |          |
|             v                   v                 |                |          |
|   +---------+-------------------+------+          |                |          |
|   |          Backend Manager           |          |                |          |
|   |  - Tier 1: PythonSDKBackend        |          |                |          |
|   |  - Tier 2: SubprocessCLIBackend    |          |                |          |
|   |  - Tier 3: MockAGYBackend (CI)     |          |                |          |
|   |  - 3-Tier Fallback Hierarchy       |          |                |          |
|   +-----------------+------------------+          |                |          |
|                     |                             |                |          |
|                     |                             v                v          |
|                     |                    +--------+----+  +--------+------+   |
|                     |                    | Diff Engine |  |  Test Runner  |   |
|                     |                    | - Git diff  |  | - 13 Tiers    |   |
|                     |                    | - Untracked |  | - Auto-Detect |   |
|                     |                    | - Stats     |  | - Diagnostics |   |
|                     |                    +--------+----+  +--------+------+   |
|                     |                             |                |          |
|                     +-----------------------------+----------------+          |
|                                                   |                           |
|                                                   v                           |
|                                      +------------+------------+              |
|                                      | Workspace Safety Engine |              |
|                                      | - Path Canonicalization |              |
|                                      | - Boundary Validation   |              |
|                                      | - Async Concurrency Lock|              |
|                                      +------------+------------+              |
+---------------------------------------------------|---------------------------+
                                                    |
                                                    v
+---------------------------------------------------+---------------------------+
|                              Target Workspace Directory                       |
|   - Multi-file source code, git repository, virtual environments, tests       |
+-------------------------------------------------------------------------------+
```

### Core Subsystems:
- **FastMCP Server (`server.py`)**: High-performance asynchronous stdio MCP server exposing 4 rich tools with strict parameter validation and stderr logging.
- **Backend Manager (`backend_manager.py`)**: Dynamic fallback coordinator orchestrating execution across Python SDK, Subprocess CLI, and offline Simulation Mock.
- **NDJSON Stream Telemetry Engine (`cli_backend.py`)**: Real-time event parsing for tool calls, thought streams, and token consumption metrics.
- **Git Unified Diff Engine (`diff_engine.py`)**: Comprehensive diff capturing tracked modifications, staged files, and newly created untracked files.
- **Multi-Ecosystem Test Runner (`test_runner.py`)**: 13-tier test framework detector (pytest, unittest, npm, cargo, go, gradle, maven, dotnet, etc.) with ANSI color stripping and structured regex diagnostics.
- **Workspace Safety Engine (`workspace.py`)**: Realpath canonicalization, boundary validation, system root blocking, and async mutex locking per workspace.

---

## 📦 3. Installation & Prerequisites

### Prerequisites:
- **Operating System**: Windows 10/11, macOS 12+, Linux (Ubuntu 20.04+, Debian, Arch, Fedora)
- **Python**: Version 3.10, 3.11, or 3.12+
- **Package Manager**: `uv` (strongly recommended) or `pip`
- **Git**: Version 2.20+ (for unified diff engine and repository tracking)
- **Google Antigravity**: Antigravity CLI (`agy`) installed and authenticated, or `google-antigravity` Python SDK (offline simulation mock runs automatically if not present).

### Quick Installation:

```bash
# Clone the repository
git clone https://github.com/google/mcp-agy.git
cd mcp-agy

# Create virtual environment and install in editable mode using uv
uv venv
uv pip install -e .

# Or using standard pip:
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On macOS/Linux:
source .venv/bin/activate
pip install -e .
```

---

## 🧰 4. Complete Tool Suite Reference

`mcp-agy` exposes 8 tools tailored for AI Architect agents: 4 synchronous ones that answer
within a call, and 4 that run AGY as a background job so a long task never has to fit inside
one.

**Which to reach for.** `agy_execute_task` and `agy_chat` block until AGY is done, so they only
work for runs shorter than your client's per-call timeout — 60 seconds on Claude Code. For
anything larger, and that is most real work, start a job:

```
agy_start_task(workspace, prompt)          -> {"job_id": "...", "status": "running"}   # returns at once
   … the architect keeps working …
agy_job_status(job_id, wait_seconds=30)    -> {"status": "completed", "result": {...}} # collect
agy_get_diff(workspace)                    -> review what AGY actually changed
agy_run_tests(workspace)                   -> verify it
```

`agy_job_status` with `wait_seconds` returns the instant the job finishes, so a short job needs
no polling loop and a long one costs one cheap call per check. The recommended waiting pattern is
monitoring `done_marker_path` on disk (e.g. `until [ -f <path> ]; do sleep 5; done`) and calling
`agy_job_status(job_id, wait_seconds=0)`. `wait_seconds` should always be kept strictly below your
client's per-call timeout: a call with `wait_seconds=300` cut by a 60s client timeout will tear down
and restart the server process. Completed job results are atomically persisted to disk before `.done`
markers appear, so finished jobs survive server restarts and return with `recovered_from_disk=True`.

| Tool Name | Parameter | Type | Required | Default | Description |
|---|---|---|---|---|---|
| **`agy_start_task`** | `workspace_path` | `str` | **Yes** | — | Target workspace. Returns a `job_id` immediately; AGY keeps running in the server. |
| | `prompt` | `str` | **Yes** | — | Self-contained instructions for AGY. |
| | `auto_approve` | `bool` | No | `True` | Auto-approve AGY's tool executions and file edits. |
| | `mode` | `Literal["accept-edits", "plan"]` | No | `"accept-edits"` | `"plan"` is the read-only form — the long-running equivalent of `agy_chat`. |
| | `timeout_seconds` | `int` | No | `600` | Bounds **the run**, not this call (1 to 3600). |
| | `rigor` | `Literal["standard", "deep", "off"]` | No | `"standard"` | Reasoning protocol for the run. Inert unless `MCP_AGY_TOOLKIT_PATH` is set — see [§6](#-6-deep-reasoning-toolkit-integration). |
| **`agy_job_status`** | `job_id` | `str` | **Yes** | — | Id returned by `agy_start_task`. |
| | `wait_seconds` | `int` | No | `0` | Block up to N seconds, returning early on completion. Capped at 45 so the call always fits inside a client timeout. |
| **`agy_cancel_job`** | `job_id` | `str` | **Yes** | — | Stops the run and kills its process tree. Not a rollback — files already written stay written. |
| **`agy_list_jobs`** | — | — | — | — | Every known job, newest first, without their results. The recovery path when a `job_id` has fallen out of context; collect a result with `agy_job_status`. |

And the 4 synchronous tools:

| Tool Name | Parameter | Type | Required | Default | Description |
|---|---|---|---|---|---|
| **`agy_execute_task`** | `workspace_path` | `str` | **Yes** | — | Target workspace path where AGY will execute coding tasks. Must be an existing directory. |
| | `prompt` | `str` | **Yes** | — | Comprehensive coding instructions, file paths, requirements, and constraints. |
| | `auto_approve` | `bool` | No | `True` | Automatically approve tool executions and file edits without interactive confirmation. |
| | `mode` | `Literal["accept-edits", "plan"]` | No | `"accept-edits"` | `"accept-edits"` for full file modifications; `"plan"` for read-only analysis without disk writes. |
| | `timeout_seconds` | `int` | No | `600` | Maximum execution duration in seconds (1 to 3600). Enforces process tree termination on timeout. |
| | `rigor` | `Literal["standard", "deep", "off"]` | No | `"standard"` | Reasoning protocol for the run. Inert unless `MCP_AGY_TOOLKIT_PATH` is set — see [§6](#-6-deep-reasoning-toolkit-integration). |
| **`agy_chat`** | `prompt` | `str` | **Yes** | — | Analytical query, codebase question, architecture review, or planning consultation. |
| | `workspace_path` | `str` | No | `""` | Optional workspace directory to provide codebase context for analysis (strictly read-only). |
| | `conversation_id` | `str` | No | `""` | UUID of a previous conversation session to maintain multi-turn dialogue context. |
| | `timeout_seconds` | `int` | No | `300` | Maximum consultation duration in seconds (1 to 1800). |
| **`agy_get_diff`** | `workspace_path` | `str` | **Yes** | — | Target workspace repository directory to inspect for git and file modifications. |
| **`agy_run_tests`** | `workspace_path` | `str` | **Yes** | — | Target workspace directory where test suites will be discovered and executed. |
| | `test_command` | `str` | No | `""` | Optional explicit test command (e.g. `pytest tests/ -v`, `cargo test`). If empty, auto-detects from 13 supported tiers. |
| | `timeout_seconds` | `int` | No | `300` | Maximum test execution duration in seconds (1 to 1800). |

---

### Detailed Return Schemas

#### 1. `TaskExecutionResult` (from `agy_execute_task`)
```json
{
  "status": "success",
  "conversation_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "response": "Implemented JWT authentication module in src/auth.py and created unit tests in tests/test_auth.py.",
  "modified_files": [
    "src/auth.py",
    "tests/test_auth.py"
  ],
  "diff_summary": "2 file(s) modified (+54, -2)",
  "duration_seconds": 14.82,
  "tokens_used": {
    "input_tokens": 2100,
    "output_tokens": 920,
    "thinking_tokens": 450,
    "cache_read_tokens": 0,
    "total_tokens": 3470
  },
  "backend_used": "sdk",
  "error_details": null,
  "reasoning": {
    "rigor": "standard",
    "toolkit_active": true,
    "toolkit_source": "/home/you/src/ai-deep-reasoning-toolkit",
    "toolkit_revision": "a7236c1",
    "installed": ["GEMINI.md", ".agents/skills/deep-verify"],
    "gate_line": "Simplicity gate: cut _fmt_row, _DEFAULTS; kept parse (LOAD-BEARING: contract rule 3).",
    "deep_verify_declined": false,
    "notes": ""
  }
}
```

`reasoning` is `null` unless the deep reasoning toolkit is configured — see [§6](#-6-deep-reasoning-toolkit-integration).

#### 2. `ChatResult` (from `agy_chat`)
```json
{
  "status": "success",
  "conversation_id": "4a1c5b8e-7e9a-4123-b123-abcdef012345",
  "response": "Based on the codebase analysis, `src/services/billing.py` depends directly on `src/db/raw_queries.py`...",
  "duration_seconds": 3.45,
  "tokens_used": {
    "input_tokens": 1200,
    "output_tokens": 480,
    "total_tokens": 1680
  },
  "backend_used": "cli",
  "error_details": null
}
```

#### 3. `DiffResult` (from `agy_get_diff`)
```json
{
  "status": "success",
  "has_changes": true,
  "unified_diff": "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -10,6 +10,18 @@\n+class JWTManager:\n+    def generate_token(self, user_id: str) -> str:\n+        ...",
  "changed_files": [
    {
      "path": "src/auth.py",
      "status": "M",
      "insertions": 48,
      "deletions": 2
    }
  ],
  "untracked_files": [
    "tests/test_auth.py"
  ],
  "summary": "1 modified, 1 untracked file(s)",
  "error_details": null
}
```

#### 4. `TestRunResult` (from `agy_run_tests`)
```json
{
  "status": "passed",
  "exit_code": 0,
  "framework": "pytest",
  "test_command_executed": "pytest tests/ -v",
  "output": "============================= test session starts =============================\ntests/test_auth.py::test_generate_token PASSED\ntests/test_auth.py::test_verify_token PASSED\n============================== 2 passed in 0.45s ==============================",
  "summary": {
    "total": 2,
    "passed": 2,
    "failed": 0,
    "skipped": 0,
    "errors": 0
  },
  "failures": [],
  "duration_seconds": 0.52,
  "error_details": null
}
```

---

## ⚙️ 5. Step-by-Step Client Setup Guides

### 1. Claude Desktop Setup

Claude Desktop interacts with MCP servers via local stdio processes.

1. Locate or create your Claude Desktop configuration file:
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json` (e.g. `C:\Users\<YourUser>\AppData\Roaming\Claude\claude_desktop_config.json`)
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Linux**: `~/.config/Claude/claude_desktop_config.json`
2. Add the `mcp-agy` configuration entry:

```json
{
  "mcpServers": {
    "mcp-agy": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\path\\to\\mcp_agy",
        "run",
        "mcp-agy"
      ],
      "env": {
        "MCP_AGY_BACKEND": "cli",
        "MCP_AGY_AUTO_FALLBACK": "0",
        "MCP_AGY_LOG_LEVEL": "INFO",
        "MCP_AGY_MODEL": "gemini-3.8-flash-high",
        "AGY_BIN_PATH": "C:\\Users\\<you>\\AppData\\Local\\agy\\bin\\agy.EXE"
      }
    }
  }
}
```
*(Replace `C:\\path\\to\\mcp_agy` with the absolute path to your `mcp_agy` repository).*

3. Fully restart Claude Desktop.
4. Click the 🔨 **Hammer icon** in the bottom right corner of Claude's prompt bar. Verify that all 8 tools (`agy_execute_task`, `agy_chat`, `agy_get_diff`, `agy_run_tests`, `agy_start_task`, `agy_job_status`, `agy_cancel_job`, `agy_list_jobs`) appear with green indicators.

---

### 2. Cursor Setup

Cursor supports project-level MCP server definitions in `.cursor/mcp.json`.

1. In your project's root directory, create `.cursor/mcp.json`.
2. Add the following JSON content:

```json
{
  "mcpServers": {
    "mcp-agy": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\path\\to\\mcp_agy",
        "run",
        "mcp-agy"
      ],
      "env": {
        "MCP_AGY_BACKEND": "cli",
        "MCP_AGY_AUTO_FALLBACK": "0",
        "MCP_AGY_LOG_LEVEL": "INFO",
        "MCP_AGY_MODEL": "gemini-3.8-flash-high",
        "AGY_BIN_PATH": "C:\\Users\\<you>\\AppData\\Local\\agy\\bin\\agy.EXE"
      }
    }
  }
}
```
*(Replace `C:\\path\\to\\mcp_agy` with the absolute path to your `mcp_agy` repository).*

3. Open Cursor Settings -> **Features** -> **MCP Servers**. Verify `mcp-agy` is listed and connected.
4. Use Cursor Composer or Chat to instruct AGY directly.

---

### 3. Cline Setup (VS Code Extension)

Cline allows automated autonomous agent workflows with configurable auto-approval.

1. In VS Code, open the Cline side panel.
2. Click the ⚙️ **Settings gear** -> **MCP Servers** tab -> **Configure MCP Servers**.
3. Edit `cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "mcp-agy": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\path\\to\\mcp_agy",
        "run",
        "mcp-agy"
      ],
      "env": {
        "MCP_AGY_BACKEND": "cli",
        "MCP_AGY_AUTO_FALLBACK": "0",
        "MCP_AGY_LOG_LEVEL": "INFO",
        "MCP_AGY_MODEL": "gemini-3.8-flash-high",
        "AGY_BIN_PATH": "C:\\Users\\<you>\\AppData\\Local\\agy\\bin\\agy.EXE"
      },
      "disabled": false,
      "autoApprove": [
        "agy_execute_task",
        "agy_chat",
        "agy_get_diff",
        "agy_run_tests"
      ]
    }
  }
}
```
*(Replace `C:\\path\\to\\mcp_agy` with the absolute path to your `mcp_agy` repository).*

---

### 4. Roo Code Setup (VS Code Extension)

Roo Code allows specialized custom modes (Architect, Code, Test) delegating to AGY.

1. Open Roo Code settings panel -> **MCP Servers**.
2. Click **Edit MCP Settings** and insert the server definition:

```json
{
  "mcpServers": {
    "mcp-agy": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\path\\to\\mcp_agy",
        "run",
        "mcp-agy"
      ],
      "env": {
        "MCP_AGY_BACKEND": "cli",
        "MCP_AGY_AUTO_FALLBACK": "0",
        "MCP_AGY_LOG_LEVEL": "INFO",
        "MCP_AGY_MODEL": "gemini-3.8-flash-high",
        "AGY_BIN_PATH": "C:\\Users\\<you>\\AppData\\Local\\agy\\bin\\agy.EXE"
      },
      "disabled": false,
      "autoApprove": [
        "agy_execute_task",
        "agy_chat",
        "agy_get_diff",
        "agy_run_tests"
      ]
    }
  }
}
```
*(Replace `C:\\path\\to\\mcp_agy` with the absolute path to your `mcp_agy` repository).*

---

### 5. Claude Code Setup

Claude Code reads `.mcp.json` from the project root (see `configs/claude_code_mcp.json`):

```json
{
  "mcpServers": {
    "mcp-agy": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\mcp_agy", "run", "mcp-agy"],
      "env": {
        "MCP_AGY_BACKEND": "cli",
        "MCP_AGY_AUTO_FALLBACK": "false",
        "MCP_AGY_LOG_LEVEL": "INFO",
        "MCP_AGY_MODEL": "gemini-3.8-flash-high"
      },
      "timeout": 1800000
    }
  }
}
```
*(Replace `C:\\path\\to\\mcp_agy` with the absolute path to your `mcp_agy` repository).*

`timeout` is not decoration — read the next section before your first real task.

---

### ⏱️ Raise the client's call timeout, or nothing here works

**Every tool in this server is a wrapper around a call that takes minutes.** A real
`agy_execute_task` on a real repository runs for 2–20 minutes; even an `agy_chat` review of a
handful of files takes several. Every MCP client caps how long it will wait for a single tool
call, and the defaults are all far below that. When the cap fires the client reports
`Error: Request timed out` and *drops the call* — the architect gets a bare failure, and the
work AGY did in those minutes is discarded.

**Claude Code's default cap is 60 seconds** — measured, not quoted: a client-side log of a live
`agy_chat` shows `still running (30s elapsed)`, `still running (60s elapsed)`, then
`Error: Request timed out`. Two ways to raise it, either is enough:

| Where | Setting | Notes |
|---|---|---|
| `.mcp.json`, per server | `"timeout": 1800000` | Milliseconds. Overrides the env var for this server only. Values under `1000` are ignored. |
| `settings.json`, `env` block | `"MCP_TOOL_TIMEOUT": "1800000"` | Milliseconds. Applies to every MCP server the client launches. |

Both are read when the client starts, so **restart the client** after changing either.

> **Progress notifications will not save you.** Claude Code documents `timeout` as a *hard
> wall-clock limit per call*, explicitly stating that progress notifications do not extend it.
> A keepalive heartbeat from this server would be wasted effort against that client — raising
> the cap is the only fix.

Other clients cap calls too — Claude Desktop, Cursor, Cline and Roo Code each expose their own
timeout setting. If a long task fails while the server log shows AGY still working, that cap is
the first thing to check.

---

## 🧠 6. Deep Reasoning Toolkit Integration

`mcp-agy` can deliver the [**AI Deep Reasoning Toolkit**](https://github.com/DuongNAD/ai-deep-reasoning-toolkit)
into every workspace it hands to AGY. The toolkit is a rule file (`GEMINI.md`) plus an
on-demand skill (`deep-verify`) that constrain how Gemini/Antigravity writes code.

The integration is **off unless you switch it on**, and it changes nothing about the architect.

### Why this belongs in the MCP server

Antigravity reads `GEMINI.md` and `.agents/skills/` from the directory it is working in, and
this server already spawns `agy` with the workspace as its cwd. Claude Code, Cursor and Copilot
read different filenames, so installing the toolkit changes **the worker's** behaviour and
leaves **the architect's** untouched:

```
Claude Code (architect)  ──▶  mcp-agy  ──▶  agy --print  (cwd = workspace)
   does not read GEMINI.md                    reads GEMINI.md + .agents/skills/
```

Doing it here rather than by hand buys one thing that matters. The toolkit's own benchmark
measured `deep-verify` **self-activating in 0 of 10 runs** on a task built to exactly the shape
the skill describes — and **5 of 5** once the prompt named the trade-offs out loud. Discovery is
what fails, not the skill. The architect is the only party that knows whether a task has two
genuinely different designs, so `rigor="deep"` is how it says so.

### Setup

```bash
git clone https://github.com/DuongNAD/ai-deep-reasoning-toolkit.git ~/src/ai-deep-reasoning-toolkit
```

Point the server at the checkout in your client config's `env` block:

```json
{
  "mcpServers": {
    "mcp-agy": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-agy", "run", "mcp-agy"],
      "env": {
        "MCP_AGY_BACKEND": "cli",
        "MCP_AGY_AUTO_FALLBACK": "false",
        "MCP_AGY_TOOLKIT_PATH": "/home/you/src/ai-deep-reasoning-toolkit"
      },
      "timeout": 1800000
    }
  }
}
```

A path that does not contain `GEMINI.md` logs a warning and disables the integration, rather
than looking like a working install.

### The `rigor` parameter

Accepted by `agy_execute_task` and `agy_start_task`.

| `rigor` | What happens | When to use it |
|---|---|---|
| `"standard"` *(default)* | Installs the toolkit in the workspace. AGY loads the always-on rules; your prompt is passed through untouched. | Everything. This is the setting the measured gains come from. |
| `"deep"` | Also prepends a preamble naming the `deep-verify` skill and requiring its Comparative Matrix. | Only when the task admits **two or more genuinely different designs** and choosing wrong is expensive to reverse. |
| `"off"` | Nothing is written, nothing is added. | A workspace you want left exactly as it is. |

**`deep` is not a free upgrade, and this is measured.** On a fully specified contract the
toolkit's benchmark recorded an identical judge score, **37% more code**, and all five subagent
tournaments electing the same architecture. Reach for it when you want the alternatives
*enumerated*, not when you want the answer to be more correct.

`deep` also permits the skill to decline. The skill states that a matrix of one real design
against two strawmen "launders a foregone conclusion as deliberation", so the preamble tells it
to decline in one line rather than manufacture alternatives — and that declination comes back
as `deep_verify_declined: true`.

Asking for `deep` without a configured toolkit does **not** silently downgrade: the result
carries `toolkit_active: false` and a note saying why, because a plain answer read as the output
of a verification pipeline is the one failure mode worth being loud about.

### What lands in the workspace

Exactly two paths, and neither is ever overwritten:

```
<workspace>/GEMINI.md
<workspace>/.agents/skills/deep-verify/
```

An existing `GEMINI.md` is left alone and reported in `notes` — it may be your own rules for
that repo, and there is no meaningful way to merge two rule files.

This happens in `mode="plan"` too. That mode promises AGY will not touch your code, and it
still does not — but the two toolkit files are written before the investigation starts, because
that is what makes the rules available to it. `installed` on the result says so every time.

`AGENTS.md` is **never** written. Antigravity reads it, but so does Claude Code, so writing it
would destroy the worker/architect isolation the whole design rests on.

**Your repository stays clean.** Both paths are registered in `.git/info/exclude`, which is
local to the clone and never committed. Because `agy_get_diff` and `modified_files` both read
`git status`, the toolkit disappears from them for free — no skip-list to keep in sync, and
nothing showing up in your own `git status` either:

```
$ git status --porcelain
M src/calc/stats.py          # your change

$ agy_get_diff(workspace)
"1 file(s) changed: 1 modified (+8, -1)"  ->  ["src/calc/stats.py"]
```

### What comes back

`TaskExecutionResult.reasoning` records the conditions the run was carried out under. Two runs
of one task under two rigor settings or two toolkit revisions are two different experiments; a
result that cannot name its arm cannot be compared against another.

```json
"reasoning": {
  "rigor": "standard",
  "toolkit_active": true,
  "toolkit_source": "/home/you/src/ai-deep-reasoning-toolkit",
  "toolkit_revision": "a7236c1",
  "installed": ["GEMINI.md", ".agents/skills/deep-verify"],
  "gate_line": "Simplicity gate: cut _fmt_row, _DEFAULTS; kept parse (LOAD-BEARING: contract rule 3).",
  "deep_verify_declined": false,
  "notes": ""
}
```

`gate_line` is the load-bearing one. `GEMINI.md` §4.4 mandates that line on every response that
ships code, and states that a missing gate line means the gate did not run. Lifting it out of
the prose gives the architect a machine-checkable signal that the protocol actually executed —
the same evidence the toolkit's benchmark uses to conclude its rule file was loaded at all.

Only the two formats the toolkit genuinely mandates are parsed. The Stage 0 tier declaration is
required to be one line but its wording is left open, and skill *activation* has no declared
marker, so neither is guessed at: a field that is confidently wrong some of the time is worse
than no field.

### What the toolkit does and does not buy

From the toolkit's own benchmark — one model, two tasks, ~48 runs, graded by a suite the agent
never sees:

| | baseline | with toolkit | |
|---|---|---|---|
| `safe-path` judge score | 417/420 | 417/420 | unchanged |
| `safe-path` AST statements | 52.3 | **33.2** | −36% |
| `event-bus` judge score | 400/400 | 320/320 | unchanged (both perfect) |
| `event-bus` AST statements | 55.5 | **49.1** | −12% |

Correctness never improved. The toolkit does not make the model *think of* a better solution;
it stops the model *shipping* things nobody asked for. Install it if you are tired of Gemini
inventing helpers, metrics and config options on its own. Do not install it expecting it to
catch bugs.

Those numbers come from one model on two single-file Python tasks with fully specified
contracts. Ambiguous requirements, multi-file refactors, and legacy codebases are untested —
and that is exactly the territory the rest of `GEMINI.md` aims at.

### Verified end to end

Against a real `agy` run on a real repository, with `MCP_AGY_AUTO_FALLBACK=false` so a
simulated answer could not pass: 14/14 checks — both artefacts installed, `AGENTS.md` absent,
`git status` free of them, `agy_get_diff` and `modified_files` reporting only the real change,
provenance stamped with the checkout's actual revision, and `gate_line` carrying a
`Simplicity gate:` line that Gemini emitted because it had read the rules file.

---

## 🎯 7. High-Yield Prompt Templates for Architect Agents

These templates are battle-tested prompts designed for LLMs acting in the **Architect Role** to orchestrate Google Antigravity:

### Template 0: The delegation loop (use this for anything that takes minutes)

The shape every other template should be run in. The architect never blocks on the worker, and
never takes the worker's word for what it did.

```markdown
1. DELEGATE — hand over one self-contained unit of work:
   agy_start_task(workspace_path="E:/Project/Thing",
                  prompt="<one job, exact files, explicit acceptance criteria>")
   -> {"job_id": "…", "status": "running"}          # returns in milliseconds

2. KEEP WORKING — read the code you are about to review, plan the next unit,
   or start a second job in a *different* workspace. Do not sit on the job id.

3. COLLECT — agy_job_status(job_id, wait_seconds=30)
   'running'   -> ask again later, the run is unharmed
   'completed' -> read result.status, result.response, result.modified_files

4. VERIFY — never accept the worker's own report as evidence:
   agy_get_diff(workspace_path=…)     # what actually changed on disk
   agy_run_tests(workspace_path=…)    # whether it still works

5. DECIDE — accept, or send back one corrective job naming exactly what was wrong.
   agy_cancel_job(job_id) if a run is heading the wrong way; cancelling is not a
   rollback, so follow it with agy_get_diff to see what already landed.
```

**Writing the prompt is the architect's real work.** A worker prompt earns its keep when it
carries: the exact files to touch and the ones not to, the defect stated as *what happens*
rather than *what to change*, an acceptance criterion the worker can check itself, and the
project's own constraints (test command, style gate, forbidden dependencies). Vague delegation
is what produces a confident report and an unusable diff.

### Template 1: Greenfield Feature Implementation & Scaffolding
```markdown
You are the High-Level Software Architect. I need you to implement a new feature in the workspace using the `agy_execute_task` tool.

**Workspace**: `/path/to/project`
**Task**: Implement a JWT-based authentication module with Redis token blacklisting.

**Prompt for `agy_execute_task`**:
"""
Implement a production-ready JWT authentication module in `src/auth/jwt.py` and `src/auth/redis_store.py`.

Requirements:
1. Create `JWTManager` class supporting token generation (`access_token` 15m expiry, `refresh_token` 7d expiry) using `PyJWT`.
2. Implement token revocation via Redis key-value expiration store with prefix `jwt_revoked:`.
3. Provide unit test suite in `tests/test_jwt_auth.py` covering token creation, validation, expiration, and revocation.
4. Run `pytest tests/test_jwt_auth.py` to ensure all tests pass with 100% assertions.

Constraints:
- Do not introduce breaking changes to existing `src/config.py`.
- Type annotations must be strict and pass `mypy`.
- Use async Redis client (`redis.asyncio`).
"""

After execution:
1. Call `agy_get_diff(workspace_path="/path/to/project")` to inspect the code changes.
2. Call `agy_run_tests(workspace_path="/path/to/project")` to confirm test suite integrity.
```

---

### Template 2: Bug Investigation, Fix & Git Diff Review Loop
```markdown
You are the AI Architect. A critical bug has been reported in our data processing pipeline.

**Workspace**: `/path/to/project`
**Bug Report**: `ZeroDivisionError` occurring in `src/analytics/metrics.py:84` when computing average latency over empty sample batches.

**Workflow**:
1. Invoke `agy_execute_task` with:
"""
Investigate and fix the ZeroDivisionError in `src/analytics/metrics.py`.
1. Check line 84 where batch latency division occurs. Guard against `len(samples) == 0` by returning 0.0 or `None` as appropriate.
2. Locate existing tests in `tests/test_metrics.py` and add regression test cases for empty batches, single-item batches, and large batches.
3. Verify the fix by running pytest.
"""
2. Inspect the exact diff using `agy_get_diff(workspace_path="/path/to/project")`.
3. Verify that only the intended lines and tests were modified.
```

---

### Template 3: Safe Code Refactoring with Regression Testing
```markdown
You are the AI Architect managing a legacy code refactoring.

**Workspace**: `/path/to/project`
**Goal**: Refactor synchronous database operations in `src/db/repository.py` to modern SQLAlchemy 2.0 AsyncSession.

**Workflow**:
1. Run baseline tests first: `agy_run_tests(workspace_path="/path/to/project")` to verify existing tests pass.
2. Invoke `agy_execute_task` with:
"""
Refactor `src/db/repository.py` from sync SQLAlchemy 1.4 syntax to SQLAlchemy 2.0 async syntax (`AsyncSession`, `select()`, `await session.execute()`).
- Update `tests/test_repository.py` with `pytest-asyncio` fixtures.
- Preserve all public method signatures and return models.
- Ensure all existing tests continue to pass.
"""
3. Run `agy_run_tests(workspace_path="/path/to/project")` to verify 0 regressions.
4. Call `agy_get_diff` to review the refactor diff.
```

---

### Template 4: Test-Driven Development (TDD) Workflow
```markdown
You are the AI Architect executing a Test-Driven Development workflow.

**Workspace**: `/path/to/project`
**Feature**: Implement a Rate Limiter using Leaky Bucket algorithm.

**Step 1: Write Failing Tests (Red Phase)**
Call `agy_execute_task`:
"""
Create `tests/test_rate_limiter.py` with comprehensive unit tests for a `LeakyBucketLimiter(capacity=10, leak_rate=2.0)` class.
Include tests for:
- Allowing burst requests up to capacity.
- Rejecting requests when capacity is exceeded.
- Leaking tokens over time.
- Thread-safety under concurrent access.
Do NOT create the implementation yet. Run pytest and confirm tests fail as expected.
"""

**Step 2: Implement Code (Green Phase)**
Call `agy_execute_task`:
"""
Implement `src/rate_limiter.py` containing `LeakyBucketLimiter` to satisfy all test cases in `tests/test_rate_limiter.py`.
Run `pytest tests/test_rate_limiter.py` until all tests pass.
"""

**Step 3: Verification & Review**
Call `agy_run_tests` and `agy_get_diff` to finalize.
```

---

### Template 5: Analytical / Architectural Consultation (Read-Only)
```markdown
You are the AI Architect consulting Google Antigravity on repository structure and design.

**Workspace**: `/path/to/project`
**Question**: "Analyze the project's dependency graph in `src/` and evaluate whether our microservices module boundaries violate domain-driven design principles."

**Action**:
Call `agy_chat(prompt="Analyze src/ dependencies and evaluate DDD module boundaries.", workspace_path="/path/to/project")`.
Review AGY's reasoning, architectural trade-offs, and refactoring plan before commissioning any disk modifications.
```

---

## 🔒 8. Security, Workspace Isolation & Concurrency Locking

When external AI agents manipulate files and run shell commands, filesystem safety is paramount. `mcp-agy` enforces multi-layer defenses:

```
                               Incoming Request (workspace_path)
                                              │
                                              ▼
                             +─────────────────────────────────+
                             │   Path Canonicalization         │
                             │   - Resolves symlinks/junctions │
                             │   - Strips null bytes (\x00)    │
                             │   - Expands user tilde (~)      │
                             +────────────────┬────────────────+
                                              │
                                              ▼
                             +─────────────────────────────────+
                             │   Filesystem Root Protection    │
                             │   - Blocks C:\, D:\, / roots    │
                             │   - Blocks raw user home root   │
                             +────────────────┬────────────────+
                                              │
                                              ▼
                             +─────────────────────────────────+
                             │   System-Critical Directory Blk │
                             │   - Blocks C:\Windows, /etc,    │
                             │     /sys, /bin, /usr, /var      │
                             +────────────────┬────────────────+
                                              │
                                              ▼
                             +─────────────────────────────────+
                             │   Allowed-Roots Whitelist Check │
                             │   - Optional boundary fence     │
                             +────────────────┬────────────────+
                                              │
                                              ▼
                             +─────────────────────────────────+
                             │   Async Mutex Concurrency Lock  │
                             │   - Serializes ops per workspace│
                             │   - Prevents Git index collisions│
                             +────────────────┬────────────────+
                                              │
                                              ▼
                                 Safe Execution Sandbox
```

1. **Path Canonicalization (`canonicalize_workspace_path`)**: Resolves all relative `.` and `..` segments, follows symlinks/junctions, and blocks null-byte string injection attacks.
2. **System Root & OS Protection (`is_system_critical_path`)**: Explicitly blocks requests targeting drive roots (`C:\`, `/`), operating system directories (`C:\Windows`, `/etc`, `/sys`, `/usr`, `/bin`), and raw user profile roots (`C:\Users\Admin`, `/home/user`). Project subdirectories within user homes (e.g. `C:\Users\Admin\Projects\repo`) are fully permitted.
3. **Boundary Validation (`validate_workspace_path`)**: Enforces optional `allowed_roots` constraints to sandbox agent operations within approved directories.
4. **Asynchronous Concurrency Locking (`WorkspaceLockManager`)**: Prevents race conditions and `.git/index.lock` contention by ensuring only one mutating operation executes per workspace directory at any given moment.

---

## 🔧 9. Environment Variables & Backend Configuration

MCP clients launch this server from a JSON config whose only tunable is the `env` block —
they cannot append CLI flags. Every variable below is therefore honored from the environment.
Where an equivalent CLI flag exists, the **explicit flag wins**; the environment is the fallback.

| Environment Variable | Allowed Values | Default | Description |
|---|---|---|---|
| `MCP_AGY_BACKEND` | `auto`, `sdk`, `cli`, `mock` | `auto` | Forces specific AGY execution backend or enables 3-tier probing hierarchy. |
| `MCP_AGY_AUTO_FALLBACK` | `1`, `true`, `yes`, `on` / `0`, `false`, `no`, `off` | `false` | Falls back to the simulated mock engine when a real backend is unavailable or errors. **Off by default on purpose** — with it on, a failed run returns a fabricated `status="success"`. |
| `AGY_BIN_PATH` | File path string | `None` (searches PATH) | Absolute path to the `agy` binary. Checked before `MCP_AGY_CLI_PATH` and before the PATH search — set this when the client launches the server without a usable `PATH`. |
| `MCP_AGY_CLI_PATH` | File path string | `None` (searches PATH) | Alternate spelling of `AGY_BIN_PATH`, checked second. |
| `MCP_AGY_MODEL` | Model ID string | `None` (agy CLI default) | Default model for both CLI and SDK backends, e.g. `gemini-3.8-flash-high`. Run `agy models` for valid ids. A per-call `model` argument overrides it. |
| `MCP_AGY_DEFAULT_MODEL` | Model ID string | `None` | Accepted alias for `MCP_AGY_MODEL`, which takes precedence when both are set. |
| `MCP_AGY_EFFORT` | `low`, `medium`, `high` | `None` (agy CLI default) | Default reasoning effort. A per-call `effort` argument overrides it. |
| `MCP_AGY_MAX_CONCURRENCY` | Positive integer | `0` (no limit) | Most `agy` processes allowed to run at once; extra background jobs wait inside the server and `timeout_seconds` counts only the run, not the wait. Each `agy` run loads every MCP server in your Antigravity config - measured ~730 MB and ~18 child processes per run on one machine (20 at once: ~14.6 GB; capped at 5: ~2.3 GB). Blank, non-numeric and non-positive values mean no limit. |
| `MCP_AGY_KILL_CHILDREN_ON_EXIT` | `1`, `true` / `0`, `false`, `no`, `off` | on (Windows only) | Puts the server in a job object so the OS kills every `agy` run it started if the server dies, however it dies. Without it, a server killed by its client left the whole `agy` trees running: measured 43 surviving processes (3 `agy.exe` plus the MCP servers each loads) after a hard kill with 3 jobs in flight, 0 with this on. |
| `MCP_AGY_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` | Logging verbosity directed strictly to `sys.stderr`. Case-insensitive; an unrecognized value logs a warning and falls back to `INFO` rather than refusing to boot. |
| `MCP_AGY_DEBUG` | `1`, `true`, `yes`, `on` / `0`, `false`, `no`, `off` | `false` | Enables debug mode and raises logging to `DEBUG`. An explicit `MCP_AGY_LOG_LEVEL` still wins over the level this implies. |
| `MCP_AGY_TOOLKIT_PATH` | Directory path string | `None` (integration off) | Checkout of the [AI Deep Reasoning Toolkit](https://github.com/DuongNAD/ai-deep-reasoning-toolkit). When set, `rigor` on `agy_execute_task` / `agy_start_task` becomes live and results carry a `reasoning` profile. A path without a `GEMINI.md` in it logs a warning and stays off. See [§6](#-6-deep-reasoning-toolkit-integration). |
| `MCP_AGY_JOB_MARKER_DIR` | Directory path string | System temp dir | Where background-job `.done` markers and persisted results are written. Redirect it to keep one machine's job records isolated (the test suite does). |

---


### Running many tasks at once

- **One workspace per write task.** Edits to one workspace run one at a time; `mode='plan'` jobs may share one. Give each write task its own `git worktree`.
- **Cap the processes.** Set `MCP_AGY_MAX_CONCURRENCY` (16 is a sensible start). Queued jobs show as `running` in `agy_list_jobs`.
- **Keep replies short.** Every `agy_job_status` result lands in the architect's context. End each prompt with `REPLY: at most 5 lines - what changed, test result, blockers`, and verify with `agy_get_diff` / `agy_run_tests` rather than the reply.
- **`/teamwork-preview` as the prompt.** A prompt that starts with AGY's `/teamwork-preview` command makes one `agy` process run a team of subagents. Measured once on four small independent utilities: 1 process / ~1.1 GB / 313 s, against 4 plain jobs at 4 processes / ~3.2 GB / 190 s. Prefer it for one large project; prefer plain jobs when latency matters or the tasks are unrelated.
- **Slim the worker.** Disable the Antigravity MCP servers a worker does not need (`agy mcp disable <name>`) - above all `mcp-agy` itself, so a worker cannot start workers of its own. This edits your global Antigravity config.

## 🧪 10. Development, Testing & Verification

### Running the Full Test Suite:

```bash
# Run all unit, integration, and protocol tests
pytest -v

# Run with coverage report
pytest --cov=src/mcp_agy -v

# Run specific test modules
pytest tests/test_workspace_isolation.py -v
pytest tests/test_cli_packaging.py -v
pytest tests/test_server_protocol.py -v
pytest tests/test_diff_engine.py -v
pytest tests/test_test_runner.py -v
```

### Verifying Stdio Stream Purity:

FastMCP stdio communication requires zero stdout corruption. All logs, diagnostics, and banners must route exclusively to `stderr`.

```bash
# Run CLI with debug logging and redirect stderr to a file
# Standard output must be empty until a JSON-RPC request is sent
python -m mcp_agy --debug 2> stderr.log
```

---

## ❓ 11. Troubleshooting & FAQ

- **Q: Claude Desktop reports "Could not connect to MCP server"?**
  - *A*: Ensure `uv` is installed and reachable in your system `PATH`. Check `claude_desktop_config.json` to ensure JSON syntax is valid and all file paths use escaped backslashes `\\` on Windows.
- **Q: What happens if Google Antigravity CLI is not installed?**
  - *A*: `mcp-agy` automatically falls back to `MockAGYBackend`, allowing complete local testing, offline development, and CI verification without requiring live credentials.
- **Q: How does `agy_run_tests` discover virtual environments?**
  - *A*: `MultiEcosystemTestRunner` automatically checks for local `.venv`, `venv`, or `env` directories and injects their executable directory into `PATH` during test execution.
- **Q: Can multiple MCP clients use `mcp-agy` simultaneously?**
  - *A*: Yes. FastMCP supports concurrent sessions, and `WorkspaceLockManager` serializes operations targeting the same workspace repository to prevent race conditions.
- **Q: A call fails with `Error: Request timed out` after about a minute, every time.**
  - *A*: That is the client giving up, not AGY. See *"Raise the client's call timeout"* at the end of §5. AGY keeps running for a moment after the client drops the call, then the server kills the process tree — no orphans, but the work is lost.
- **Q: `error_details` says `invalid model selection ... conflicts with --effort=low`.**
  - *A*: Model ids ending in `-low` / `-high` already carry a reasoning effort, so passing `effort` as well is a contradiction the CLI refuses before it starts. Either drop the `effort` argument or pick a model id without the suffix. Run `agy models` for the valid ids.
- **Q: A call returns `status: "error"` and `AGY returned an empty response`.**
  - *A*: AGY started, decided it needed a tool, and could not use it — most often a permission it cannot prompt for in headless mode. The rest of `error_details` carries the tool that was refused and AGY's own stderr. Check that the config passes `auto_approve` (the default) so the CLI runs with `--dangerously-skip-permissions`.

---

## 📜 12. License & Contributing

- Distributed under the **MIT License**. See `LICENSE` for details.
- Contributions, bug reports, and feature requests are welcome via GitHub Pull Requests and Issues.
