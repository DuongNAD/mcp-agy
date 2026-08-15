# FastMCP AGY End-to-End Test Infrastructure Reference

`TEST_INFRA.md` documents the testing architecture, design philosophy, 4-tier verification matrix, fixture library catalog, and execution procedures for the **FastMCP AGY** test harness.

---

## 1. Test Architecture & Design Philosophy

The FastMCP AGY test suite is built on a **requirement-driven, opaque-box testing philosophy**:

1. **Opaque-Box Protocol Testing**:
   - Tests interact with FastMCP AGY exclusively through standard Model Context Protocol (MCP) interfaces (`ClientSession` over in-memory AnyIO stream channels or subprocess stdio pipes).
   - Test assertions verify JSON-RPC 2.0 protocol compliance, tool registration schemas, structured Pydantic v2 return models, and genuine filesystem side-effects.

2. **Dual-Transport Testing Architecture**:
   - **In-Memory AnyIO Stream Transport** (`client_factory`): Connects `ClientSession` directly to the FastMCP low-level server using AnyIO memory streams. Runs in-process with zero subprocess overhead, enabling sub-millisecond execution times and deterministic protocol verification.
   - **Subprocess Stdio Transport** (`stdio_client_factory`): Spawns `sys.executable -m mcp_agy` via `mcp.client.stdio.stdio_client`. Tests OS-level process boundary interactions, stdio buffering, environment inheritance, and CLI entrypoint behavior.

3. **Zero Host Side-Effects & Windows Leak-Free Lifecycle**:
   - All tests execute in isolated ephemeral workspaces managed by `EphemeralWorkspace`.
   - Teardown implements Windows-specific file lock handling (`stat.S_IWRITE`), recursively clearing read-only attributes on Git objects (`.git/objects/*`) to prevent `PermissionError: [WinError 5] Access is denied` or `[WinError 32]`.

4. **100% Deterministic Offline CI Default**:
   - Tests run by default against `ProgrammableMockAGYBackend`, completing the entire test matrix in < 5 seconds without cloud LLM dependencies or network calls.
   - Live integration tests against real binaries (`agy.EXE`) are gated behind the `@pytest.mark.live` marker.

```
+-------------------------------------------------------------------------------+
|                             External MCP Client                               |
|         (ClientSession via AnyIO Stream OR Subprocess Stdio Transport)        |
+---------------------------------------+---------------------------------------+
                                        |
                                        | JSON-RPC 2.0 Frames
                                        v
+---------------------------------------+---------------------------------------+
|                         FastMCP Server (server.py)                            |
|                                                                               |
|  +-------------------+  +---------------+  +------------+  +---------------+  |
|  | agy_execute_task  |  |   agy_chat    |  |agy_get_diff|  | agy_run_tests |  |
|  +---------+---------+  +-------+-------+  +-----+------+  +-------+-------+  |
+------------|--------------------|----------------|-----------------|----------+
             |                    |                |                 |
             v                    v                v                 v
+---------------------------------+   +-----------------------------------------+
|   ProgrammableMockAGYBackend    |   |           Target Workspace              |
|  - Synthetic file edits on disk |   |  - EphemeralWorkspace (isolated tempdir)|
|  - SHA-256 read-only snapshots  |   |  - Git states (unborn/clean/dirty/etc.) |
|  - Realistic token telemetry    |   |  - Multi-ecosystem configs (pytest/npm) |
+---------------------------------+   +-----------------------------------------+
```

---

## 2. Feature Inventory Mapping to Test Tiers

The test suite covers the four primary MCP tools across four verification tiers:

| # | Feature Tool | Description | Target Coverage | Tier 1 | Tier 2 | Tier 3 | Tier 4 |
|---|---|---|---|:---:|:---:|:---:|:---:|
| 1 | `agy_execute_task` | Autonomous coding worker in workspace | File creation, edits, refactoring, telemetry | 5+ | 5+ | ✓ | ✓ |
| 2 | `agy_chat` | Read-only analytical & planning consultation | Read-only invariant, multi-turn dialogue | 5+ | 5+ | ✓ | ✓ |
| 3 | `agy_get_diff` | Unified git diff inspection | Staged, unstaged, untracked, binary, unborn | 5+ | 5+ | ✓ | ✓ |
| 4 | `agy_run_tests` | Automated test suite execution & diagnostics | Pytest, unittest, npm, cargo, timeout kill | 5+ | 5+ | ✓ | ✓ |

### Tier Breakdown & Scope

- **Tier 1: Feature Coverage (`test_tier1_feature_coverage.py`)** — `>= 20 tests`
  - Validates core happy-path behaviors, response models (`TaskExecutionResult`, `ChatResult`, `DiffResult`, `TestRunResult`), token usage metrics, auto-approval flags, and framework auto-detection.
- **Tier 2: Boundary & Corner Cases (`test_tier2_boundary_corner.py`)** — `>= 20 tests`
  - Validates edge cases: non-existent/invalid paths, directory traversal prevention, empty/whitespace prompts, unborn Git branches (0 commits), binary diffs, large diff truncations, process timeouts, and workspace concurrency locks.
- **Tier 3: Cross-Feature Combinations (`test_tier3_cross_feature.py`)** — `>= 6 tests`
  - Validates multi-step workflows: `execute -> diff -> test` cycles, `chat` planning followed by `execute` implementation, broken test fixing loops, and multi-turn state transitions.
- **Tier 4: Real-World Scenarios (`test_tier4_real_world.py`)** — `>= 5 tests`
  - Validates end-to-end architect scenarios: real subprocess stdio lifecycle, Python package bootstrapping, Node.js/NPM project workflows, legacy code refactoring, and backend resilience.

---

## 3. Fixture Library Reference (`tests/conftest.py`)

All fixtures are globally registered in `tests/conftest.py` and available to all test modules.

### 3.1 Base Ephemeral Workspace Class

#### `EphemeralWorkspace`
```python
class EphemeralWorkspace:
    path: Path                   # Resolved canonical Path
    str_path: str                # String path representation
    raw_path: str                # Temporary directory path string
    
    # Path compatibility
    __str__() -> str
    __fspath__() -> str
    __truediv__(other: str | Path) -> Path
    
    # Filesystem operations
    write_file(rel_path: str | Path, content: str | bytes, encoding: str = "utf-8") -> Path
    read_file(rel_path: str | Path, encoding: str = "utf-8") -> str
    read_bytes(rel_path: str | Path) -> bytes
    delete_file(rel_path: str | Path) -> None
    exists(rel_path: str | Path) -> bool
    list_files(include_git: bool = False) -> list[str]
    
    # Git operations
    git(*args: str) -> subprocess.CompletedProcess[str]
    init_git(user_name="Test User", user_email="test@example.com", initial_commit=True, initial_file="README.md", initial_content="# Repo\n") -> None
    stage(*rel_paths: str) -> None
    commit(message: str) -> None
    
    # Snapshot & Invariant verification
    snapshot_hashes(include_git: bool = False) -> dict[str, str]
    verify_snapshot_unchanged(initial_snapshot: dict[str, str], include_git: bool = False) -> tuple[bool, str]
    
    # Windows-safe teardown
    cleanup() -> None
```

---

### 3.2 Fixture Catalog

#### Base & Non-Git Workspaces
| Fixture Name | Type | Description |
|---|---|---|
| `ephemeral_workspace` | `EphemeralWorkspace` | Fresh empty temporary directory with automatic leak-free teardown |
| `temp_workspace` | `EphemeralWorkspace` | Alias for `ephemeral_workspace` |
| `clean_workspace` | `EphemeralWorkspace` | Directory containing non-git sample files (`main.py`, `config.json`) |
| `non_git_workspace` | `EphemeralWorkspace` | Directory without `.git` directory for testing `status="not_a_git_repo"` |

#### Git State Workspaces
| Fixture Name | Type | Description |
|---|---|---|
| `clean_git_workspace` | `EphemeralWorkspace` | Git repo with initial commit and clean working tree (`src/app.py`) |
| `git_workspace` | `EphemeralWorkspace` | Alias for `clean_git_workspace` |
| `unborn_git_workspace` | `EphemeralWorkspace` | Git repo with 0 commits (unborn HEAD), staged and untracked files |
| `staged_changes_workspace` | `EphemeralWorkspace` | Git repo with staged modifications (`src/app.py`) and staged new file (`src/utils.py`) |
| `staged_git_workspace` | `EphemeralWorkspace` | Alias for `staged_changes_workspace` |
| `unstaged_changes_workspace`| `EphemeralWorkspace` | Git repo with unstaged edits in tracked files |
| `untracked_files_workspace` | `EphemeralWorkspace` | Git repo with untracked new files and subdirectories (`new_feature.py`, `docs/guide.md`) |
| `dirty_git_workspace` | `EphemeralWorkspace` | Combined dirty repo: Staged (A, M), Unstaged (M), MM (both), Untracked (??), Deleted (D) |
| `binary_git_workspace` | `EphemeralWorkspace` | Git repo with modified binary PNG and untracked binary `.bin` |
| `large_diff_workspace` | `EphemeralWorkspace` | Git repo with 5,000 modified lines for truncation and limit testing |
| `gitignore_workspace` | `EphemeralWorkspace` | Git repo with `.gitignore` rules (`*.log`, `build/`, `*.tmp`) and ignored files |

#### Multi-Ecosystem Test Runner Workspaces
| Fixture Name | Type | Description |
|---|---|---|
| `pytest_passing_workspace` | `EphemeralWorkspace` | Python project with `pytest.ini` and 2 passing tests (`tests/test_math.py`) |
| `pytest_workspace` | `EphemeralWorkspace` | Alias for `pytest_passing_workspace` |
| `pytest_failing_workspace` | `EphemeralWorkspace` | Python project with `pytest.ini` and 1 failing test (`tests/test_failing.py`) |
| `pytest_mixed_workspace` | `EphemeralWorkspace` | Python project with 2 passed, 1 failed, and 1 skipped test (`tests/test_mixed.py`) |
| `unittest_workspace` | `EphemeralWorkspace` | Python stdlib `unittest.TestCase` suite (`tests/test_suite.py`) |
| `npm_passing_workspace` | `EphemeralWorkspace` | Node.js project with `package.json` and passing `node test.js` script |
| `npm_workspace` | `EphemeralWorkspace` | Alias for `npm_passing_workspace` |
| `npm_failing_workspace` | `EphemeralWorkspace` | Node.js project with `package.json` and failing `node test.js` script (exit code 1) |
| `cargo_workspace` | `EphemeralWorkspace` | Rust project with `Cargo.toml` and `src/lib.rs` with `#[test]` |
| `hanging_test_workspace` | `EphemeralWorkspace` | Test suite with infinite sleep script to verify timeout & process tree kill |
| `custom_script_workspace` | `EphemeralWorkspace` | Workspace with custom runner script (`run_custom_tests.py`) |
| `no_framework_workspace` | `EphemeralWorkspace` | Workspace with documentation files and no recognizable test configs |

#### Backend & Server Fixtures
| Fixture Name | Type | Description |
|---|---|---|
| `mock_backend` | `ProgrammableMockAGYBackend` | Controllable backend with synthetic file synthesis, rule tables, and telemetry |
| `test_server` | `FastMCP` | FastMCP server instance configured with `mock_backend` |
| `fastmcp_server` | `FastMCP` | Alias for `test_server` |

#### Client Transport Fixtures
| Fixture Name | Type | Description |
|---|---|---|
| `client_factory` | Async Context Manager | Creates an in-memory `ClientSession` over AnyIO memory streams connected to server |
| `stdio_client_factory` | Async Context Manager | Spawns a subprocess stdio `ClientSession` running `python -m mcp_agy` |
| `mcp_client` | `ClientSession` | Pre-initialized in-memory client session connected to `test_server` |

---

## 4. Usage Patterns & Code Examples

### Pattern 1: Invoking Tools via In-Memory Client Session
```python
import json
import pytest

@pytest.mark.anyio
async def test_execute_task_workflow(test_server, client_factory, git_workspace):
    async with client_factory(test_server) as session:
        # Call agy_execute_task
        res = await session.call_tool(
            "agy_execute_task",
            {
                "workspace_path": str(git_workspace),
                "prompt": "Create calculator.py with add function",
                "auto_approve": True,
            },
        )
        data = json.loads(res.content[0].text)
        assert data["status"] == "success"
        assert "calculator.py" in data["modified_files"]
        assert git_workspace.exists("calculator.py")
```

### Pattern 2: Verifying Chat Read-Only Invariant with SHA-256 Snapshots
```python
import json
import pytest

@pytest.mark.anyio
async def test_chat_read_only_invariant(test_server, client_factory, clean_git_workspace):
    # Capture initial workspace hash snapshot
    initial_snapshot = clean_git_workspace.snapshot_hashes()
    
    async with client_factory(test_server) as session:
        res = await session.call_tool(
            "agy_chat",
            {
                "prompt": "Analyze repository structure and recommend refactorings",
                "workspace_path": str(clean_git_workspace),
            },
        )
        data = json.loads(res.content[0].text)
        assert data["status"] == "success"
    
    # Assert zero filesystem modifications occurred
    unchanged, desc = clean_git_workspace.verify_snapshot_unchanged(initial_snapshot)
    assert unchanged, f"Chat modified workspace: {desc}"
```

### Pattern 3: Subprocess Stdio Transport Verification
```python
import json
import pytest

@pytest.mark.anyio
async def test_subprocess_stdio_handshake(stdio_client_factory):
    async with stdio_client_factory() as session:
        # Verify JSON-RPC initialize handshake and tool discovery over real OS stdio
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {"agy_execute_task", "agy_chat", "agy_get_diff", "agy_run_tests"}.issubset(names)
```

---

## 5. Verification Commands

Run the following commands to execute test suites and verify test infrastructure:

```powershell
# 1. Collect all tests without execution (verify conftest.py & imports)
py -m pytest --collect-only

# 2. Run all unit and protocol tests
py -m pytest tests/ -v

# 3. Run specific test module
py -m pytest tests/test_server_protocol.py -v

# 4. Run tests by marker tier
py -m pytest -m tier1 -v
py -m pytest -m tier2 -v
py -m pytest -m tier3 -v
py -m pytest -m tier4 -v

# 5. Run live integration tests (requires real agy.EXE installed)
py -m pytest -m live -v
```
