"""Multi-ecosystem automated test runner and failure diagnostics engine for FastMCP AGY.

Provides 13-tier framework auto-detection (Pytest, Unittest, Node.js, Cargo, Go,
Gradle, Maven, .NET, Ruby, Make), custom command execution, virtualenv discovery,
headless environment sanitization, ANSI code stripping, regex failure diagnostics
parsing, and process tree termination.
"""

from __future__ import annotations

import asyncio
import glob
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time
from typing import Dict, List, Optional, Tuple

from mcp_agy.core.models import TestFailure, TestRunResult, TestSummary
from mcp_agy.utils.logger import get_logger
from mcp_agy.utils.process import run_subprocess_async, terminate_process_tree

logger = get_logger("mcp_agy.core.test_runner")

ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\|(?=\x1b))|[PX^_].*?(?:\x1b\\|(?=\x1b))|[@-Z\\_]|\[)"
)

# pytest's real terminal summary line: a full-width rule whose text carries an outcome count,
# e.g. "==== 5 passed in 0.12s ====" or "=== 1 failed, 2 passed, 1 skipped in 0.5s ===".
# Matching the whole shape rather than a bare "=== " substring keeps an ordinary decorative
# banner in some other framework's output from being read as a pytest run.
_PYTEST_SUMMARY_LINE = re.compile(
    r"^=+.*?\b\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b.*?=+$",
    re.MULTILINE,
)

# Under `-q` pytest drops the surrounding rule and prints the counts bare, e.g.
# "22 passed, 1 warning in 6.69s". Only the banner form was recognised, so a custom
# `test_command` carrying -q fell through to the generic parser: a 22-passed run came back
# as passed=1 alongside failed=1, because the generic scan found the word "fail" inside an
# unrelated pydantic warning ("settings sources may fail to correctly resolve its value").
# Anchored on the leading count and pytest's trailing duration - which it always appends,
# with an extra "(0:01:05)" once a run passes a minute - so prose cannot match.
_PYTEST_QUIET_SUMMARY_LINE = re.compile(
    r"^\s*\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b"
    r"[^\n]*\bin\s+[\d.]+s\b[^\n]*$",
    re.MULTILINE,
)

# Verdict markers for the last-resort generic parser, matched case-sensitively: an unknown
# runner shouts its result ("FAILED", "PASS"), while ordinary prose in a warning or traceback
# says "fail" in lower case. The old case-insensitive scan could not tell the two apart.
_GENERIC_FAILURE_TOKEN = re.compile(r"\b(?:FAIL|FAILED|FAILURE|FAILURES|ERROR|ERRORS)\b")
_GENERIC_PASS_TOKEN = re.compile(r"\b(?:PASS|PASSED|SUCCESS|OK)\b")


def strip_ansi_codes(text: str) -> str:
    """Strip terminal ANSI CSI, OSC, and control escape sequences from text."""
    if not text:
        return ""
    return ANSI_ESCAPE_PATTERN.sub("", text)


def split_command_args(cmd_str: str) -> List[str]:
    """Split command string into arguments list cross-platform."""
    if not cmd_str or not cmd_str.strip():
        return []
    if os.name == "nt":
        # On Windows, preserve backslashes and strip surrounding quotes
        raw_parts = shlex.split(cmd_str, posix=False)
        return [part.strip('\'"') for part in raw_parts if part.strip('\'"')]
    return shlex.split(cmd_str, posix=True)


class MultiEcosystemTestRunner:
    """Universal multi-language test suite runner and failure diagnostics engine."""

    def __init__(self, default_timeout_seconds: int = 300) -> None:
        self.default_timeout_seconds = default_timeout_seconds

    def _discover_virtualenv_bin(self, workspace_path: str) -> Optional[Tuple[str, str]]:
        """Discover virtualenv directory inside workspace and return (bin_dir, venv_root)."""
        candidates = [".venv", "venv", "env", ".env"]
        for cand in candidates:
            cand_path = os.path.join(workspace_path, cand)
            if os.path.isdir(cand_path):
                if os.name == "nt":
                    scripts_dir = os.path.join(cand_path, "Scripts")
                    if os.path.isdir(scripts_dir):
                        return scripts_dir, cand_path
                else:
                    bin_dir = os.path.join(cand_path, "bin")
                    if os.path.isdir(bin_dir):
                        return bin_dir, cand_path
        return None

    def _build_sanitized_env(self, workspace_path: str) -> Dict[str, str]:
        """Construct sanitized execution environment with virtualenv PATH prepending."""
        env = os.environ.copy()

        # Headless and color sanitization
        env["NO_COLOR"] = "1"
        env["FORCE_COLOR"] = "0"
        env["TERM"] = "dumb"
        env["CI"] = "true"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTEST_ADDOPTS"] = "--color=no"
        env["CARGO_TERM_COLOR"] = "never"
        env["NPM_CONFIG_COLOR"] = "false"
        env["RUST_BACKTRACE"] = "1"

        # Virtualenv discovery and PATH injection
        venv_info = self._discover_virtualenv_bin(workspace_path)
        if venv_info:
            bin_dir, venv_root = venv_info
            current_path = env.get("PATH", "")
            env["PATH"] = f"{bin_dir}{os.pathsep}{current_path}"
            env["VIRTUAL_ENV"] = venv_root
            env.pop("PYTHONHOME", None)

        return env

    def detect_framework(self, workspace_path: str) -> Tuple[str, List[str]]:
        """Detect the appropriate test framework and formulate test execution command.

        Evaluates the workspace against the 13-tier detection matrix in strict order.

        Returns:
            Tuple of (framework_name, command_args_list).
        """
        norm_ws = os.path.normpath(os.path.abspath(workspace_path))
        if not os.path.isdir(norm_ws):
            return "none", []

        # Tier 1: Virtualenv Pytest
        venv_info = self._discover_virtualenv_bin(norm_ws)
        if venv_info:
            bin_dir, _ = venv_info
            pytest_exe = os.path.join(bin_dir, "pytest.exe" if os.name == "nt" else "pytest")
            if os.path.isfile(pytest_exe):
                return "pytest", [pytest_exe, "-v"]

        # Tier 2: uv Pytest
        uv_lock = os.path.join(norm_ws, "uv.lock")
        pyproject = os.path.join(norm_ws, "pyproject.toml")
        has_uv = os.path.isfile(uv_lock)
        if not has_uv and os.path.isfile(pyproject):
            try:
                content = Path(pyproject).read_text(encoding="utf-8", errors="ignore")
                if "[tool.uv" in content or "uv.sources" in content:
                    has_uv = True
            except Exception:
                pass
        if has_uv:
            return "pytest", ["uv", "run", "pytest", "-v"]

        # Tier 3: Poetry Pytest
        poetry_lock = os.path.join(norm_ws, "poetry.lock")
        if os.path.isfile(poetry_lock):
            return "pytest", ["poetry", "run", "pytest", "-v"]

        # Tier 4: System Pytest (pytest configs, tox, setup.cfg, or tests/ directory)
        pytest_configs = ["pytest.ini", "setup.cfg", "tox.ini", "conftest.py"]
        has_pytest_config = any(os.path.isfile(os.path.join(norm_ws, cfg)) for cfg in pytest_configs)
        if not has_pytest_config and os.path.isfile(pyproject):
            try:
                content = Path(pyproject).read_text(encoding="utf-8", errors="ignore")
                if "[tool.pytest" in content or "pytest" in content:
                    has_pytest_config = True
            except Exception:
                pass

        tests_dir = os.path.join(norm_ws, "tests")
        has_pytest_files = False
        if os.path.isdir(tests_dir):
            for root, _, files in os.walk(tests_dir):
                if any(f.startswith("test_") and f.endswith(".py") or f.endswith("_test.py") for f in files):
                    has_pytest_files = True
                    break

        # Tests living beside the code, with no tests/ dir and no config file, is an ordinary
        # small-project layout — and one AGY produces on its own when told to "add a test".
        # Without this the workspace reports no_framework_detected and the architect concludes
        # the project has no tests, while test_*.py sits in the root.
        if not has_pytest_files:
            try:
                has_pytest_files = any(
                    entry.is_file()
                    and entry.name.endswith(".py")
                    and (entry.name.startswith("test_") or entry.name.endswith("_test.py"))
                    for entry in os.scandir(norm_ws)
                )
            except OSError:
                pass

        if has_pytest_config or has_pytest_files:
            return "pytest", [sys.executable, "-m", "pytest", "-v"]

        # Tier 5: Python Unittest.
        #
        # A bare `tests/` directory is not evidence of a Python project: Rust puts its
        # integration tests in `tests/*.rs`, and this tier used to claim any workspace that
        # had the directory at all. A real crate (liva-native-core: Cargo.toml beside
        # tests/*.rs) was reported as `unittest`, `unittest discover` found no Python and
        # exited clean, and Tier 7 Cargo below was never reached - so the crate's whole Rust
        # suite silently reported as an empty pass. Require actual Python in tests/.
        has_python_in_tests_dir = False
        if os.path.isdir(tests_dir):
            for _root, _dirs, files in os.walk(tests_dir):
                if any(f.endswith(".py") for f in files):
                    has_python_in_tests_dir = True
                    break

        if os.path.isfile(os.path.join(norm_ws, "tests", "test_suite.py")) or has_python_in_tests_dir:
            return "unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests"]

        # Tier 6: Node.js Test Runners (package.json + lockfiles)
        pkg_json = os.path.join(norm_ws, "package.json")
        if os.path.isfile(pkg_json):
            if os.path.isfile(os.path.join(norm_ws, "pnpm-lock.yaml")):
                return "pnpm", ["pnpm", "test"]
            if os.path.isfile(os.path.join(norm_ws, "yarn.lock")):
                return "yarn", ["yarn", "test"]
            if os.path.isfile(os.path.join(norm_ws, "bun.lockb")) or os.path.isfile(os.path.join(norm_ws, "bun.lock")):
                return "bun", ["bun", "test"]
            return "npm", ["npm", "test"]

        # Tier 7: Rust Cargo
        if os.path.isfile(os.path.join(norm_ws, "Cargo.toml")):
            return "cargo", ["cargo", "test"]

        # Tier 8: Go Test
        if os.path.isfile(os.path.join(norm_ws, "go.mod")):
            return "go", ["go", "test", "-v", "./..."]
        # Check for go files
        for root, _, files in os.walk(norm_ws):
            if any(f.endswith(".go") for f in files):
                return "go", ["go", "test", "-v", "./..."]
            break

        # Tier 9: Gradle
        has_gradle = os.path.isfile(os.path.join(norm_ws, "build.gradle")) or os.path.isfile(os.path.join(norm_ws, "build.gradle.kts"))
        if has_gradle:
            if os.name == "nt" and os.path.isfile(os.path.join(norm_ws, "gradlew.bat")):
                return "gradle", [os.path.join(norm_ws, "gradlew.bat"), "test"]
            if os.path.isfile(os.path.join(norm_ws, "gradlew")):
                return "gradle", ["./gradlew", "test"]
            return "gradle", ["gradle", "test"]

        # Tier 10: Maven
        if os.path.isfile(os.path.join(norm_ws, "pom.xml")):
            if os.name == "nt" and os.path.isfile(os.path.join(norm_ws, "mvnw.cmd")):
                return "maven", [os.path.join(norm_ws, "mvnw.cmd"), "test"]
            if os.path.isfile(os.path.join(norm_ws, "mvnw")):
                return "maven", ["./mvnw", "test"]
            return "maven", ["mvn", "test"]

        # Tier 11: .NET
        sln_files = glob.glob(os.path.join(norm_ws, "*.sln")) + glob.glob(os.path.join(norm_ws, "*.csproj")) + glob.glob(os.path.join(norm_ws, "*.fsproj"))
        if sln_files:
            return "dotnet", ["dotnet", "test"]

        # Tier 12: Ruby
        if os.path.isfile(os.path.join(norm_ws, "Gemfile")) or os.path.isfile(os.path.join(norm_ws, ".rspec")):
            return "ruby", ["bundle", "exec", "rspec"]
        if os.path.isfile(os.path.join(norm_ws, "Rakefile")):
            return "ruby", ["rake", "test"]

        # Tier 13: Makefile
        for make_name in ["Makefile", "makefile", "GNUmakefile"]:
            make_path = os.path.join(norm_ws, make_name)
            if os.path.isfile(make_path):
                try:
                    make_content = Path(make_path).read_text(encoding="utf-8", errors="ignore")
                    if re.search(r"^test\s*:", make_content, re.MULTILINE):
                        return "make", ["make", "test"]
                except Exception:
                    pass

        return "none", []

    def parse_diagnostics(self, framework: str, output: str) -> Tuple[TestSummary, List[TestFailure]]:
        """Extract structured summary counts and granular failure records from test output."""
        clean_text = strip_ansi_codes(output)

        fw = framework.lower()

        # Handle custom sniffer.
        #
        # Ordered by marker specificity, most definitive first. The pytest branch used to lead
        # on `"=== " in text and "passed" in text`, and cargo's own summary line always contains
        # the word "passed" - so any project that printed a `=== banner ===` anywhere in its test
        # output had its cargo results handed to the pytest parser. Reproduced against
        # liva-native-core, whose suite prints "=== 100-Turn Interruption Stress Test Results ===":
        # 35 passing tests came back as 21 passed plus a phantom error.
        if fw == "custom":
            if "test result:" in clean_text:
                fw = "cargo"
            elif "--- FAIL:" in clean_text or "--- PASS:" in clean_text:
                fw = "go"
            elif (
                "Test Suites:" in clean_text
                or re.search(r"^Tests:\s", clean_text, re.MULTILINE)
                # Vitest's own summary line, which carries no colon: "Tests  27 passed (27)"
                or re.search(r"^[ \t]*Tests[ \t]+[^\n(]*\(\d+\)[ \t]*$", clean_text, re.MULTILINE)
            ):
                fw = "npm"
            elif re.search(r"^Ran \d+ tests? in ", clean_text, re.MULTILINE):
                fw = "unittest"
            elif _PYTEST_SUMMARY_LINE.search(clean_text) or _PYTEST_QUIET_SUMMARY_LINE.search(clean_text):
                fw = "pytest"

        if fw in ("pytest", "uv", "poetry"):
            return self._parse_pytest_diagnostics(clean_text)
        elif fw == "cargo":
            return self._parse_cargo_diagnostics(clean_text)
        elif fw in ("npm", "pnpm", "yarn", "bun", "jest", "vitest"):
            return self._parse_jest_diagnostics(clean_text)
        elif fw == "go":
            return self._parse_go_diagnostics(clean_text)
        elif fw == "unittest":
            return self._parse_unittest_diagnostics(clean_text)

        return self._parse_generic_diagnostics(clean_text)

    def _parse_pytest_diagnostics(self, text: str) -> Tuple[TestSummary, List[TestFailure]]:
        """Parse Pytest summary line and detailed failure tracebacks."""
        passed = 0
        failed = 0
        skipped = 0
        errors = 0

        # Summary line regex: e.g. "=== 1 failed, 5 passed, 1 skipped in 0.50s ==="
        m_pass = re.search(r"(\d+)\s+passed", text)
        if m_pass:
            passed = int(m_pass.group(1))

        m_fail = re.search(r"(\d+)\s+failed", text)
        if m_fail:
            failed = int(m_fail.group(1))

        m_skip = re.search(r"(\d+)\s+skipped", text)
        if m_skip:
            skipped = int(m_skip.group(1))

        m_xfail = re.search(r"(\d+)\s+xfail(?:ed)?", text)
        if m_xfail:
            skipped += int(m_xfail.group(1))

        m_xpass = re.search(r"(\d+)\s+xpass(?:ed)?", text)
        if m_xpass:
            passed += int(m_xpass.group(1))

        m_err = re.search(r"(\d+)\s+error(?:s)?\b", text)
        if m_err:
            errors = int(m_err.group(1))

        total = passed + failed + skipped + errors

        # Extract detailed traceback sections: e.g. "___ test_name ___ ... ___"
        failures: List[TestFailure] = []
        detailed_blocks: Dict[str, Tuple[Optional[str], str, str]] = {}  # test_header -> (location, msg, traceback)

        tb_pattern = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}$([\s\S]*?)(?=^_{3,}|\n={3,}|\Z)", re.MULTILINE)
        for match in tb_pattern.finditer(text):
            header = match.group(1).strip()
            body = match.group(2).strip()

            location = None
            loc_match = re.search(r"^([^\n:]+:\d+):", body, re.MULTILINE)
            if loc_match:
                location = loc_match.group(1)

            message = ""
            e_lines = re.findall(r"^E\s+(.*)", body, re.MULTILINE)
            if e_lines:
                message = "\n".join(e_lines)
            else:
                last_line = [l for l in body.splitlines() if l.strip()]
                message = last_line[-1] if last_line else "Assertion or runtime error"

            detailed_blocks[header] = (location, message, body)

        # Match short failure lines: e.g. "FAILED tests/test_auth.py::test_login - AssertionError: 401 != 200"
        short_pattern = re.compile(
            r"^(?:FAILED|ERROR)\s+(\S+::\S+|\S+)(?:\s+-\s+(.*))?$",
            re.MULTILINE,
        )
        seen_test_ids = set()
        for match in short_pattern.finditer(text):
            test_id = match.group(1).strip()
            short_msg = match.group(2).strip() if match.group(2) else ""
            seen_test_ids.add(test_id)

            # Try to match detailed block by test function name or full id
            fn_name = test_id.split("::")[-1]
            location = None
            traceback_str = None
            msg = short_msg

            if fn_name in detailed_blocks:
                loc, d_msg, tb = detailed_blocks[fn_name]
                location = loc
                traceback_str = tb
                if not msg:
                    msg = d_msg
            elif test_id in detailed_blocks:
                loc, d_msg, tb = detailed_blocks[test_id]
                location = loc
                traceback_str = tb
                if not msg:
                    msg = d_msg
            else:
                # Search partial
                for h_name, (loc, d_msg, tb) in detailed_blocks.items():
                    h_as_colon = h_name.replace(".", "::")
                    if (
                        h_name == fn_name
                        or h_name in test_id
                        or h_as_colon in test_id
                        or fn_name in h_name
                        or test_id.endswith(h_name)
                    ):
                        location = loc
                        traceback_str = tb
                        if not msg:
                            msg = d_msg
                        break

            if not location:
                # Infer location from test_id: e.g. "tests/test_math.py::test_add" -> "tests/test_math.py"
                location = test_id.split("::")[0]

            failures.append(
                TestFailure(
                    test_id=test_id,
                    message=msg or "Test failed",
                    location=location,
                    traceback=traceback_str,
                )
            )

        # If failures were in detailed blocks but not in short lines
        if not failures and detailed_blocks:
            for header, (loc, d_msg, tb) in detailed_blocks.items():
                failures.append(
                    TestFailure(
                        test_id=header,
                        message=d_msg or "Test failed",
                        location=loc,
                        traceback=tb,
                    )
                )

        summary = TestSummary(
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=errors,
        )
        return summary, failures

    def _parse_cargo_diagnostics(self, text: str) -> Tuple[TestSummary, List[TestFailure]]:
        """Parse Rust Cargo test results and failure blocks."""
        passed = 0
        failed = 0
        skipped = 0
        errors = 0

        # Pattern: "test result: FAILED. 4 passed; 2 failed; 1 ignored; 0 measured; 0 filtered out"
        #
        # One line PER TEST BINARY. A crate with several integration targets - or any run of
        # `cargo test`, which also builds unit, doc and each tests/*.rs target - emits several.
        # This used to `re.search` and report only the first, so a real 4-suite run of 43 tests
        # came back as 21. The dangerous shape is a first suite that passes and a later one that
        # fails: the summary then reads `failed: 0` beside a failed status, and an architect
        # agent reading the counts concludes nothing broke. Sum every line instead.
        matches = list(
            re.finditer(
                r"test result:\s+(?:ok|FAILED)\.\s+(\d+)\s+passed;\s+(\d+)\s+failed;\s+(\d+)\s+ignored;\s+(\d+)\s+measured;\s*(\d+)\s+filtered out",
                text,
            )
        )
        if matches:
            for m_res in matches:
                passed += int(m_res.group(1))
                failed += int(m_res.group(2))
                skipped += int(m_res.group(3)) + int(m_res.group(5))
        else:
            m_pass = re.findall(r"test\s+([^\s]+)\s+\.\.\.\s+ok", text)
            m_fail = re.findall(r"test\s+([^\s]+)\s+\.\.\.\s+FAILED", text)
            passed = len(m_pass)
            failed = len(m_fail)

        total = passed + failed + skipped + errors

        failures: List[TestFailure] = []
        # Extract individual failure blocks: "---- test_name stdout ----"
        tb_pattern = re.compile(
            r"----\s+([^\s]+)\s+stdout\s+----\n([\s\S]*?)(?=(?:----\s+[^\s]+\s+stdout\s+----|\nfailures:|\ntest result:|\Z))"
        )
        for match in tb_pattern.finditer(text):
            test_id = match.group(1).strip()
            body = match.group(2).strip()

            location = None
            msg = "Cargo test assertion failed"

            # Panic info: panicked at 'message', src/lib.rs:25:9
            panic_m = re.search(r"panicked at (['\"])(.*?)\1,\s*([^\n]+)", body, re.DOTALL)
            if panic_m:
                msg = panic_m.group(2).strip()
                location = panic_m.group(3).strip()

            failures.append(
                TestFailure(
                    test_id=test_id,
                    message=msg,
                    location=location,
                    traceback=body,
                )
            )

        # Fallback to failures list: "failures:\n    calc::test_div_zero\n    auth::test_token"
        if not failures and failed > 0:
            m_list = re.search(r"failures:\n((?:\s+[^\s\n]+\n)+)", text)
            if m_list:
                for line in m_list.group(1).splitlines():
                    t_id = line.strip()
                    if t_id and not t_id.startswith("----"):
                        failures.append(
                            TestFailure(
                                test_id=t_id,
                                message="Cargo test failed",
                                location=None,
                                traceback=None,
                            )
                        )

        summary = TestSummary(
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=errors,
        )
        return summary, failures

    def _parse_jest_diagnostics(self, text: str) -> Tuple[TestSummary, List[TestFailure]]:
        """Parse Jest / Vitest test output."""
        passed = 0
        failed = 0
        skipped = 0
        errors = 0
        total = 0

        # Vitest drops Jest's colon and its "N total", writing the total in parentheses instead:
        # "Tests  27 passed (27)", or "Tests  2 failed | 25 passed (27)" when something broke.
        # Matched before the Jest form because a vitest run has no line the Jest patterns can
        # read - a real 27-test run came back as the generic parser's "1 passed", which tells
        # the architect the suite ran but not that it ran *27 tests*.
        m_vitest = re.search(r"^[ \t]*Tests[ \t]+([^\n(]*)\((\d+)\)[ \t]*$", text, re.MULTILINE)
        if m_vitest:
            body, total_text = m_vitest.group(1), m_vitest.group(2)
            for count_text, label in re.findall(
                r"(\d+)\s+(passed|failed|skipped|todo|pending)", body
            ):
                count = int(count_text)
                if label == "passed":
                    passed = count
                elif label == "failed":
                    failed = count
                else:
                    skipped += count
            total = int(total_text)
            return (
                TestSummary(
                    total=total, passed=passed, failed=failed, skipped=skipped, errors=errors
                ),
                self._parse_jest_failure_blocks(text),
            )

        # Pattern: "Tests: 1 failed, 2 skipped, 12 passed, 15 total" (order-independent)
        m_tests_line = re.search(r"Tests:\s+([^\n]+)", text)
        if m_tests_line:
            line = m_tests_line.group(1)
            m_fail = re.search(r"(\d+)\s+failed", line)
            m_pass = re.search(r"(\d+)\s+passed", line)
            m_skip = re.search(r"(\d+)\s+(?:skipped|pending|todo)", line)
            m_tot = re.search(r"(\d+)\s+total", line)
            if m_fail:
                failed = int(m_fail.group(1))
            if m_pass:
                passed = int(m_pass.group(1))
            if m_skip:
                skipped = int(m_skip.group(1))
            if m_tot:
                total = int(m_tot.group(1))
            else:
                total = passed + failed + skipped
        else:
            m_suites = re.search(
                r"Test Suites:\s+(?:(\d+)\s+failed,?\s*)?(?:(\d+)\s+passed,?\s*)?(\d+)\s+total",
                text,
            )
            if m_suites:
                failed = int(m_suites.group(1)) if m_suites.group(1) else 0
                passed = int(m_suites.group(2)) if m_suites.group(2) else 0
                total = int(m_suites.group(3)) if m_suites.group(3) else (passed + failed)

        failures = self._parse_jest_failure_blocks(text)

        summary = TestSummary(
            total=total or (passed + failed + skipped + errors),
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=errors,
        )
        return summary, failures

    def _parse_jest_failure_blocks(self, text: str) -> List[TestFailure]:
        """Extract the per-failure blocks Jest and Vitest both mark with a bullet."""
        failures: List[TestFailure] = []
        # Pattern: "● Auth Module › fails with bad password"
        block_pattern = re.compile(r"●\s+([^\n]+)\n([\s\S]*?)(?=(?:\n\s*●|\nTest Suites:|\nTests:|\Z))")
        for match in block_pattern.finditer(text):
            test_id = match.group(1).strip()
            body = match.group(2).strip()

            location = None
            loc_m = re.search(r"at (?:.+ \()?([a-zA-Z0-9_\-\.\/\\]+\.(?:ts|js|jsx|tsx):\d+(?::\d+)?)\)?", body)
            if loc_m:
                location = loc_m.group(1).replace("\\", "/")

            msg_lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("at ")]
            msg = msg_lines[0] if msg_lines else "Jest/Vitest assertion failed"

            failures.append(
                TestFailure(
                    test_id=test_id,
                    message=msg,
                    location=location,
                    traceback=body,
                )
            )
        return failures

    def _parse_go_diagnostics(self, text: str) -> Tuple[TestSummary, List[TestFailure]]:
        """Parse Go test output."""
        passes = re.findall(r"^\s*--- PASS:\s+([^\s\(]+)", text, re.MULTILINE)
        fails = re.findall(r"^\s*--- FAIL:\s+([^\s\(]+)", text, re.MULTILINE)
        skips = re.findall(r"^\s*--- SKIP:\s+([^\s\(]+)", text, re.MULTILINE)

        passed = len(passes)
        failed = len(fails)
        skipped = len(skips)
        total = passed + failed + skipped

        failures: List[TestFailure] = []
        tb_pattern = re.compile(
            r"^\s*--- FAIL:\s+([^\s\(]+)(?:\s+\([^\)]+\))?\n([\s\S]*?)(?=(?:^\s*=== RUN|^\s*--- PASS|^\s*--- FAIL|^\s*--- SKIP|^FAIL\b|^ok\b|\Z))",
            re.MULTILINE,
        )
        for match in tb_pattern.finditer(text):
            test_id = match.group(1).strip()
            body = match.group(2).strip()

            location = None
            msg = "Go test failed"

            loc_m = re.search(r"^\s*([a-zA-Z0-9_\-\.\/\\]+\.go:\d+):(?:\s*(.*))?", body, re.MULTILINE)
            if loc_m:
                location = loc_m.group(1).replace("\\", "/")
                if loc_m.group(2):
                    msg = loc_m.group(2).strip()

            failures.append(
                TestFailure(
                    test_id=test_id,
                    message=msg,
                    location=location,
                    traceback=body,
                )
            )

        summary = TestSummary(
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=0,
        )
        return summary, failures

    def _parse_unittest_diagnostics(self, text: str) -> Tuple[TestSummary, List[TestFailure]]:
        """Parse Python standard library unittest output."""
        total = 0
        failed = 0
        errors = 0
        skipped = 0

        # Pattern: "Ran 5 tests in 0.012s"
        m_ran = re.search(r"Ran (\d+) tests? in ([\d\.]+)s", text)
        if m_ran:
            total = int(m_ran.group(1))

        # Pattern: "FAILED (failures=1, errors=1, skipped=1)"
        m_failed = re.search(r"FAILED\s+\((?:failures=(\d+))?,?\s*(?:errors=(\d+))?,?\s*(?:skipped=(\d+))?\)", text)
        if m_failed:
            failed = int(m_failed.group(1)) if m_failed.group(1) else 0
            errors = int(m_failed.group(2)) if m_failed.group(2) else 0
            skipped = int(m_failed.group(3)) if m_failed.group(3) else 0
        else:
            m_ok_skip = re.search(r"OK\s+\(skipped=(\d+)\)", text)
            if m_ok_skip:
                skipped = int(m_ok_skip.group(1))

        passed = max(0, total - failed - errors - skipped)

        failures: List[TestFailure] = []
        tb_pattern = re.compile(
            r"(?:^|\n)(FAIL|ERROR):\s+([^\n]+)\n-+\n([\s\S]*?)(?=(?:\n(?:FAIL|ERROR):|\n={3,}|\n-+\nRan |\Z))"
        )
        for match in tb_pattern.finditer(text):
            raw_id = match.group(2).strip()
            body = match.group(3).strip()

            # e.g. "test_auth (test_pkg.TestAuth)" -> "test_pkg.TestAuth.test_auth"
            test_id = raw_id
            m_id = re.match(r"([^\s]+)\s+\(([^\)]+)\)", raw_id)
            if m_id:
                test_id = f"{m_id.group(2)}.{m_id.group(1)}"

            location = None
            loc_matches = re.findall(r'File "([^"]+)", line (\d+)', body)
            if loc_matches:
                file_part = loc_matches[-1][0].replace("\\", "/")
                line_num = loc_matches[-1][1]
                location = f"{file_part}:{line_num}"

            msg = "Unittest assertion or runtime error"
            last_lines = [l.strip() for l in body.splitlines() if l.strip()]
            if last_lines:
                msg = last_lines[-1]

            failures.append(
                TestFailure(
                    test_id=test_id,
                    message=msg,
                    location=location,
                    traceback=body,
                )
            )

        summary = TestSummary(
            total=total or (passed + failed + errors + skipped),
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=errors,
        )
        return summary, failures

    def _parse_generic_diagnostics(self, text: str) -> Tuple[TestSummary, List[TestFailure]]:
        """Fallback generic diagnostics parser."""
        failures: List[TestFailure] = []

        err_matches = re.findall(r"(?:AssertionError|Error|FATAL|Exception|panic):\s*(.*)", text)
        for err_msg in err_matches[:10]:
            failures.append(
                TestFailure(
                    test_id="custom",
                    message=err_msg.strip() or "Generic failure",
                    location=None,
                    traceback=None,
                )
            )

        # A runner states its verdict where it finishes, so read the tail rather than the whole
        # transcript: the body carries warnings, tracebacks and log lines whose wording has
        # nothing to say about the outcome.
        tail = "\n".join([line for line in text.splitlines() if line.strip()][-10:])

        has_failure_text = bool(_GENERIC_FAILURE_TOKEN.search(tail))
        has_pass_text = bool(_GENERIC_PASS_TOKEN.search(tail))

        if has_failure_text and not has_pass_text:
            summary = TestSummary(total=1, passed=0, failed=1, skipped=0, errors=0)
        elif has_pass_text and not has_failure_text:
            summary = TestSummary(total=1, passed=1, failed=0, skipped=0, errors=0)
        else:
            # Both markers present, or neither: an unknown runner's output carries no verdict
            # this parser can trust. An empty summary hands the call to run_tests, which fills
            # it from the process exit code - the one signal here that is not a guess. The old
            # branch instead invented total=1 with passed=1 and failed=1 set at once, so a
            # clean run reported a failure beside its own success.
            summary = TestSummary(total=0, passed=0, failed=0, skipped=0, errors=0)

        return summary, failures

    async def run_tests(
        self,
        workspace_path: str,
        test_command: str = "",
        timeout_seconds: int = 300,
    ) -> TestRunResult:
        """Run workspace test suite asynchronously and return structured execution result."""
        start_time = time.monotonic()
        effective_timeout = timeout_seconds if timeout_seconds > 0 else self.default_timeout_seconds

        # 1. Workspace validation
        if not workspace_path or not workspace_path.strip():
            return TestRunResult(
                status="error",
                exit_code=1,
                framework="none",
                test_command_executed=test_command,
                output="",
                summary=TestSummary(errors=1),
                failures=[],
                duration_seconds=0.0,
                error_details="Workspace path cannot be empty or whitespace only.",
            )

        norm_workspace = os.path.normpath(os.path.abspath(workspace_path))
        if not os.path.exists(norm_workspace):
            return TestRunResult(
                status="error",
                exit_code=1,
                framework="none",
                test_command_executed=test_command,
                output="",
                summary=TestSummary(errors=1),
                failures=[],
                duration_seconds=0.0,
                error_details=f"Workspace directory not found: {norm_workspace}",
            )

        if not os.path.isdir(norm_workspace):
            return TestRunResult(
                status="error",
                exit_code=1,
                framework="none",
                test_command_executed=test_command,
                output="",
                summary=TestSummary(errors=1),
                failures=[],
                duration_seconds=0.0,
                error_details=f"Workspace path is not a directory: {norm_workspace}",
            )

        # 2. Formulate command and framework
        if test_command and test_command.strip():
            framework = "custom"
            cmd_args = split_command_args(test_command.strip())
            executed_cmd_str = test_command.strip()
        else:
            framework, cmd_args = self.detect_framework(norm_workspace)
            if framework == "none" or not cmd_args:
                return TestRunResult(
                    status="no_framework_detected",
                    exit_code=0,
                    framework="none",
                    test_command_executed="",
                    output="",
                    summary=TestSummary(),
                    failures=[],
                    duration_seconds=round(time.monotonic() - start_time, 4),
                    error_details=None,
                )
            executed_cmd_str = " ".join(cmd_args)

        # 3. Environment preparation
        env = self._build_sanitized_env(norm_workspace)

        # 4. Check executable existence on PATH
        bin_target = cmd_args[0]
        if os.name == "nt" and bin_target.lower() in ("echo", "type", "dir", "copy", "del", "move", "mkdir", "rmdir", "cls", "rem", "call"):
            cmd_args = ["cmd.exe", "/c"] + cmd_args
            bin_target = "cmd.exe"

        # If not an explicit path that exists on disk, check PATH.
        #
        # A path that exists must still be made absolute before it is spawned. CreateProcess
        # resolves a relative executable against the *server's* working directory and ignores
        # the `cwd=` handed to the child, so a caller-supplied `.venv\Scripts\python.exe` - which
        # this branch just confirmed on disk under the workspace - died with [WinError 2] the
        # moment it reached create_subprocess_exec. Architect agents reach for exactly that
        # relative form when they want a workspace's own interpreter.
        ws_relative_bin = os.path.join(norm_workspace, bin_target)
        if os.path.isfile(bin_target):
            cmd_args[0] = os.path.abspath(bin_target)
        elif os.path.isfile(ws_relative_bin):
            cmd_args[0] = os.path.abspath(ws_relative_bin)
        else:
            resolved_bin = shutil.which(bin_target, path=env.get("PATH"))
            if not resolved_bin:
                duration = time.monotonic() - start_time
                return TestRunResult(
                    status="error",
                    exit_code=127,
                    framework=framework,
                    test_command_executed=executed_cmd_str,
                    output="",
                    summary=TestSummary(errors=1),
                    failures=[],
                    duration_seconds=round(duration, 4),
                    error_details=f"Executable not found on PATH: '{bin_target}'",
                )
            # Spawn the resolved path, not the bare name. shutil.which applies PATHEXT and finds
            # npm.cmd / yarn.cmd / gradlew.bat, but CreateProcess does not, so passing the bare
            # name to create_subprocess_exec fails with [WinError 2] for every .cmd/.bat runner.
            cmd_args[0] = resolved_bin

        # 5. Subprocess execution
        try:
            exit_code, stdout, stderr = await run_subprocess_async(
                cmd=cmd_args,
                cwd=norm_workspace,
                env=env,
                timeout_seconds=float(effective_timeout),
            )
            duration = time.monotonic() - start_time
            combined_output = strip_ansi_codes((stdout + "\n" + stderr).strip())

            # 6. Parse diagnostics
            summary, failures = self.parse_diagnostics(framework, combined_output)

            # Ensure summary is populated if exit_code == 0 and summary total is 0
            if exit_code == 0:
                if summary.total == 0:
                    summary = TestSummary(total=1, passed=1, failed=0, skipped=0, errors=0)
                status = "passed"
            else:
                if summary.total == 0:
                    summary = TestSummary(total=1, passed=0, failed=1, skipped=0, errors=0)
                status = "failed"

            return TestRunResult(
                status=status,
                exit_code=exit_code,
                framework=framework,
                test_command_executed=executed_cmd_str,
                output=combined_output,
                summary=summary,
                failures=failures,
                duration_seconds=round(duration, 4),
                error_details=None if exit_code == 0 else f"Test run exited with code {exit_code}",
            )

        except asyncio.TimeoutError:
            duration = time.monotonic() - start_time
            return TestRunResult(
                status="timeout",
                exit_code=-9,
                framework=framework,
                test_command_executed=executed_cmd_str,
                output="",
                summary=TestSummary(errors=1),
                failures=[],
                duration_seconds=round(duration, 4),
                error_details=f"Test execution timed out after {effective_timeout} seconds; killed process tree.",
            )
        except Exception as exc:
            duration = time.monotonic() - start_time
            return TestRunResult(
                status="error",
                exit_code=1,
                framework=framework,
                test_command_executed=executed_cmd_str,
                output="",
                summary=TestSummary(errors=1),
                failures=[],
                duration_seconds=round(duration, 4),
                error_details=str(exc),
            )


_default_test_runner = MultiEcosystemTestRunner()


async def execute_workspace_tests(
    workspace_path: str,
    test_command: str = "",
    timeout_seconds: int = 300,
) -> TestRunResult:
    """Convenience function executing workspace tests using default MultiEcosystemTestRunner instance."""
    return await _default_test_runner.run_tests(
        workspace_path=workspace_path,
        test_command=test_command,
        timeout_seconds=timeout_seconds,
    )
