"""Empirical Analysis and Benchmark Harness for test_runner.py.

This script tests the 5 empirical findings and collects runtime benchmarks.
"""

from __future__ import annotations

import re
import time

def verify_jest_regex():
    text = "Tests:       1 failed, 2 skipped, 12 passed, 15 total"
    m_tests = re.search(
        r"Tests:\s+(?:(\d+)\s+failed,?\s*)?(?:(\d+)\s+passed,?\s*)?(?:(\d+)\s+skipped,?\s*)?(?:(\d+)\s+pending,?\s*)?(?:(\d+)\s+todo,?\s*)?(\d+)\s+total",
        text,
    )
    print("Jest original regex match:", m_tests)
    # Why it failed: '2 skipped' appears before '12 passed', but regex assumes passed before skipped!
    # Flexible regex:
    failed = re.search(r"(\d+)\s+failed", text)
    passed = re.search(r"(\d+)\s+passed", text)
    skipped = re.search(r"(\d+)\s+skipped", text)
    total = re.search(r"(\d+)\s+total", text)
    print(f"Flexible Jest parsing: total={total.group(1) if total else 0}, passed={passed.group(1) if passed else 0}, failed={failed.group(1) if failed else 0}, skipped={skipped.group(1) if skipped else 0}")

def verify_pytest_xpass():
    text = "1 failed, 15 passed, 3 skipped, 2 xfailed, 1 xpass, 1 error"
    m_xpass_old = re.search(r"(\d+)\s+xpassed", text)
    m_xpass_new = re.search(r"(\d+)\s+xpass(?:ed)?\b", text)
    print(f"Pytest xpass old: {m_xpass_old}, new: {m_xpass_new.group(1) if m_xpass_new else None}")

def verify_cargo_panic():
    cargo_body = """thread 'parser::test_json_mismatch' panicked at 'assertion `left == right` failed
  left: {"name": "alpha"}
 right: {"name": "beta"}', crates/parser/src/lib.rs:112:9"""
    panic_m_old = re.search(r"panicked at ['\"](.*?)['\"],\s*([^\n]+)", cargo_body)
    panic_m_new = re.search(r"panicked at ['\"]([\s\S]*?)['\"],\s*([^\n]+)", cargo_body)
    print(f"Cargo panic old: {panic_m_old}")
    print(f"Cargo panic with [\\s\\S]*: msg={repr(panic_m_new.group(1)) if panic_m_new else None}, loc={panic_m_new.group(2) if panic_m_new else None}")

def verify_go_subtests():
    go_text = """=== RUN   TestRouter
=== RUN   TestRouter/GET_/api/v1/health
=== RUN   TestRouter/POST_/api/v1/users_invalid_payload
--- FAIL: TestRouter (0.05s)
    --- PASS: TestRouter/GET_/api/v1/health (0.01s)
    --- FAIL: TestRouter/POST_/api/v1/users_invalid_payload (0.02s)
        router_test.go:78: expected HTTP 400 Bad Request, got HTTP 500 Internal Server Error
=== RUN   TestDatabase
--- PASS: TestDatabase (0.01s)
FAIL"""
    old_passes = re.findall(r"^--- PASS:\s+([^\s\(]+)", go_text, re.MULTILINE)
    new_passes = re.findall(r"^\s*--- PASS:\s+([^\s\(]+)", go_text, re.MULTILINE)
    print(f"Go passes old: {old_passes}")
    print(f"Go passes with \\s*: {new_passes}")

def verify_ansi_redos():
    # Test unclosed OSC sequences
    ANSI_ESCAPE_PATTERN = re.compile(
        r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[@-Z\\_])"
    )
    test_str = ("\x1b]0;unclosed_title_" + "x" * 50) * 1000 + "Normal text"
    t0 = time.monotonic()
    ANSI_ESCAPE_PATTERN.sub("", test_str)
    t1 = time.monotonic()
    print(f"ANSI unclosed OSC stripping time (1000 unclosed sequences): {(t1-t0)*1000:.2f}ms")

if __name__ == "__main__":
    print("=== Empirical Verification ===")
    verify_jest_regex()
    verify_pytest_xpass()
    verify_cargo_panic()
    verify_go_subtests()
    verify_ansi_redos()
