"""Comprehensive Unit and Integration Tests for MultiEcosystemTestRunner."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import pytest

from mcp_agy.core.models import TestFailure, TestRunResult, TestSummary
from mcp_agy.core.test_runner import (
    _PYTEST_SUMMARY_LINE,
    MultiEcosystemTestRunner,
    execute_workspace_tests,
    split_command_args,
    strip_ansi_codes,
)


class TestHelperFunctions:
    def test_strip_ansi_codes(self):
        colored_text = "\x1b[32mPASS\x1b[0m \x1b[31;1mFAIL\x1b[0m\x1b]0;Title\x07\x1b[2K"
        clean = strip_ansi_codes(colored_text)
        assert clean == "PASS FAIL"
        assert "\x1b" not in clean

    def test_split_command_args(self):
        if os.name == "nt":
            args = split_command_args('pytest "tests/test auth.py" -v')
            assert args == ["pytest", "tests/test auth.py", "-v"]
        else:
            args = split_command_args('pytest "tests/test auth.py" -v')
            assert args == ["pytest", "tests/test auth.py", "-v"]


class TestFrameworkDetectionMatrix:
    def test_detect_virtualenv_pytest(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        scripts_name = "Scripts" if os.name == "nt" else "bin"
        venv_scripts = tmp_path / ".venv" / scripts_name
        venv_scripts.mkdir(parents=True)
        pytest_name = "pytest.exe" if os.name == "nt" else "pytest"
        pytest_bin = venv_scripts / pytest_name
        pytest_bin.write_text("#!/bin/sh\n", encoding="utf-8")

        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"
        assert str(pytest_bin) in cmd[0] or pytest_name in cmd[0]

    def test_detect_uv_lock(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "uv.lock").write_text("", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"
        assert cmd == ["uv", "run", "pytest", "-v"]

    def test_detect_uv_pyproject_toml(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "pyproject.toml").write_text('[tool.uv]\ndev-dependencies = ["pytest"]\n', encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"
        assert cmd == ["uv", "run", "pytest", "-v"]

    def test_detect_poetry_lock(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"
        assert cmd == ["poetry", "run", "pytest", "-v"]

    def test_detect_system_pytest_config(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"
        assert sys.executable in cmd[0]
        assert "pytest" in cmd

    def test_detect_pytest_in_tests_dir(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_something.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"
        assert "pytest" in cmd

    def test_rust_crate_with_tests_dir_detects_cargo_not_unittest(self, tmp_path):
        """A Rust crate keeps its integration tests in tests/*.rs and must still detect cargo.

        Regression: Tier 5 fired on the bare existence of a tests/ directory, so a real crate
        (Cargo.toml beside tests/*.rs) was reported as `unittest`. `unittest discover` then
        found no Python, exited 0, and the crate's entire Rust suite reported as an empty pass
        while Tier 7 Cargo was never reached.
        """
        runner = MultiEcosystemTestRunner()
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "crate_under_test"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "lib.rs").write_text("pub fn f() -> u8 { 1 }\n", encoding="utf-8")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "integration_stress.rs").write_text(
            "#[test]\nfn works() { assert_eq!(1, 1); }\n", encoding="utf-8"
        )
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "cargo", f"Rust crate misdetected as {fw!r}"
        assert cmd == ["cargo", "test"]

    def test_go_module_with_tests_dir_is_not_unittest(self, tmp_path):
        """Verifies the same bare-tests-dir trap does not swallow a Go module."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.22\n", encoding="utf-8")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "main_test.go").write_text("package tests\n", encoding="utf-8")
        fw, _ = runner.detect_framework(str(tmp_path))
        assert fw == "go", f"Go module misdetected as {fw!r}"

    def test_tests_dir_holding_python_still_detects_unittest(self, tmp_path):
        """Verifies the fix does not cost a genuine unittest project its detection.

        Only `.py` files that are neither `test_*` nor `*_test` are present, so Tier 4 pytest
        does not claim it and Tier 5 unittest is the correct answer.
        """
        runner = MultiEcosystemTestRunner()
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "suite_alpha.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def runTest(self): pass\n",
            encoding="utf-8",
        )
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "unittest"
        assert cmd[-2:] == ["-s", "tests"]

    def test_empty_tests_dir_falls_through_to_later_tiers(self, tmp_path):
        """Verifies an empty tests/ directory no longer masquerades as a Python suite."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "tests").mkdir()
        (tmp_path / "notes.txt").write_text("nothing here", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "none"
        assert cmd == []

    def test_detect_python_unittest(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_suite.py").write_text("import unittest\n", encoding="utf-8")
        # System pytest config or test_*.py will trigger pytest if present, but here pytest will trigger unless we test unittest discover
        # Pytest Tier 4 takes precedence if test_*.py is in tests/, but if only test_suite.py is in tests/, Tier 5 is reached
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw in ("pytest", "unittest")

    def test_detect_node_pnpm(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
        (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pnpm"
        assert cmd == ["pnpm", "test"]

    def test_detect_node_yarn(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
        (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "yarn"
        assert cmd == ["yarn", "test"]

    def test_detect_node_bun(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
        (tmp_path / "bun.lockb").write_text("", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "bun"
        assert cmd == ["bun", "test"]

    def test_detect_node_npm_default(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "npm"
        assert cmd == ["npm", "test"]

    def test_detect_cargo(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "my_rust_pkg"\n', encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "cargo"
        assert cmd == ["cargo", "test"]

    def test_detect_go(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "go.mod").write_text("module my_go_pkg\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "go"
        assert cmd == ["go", "test", "-v", "./..."]

    def test_detect_gradle(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "gradle"
        assert "test" in cmd

    def test_detect_maven(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "maven"
        assert "test" in cmd

    def test_detect_dotnet(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "app.csproj").write_text("<Project></Project>\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "dotnet"
        assert cmd == ["dotnet", "test"]

    def test_detect_ruby(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "ruby"
        assert cmd == ["bundle", "exec", "rspec"]

    def test_detect_makefile(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "make"
        assert cmd == ["make", "test"]

    def test_detect_pytest_from_root_level_test_file(self, tmp_path):
        """Tests beside the code, with no tests/ dir and no config, are still a pytest project.

        This is the layout AGY produces when told to "add a test" to a small project. Missing
        it reports no_framework_detected and the architect concludes there are no tests at all.
        """
        runner = MultiEcosystemTestRunner()
        (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (tmp_path / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
        )
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"
        assert sys.executable in cmd[0]
        assert "pytest" in cmd

    def test_detect_pytest_from_root_level_suffix_naming(self, tmp_path):
        """Verifies the `<name>_test.py` spelling is detected at the root, matching tests/ handling."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "calc_test.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        fw, _ = runner.detect_framework(str(tmp_path))
        assert fw == "pytest"

    def test_root_level_non_test_python_files_are_not_pytest(self, tmp_path):
        """Verifies ordinary .py files do not masquerade as a test suite.

        `latest_snapshot.py` ends in neither prefix nor suffix and must not trigger pytest.
        """
        runner = MultiEcosystemTestRunner()
        (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (tmp_path / "latest_snapshot.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "contest_results.py").write_text("y = 2\n", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "none"
        assert cmd == []

    def test_root_level_test_dir_is_not_mistaken_for_a_test_file(self, tmp_path):
        """Verifies a *directory* named test_foo.py does not register as a pytest file."""
        runner = MultiEcosystemTestRunner()
        (tmp_path / "test_decoy.py").mkdir()
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "none"
        assert cmd == []

    def test_no_framework_detected(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        (tmp_path / "notes.txt").write_text("Hello world", encoding="utf-8")
        fw, cmd = runner.detect_framework(str(tmp_path))
        assert fw == "none"
        assert cmd == []


class TestDiagnosticsParsers:
    def test_parse_pytest_summary_and_failures(self):
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "============================= test session starts =============================\n"
            "FAILED tests/test_auth.py::test_login - AssertionError: 401 != 200\n"
            "__________________________________ test_login __________________________________\n"
            "tests/test_auth.py:42: in test_login\n"
            "    assert res.status == 200\n"
            "E   AssertionError: 401 != 200\n"
            "=================== 1 failed, 5 passed, 1 skipped, 1 error in 0.50s ===================\n"
        )
        summary, failures = runner.parse_diagnostics("pytest", sample_output)
        assert summary.total == 8
        assert summary.passed == 5
        assert summary.failed == 1
        assert summary.skipped == 1
        assert summary.errors == 1
        assert len(failures) == 1
        assert failures[0].test_id == "tests/test_auth.py::test_login"
        assert "AssertionError" in failures[0].message
        assert "tests/test_auth.py" in (failures[0].location or "")

    def test_parse_cargo_diagnostics(self):
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "running 4 tests\n"
            "test calc::test_ok ... ok\n"
            "test calc::test_overflow ... FAILED\n\n"
            "failures:\n\n"
            "---- calc::test_overflow stdout ----\n"
            "thread 'calc::test_overflow' panicked at 'assertion failed: `(left == right)`', src/calc.rs:15:9\n"
            "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n\n"
            "test result: FAILED. 3 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.05s\n"
        )
        summary, failures = runner.parse_diagnostics("cargo", sample_output)
        assert summary.passed == 3
        assert summary.failed == 1
        assert len(failures) == 1
        assert failures[0].test_id == "calc::test_overflow"
        assert "assertion failed" in failures[0].message
        assert "src/calc.rs:15:9" in (failures[0].location or "")

    def test_custom_sniffer_routes_cargo_output_containing_a_banner(self):
        """A `=== banner ===` in cargo output must not divert it to the pytest parser.

        Regression, reproduced against liva-native-core (which prints
        "=== 100-Turn Interruption Stress Test Results ==="): the sniffer led with
        `"=== " in text and "passed" in text`, and cargo's own summary line always contains
        "passed", so 35 passing tests were reported as 21 passed plus a phantom error.
        """
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "=== 100-Turn Interruption Stress Test Results ===\n"
            "Max Preemption Latency: 84.5us\n"
            "=================================================\n"
            "test result: ok. 21 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.37s\n"
            "test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.99s\n"
            "test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 89.65s\n"
        )
        summary, _ = runner.parse_diagnostics("custom", sample_output)
        assert summary.passed == 35, "cargo output was not parsed by the cargo parser"
        assert summary.errors == 0, "phantom error invented by the wrong parser"
        assert summary.total == 35

    def test_custom_sniffer_still_recognizes_real_pytest_output(self):
        """Verifies tightening the pytest marker did not cost pytest its own detection."""
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "============================= test session starts =============================\n"
            "collected 3 items\n\n"
            "tests/test_a.py ..F                                                      [100%]\n\n"
            "=========================== short test summary info ===========================\n"
            "FAILED tests/test_a.py::test_c - assert 1 == 2\n"
            "========================= 1 failed, 2 passed in 0.31s =========================\n"
        )
        summary, _ = runner.parse_diagnostics("custom", sample_output)
        assert summary.passed == 2
        assert summary.failed == 1

    def test_custom_sniffer_ignores_a_decorative_banner_alone(self):
        """A banner with no outcome counts is not a pytest summary line."""
        runner = MultiEcosystemTestRunner()
        assert not _PYTEST_SUMMARY_LINE.search("=== Benchmark Results ===\n=== Done ===\n")
        assert _PYTEST_SUMMARY_LINE.search("==== 5 passed in 0.12s ====")

    def test_custom_sniffer_recognizes_quiet_mode_pytest_output(self):
        """`pytest -q` prints its counts with no surrounding rule.

        Regression, reproduced against this repo's own suite: only the banner form was
        matched, so a custom `test_command` carrying -q fell through to the generic parser,
        which found the word "fail" inside an unrelated pydantic warning. A clean 22-passed
        run came back as passed=1 beside failed=1, contradicting its own exit code of 0.
        """
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "......................                                                   [100%]\n"
            "============================== warnings summary ===============================\n"
            "pydantic_settings/sources/utils.py:47: IncompleteFieldDefinitionWarning: Field\n"
            "  'lifespan' has an incomplete definition: its annotation contains an unresolved\n"
            "  forward reference, so settings sources may fail to correctly resolve its value.\n"
            "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
            "22 passed, 1 warning in 6.69s\n"
        )
        summary, _ = runner.parse_diagnostics("custom", sample_output)
        assert summary.passed == 22, "quiet-mode pytest output was not routed to the pytest parser"
        assert summary.failed == 0, "prose in a warning was read as a failure"
        assert summary.total == 22

    def test_custom_sniffer_reads_quiet_output_of_a_run_past_a_minute(self):
        """Past 60s pytest appends a clock reading after the duration."""
        runner = MultiEcosystemTestRunner()
        summary, _ = runner.parse_diagnostics(
            "custom", "380 passed, 1 warning in 61.00s (0:01:01)\n"
        )
        assert summary.passed == 380
        assert summary.total == 380

    def test_generic_parser_ignores_the_word_fail_in_prose(self):
        """An unknown runner's verdict is shouted; prose that says "fail" is not a verdict."""
        runner = MultiEcosystemTestRunner()
        summary, _ = runner.parse_diagnostics(
            "custom", "starting run\nthe cache may fail to warm up\nAll checks OK\n"
        )
        assert summary.passed == 1
        assert summary.failed == 0

    def test_generic_parser_leaves_an_ambiguous_verdict_to_the_exit_code(self):
        """Both markers at once is not a verdict: an empty summary defers to run_tests.

        The old branch set passed=1 and failed=1 together under total=1, so the counts did
        not even add up to their own total.
        """
        runner = MultiEcosystemTestRunner()
        summary, _ = runner.parse_diagnostics("custom", "PASSED stage one\nFAILED stage two\n")
        assert summary.total == 0
        assert summary.passed == 0
        assert summary.failed == 0

    def test_vitest_counts_are_read_not_guessed(self):
        """Vitest writes its total in parentheses and omits Jest's colon and "N total".

        Captured live from `npx vitest run` against E:\\Project\\LIVA\\liva-ui: none of the Jest
        patterns match this, so a real 27-test run fell through to the generic parser and came
        back as `total: 1, passed: 1` - true about the outcome, silent about the suite.
        """
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "RUN  v4.1.5 E:/Project/LIVA/liva-ui\n"
            "\n"
            " x tests/composables/useGateway.test.ts (27 tests) 191ms\n"
            "\n"
            " Test Files  1 passed (1)\n"
            "      Tests  27 passed (27)\n"
            "   Start at  23:21:22\n"
            "   Duration  15.28s (transform 111ms, setup 686ms)\n"
        )
        summary, _ = runner.parse_diagnostics("custom", sample_output)
        assert summary.total == 27, "vitest output was not routed to the jest/vitest parser"
        assert summary.passed == 27
        assert summary.failed == 0

    def test_vitest_failure_counts_are_split_correctly(self):
        """A mixed vitest run separates its counts with a pipe, not a comma."""
        runner = MultiEcosystemTestRunner()
        summary, _ = runner.parse_diagnostics(
            "npm",
            " Test Files  1 failed | 3 passed (4)\n"
            "      Tests  2 failed | 1 skipped | 24 passed (27)\n",
        )
        assert summary.total == 27
        assert summary.passed == 24
        assert summary.failed == 2
        assert summary.skipped == 1

    def test_jest_output_still_parses_after_the_vitest_branch(self):
        """The Jest form must keep working: its summary has a colon and an explicit total."""
        runner = MultiEcosystemTestRunner()
        summary, _ = runner.parse_diagnostics(
            "npm",
            "Test Suites: 1 failed, 4 passed, 5 total\n"
            "Tests:       3 failed, 2 skipped, 45 passed, 50 total\n",
        )
        assert summary.total == 50
        assert summary.passed == 45
        assert summary.failed == 3
        assert summary.skipped == 2

    def test_parse_cargo_aggregates_all_test_binaries(self):
        """One `test result:` line is emitted per test binary and all of them must be counted.

        Regression, reproduced against liva-native-core: a run of four integration targets
        totalling 43 tests reported summary.total == 21, because only the first line was read.
        """
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "     Running tests\\vad_adversarial_stress.rs (target\\release\\deps\\vad-c9ba.exe)\n"
            "test result: ok. 21 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.16s\n"
            "     Running tests\\anti_hallucination_adversarial_stress.rs (target\\release\\deps\\ah-ee49.exe)\n"
            "test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.54s\n"
            "     Running tests\\stt_adversarial_stress.rs (target\\release\\deps\\stt-4b83.exe)\n"
            "test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 73.25s\n"
            "     Running tests\\duplex_bargein_stress.rs (target\\release\\deps\\duplex-5510.exe)\n"
            "test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.26s\n"
        )
        summary, failures = runner.parse_diagnostics("cargo", sample_output)
        assert summary.passed == 43
        assert summary.failed == 0
        assert summary.total == 43
        assert failures == []

    def test_parse_cargo_counts_failures_in_a_later_binary(self):
        """A later suite's failures must surface even when the first suite passed.

        This is the dangerous shape of the bug: reading only the leading `ok` line reported
        `failed: 0` next to a failed run, so an architect agent concluded nothing broke.
        """
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "     Running tests\\suite_alpha.rs (target\\debug\\deps\\alpha.exe)\n"
            "test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.10s\n"
            "     Running tests\\suite_beta.rs (target\\debug\\deps\\beta.exe)\n"
            "test beta::slow_path ... FAILED\n\n"
            "failures:\n\n"
            "---- beta::slow_path stdout ----\n"
            "thread 'beta::slow_path' panicked at 'took 817ms (>200ms limit)', tests/suite_beta.rs:74:5\n\n"
            "test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.88s\n"
        )
        summary, failures = runner.parse_diagnostics("cargo", sample_output)
        assert summary.failed == 1, "a failure in a later test binary was dropped"
        assert summary.passed == 7
        assert summary.total == 8
        assert len(failures) == 1
        assert failures[0].test_id == "beta::slow_path"

    def test_parse_cargo_sums_ignored_and_filtered_across_binaries(self):
        """Verifies skipped counts aggregate too, rather than reporting only the first binary."""
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "test result: ok. 3 passed; 0 failed; 2 ignored; 0 measured; 1 filtered out; finished in 0.10s\n"
            "test result: ok. 4 passed; 0 failed; 1 ignored; 0 measured; 3 filtered out; finished in 0.20s\n"
        )
        summary, _ = runner.parse_diagnostics("cargo", sample_output)
        assert summary.passed == 7
        assert summary.skipped == 7  # (2+1) ignored + (1+3) filtered
        assert summary.total == 14

    def test_parse_jest_diagnostics(self):
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "FAIL src/auth.test.ts\n"
            "  ● Auth Module › should reject invalid password\n\n"
            "    expect(received).toBe(expected) // Object.is equality\n\n"
            "    Expected: 401\n"
            "    Received: 200\n\n"
            "      at Object.<anonymous> (src/auth.test.ts:25:21)\n\n"
            "Test Suites: 1 failed, 1 total\n"
            "Tests:       1 failed, 4 passed, 5 total\n"
            "Snapshots:   0 total\n"
            "Time:        1.245 s\n"
        )
        summary, failures = runner.parse_diagnostics("npm", sample_output)
        assert summary.total == 5
        assert summary.passed == 4
        assert summary.failed == 1
        assert len(failures) == 1
        assert "Auth Module › should reject invalid password" in failures[0].test_id
        assert "src/auth.test.ts" in (failures[0].location or "")

    def test_parse_go_diagnostics(self):
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "=== RUN   TestAdd\n"
            "--- PASS: TestAdd (0.00s)\n"
            "=== RUN   TestDivide\n"
            "--- FAIL: TestDivide (0.01s)\n"
            "    calc_test.go:42: expected error on division by zero, got nil\n"
            "FAIL\n"
            "FAIL\tpkg/calc\t0.035s\n"
        )
        summary, failures = runner.parse_diagnostics("go", sample_output)
        assert summary.passed == 1
        assert summary.failed == 1
        assert len(failures) == 1
        assert failures[0].test_id == "TestDivide"
        assert "calc_test.go:42" in (failures[0].location or "")

    def test_parse_unittest_diagnostics(self):
        runner = MultiEcosystemTestRunner()
        sample_output = (
            "FAIL: test_division (tests.test_math.MathTests)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            '  File "tests/test_math.py", line 18, in test_division\n'
            "    self.assertEqual(divide(10, 2), 4)\n"
            "AssertionError: 5.0 != 4\n\n"
            "----------------------------------------------------------------------\n"
            "Ran 4 tests in 0.003s\n\n"
            "FAILED (failures=1)\n"
        )
        summary, failures = runner.parse_diagnostics("unittest", sample_output)
        assert summary.total == 4
        assert summary.passed == 3
        assert summary.failed == 1
        assert len(failures) == 1
        assert "test_division" in failures[0].test_id
        assert "AssertionError" in failures[0].message


class TestExecutionEnvironmentAndProcessSafety:
    @pytest.mark.asyncio
    async def test_ansi_escape_code_stripping(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        script = tmp_path / "print_color.py"
        script.write_text("import sys; sys.stdout.write('\\x1b[32mPASS\\x1b[0m \\x1b[31mFAIL\\x1b[0m\\n')\n", encoding="utf-8")
        res = await runner.run_tests(str(tmp_path), test_command=f'"{sys.executable}" print_color.py')
        assert "\x1b" not in res.output
        assert "PASS FAIL" in res.output

    @pytest.mark.asyncio
    async def test_sanitized_environment_variables(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        script = tmp_path / "check_env.py"
        script.write_text(
            "import os\n"
            "print(f'NO_COLOR={os.environ.get(\"NO_COLOR\")}')\n"
            "print(f'CI={os.environ.get(\"CI\")}')\n"
            "print(f'PYTHONUNBUFFERED={os.environ.get(\"PYTHONUNBUFFERED\")}')\n",
            encoding="utf-8",
        )
        res = await runner.run_tests(str(tmp_path), test_command=f'"{sys.executable}" check_env.py')
        assert "NO_COLOR=1" in res.output
        assert "CI=true" in res.output
        assert "PYTHONUNBUFFERED=1" in res.output

    @pytest.mark.asyncio
    async def test_hanging_test_timeout_and_process_kill(self, hanging_test_workspace):
        runner = MultiEcosystemTestRunner()
        res = await runner.run_tests(
            str(hanging_test_workspace),
            test_command=f'"{sys.executable}" hang.py',
            timeout_seconds=1,
        )
        assert res.status == "timeout"
        assert res.exit_code == -9
        assert res.error_details is not None
        assert "timed out" in res.error_details.lower()

    @pytest.mark.asyncio
    async def test_missing_executable_command(self, tmp_path):
        runner = MultiEcosystemTestRunner()
        res = await runner.run_tests(str(tmp_path), test_command="nonexistent_binary_tool_xyz")
        assert res.status == "error"
        assert res.exit_code == 127
        assert "Executable not found on PATH" in (res.error_details or "")

    @pytest.mark.asyncio
    async def test_path_resolved_executable_is_spawned_by_absolute_path(self, tmp_path, monkeypatch):
        captured: dict[str, list[str]] = {}

        async def _fake_run(cmd, cwd, env, timeout_seconds):
            captured["cmd"] = list(cmd)
            return 0, "", ""

        monkeypatch.setattr("mcp_agy.core.test_runner.run_subprocess_async", _fake_run)
        runner = MultiEcosystemTestRunner()
        await runner.run_tests(str(tmp_path), test_command="git --version")

        assert os.path.isabs(captured["cmd"][0])
        assert captured["cmd"][1:] == ["--version"]

    @pytest.mark.asyncio
    async def test_workspace_relative_executable_is_spawned_by_absolute_path(self, tmp_path, monkeypatch):
        """An executable that exists only under the workspace must still spawn.

        Regression: CreateProcess resolves a relative executable against the server's own
        working directory and ignores the `cwd=` handed to the child, so a caller-supplied
        `.venv\\Scripts\\python.exe` - confirmed on disk by this very branch - reached
        create_subprocess_exec unchanged and died with [WinError 2].
        """
        bin_name = "Scripts" if os.name == "nt" else "bin"
        bin_dir = tmp_path / ".venv" / bin_name
        bin_dir.mkdir(parents=True)
        fake_exe = bin_dir / ("python.exe" if os.name == "nt" else "python")
        fake_exe.write_text("", encoding="utf-8")
        relative_cmd = os.path.join(".venv", bin_name, fake_exe.name)

        captured: dict[str, list[str]] = {}

        async def _fake_run(cmd, cwd, env, timeout_seconds):
            captured["cmd"] = list(cmd)
            return 0, "", ""

        monkeypatch.setattr("mcp_agy.core.test_runner.run_subprocess_async", _fake_run)
        # Stand somewhere the relative path does not resolve, which is the whole point:
        # only workspace_path gives it meaning.
        monkeypatch.chdir(tmp_path.parent)

        runner = MultiEcosystemTestRunner()
        res = await runner.run_tests(str(tmp_path), test_command=f"{relative_cmd} -m pytest")

        assert res.status != "error", f"relative executable was rejected: {res.error_details}"
        assert os.path.isabs(captured["cmd"][0])
        assert os.path.normcase(captured["cmd"][0]) == os.path.normcase(str(fake_exe))
        assert captured["cmd"][1:] == ["-m", "pytest"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name != "nt", reason="PATHEXT launcher resolution is Windows-specific")
    async def test_cmd_shim_runner_is_spawnable(self, tmp_path, monkeypatch):
        # npm/npx/yarn/pnpm/gradlew are .cmd or .bat shims. shutil.which finds them via PATHEXT
        # but CreateProcess does not, so spawning the bare name fails with [WinError 2].
        shim_dir = tmp_path / "shims"
        shim_dir.mkdir()
        (shim_dir / "faketestrunner.cmd").write_text("@echo off\r\necho SHIM_RAN\r\n", encoding="ascii")
        monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

        runner = MultiEcosystemTestRunner()
        res = await runner.run_tests(str(tmp_path), test_command="faketestrunner")

        assert res.status == "passed", res.error_details
        assert "SHIM_RAN" in res.output

    @pytest.mark.asyncio
    async def test_no_framework_detected_workspace(self, no_framework_workspace):
        runner = MultiEcosystemTestRunner()
        res = await runner.run_tests(str(no_framework_workspace))
        assert res.status == "no_framework_detected"
        assert res.framework == "none"

    @pytest.mark.asyncio
    async def test_execute_workspace_tests_convenience_function(self, pytest_passing_workspace):
        res = await execute_workspace_tests(str(pytest_passing_workspace))
        assert isinstance(res, TestRunResult)
        assert res.status == "passed"
        assert res.summary.passed >= 2
