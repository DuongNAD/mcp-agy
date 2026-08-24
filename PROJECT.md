# Project: Python FastMCP Server for Google Antigravity (AGY)

## Architecture
The `mcp_agy` package exposes Google Antigravity (AGY) as an autonomous coding worker for external AI architect agents (such as Claude Desktop, Cursor, Cline, Roo Code). Claude/Architect acts as the high-level commander while AGY executes coding tasks, file modifications, diff inspections, and testing.

```
+-------------------------------------------------------------------------+
|                  External AI Architect Agent                           |
|       (Claude Desktop, Cursor, Cline, Roo Code, Custom MCP Client)      |
+------------------------------------+------------------------------------+
                                     |
                                     | JSON-RPC 2.0 over Stdio (Clean)
                                     v
+------------------------------------+------------------------------------+
|                         mcp_agy (FastMCP)                              |
|                                                                         |
|  +-------------------+  +---------------+  +------------+  +---------+  |
|  | agy_execute_task  |  |   agy_chat    |  |agy_get_diff|  |agy_run_ |  |
|  |                   |  |               |  |            |  |  tests  |  |
|  +---------+---------+  +-------+-------+  +-----+------+  +----+----+  |
|            |                    |                |              |       |
|            v                    v                |              |       |
|  +---------+--------------------+-------+        |              |       |
|  |           Backend Manager            |        |              |       |
|  |  - Abstract AGYBackend               |        |              |       |
|  |  - SubprocessCLIBackend (agy.EXE)    |        |              |       |
|  |  - PythonSDKBackend (google-agy)     |        |              |       |
|  |  - MockAGYBackend (Offline/CI)       |        |              |       |
|  |  - Fallback Hierarchy & Telemetry    |        |              |       |
|  +------------------+-------------------+        |              |       |
|                     |                            |              |       |
|                     |                            v              v       |
|                     |                   +--------+---+  +-------+----+  |
|                     |                   | Diff Engine|  |Test Runner |  |
|                     |                   | - Git diff |  | - 13 Types |  |
|                     |                   | - Untracked|  | - Pytest/  |  |
|                     |                   | - Stats    |  |   npm/cargo|  |
|                     |                   +--------+---+  +-------+----+  |
|                     |                            |              |       |
+---------------------|----------------------------|--------------|-------+
                      |                            |              |
                      v                            v              v
+-------------------------------------------------------------------------+
|                         Target Workspace                                |
|  - File creation, edits, refactoring, terminal commands, test execution |
+-------------------------------------------------------------------------+
```

---

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | FastMCP Stdio Server | FastMCP instance communicating cleanly over stdio transport | M1 | survey |
| 2 | Tool: `agy_execute_task` | Autonomous coding worker execution with permissions auto-approval | M1 | survey |
| 3 | Tool: `agy_chat` | Read-only analytical / architectural consultation | M1 | survey |
| 4 | Tool: `agy_get_diff` | Repository diff inspection tool interface | M1 | survey |
| 5 | Tool: `agy_run_tests` | Workspace test execution tool interface | M1 | survey |
| 6 | Pydantic v2 Models | Type-safe request/response and error schema models | M1 | survey |
| 7 | Stderr-Only Logging | Directing all server logs strictly to sys.stderr to protect stdio JSON-RPC | M1 | survey |
| 8 | Abstract AGYBackend | Extensible backend contract for execution engines | M2 | survey |
| 9 | SubprocessCLIBackend | Native `agy.EXE` subprocess execution via `--output-format stream-json` | M2 | survey |
| 10 | NDJSON Telemetry Parser | Real-time tool call extraction, thought streaming, and token usage metrics | M2 | survey |
| 11 | PythonSDKBackend | `google.antigravity` SDK integration adapter with `CapabilitiesConfig` | M2 | survey |
| 12 | 3-Tier Fallback Hierarchy | Dynamic fallback: Python SDK -> CLI Subprocess -> Robust Simulation Mock | M2 | survey |
| 13 | Process Tree & Timeout Engine | Safe process tree termination (`psutil` / `taskkill`) and timeout enforcement | M2 | survey |
| 14 | Git Unified Diff Engine | Full diff combining tracked modifications, staged files, and untracked additions | M3 | survey |
| 15 | Multi-Ecosystem Test Runner | 13-tier test framework auto-detection (pytest, unittest, npm, cargo, go, etc.) | M3 | survey |
| 16 | Test Diagnostics Parser | Regex parsing of test outcomes, durations, and failure traces | M3 | survey |
| 17 | Workspace Isolation & Locking | Path canonicalization, boundary validation, and async workspace locking | M4 | survey |
| 18 | Client Configuration Templates | Ready-to-use JSON configs for Claude Desktop, Cursor, Cline, Roo Code | M4 | survey |
| 19 | Packaging & CLI Entrypoint | `pyproject.toml` (hatchling build), `mcp-agy` script, `python -m mcp_agy` | M4 | survey |
| 20 | Professional Documentation | Comprehensive `README.md`, architecture, client setup guides, prompt templates | M4 | user requirement |
| 21 | Automated E2E Test Suite | Requirement-driven opaque-box test suite (Tiers 1-4) | E2E / M5 | survey |
| 22 | Adversarial Hardening & Audit | White-box adversarial testing (Tier 5) and forensic integrity verification | M5 | survey |
| 23 | Deep Reasoning Toolkit Delivery | Provisioning `GEMINI.md` + `deep-verify` into the workspace, hidden from git via `.git/info/exclude` | M6 | user requirement |
| 24 | Tool: `rigor` parameter | `standard`/`deep`/`off` on task tools; `deep` names the skill the benchmark showed is never self-discovered | M6 | user requirement |
| 25 | Reasoning Provenance & Gate Report | `ReasoningProfile` on results: rigor, toolkit revision, and the mandated `Simplicity gate:` line lifted from the response | M6 | user requirement |

---

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| E2E | E2E Testing Track | Requirement-driven test harness, runner, and test cases (Tiers 1-4) | none | DONE |
| 1 | Core MCP Protocol & Tools | FastMCP server, 4 core tool registrations, Pydantic schemas, stderr logging | none | DONE |
| 2 | AGY Execution Backend | Abstract backend, CLI subprocess NDJSON stream parser, SDK adapter, fallback & timeout | M1 contracts | DONE |
| 3 | Diff Inspection & Test Runner | Git unified diff engine (tracked + untracked) and multi-ecosystem test runner | M1 contracts | DONE |
| 4 | Workspace Safety, Packaging & Docs | Workspace isolation, client configs, `pyproject.toml`, and comprehensive documentation | M1, M2, M3 | DONE |
| 5 | E2E Integration & Final Audit | 100% E2E test pass (Tiers 1-4), Tier 5 adversarial hardening, and forensic audit | E2E, M1-M4 | DONE |
| 6 | Deep Reasoning Toolkit Integration | Optional delivery of https://github.com/DuongNAD/ai-deep-reasoning-toolkit into the worker's workspace, with provenance and gate reporting on results | M1, M2 | DONE |

---

## Interface Contracts

### 1. `mcp_agy.core.models`
```python
from pydantic import BaseModel, Field
from typing import Literal

class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0

class TaskExecutionResult(BaseModel):
    status: Literal["success", "error", "timeout"]
    conversation_id: str = ""
    response: str = ""
    modified_files: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    duration_seconds: float = 0.0
    tokens_used: TokenUsage = Field(default_factory=TokenUsage)
    backend_used: str = "cli"
    error_details: str | None = None
    reasoning: ReasoningProfile | None = None

class ReasoningProfile(BaseModel):
    rigor: Literal["off", "standard", "deep"] = "off"
    toolkit_active: bool = False
    toolkit_source: str = ""
    toolkit_revision: str = ""
    installed: list[str] = Field(default_factory=list)
    gate_line: str = ""
    deep_verify_declined: bool = False
    notes: str = ""

class ChatResult(BaseModel):
    status: Literal["success", "error", "timeout"]
    conversation_id: str = ""
    response: str = ""
    duration_seconds: float = 0.0
    tokens_used: TokenUsage = Field(default_factory=TokenUsage)
    backend_used: str = "cli"
    error_details: str | None = None

class FileDiffStat(BaseModel):
    path: str
    status: str  # "M", "A", "D", "R", "??"
    insertions: int = 0
    deletions: int = 0

class DiffResult(BaseModel):
    status: Literal["success", "error", "not_a_git_repo"]
    has_changes: bool = False
    unified_diff: str = ""
    changed_files: list[FileDiffStat] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)
    summary: str = ""
    error_details: str | None = None

class TestFailure(BaseModel):
    test_id: str
    message: str
    location: str | None = None
    traceback: str | None = None

class TestSummary(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0

class TestRunResult(BaseModel):
    status: Literal["passed", "failed", "error", "timeout", "no_framework_detected"]
    exit_code: int = 0
    framework: str = "custom"
    test_command_executed: str = ""
    output: str = ""
    summary: TestSummary = Field(default_factory=TestSummary)
    failures: list[TestFailure] = Field(default_factory=list)
    duration_seconds: float = 0.0
    error_details: str | None = None
```

### 2. `mcp_agy.core.backend`
```python
from abc import ABC, abstractmethod

class AGYBackend(ABC):
    @abstractmethod
    async def execute_task(
        self,
        workspace_path: str,
        prompt: str,
        auto_approve: bool = True,
        mode: str = "accept-edits",
        timeout_seconds: int = 600,
    ) -> TaskExecutionResult: ...

    @abstractmethod
    async def chat(
        self,
        prompt: str,
        workspace_path: str = "",
        conversation_id: str = "",
        timeout_seconds: int = 300,
    ) -> ChatResult: ...
```

---

## Code Layout
```text
mcp_agy/
├── pyproject.toml
├── requirements.txt
├── README.md
├── LICENSE
├── configs/
│   ├── claude_desktop_config.json
│   ├── cursor_mcp.json
│   ├── cline_mcp_settings.json
│   └── roo_code_mcp_settings.json
├── src/
│   └── mcp_agy/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── server.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── backend.py
│       │   ├── backend_manager.py
│       │   ├── cli_backend.py
│       │   ├── sdk_backend.py
│       │   ├── mock_backend.py
│       │   ├── diff_engine.py
│       │   └── test_runner.py
│       └── utils/
│           ├── __init__.py
│           ├── process.py
│           ├── workspace.py
│           └── logger.py
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── empirical_harness.py
    ├── test_server_protocol.py
    ├── test_backend_execution.py
    ├── test_adversarial_backend.py
    ├── test_cli_packaging.py
    ├── test_diff_engine.py
    ├── test_model_plumbing.py
    ├── test_stream_purity_adversarial.py
    ├── test_test_runner.py
    ├── test_test_runner_adversarial.py
    ├── test_workspace_isolation.py
    ├── test_empirical_challenger2.py
    ├── test_empirical_challenger_stress.py
    ├── test_tier1_feature_coverage.py
    ├── test_tier2_boundary_corner.py
    ├── test_tier3_cross_feature.py
    └── test_tier4_real_world.py
```
