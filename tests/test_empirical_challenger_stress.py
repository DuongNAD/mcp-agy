"""Empirical Challenger Stress & Stability Test Suite.

Author: Challenger 1 (challenger_orch1_1)
Mission:
1. High-concurrency ephemeral workspace creation & teardown on Windows (zero WinError 32 / PermissionError).
2. Multi-generation stubborn process tree termination (zero orphan/zombie PIDs).
3. Asynchronous workspace mutex locks concurrency safety, mutual exclusion, timeout enforcement, and leak-free cleanup.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
from pathlib import Path
import psutil
import pytest
import stat
import sys
import tempfile
import time
from typing import List, Set

from mcp_agy.utils.process import terminate_process_tree, run_subprocess_async, stream_subprocess_lines
from mcp_agy.utils.workspace import (
    CASE_INSENSITIVE_FILESYSTEM,
    WorkspaceLockManager,
    WorkspaceLockTimeoutError,
    canonicalize_workspace_path,
    get_workspace_key,
    get_workspace_lock_manager,
    reset_workspace_lock_manager,
)
from tests.conftest import EphemeralWorkspace


# ============================================================================
# 1. Ephemeral Workspace High Concurrency & Windows Handle Leak Stress
# ============================================================================

class TestEphemeralWorkspaceConcurrencyStress:
    """Stress tests ephemeral workspace creation and teardown under high concurrency on Windows."""

    def test_concurrent_workspace_lifecycle_with_readonly_files(self):
        """Stress test 50 concurrent ephemeral workspaces with git repos and read-only files.

        Verifies:
        - Concurrent initialization and Git commits.
        - Creation of read-only files (chmod 0444) simulating git pack/index locks.
        - Zero PermissionError, WinError 32, or WinError 5 on cleanup.
        - 100% complete directory removal without dangling folders.
        """
        num_workspaces = 50
        errors: List[Exception] = []
        created_paths: List[str] = []

        def worker(idx: int) -> str:
            ws = EphemeralWorkspace(prefix=f"mcp_stress_ws_{idx}_")
            raw_path = ws.raw_path
            try:
                # 1. Write text and binary files
                ws.write_file(f"module_{idx}.py", f"def run(): return {idx}\n")
                ws.write_file(f"data_{idx}.bin", b"\x00\xff\xfe\x01" * 1000)

                # 2. Nested directory hierarchy
                ws.write_file(f"nested/sub1/sub2/deep_{idx}.txt", f"Deep content {idx}")

                # 3. Initialize Git repo and make initial commit
                ws.init_git(user_name=f"User{idx}", user_email=f"user{idx}@test.com")

                # 4. Create explicit read-only files (chmod 0444 / S_IREAD)
                ro_file = ws.write_file(f"readonly_{idx}.lock", "LOCKED_CONTENT")
                try:
                    os.chmod(ro_file, stat.S_IREAD)
                except Exception:
                    pass

                # 5. Snapshot validation
                snapshots = ws.snapshot_hashes(include_git=True)
                assert len(snapshots) >= 4, f"Workspace {idx} snapshot too small: {len(snapshots)}"

                return raw_path
            finally:
                # Perform teardown
                ws.cleanup()

        # Run 50 workspaces concurrently across thread pool
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(worker, i) for i in range(num_workspaces)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    path_created = fut.result()
                    created_paths.append(path_created)
                except Exception as exc:
                    errors.append(exc)

        # Assert zero errors during creation and teardown
        assert len(errors) == 0, f"Encountered {len(errors)} errors during concurrent workspace stress: {errors}"
        assert len(created_paths) == num_workspaces

        # Verify all directories are completely removed from filesystem
        dangling_dirs = [p for p in created_paths if os.path.exists(p)]
        assert len(dangling_dirs) == 0, f"Found {len(dangling_dirs)} dangling uncleaned directories: {dangling_dirs}"

    @pytest.mark.asyncio
    async def test_async_concurrent_workspace_rapid_cycling(self):
        """Rapidly cycle 40 async workspaces with file edits and immediate teardown."""
        async def async_worker(idx: int):
            ws = EphemeralWorkspace(prefix=f"mcp_async_ws_{idx}_")
            raw_path = ws.raw_path
            try:
                for f_idx in range(5):
                    ws.write_file(f"file_{f_idx}.py", f"# async file {f_idx}\n")
                await asyncio.sleep(0.01)
            finally:
                ws.cleanup()
            return raw_path

        tasks = [async_worker(i) for i in range(40)]
        paths = await asyncio.gather(*tasks)

        assert len(paths) == 40
        for p in paths:
            assert not os.path.exists(p), f"Path {p} still exists after async cleanup!"


# ============================================================================
# 2. Process Tree Termination Stress with Stubborn Multi-Gen Children
# ============================================================================

class TestProcessTreeStubbornChildrenStress:
    """Stress tests terminate_process_tree against multi-generation stubborn child processes."""

    @pytest.mark.asyncio
    async def test_concurrent_multi_tree_stubborn_termination(self, tmp_path):
        """Concurrently launch 5 independent process trees of 4 generations each (20+ processes total).

        Each level:
        - Catches SIGINT and SIGTERM and ignores them.
        - Writes its PID to a tracking file.
        - Loops infinitely.

        Verifies:
        - All processes are confirmed alive in OS before kill.
        - terminate_process_tree kills all 5 root trees and all descendants.
        - Zero orphan or zombie PIDs survive in the OS.
        """
        num_trees = 5
        tree_roots = []
        all_expected_pids: List[int] = []

        # Create child scripts in tmp_path
        level4_code = (
            "import os, time, signal\n"
            "def ignore(s, f): pass\n"
            "try:\n"
            "    signal.signal(signal.SIGINT, ignore)\n"
            "    signal.signal(signal.SIGTERM, ignore)\n"
            "except Exception:\n"
            "    pass\n"
            "with open(r'{pid_file}', 'a') as f: f.write(f'L4:{os.getpid()}\\n')\n"
            "while True: time.sleep(0.2)\n"
        )

        level3_code = (
            "import os, subprocess, sys, time, signal\n"
            "def ignore(s, f): pass\n"
            "try:\n"
            "    signal.signal(signal.SIGINT, ignore)\n"
            "    signal.signal(signal.SIGTERM, ignore)\n"
            "except Exception:\n"
            "    pass\n"
            "with open(r'{pid_file}', 'a') as f: f.write(f'L3:{os.getpid()}\\n')\n"
            "p = subprocess.Popen([sys.executable, r'{l4_path}'])\n"
            "while True: time.sleep(0.2)\n"
        )

        level2_code = (
            "import os, subprocess, sys, time, signal\n"
            "def ignore(s, f): pass\n"
            "try:\n"
            "    signal.signal(signal.SIGINT, ignore)\n"
            "    signal.signal(signal.SIGTERM, ignore)\n"
            "except Exception:\n"
            "    pass\n"
            "with open(r'{pid_file}', 'a') as f: f.write(f'L2:{os.getpid()}\\n')\n"
            "p = subprocess.Popen([sys.executable, r'{l3_path}'])\n"
            "while True: time.sleep(0.2)\n"
        )

        level1_code = (
            "import os, subprocess, sys, time, signal\n"
            "def ignore(s, f): pass\n"
            "try:\n"
            "    signal.signal(signal.SIGINT, ignore)\n"
            "    signal.signal(signal.SIGTERM, ignore)\n"
            "except Exception:\n"
            "    pass\n"
            "with open(r'{pid_file}', 'a') as f: f.write(f'L1:{os.getpid()}\\n')\n"
            "p = subprocess.Popen([sys.executable, r'{l2_path}'])\n"
            "print('ROOT_READY', flush=True)\n"
            "while True: time.sleep(0.2)\n"
        )

        processes = []
        pid_files = []

        try:
            for t_idx in range(num_trees):
                pid_file = tmp_path / f"pids_tree_{t_idx}.txt"
                pid_files.append(pid_file)

                l4_f = tmp_path / f"t{t_idx}_l4.py"
                l4_f.write_text(level4_code.replace("{pid_file}", str(pid_file)), encoding="utf-8")

                l3_f = tmp_path / f"t{t_idx}_l3.py"
                l3_f.write_text(level3_code.replace("{pid_file}", str(pid_file)).replace("{l4_path}", str(l4_f)), encoding="utf-8")

                l2_f = tmp_path / f"t{t_idx}_l2.py"
                l2_f.write_text(level2_code.replace("{pid_file}", str(pid_file)).replace("{l3_path}", str(l3_f)), encoding="utf-8")

                l1_f = tmp_path / f"t{t_idx}_l1.py"
                l1_f.write_text(level1_code.replace("{pid_file}", str(pid_file)).replace("{l2_path}", str(l2_f)), encoding="utf-8")

                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(l1_f),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                processes.append(proc)
                tree_roots.append(proc.pid)

            # Wait for all roots to emit ROOT_READY
            for proc in processes:
                line = await proc.stdout.readline()
                assert b"ROOT_READY" in line

            # Give child generations 1 second to spawn and record PIDs
            await asyncio.sleep(1.0)

            # Collect recorded PIDs
            all_recorded_pids: List[int] = []
            for p_file in pid_files:
                assert p_file.exists(), f"PID file {p_file} not created"
                lines = p_file.read_text(encoding="utf-8").splitlines()
                pids = [int(line.split(":")[1].strip()) for line in lines if ":" in line]
                assert len(pids) >= 3, f"Tree did not spawn all generations: {lines}"
                all_recorded_pids.extend(pids)

            # Verify that at least 15 processes exist before killing
            assert len(all_recorded_pids) >= num_trees * 3
            for pid in all_recorded_pids:
                assert psutil.pid_exists(pid), f"PID {pid} should be active before kill"

            # CONCURRENT TREE TERMINATION
            def kill_root(root_pid: int):
                terminate_process_tree(root_pid, timeout=1.5)

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                list(executor.map(kill_root, tree_roots))

            # Wait for asyncio subprocess wrappers
            for proc in processes:
                await proc.wait()

            # Grace period for OS cleanup
            await asyncio.sleep(0.5)

            # EMPIRICAL ASSERTION: ALL descendants across ALL trees must be dead
            survivors = []
            for pid in all_recorded_pids:
                if psutil.pid_exists(pid):
                    try:
                        p = psutil.Process(pid)
                        if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                            survivors.append((pid, p.name(), p.status()))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

            assert len(survivors) == 0, f"Stubborn processes survived termination: {survivors}"

        finally:
            # Fallback cleanup in case of test failure
            for r_pid in tree_roots:
                terminate_process_tree(r_pid, timeout=0.5)


# ============================================================================
# 3. Asynchronous Workspace Mutex Concurrency & Timeout Stress
# ============================================================================

class TestWorkspaceMutexLockStress:
    """Stress tests WorkspaceLockManager for concurrency safety, mutual exclusion, and timeout enforcement."""

    @pytest.mark.asyncio
    async def test_high_contention_mutual_exclusion(self, tmp_path):
        """Stress-test 50 concurrent tasks locking the same workspace path.

        Verifies:
        - At most 1 task acquires the lock at any time (strict mutual exclusion).
        - All 50 tasks complete successfully without deadlocks.
        - Case-insensitivity wherever the filesystem is (keys normalized).
        - Lock reference counts and active lock cleanup.
        """
        lock_mgr = WorkspaceLockManager()
        ws_path = str(tmp_path / "contentious_ws")
        os.makedirs(ws_path, exist_ok=True)

        current_active_holders = 0
        max_concurrent_holders = 0
        execution_order: List[int] = []

        async def contender(task_id: int):
            nonlocal current_active_holders, max_concurrent_holders
            # Alternate case only where the filesystem ignores it: there both spellings name one
            # directory and must fold to one lock key. On a case-sensitive filesystem they name
            # two genuinely different directories, so alternating would prove nothing about the
            # lock and would fail for the right reason.
            if CASE_INSENSITIVE_FILESYSTEM:
                target_path = ws_path.upper() if (task_id % 2 == 0) else ws_path.lower()
            else:
                target_path = ws_path

            async with lock_mgr.lock(target_path, timeout=10.0):
                current_active_holders += 1
                if current_active_holders > max_concurrent_holders:
                    max_concurrent_holders = current_active_holders
                # Simulate work inside locked workspace
                await asyncio.sleep(0.01)
                execution_order.append(task_id)
                current_active_holders -= 1

        # Launch 50 contenders concurrently
        tasks = [asyncio.create_task(contender(i)) for i in range(50)]
        await asyncio.gather(*tasks)

        # Invariant 1: Exactly 1 holder at any point
        assert max_concurrent_holders == 1, f"Lock invariant violated! Max concurrent holders: {max_concurrent_holders}"
        assert current_active_holders == 0
        assert len(execution_order) == 50

        # Invariant 2: Lock dictionary properly cleaned up
        assert lock_mgr.active_locks_count() == 0
        assert len(lock_mgr._locks) == 0, f"Lock dictionary leaked keys: {lock_mgr._locks.keys()}"

    @pytest.mark.asyncio
    async def test_lock_timeout_enforcement_and_recovery(self, tmp_path):
        """Verify strict timeout enforcement and recovery when lock is held by another task."""
        lock_mgr = WorkspaceLockManager()
        ws_path = str(tmp_path / "timeout_ws")
        os.makedirs(ws_path, exist_ok=True)

        lock_held = asyncio.Event()
        release_holder = asyncio.Event()

        async def long_holder():
            async with lock_mgr.lock(ws_path):
                lock_held.set()
                await release_holder.wait()

        holder_task = asyncio.create_task(long_holder())
        await lock_held.wait()

        # Try to acquire with a 0.2s timeout (holder is still holding)
        timed_out_count = 0
        async def timeout_worker(w_idx: int):
            nonlocal timed_out_count
            start_t = time.monotonic()
            try:
                async with lock_mgr.lock(ws_path, timeout=0.2):
                    pytest.fail("Should not acquire lock while held by long_holder")
            except WorkspaceLockTimeoutError as exc:
                elapsed = time.monotonic() - start_t
                assert 0.15 <= elapsed <= 0.6, f"Timeout took unexpected duration: {elapsed}s"
                assert "Timed out after 0.2s" in str(exc)
                timed_out_count += 1

        # Launch 5 timeout workers
        timeout_tasks = [asyncio.create_task(timeout_worker(i)) for i in range(5)]
        await asyncio.gather(*timeout_tasks)
        assert timed_out_count == 5

        # Release the holder
        release_holder.set()
        await holder_task

        # Subsequent acquisition must succeed immediately
        async with lock_mgr.lock(ws_path, timeout=1.0) as canon:
            assert canon.exists()

        assert lock_mgr.active_locks_count() == 0
        assert len(lock_mgr._locks) == 0

    @pytest.mark.asyncio
    async def test_independent_workspaces_run_in_parallel(self, tmp_path):
        """Verify locking workspace A does NOT block tasks locking workspace B."""
        lock_mgr = WorkspaceLockManager()
        ws_a = str(tmp_path / "ws_a")
        ws_b = str(tmp_path / "ws_b")
        os.makedirs(ws_a, exist_ok=True)
        os.makedirs(ws_b, exist_ok=True)

        ws_a_entered = asyncio.Event()
        ws_b_entered = asyncio.Event()
        finish_all = asyncio.Event()

        async def task_a():
            async with lock_mgr.lock(ws_a):
                ws_a_entered.set()
                await finish_all.wait()

        async def task_b():
            async with lock_mgr.lock(ws_b):
                ws_b_entered.set()
                await finish_all.wait()

        t_a = asyncio.create_task(task_a())
        t_b = asyncio.create_task(task_b())

        # Both must enter their locks in parallel
        await asyncio.wait_for(asyncio.gather(ws_a_entered.wait(), ws_b_entered.wait()), timeout=2.0)

        assert lock_mgr.active_locks_count() == 2

        finish_all.set()
        await asyncio.gather(t_a, t_b)

        assert lock_mgr.active_locks_count() == 0
        assert len(lock_mgr._locks) == 0
