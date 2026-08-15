# E2E Test Suite Ready

## Test Runner
- **Primary Command**: `$env:MCP_AGY_BACKEND="mock"; py -m pytest tests/ -v`
- **Tier 1-4 Command**: `$env:MCP_AGY_BACKEND="mock"; py -m pytest tests/test_tier1_feature_coverage.py tests/test_tier2_boundary_corner.py tests/test_tier3_cross_feature.py tests/test_tier4_real_world.py -v`
- **Expected Outcome**: 100% pass rate with exit code 0

## Coverage Summary
| Tier | Count | Description | Target | Status |
|------|------:|-------------|:------:|:------:|
| **Tier 1: Feature Coverage** | 26 | Coverage across all 4 core FastMCP tools (`agy_execute_task`, `agy_chat`, `agy_get_diff`, `agy_run_tests`) | $\ge 20$ | **PASSED** |
| **Tier 2: Boundary & Corner** | 32 | Boundary conditions, empty prompts, non-git paths, unborn branches, timeouts, crash recovery, large diffs | $\ge 20$ | **PASSED** |
| **Tier 3: Cross-Feature** | 8 | Pairwise multi-tool workflows, closed-loop debugging, multi-turn chat, 4-tool integration | $\ge 6$ | **PASSED** |
| **Tier 4: Real-World Scenarios** | 6 | Full stdio subprocess lifecycle, Claude/Cursor architect simulation, hybrid multi-language projects, legacy refactor | $\ge 5$ | **PASSED** |
| **Total E2E Tests** | **72** | **Requirement-driven, opaque-box test suite** | **$\ge 51$** | **PASSED** |
| Supporting Protocol, Adversarial & Engine Tests | 337 | FastMCP protocol frames, fallback hierarchy, NDJSON parser, stream purity, env-var configuration, shipped-config integrity, framework detection, subprocess stderr handling, background job lifecycle, adversarial stress tests | N/A | **PASSED** |
| **Grand Total Matrix** | **409** | **Complete project test suite (100% passed, 0 xfailed)** | N/A | **PASSED** |

## Live Integration Verification (real `agy.EXE`, not mock)

The matrix above runs against `ProgrammableMockAGYBackend`. The following were additionally
verified end-to-end against the real Google Antigravity binary (`agy.EXE` v1.1.13):

| Check | Method | Result |
|---|---|:---:|
| NDJSON stream schema matches the parser | Raw `agy --output-format stream-json` capture compared against `SubprocessCLIBackend` event handling | **PASS** |
| `agy_execute_task` writes real files | Live task in an ephemeral workspace; file content and `modified_files` telemetry verified on disk | **PASS** |
| `agy_chat` read-only consultation | Live `plan` mode round-trip with token telemetry | **PASS** |
| All 4 tools over OS stdio | `sys.executable -m mcp_agy` subprocess driven by `mcp.client.stdio` | **PASS** |
| Launch under a client's stripped environment | Spawned with only the config `env` block plus the Windows floor — **no `PATH`** | **PASS** |
| `agy_get_diff` on a large real repo | 113 changed files / 272 KB unified diff (`E:\Project\LIVA`) | **PASS** |
| `agy_run_tests` on a real Rust workspace | Multi-binary `cargo test` run, 50 tests aggregated across 5 integration targets | **PASS** |
| A CLI-level refusal reaches the caller | Live `agy_chat` with a conflicting `--model`/`--effort` pair; the CLI's own message now arrives in `error_details` | **PASS** |
| A child flooding stderr does not hang the stream | 512 KB written to stderr ahead of the child's last stdout line; the line still arrives and the tail is kept | **PASS** |
| `agy_start_task` returns while AGY works | Live task through Claude Code's own MCP connection; job id returned at once, `agy.EXE` confirmed running in the OS process table alongside `running_count: 1` | **PASS** |
| `agy_job_status(wait_seconds)` collects a real result | Same job collected on one 45 s wait; returned at 13.8 s with token telemetry, and both claimed files verified on disk (`pytest` on AGY's own suite: 5 passed) | **PASS** |
| `agy_cancel_job` kills the whole process tree | A live run had grown a 14-process tree under `agy.EXE` (node, uvx, browser-use, cmd, powershell). After cancel: 0 survivors, and no partial files landed | **PASS** |
| Two jobs run concurrently in separate workspaces | Both started ~1.5 s apart, two `agy.EXE` processes observed at once, overlapping run windows, both results correct on disk (behaviour asserted by executing AGY's `slugify` and `roman`) | **PASS** |
| An unknown job id is reported, not guessed | Fabricated UUID returned `status: not_found` naming server restart as the cause | **PASS** |
| The `agy_list_jobs` fix holds (defect 11, re-verified after restart) | Fresh server process, one live 25.8 s task: the listing carried `result: null` while running and still `result: null` once `completed`, while `agy_job_status` on the same id returned the full result. AGY's claimed file verified by executing it | **PASS** |
| The Vitest parser fix reads a real suite (defect 10, verified post-fix) | `agy_run_tests` on `E:\Project\LIVA\liva-ui` reported `total: 404, passed: 404`, matching Vitest's own `Tests  404 passed (404)` across 38 test files. The pre-fix generic parser reported `total: 1` | **PASS** |

## Defects Found by Live Use and Fixed

Running the server against a real project surfaced defects the mock suite could not. Numbers
1–5 came from a `agy_run_tests` session against `E:\Project\LIVA`; 6–9 from driving `agy_chat`
at the same repository through Claude Code:

| # | Defect | Impact | Status |
|---|--------|--------|:------:|
| 1 | `MCP_AGY_LOG_LEVEL` / `MCP_AGY_DEBUG` / `MCP_AGY_DEFAULT_MODEL` documented and set in all 4 shipped client configs, but read by no code | Env-only clients (Claude Desktop, Cursor, Cline, Roo Code) cannot configure the server at all; the setting silently does nothing | **FIXED** |
| 2 | `serverInfo.version` reported the installed `mcp` SDK version (`1.29.0`) | Clients showed the wrong build; the one version a user can act on in a bug report was unavailable | **FIXED** |
| 3 | Tier 5 fired on a bare `tests/` directory, so a Rust crate (`Cargo.toml` beside `tests/*.rs`) detected as Python `unittest`; `unittest discover` found nothing, exited 0, and Tier 7 Cargo was never reached | A crate's entire Rust suite reported as an empty pass | **FIXED** |
| 4 | `_parse_cargo_diagnostics` read only the first `test result:` line | A 43-test run reported `total: 21`; a failure in any later test binary reported `failed: 0` beside a failed status | **FIXED** |
| 5 | The `custom` framework sniffer tested a loose pytest marker (`"=== "` substring plus the word `passed`) before cargo's definitive `test result:`. Cargo's summary always contains "passed", so any project printing a `=== banner ===` had its cargo output handed to the pytest parser | 35 passing tests reported as `21 passed` plus a phantom `errors: 1` | **FIXED** |
| 6 | `result.status: ERROR` set `status="error"` but never read `result.error` beside it | Every CLI-level refusal — invalid model/effort combination, auth failure, rejected workspace — reached the architect as `error_details: null`: a failure with nothing to act on | **FIXED** |
| 7 | A tool that fails is reported in a `step_update` whose `state` is `ERROR`, and only `state == "DONE"` was handled. AGY then finishes on `result.status: SUCCESS` with an empty response | A denied `run_command` returned `status: "success"`, `response: ""`, `error_details: null`. The architect saw a successful call that simply said nothing, and the failed tool was missing from `tool_calls` entirely | **FIXED** |
| 8 | `stream_subprocess_lines` piped the child's stderr and never read it | AGY writes its diagnosis there (`"a tool required the \"command\" permission that headless mode cannot prompt for"`) and it was discarded. Worse, an unread pipe fills at ~64 KB and blocks the child mid-write: a chatty run hangs until the timeout fires. Regression test confirms the hang without the fix | **FIXED** |
| 10 | The Jest parser keys on `Test Suites:` and `^Tests:` — Vitest writes neither, using `Tests  27 passed (27)` with no colon and the total in parentheses | A live 27-test Vitest run on `E:\Project\LIVA\liva-ui` fell through to the generic parser and reported `total: 1, passed: 1`: right about the outcome, silent about the suite. An architect deciding whether a fix held cannot tell that from a single smoke test | **FIXED** |
| 9a | The four original tools are all synchronous, so every one of them is capped by the client's per-call timeout. Raising that cap helps, but it also means the architect's conversation blocks for the whole of a twenty-minute task | The architecture this server exists for — an architect agent delegating long autonomous work — could not actually be run. Fixed by adding `agy_start_task` / `agy_job_status` / `agy_cancel_job` / `agy_list_jobs`: the run happens in the server, the call returns an id at once, and `wait_seconds` collects the result within any client's cap | **FIXED** |
| 11 | `agy_list_jobs` reused the status renderer, so every finished job in the listing carried AGY's full result — prose response, diff summary and telemetry | The registry retains up to 100 completed jobs, so asking "what is still running?" could return 100 full run reports. The listing spent the architect's context, the one resource the background-job design exists to protect. Found by reading a real 4-job listing after the live run above | **FIXED** |
| 9 | Client-side call timeouts were undocumented, and no shipped config set one | Claude Code caps a tool call at 60 s by default, while a real AGY task runs for minutes. Every long call died with `Error: Request timed out` and the work was thrown away. Documented in README §5 with a `timeout` now set in `configs/claude_code_mcp.json`. Not fixable server-side: the client documents the cap as a hard wall-clock limit that progress notifications do not extend | **DOCUMENTED** |

## Feature Matrix Checklist
| Feature | Tier 1 | Tier 2 | Tier 3 | Tier 4 | Verification Status |
|---------|:------:|:------:|:------:|:------:|:-------------------:|
| `agy_execute_task` | 7 | 8 | 4 | 5 | **VERIFIED (APPROVE)** |
| `agy_chat` | 7 | 7 | 3 | 4 | **VERIFIED (APPROVE)** |
| `agy_get_diff` | 6 | 9 | 4 | 3 | **VERIFIED (APPROVE)** |
| `agy_run_tests` | 6 | 8 | 3 | 4 | **VERIFIED (APPROVE)** |

## Test Infrastructure Summary
- **Test Infrastructure Reference**: `TEST_INFRA.md`
- **Master Fixture Library**: `tests/conftest.py` (1,005 lines)
  - `EphemeralWorkspace`: Windows leak-free teardown (`os.chmod(..., stat.S_IWRITE)`), Git initialization, SHA-256 snapshot hashing for read-only invariants.
  - 20+ specialized Git and multi-ecosystem workspace fixtures.
  - `ProgrammableMockAGYBackend`: Deterministic disk synthesis, token telemetry, error and timeout simulation.
  - Dual MCP clients: `client_factory` (AnyIO in-memory stream) and `stdio_client_factory` (OS subprocess stdio).

## Audit & Verification Verdicts
| Verification Role | Agent | Verdict | Key Evidence |
|-------------------|-------|:-------:|--------------|
| **Reviewer 1 (Tiers 1 & 2)** | `reviewer_e2e_1` | **APPROVE** | 58/58 tests passed in 14.85s; full schema & boundary validation |
| **Reviewer 2 (Tiers 3 & 4)** | `reviewer_e2e_2` | **APPROVE** | 14/14 tests passed; stdio JSON-RPC transport & architect workflows verified |
| **Challenger 1 (Stress & Stability)** | `challenger_e2e_1` | **APPROVE** | 50 workspaces, 50 AnyIO cancellations, 20 tree kills with 0 leaks |
| **Challenger 2 (Requirement Fidelity)** | `challenger_e2e_2` | **APPROVE** | 181/181 tests passed; complete R1-R4 fidelity & error injection resilience |
| **Forensic Integrity Auditor** | `auditor_e2e_1` | **CLEAN** | 647 substantive assertions scanned; 0 fake passes; 0 integrity violations |

Gate Result: **PASS**
