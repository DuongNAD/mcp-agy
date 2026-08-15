"""FastMCP AGY Test Suite.

Contains comprehensive E2E test suites and fixtures across all 4 testing tiers:
- Tier 1: Feature Coverage (agy_execute_task, agy_chat, agy_get_diff, agy_run_tests)
- Tier 2: Boundary & Corner Cases (timeouts, path traversals, git unborn/binary states)
- Tier 3: Cross-Feature Combinations (multi-step workflows, locking, lifecycle)
- Tier 4: Real-World Scenarios (subprocess stdio, multi-language ecosystems)
"""

__version__ = "0.1.0"
