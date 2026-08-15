"""Adversarial Empirical Stress-Testing Suite for MultiEcosystemTestRunner.

Validates 5 key attack vectors:
1. Long-running & deeply nested process trees with SIGTERM/SIGINT resistance.
2. Heavily colorized, malformed, and ReDoS-inducing terminal escape sequences.
3. Adversarial output formats across Pytest, Cargo, Jest/Vitest, Go, and Unittest.
4. 13-tier detection matrix edge cases (conflicts, empty venvs, corrupt configs, empty makefiles).
5. Windows shell command quoting, injection resistance, and argument splitting.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import psutil
import pytest
import sys
import tempfile
import time

from mcp_agy.core.models import TestFailure, TestRunResult, TestSummary
from mcp_agy.core.test_runner import (
    MultiEcosystemTestRunner,
    execute_workspace_tests,
    split_command_args,
    strip_ansi_codes,
)
from mcp_agy.utils.process import terminate_process_tree


# ============================================================================
# Vector 1: Process Tree Deep Termination & Signal Resistance
# ============================================================================

class TestProcessTreeDeepTermination:
    """Stress tests recursive process tree termination on stubborn multi-generation child trees."""

    @pytest.mark.asyncio
    async def test_deep_nested_process_tree_termination_on_timeout(self, tmp_path):
        """Spawns 4 generations of stubborn processes that ignore SIGINT/SIGTERM.

        Verifies all generations (parent -> child -> grandchild -> great-grandchild)
        are killed upon timeout and no zombie/orphan processes survive.
        """
        runner = MultiEcosystemTestRunner(default_timeout_seconds=2)
        pid_file = tmp_path / "pids.txt"

        # Great-grandchild script (level 4)
        level4_py = tmp_path / "level4.py"
        level4_py.write_text(
            "import os, time, signal\n"
            "def handler(signum, frame): pass\n"
            "try:\n"
            "    signal.signal(signal.SIGINT, handler)\n"
            "    signal.signal(signal.SIGTERM, handler)\n"
            "except Exception:\n"
            "    pass\n"
            f"with open(r'{pid_file}', 'a') as f:\n"
            "    f.write(f'{os.getpid()}\\n')\n"
            "while True:\n"
            "    time.sleep(0.5)\n",
            encoding="utf-8",
        )

        # Grandchild script (level 3)
        level3_py = tmp_path / "level3.py"
        level3_py.write_text(
            "import os, subprocess, sys, time, signal\n"
            "def handler(signum, frame): pass\n"
            "try:\n"
            "    signal.signal(signal.SIGINT, handler)\n"
            "    signal.signal(signal.SIGTERM, handler)\n"
            "except Exception:\n"
            "    pass\n"
            f"with open(r'{pid_file}', 'a') as f:\n"
            "    f.write(f'{os.getpid()}\\n')\n"
            f"p = subprocess.Popen([sys.executable, r'{level4_py}'])\n"
            "while True:\n"
            "    time.sleep(0.5)\n",
            encoding="utf-8",
        )

        # Child script (level 2)
        level2_py = tmp_path / "level2.py"
        level2_py.write_text(
            "import os, subprocess, sys, time, signal\n"
            "def handler(signum, frame): pass\n"
            "try:\n"
            "    signal.signal(signal.SIGINT, handler)\n"
            "    signal.signal(signal.SIGTERM, handler)\n"
            "except Exception:\n"
            "    pass\n"
            f"with open(r'{pid_file}', 'a') as f:\n"
            "    f.write(f'{os.getpid()}\\n')\n"
            f"p = subprocess.Popen([sys.executable, r'{level3_py}'])\n"
            "while True:\n"
            "    time.sleep(0.5)\n",
            encoding="utf-8",
        )

        # Root runner script (level 1)
        root_py = tmp_path / "root.py"
        root_py.write_text(
            "import os, subprocess, sys, time, signal\n"
            "def handler(signum, frame): pass\n"
            "try:\n"
            "    signal.signal(signal.SIGINT, handler)\n"
            "    signal.signal(signal.SIGTERM, handler)\n"
            "except Exception:\n"
            "    pass\n"
            f"with open(r'{pid_file}', 'a') as f:\n"
            "    f.write(f'{os.getpid()}\\n')\n"
            f"p = subprocess.Popen([sys.executable, r'{level2_py}'])\n"
            "while True:\n"
            "    time.sleep(0.5)\n",
            encoding="utf-8",
        )

        cmd = f'"{sys.executable}" "{root_py}"'
        start = time.monotonic()
        res = await runner.run_tests(str(tmp_path), test_command=cmd, timeout_seconds=2)
        elapsed = time.monotonic() - start

        assert res.status == "timeout"
        assert res.exit_code == -9
        assert elapsed >= 1.8 and elapsed < 8.0

        # Wait brief grace period for OS to release PIDs
        await asyncio.sleep(0.5)

        # Read recorded PIDs
        assert pid_file.exists(), "PIDs file should have been written by child tree"
        recorded_pids = [int(p.strip()) for p in pid_file.read_text(encoding="utf-8").splitlines() if p.strip()]
        assert len(recorded_pids) >= 3, f"Expected at least 3 levels of PIDs, got {recorded_pids}"

        # Empirically verify ALL recorded descendant processes are dead
        survivors = []
        for pid in recorded_pids:
            if psutil.pid_exists(pid):
                try:
                    proc = psutil.Process(pid)
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                        survivors.append((pid, proc.name()))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        assert not survivors, f"Discovered surviving orphan processes after tree termination: {survivors}"

    def test_direct_terminate_process_tree_handles_invalid_or_dead_pids(self):
        """Verifies terminate_process_tree handles non-existent, negative, or 0 PIDs without raising."""
        terminate_process_tree(-1)
        terminate_process_tree(0)
        terminate_process_tree(99999999)  # Non-existent PID

    @pytest.mark.asyncio
    async def test_large_output_throughput_no_deadlock(self, tmp_path):
        """Verifies runner does not deadlock when child process outputs 2MB of text."""
        runner = MultiEcosystemTestRunner()
        script = tmp_path / "large_output.py"
        script.write_text(
            "import sys\n"
            "for i in range(20000):\n"
            "    sys.stdout.write(f'Line {i}: ' + 'X' * 80 + '\\n')\n"
            "sys.stdout.flush()\n",
            encoding="utf-8",
        )
        res = await runner.run_tests(str(tmp_path), test_command=f'"{sys.executable}" "{script}"', timeout_seconds=10)
        assert res.status == "passed"
        assert res.exit_code == 0
        assert "Line 19999" in res.output


# ============================================================================
# Vector 2: Heavily Colorized and Malformed Terminal Escape Sequences
# ============================================================================

class TestANSIAndEscapeSequenceSanitization:
    """Stress tests terminal escape sequence handling against pathological and complex sequences."""

    def test_complex_osc_hyperlinks_and_titles(self):
        raw = (
            "\x1b]0;Adversarial Title\x07"
            "Standard Text "
            "\x1b]8;;https://example.com/malicious\x1b\\Click Here\x1b]8;;\x1b\\"
            " More Text"
            "\x1b]50;SetFont=Consolas\x07"
            "\x1b]1337;File=inline=1;size=1000:AAECAw==\x07"
        )
        cleaned = strip_ansi_codes(raw)
        assert cleaned == "Standard Text Click Here More Text"
        assert "\x1b" not in cleaned
        assert "\x07" not in cleaned

    def test_24bit_rgb_truecolor_and_256color(self):
        raw = (
            "\x1b[38;2;255;128;64mOrange Text\x1b[0m "
            "\x1b[48;2;0;0;128;1mDark Blue Bold BG\x1b[0m "
            "\x1b[38;5;196mRed256\x1b[0m"
        )
        cleaned = strip_ansi_codes(raw)
        assert cleaned == "Orange Text Dark Blue Bold BG Red256"

    def test_cursor_controls_and_screen_clearing(self):
        raw = (
            "\x1b[2J\x1b[H\x1b[10;20H"
            "Positioned Line"
            "\x1b[?25l\x1b[?25h\x1b[6n\x1b[s\x1b[u\x1b[1A\x1b[2K\r"
            "Replaced Line"
        )
        cleaned = strip_ansi_codes(raw)
        assert "Positioned Line" in cleaned
        assert "Replaced Line" in cleaned
        assert "\x1b" not in cleaned

    def test_pathological_unclosed_and_large_escape_sequences_redos_immunity(self):
        """Tests that unclosed or repetitive escape sequences execute in <200ms without catastrophic backtracking."""
        pathological = "\x1b[" * 10000 + "A" + "\x1b]0;unclosed" * 5000 + "Normal Text"
        start = time.monotonic()
        cleaned = strip_ansi_codes(pathological)
        elapsed = time.monotonic() - start

        assert elapsed < 0.2, f"ANSI stripping took too long ({elapsed}s), possible ReDoS vulnerability"
        assert "Normal Text" in cleaned


# ============================================================================
# Vector 3: Adversarial Output Formats (Pytest, Cargo, Jest, Go, Unittest)
# ============================================================================

class TestAdversarialFrameworkOutputs:
    """Stress tests regex diagnostics parsers against multiline diffs, nested tracebacks, and panics."""

    def test_pytest_nested_tracebacks_and_multiline_diffs(self):
        runner = MultiEcosystemTestRunner()
        output = (
            "============================= test session starts =============================\n"
            "FAILED tests/test_core.py::TestAPI::test_query[arg1-special/chars@#$-utf_🚀] - AssertionError: Detailed diff below\n"
            "_________________ TestAPI.test_query[arg1-special/chars@#$-utf_🚀] _________________\n"
            "tests/test_core.py:150: in test_query\n"
            "    self.assert_response(actual, expected)\n"
            "tests/test_core.py:85: in assert_response\n"
            "    assert actual == expected\n"
            "E   AssertionError: Detailed diff below\n"
            "E     {\n"
            "E      - 'status': 200,\n"
            "E      + 'status': 500,\n"
            "E      ?          ^^^\n"
            "E        'items': [1, 2, 3],\n"
            "E     }\n"
            "FAILED tests/test_recursion.py::test_infinite - RecursionError: maximum recursion depth exceeded\n"
            "_________________________________ test_infinite _________________________________\n"
            "tests/test_recursion.py:12: in test_infinite\n"
            "    return test_infinite()\n"
            "E   RecursionError: maximum recursion depth exceeded\n"
            "=================== 2 failed, 15 passed, 3 skipped, 2 xfailed, 1 xpass, 1 error in 4.56s ===================\n"
        )
        summary, failures = runner.parse_diagnostics("pytest", output)
        assert summary.total == 24
        assert summary.passed == 16  # 15 passed + 1 xpassed
        assert summary.failed == 2
        assert summary.skipped == 5  # 3 skipped + 2 xfailed
        assert summary.errors == 1

        assert len(failures) == 2
        f1 = failures[0]
        assert "arg1-special/chars@#$-utf_🚀" in f1.test_id
        assert "AssertionError" in f1.message
        assert "tests/test_core.py" in (f1.location or "")
        assert f1.traceback is not None
        assert "status': 500" in f1.traceback

        f2 = failures[1]
        assert f2.test_id == "tests/test_recursion.py::test_infinite"
        assert "RecursionError" in f2.message

    def test_pytest_standard_multiline_diff_passed(self):
        """Standard pytest summary line with plural xpassed."""
        runner = MultiEcosystemTestRunner()
        output = (
            "FAILED tests/test_core.py::test_fail - AssertionError: failed\n"
            "__________________________________ test_fail __________________________________\n"
            "tests/test_core.py:10: in test_fail\n"
            "    assert 1 == 2\n"
            "E   AssertionError: assert 1 == 2\n"
            "=================== 1 failed, 5 passed, 2 skipped, 2 xpassed in 0.50s ===================\n"
        )
        summary, failures = runner.parse_diagnostics("pytest", output)
        assert summary.total == 10
        assert summary.passed == 7  # 5 passed + 2 xpassed
        assert summary.failed == 1
        assert summary.skipped == 2
        assert len(failures) == 1

    def test_cargo_panic_with_special_characters_and_json(self):
        runner = MultiEcosystemTestRunner()
        output = (
            "running 8 tests\n"
            "test api::test_ok ... ok\n"
            "test worker::test_spawn ... ok\n"
            "test parser::test_json_mismatch ... FAILED\n\n"
            "failures:\n\n"
            "---- parser::test_json_mismatch stdout ----\n"
            "thread 'parser::test_json_mismatch' panicked at 'assertion `left == right` failed\n"
            "  left: {\"name\": \"alpha\", \"tags\": [\"a\", \"b\"]}\n"
            " right: {\"name\": \"beta\", \"tags\": [\"a\", \"b\"]}', crates/parser/src/lib.rs:112:9\n"
            "stack backtrace:\n"
            "   0: rust_begin_unwind\n"
            "   1: core::panicking::panic_fmt\n\n"
            "test result: FAILED. 6 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.22s\n"
        )
        summary, failures = runner.parse_diagnostics("cargo", output)
        assert summary.total == 8
        assert summary.passed == 6
        assert summary.failed == 1
        assert summary.skipped == 1

        assert len(failures) == 1
        assert failures[0].test_id == "parser::test_json_mismatch"
        assert "crates/parser/src/lib.rs:112:9" in (failures[0].location or "")
        assert "assertion" in failures[0].message

    def test_cargo_single_line_panic(self):
        """Single line panic matches without multiline issue."""
        runner = MultiEcosystemTestRunner()
        output = (
            "running 2 tests\n"
            "test math::test_ok ... ok\n"
            "test math::test_div ... FAILED\n\n"
            "failures:\n\n"
            "---- math::test_div stdout ----\n"
            "thread 'math::test_div' panicked at 'attempt to divide by zero', src/math.rs:25:5\n\n"
            "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        summary, failures = runner.parse_diagnostics("cargo", output)
        assert summary.total == 2
        assert summary.passed == 1
        assert summary.failed == 1
        assert len(failures) == 1
        assert failures[0].test_id == "math::test_div"
        assert "attempt to divide by zero" in failures[0].message
        assert "src/math.rs:25:5" in (failures[0].location or "")

    def test_jest_nested_describes_and_multiline_diffs(self):
        runner = MultiEcosystemTestRunner()
        output = (
            "FAIL src/services/auth/oauth.test.ts\n"
            "  ● Authentication Module › OAuth 2.0 Flow › Refresh Token › rejects expired refresh tokens\n\n"
            "    expect(received).toEqual(expected) // deep equality\n\n"
            "    - Expected  - 1\n"
            "    + Received  + 1\n\n"
            "      Object {\n"
            "    -   \"statusCode\": 401,\n"
            "    +   \"statusCode\": 500,\n"
            "      }\n\n"
            "      at Object.<anonymous> (src/services/auth/oauth.test.ts:88:24)\n\n"
            "Test Suites: 1 failed, 3 passed, 4 total\n"
            "Tests:       1 failed, 2 skipped, 12 passed, 15 total\n"
            "Snapshots:   0 total\n"
            "Time:        3.456 s\n"
        )
        summary, failures = runner.parse_diagnostics("jest", output)
        assert summary.total == 15
        assert summary.passed == 12
        assert summary.failed == 1
        assert summary.skipped == 2

        assert len(failures) == 1
        assert "OAuth 2.0 Flow › Refresh Token › rejects expired refresh tokens" in failures[0].test_id
        assert "src/services/auth/oauth.test.ts:88:24" in (failures[0].location or "")
        assert "expect(received).toEqual(expected)" in failures[0].message

    def test_jest_standard_order_summary(self):
        """Standard order: failed, passed, skipped, total."""
        runner = MultiEcosystemTestRunner()
        output = (
            "FAIL src/auth.test.ts\n"
            "  ● Auth › test 1\n"
            "    Error: fail\n"
            "      at Object.<anonymous> (src/auth.test.ts:10:5)\n"
            "Tests: 1 failed, 9 passed, 1 skipped, 11 total\n"
        )
        summary, failures = runner.parse_diagnostics("jest", output)
        assert summary.total == 11
        assert summary.passed == 9
        assert summary.failed == 1
        assert summary.skipped == 1

    def test_go_subtests_and_parallel_failures(self):
        runner = MultiEcosystemTestRunner()
        output = (
            "=== RUN   TestRouter\n"
            "=== RUN   TestRouter/GET_/api/v1/health\n"
            "=== RUN   TestRouter/POST_/api/v1/users_invalid_payload\n"
            "--- FAIL: TestRouter (0.05s)\n"
            "    --- PASS: TestRouter/GET_/api/v1/health (0.01s)\n"
            "    --- FAIL: TestRouter/POST_/api/v1/users_invalid_payload (0.02s)\n"
            "        router_test.go:78: expected HTTP 400 Bad Request, got HTTP 500 Internal Server Error\n"
            "=== RUN   TestDatabase\n"
            "--- PASS: TestDatabase (0.01s)\n"
            "FAIL\n"
            "FAIL\tgithub.com/example/api/v1\t0.082s\n"
        )
        summary, failures = runner.parse_diagnostics("go", output)
        assert summary.passed == 2  # TestRouter/GET and TestDatabase
        assert summary.failed == 2  # TestRouter and TestRouter/POST
        assert len(failures) >= 1

    def test_unittest_subtest_and_multiple_failure_blocks(self):
        runner = MultiEcosystemTestRunner()
        output = (
            "FAIL: test_validate_email (tests.test_validation.ValidationTestCase.test_validate_email) [email='bad@']\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            '  File "C:\\work\\tests\\test_validation.py", line 45, in test_validate_email\n'
            "    self.assertTrue(is_valid_email(email))\n"
            "AssertionError: False is not true\n\n"
            "ERROR: test_database_connection (tests.test_validation.ValidationTestCase.test_database_connection)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            '  File "C:\\work\\tests\\test_validation.py", line 90, in test_database_connection\n'
            "    connect_db()\n"
            "ConnectionRefusedError: [Errno 111] Connection refused\n\n"
            "----------------------------------------------------------------------\n"
            "Ran 10 tests in 0.120s\n\n"
            "FAILED (failures=1, errors=1, skipped=2)\n"
        )
        summary, failures = runner.parse_diagnostics("unittest", output)
        assert summary.total == 10
        assert summary.passed == 6
        assert summary.failed == 1
        assert summary.errors == 1
        assert summary.skipped == 2

        assert len(failures) == 2
        assert "test_validate_email" in failures[0].test_id
        assert "False is not true" in failures[0].message
        assert "tests/test_validation.py:45" in (failures[0].location or "")

        assert "test_database_connection" in failures[1].test_id
        assert "ConnectionRefusedError" in failures[1].message


# ============================================================================
# Vector 4: Edge Cases in 13-Tier Detection
# ============================================================================

class TestDetectionMatrixEdgeCases:
    """Stress tests 13-tier detection against ambiguous, empty, conflicting, and malformed files."""

    def test_conflicting_lockfiles_strict_priority(self, tmp_path):
        """Verifies uv.lock (Tier 2) beats poetry.lock (Tier 3), package.json (Tier 6), and Cargo.toml (Tier 7)."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "uv.lock").write_text("", encoding="utf-8")
        (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "Cargo.toml").write_text("", encoding="utf-8")

        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"
        assert cmd == ["uv", "run", "pytest", "-v"]

    def test_empty_virtualenv_falls_through_gracefully(self, tmp_path):
        """An empty .venv directory without pytest should fall through to subsequent tiers."""
        runner = MultiEcosystemTestRunner()
        # Empty .venv
        (tmp_path / ".venv").mkdir()
        # Cargo.toml present
        (tmp_path / "Cargo.toml").write_text('[package]\nname="test"\n', encoding="utf-8")

        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "cargo"
        assert cmd == ["cargo", "test"]

    def test_empty_makefile_or_makefile_without_test_target(self, tmp_path):
        """Makefile without test: target should NOT trigger make framework."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "Makefile").write_text("all:\n\tgcc main.c -o main\nclean:\n\trm main\n", encoding="utf-8")

        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "none"
        assert cmd == []

    def test_makefile_lowercase_and_gnumakefile_with_test_target(self, tmp_path):
        """Tests makefile (lowercase) and GNUmakefile detection."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "GNUmakefile").write_text("test:\n\tpytest\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "make"
        assert cmd == ["make", "test"]

    def test_gradle_kotlin_dsl_detection(self, tmp_path):
        """Tests build.gradle.kts detection."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "build.gradle.kts").write_text("tasks.test { useJUnitPlatform() }\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "gradle"
        assert "test" in cmd

    def test_dotnet_fsproj_detection(self, tmp_path):
        """Tests .fsproj F# project detection."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "App.fsproj").write_text("<Project Sdk='Microsoft.NET.Sdk'></Project>\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "dotnet"
        assert cmd == ["dotnet", "test"]

    def test_malformed_pyproject_toml_does_not_crash(self, tmp_path):
        """Malformed or binary pyproject.toml should be handled gracefully without exception."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "pyproject.toml").write_bytes(b"\x00\xff\xfe\x12\x34[invalid toml == ??")

        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "none"
        assert cmd == []

    def test_empty_tests_directory_without_test_files(self, tmp_path):
        """A tests/ directory containing only text/log files should fall back to unittest or none."""
        runner = MultiEcosystemTestRunner()
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "notes.txt").write_text("No python test files here", encoding="utf-8")

        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw in ("unittest", "none")

    @pytest.mark.asyncio
    async def test_workspace_validation_errors(self, tmp_path):
        """Tests invalid workspace paths (empty, non-existent, file)."""
        runner = MultiEcosystemTestRunner()

        # Empty workspace
        r1 = await runner.run_tests("")
        assert r1.status == "error"
        assert "cannot be empty" in (r1.error_details or "")

        # Non-existent workspace
        r2 = await runner.run_tests(str(tmp_path / "nonexistent_dir_999"))
        assert r2.status == "error"
        assert "not found" in (r2.error_details or "")

        # File instead of directory
        f = tmp_path / "file.txt"
        f.write_text("hello", encoding="utf-8")
        r3 = await runner.run_tests(str(f))
        assert r3.status == "error"
        assert "not a directory" in (r3.error_details or "")


# ============================================================================
# Vector 5: Windows Quoting, Shell Injection & Custom Command Execution
# ============================================================================

class TestWindowsQuotingAndCommandSecurity:
    """Stress tests custom command argument splitting and execution safety on Windows."""

    def test_split_command_args_complex_windows_quotes(self):
        cmd = r'pytest -k "test_foo and not test_bar" -m "unit or integration" --log-cli-level=INFO'
        args = split_command_args(cmd)
        assert args == [
            "pytest",
            "-k",
            "test_foo and not test_bar",
            "-m",
            "unit or integration",
            "--log-cli-level=INFO",
        ]

    def test_split_command_args_paths_with_spaces_and_backslashes(self):
        cmd = r'"C:\Program Files\Python\python.exe" "C:\My Work\tests\test auth.py"'
        args = split_command_args(cmd)
        assert args == [
            r"C:\Program Files\Python\python.exe",
            r"C:\My Work\tests\test auth.py",
        ]

    def test_split_command_args_empty_and_whitespace(self):
        assert split_command_args("") == []
        assert split_command_args("   \t\n  ") == []

    @pytest.mark.asyncio
    async def test_shell_injection_characters_treated_as_literal_arguments(self, tmp_path):
        """Verifies shell chaining metacharacters (&, &&, |, ;, `) are NOT evaluated by a shell."""
        runner = MultiEcosystemTestRunner()
        script = tmp_path / "arg_printer.py"
        script.write_text(
            "import sys\n"
            "print('ARGS:', sys.argv[1:])\n",
            encoding="utf-8",
        )

        test_cmd = f'"{sys.executable}" "{script}" & echo INJECTED_EXECUTION'
        res = await runner.run_tests(str(tmp_path), test_command=test_cmd)

        assert res.status == "passed"
        assert "ARGS:" in res.output
        assert "&" in res.output
        assert "INJECTED_EXECUTION" in res.output

    @pytest.mark.asyncio
    async def test_windows_builtin_cmd_execution(self, tmp_path):
        """Verifies built-in commands like echo/dir are wrapped with cmd.exe /c properly on Windows."""
        runner = MultiEcosystemTestRunner()
        res = await runner.run_tests(str(tmp_path), test_command="echo MCP_AGY_WINDOWS_TEST_PASS")
        assert res.status == "passed"
        assert "MCP_AGY_WINDOWS_TEST_PASS" in res.output

    @pytest.mark.asyncio
    async def test_nonexistent_executable_fails_fast_without_hanging(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        start = time.monotonic()
        res = await runner.run_tests(str(tmp_path), test_command="absolutely_nonexistent_binary_12345 --opt")
        elapsed = time.monotonic() - start

        assert elapsed < 1.0
        assert res.status == "error"
        assert res.exit_code == 127
        assert "Executable not found on PATH" in (res.error_details or "")
