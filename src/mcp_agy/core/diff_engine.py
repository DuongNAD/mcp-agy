"""Git unified diff engine and status inspection for FastMCP AGY workspaces.

Provides comprehensive workspace diff inspection capturing staged, unstaged,
and untracked modifications, producing structured FileDiffStat metrics and
unified diffs with configurable size limits, unborn repository safety,
binary file handling, and non-blocking asynchronous execution.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import shutil
from typing import Dict, List, Optional, Set, Tuple

from mcp_agy.core.models import DiffResult, FileDiffStat
from mcp_agy.utils.logger import get_logger
from mcp_agy.utils.process import run_subprocess_async

logger = get_logger("mcp_agy.core.diff_engine")

EMPTY_TREE_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def normalize_rel_path(path_str: str) -> str:
    """Normalize a filesystem path to forward slashes with quotes stripped."""
    cleaned = path_str.strip().strip('"').strip("'")
    return cleaned.replace("\\", "/")


def normalize_numstat_path(raw_path: str) -> str:
    """Normalize git numstat path output handling rename formats."""
    # Handles '{dir_a => dir_b}/file.txt' -> 'dir_b/file.txt'
    # Handles 'dir/{old => new}.txt' -> 'dir/new.txt'
    # Handles 'old.txt => new.txt' -> 'new.txt'
    path = re.sub(r"\{.*?=>\s*(.*?)\}", r"\1", raw_path)
    if " => " in path:
        path = path.split(" => ")[-1]
    return normalize_rel_path(path)


def resolve_file_status(xy_code: str) -> str:
    """Map 2-character Git porcelain XY status code to canonical FileDiffStat status."""
    xy = xy_code.strip()
    if not xy or xy == "??":
        return "??"
    if "D" in xy:
        return "D"
    if "A" in xy or xy_code.startswith("A"):
        return "A"
    if "R" in xy or xy_code.startswith("R"):
        return "R"
    return "M"


def is_binary_bytes(file_bytes: bytes) -> bool:
    """Check if byte sequence represents binary content via null byte inspection."""
    return b"\x00" in file_bytes[:8192]


def generate_untracked_synthetic_diff(workspace_dir: str, rel_path: str) -> Tuple[str, int]:
    """Generate synthetic unified diff and count line insertions for an untracked file."""
    full_path = os.path.join(workspace_dir, rel_path)
    if not os.path.isfile(full_path):
        return "", 0

    try:
        with open(full_path, "rb") as f:
            raw_bytes = f.read()
    except Exception as exc:
        logger.warning(f"Unable to read untracked file {full_path}: {exc}")
        return "", 0

    norm_path = normalize_rel_path(rel_path)

    if is_binary_bytes(raw_bytes):
        diff_text = (
            f"diff --git a/{norm_path} b/{norm_path}\n"
            f"new file mode 100644\n"
            f"Binary files /dev/null and b/{norm_path} differ"
        )
        return diff_text, 0

    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()

    if not lines:
        diff_text = (
            f"diff --git a/{norm_path} b/{norm_path}\n"
            f"new file mode 100644"
        )
        return diff_text, 0

    if len(lines) == 1:
        diff_text = (
            f"diff --git a/{norm_path} b/{norm_path}\n"
            f"new file mode 100644\n"
            f"--- /dev/null\n"
            f"+++ b/{norm_path}\n"
            f"@@ -0,0 +1 @@\n"
            f"+{lines[0]}"
        )
        return diff_text, 1

    diff_lines = [
        f"diff --git a/{norm_path} b/{norm_path}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{norm_path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    diff_lines.extend(f"+{line}" for line in lines)
    return "\n".join(diff_lines), len(lines)


class GitDiffEngine:
    """Comprehensive git workspace diff inspection and statistics engine."""

    def __init__(
        self,
        default_max_diff_lines: int = 5000,
        default_max_diff_bytes: int = 500_000,
    ) -> None:
        self.default_max_diff_lines = default_max_diff_lines
        self.default_max_diff_bytes = default_max_diff_bytes

    def _get_git_env(self) -> Dict[str, str]:
        """Construct sanitized environment variables for Git subprocess invocations."""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["LC_ALL"] = "C"
        return env

    async def get_diff(
        self,
        workspace_path: str,
        max_diff_lines: Optional[int] = None,
        max_diff_bytes: Optional[int] = None,
    ) -> DiffResult:
        """Inspect git status and generate a unified diff of all workspace changes."""
        effective_max_lines = max_diff_lines if max_diff_lines is not None else self.default_max_diff_lines
        effective_max_bytes = max_diff_bytes if max_diff_bytes is not None else self.default_max_diff_bytes

        # 1. Path validation
        if not workspace_path or not workspace_path.strip():
            return DiffResult(
                status="error",
                has_changes=False,
                summary="",
                error_details="Workspace path cannot be empty or whitespace only.",
            )

        norm_workspace = os.path.normpath(os.path.abspath(workspace_path))
        if not os.path.exists(norm_workspace):
            return DiffResult(
                status="error",
                has_changes=False,
                summary="Workspace directory does not exist.",
                error_details=f"Directory not found: {norm_workspace}",
            )

        if not os.path.isdir(norm_workspace):
            return DiffResult(
                status="error",
                has_changes=False,
                summary="Workspace path is not a directory.",
                error_details=f"Path is not a directory: {norm_workspace}",
            )

        git_env = self._get_git_env()
        git_cmd = shutil.which("git") or "git"

        # 2. Check if valid git repository
        try:
            code, stdout, stderr = await run_subprocess_async(
                [git_cmd, "rev-parse", "--is-inside-work-tree"],
                cwd=norm_workspace,
                env=git_env,
                timeout_seconds=10.0,
            )
            if code != 0 or stdout.strip().lower() != "true":
                return DiffResult(
                    status="not_a_git_repo",
                    has_changes=False,
                    summary="Directory is not a git repository.",
                    error_details=None,
                )
        except Exception as exc:
            logger.error(f"Git repo validation failed: {type(exc).__name__}: {exc}", exc_info=True)
            return DiffResult(
                status="error",
                has_changes=False,
                summary="Failed to execute git repository validation.",
                error_details=f"{type(exc).__name__}: {exc}",
            )

        # 3. Detect if repository is unborn (HEAD exists or 0 commits)
        code_head, _, _ = await run_subprocess_async(
            [git_cmd, "rev-parse", "--verify", "HEAD"],
            cwd=norm_workspace,
            env=git_env,
            timeout_seconds=10.0,
        )
        has_head = (code_head == 0)

        # 4. Query git status
        code_stat, status_out, status_err = await run_subprocess_async(
            [git_cmd, "status", "--porcelain=v1", "-uall"],
            cwd=norm_workspace,
            env=git_env,
            timeout_seconds=30.0,
        )
        if code_stat != 0:
            return DiffResult(
                status="error",
                has_changes=False,
                summary="Failed to query git status.",
                error_details=status_err.strip() or f"git status exited with code {code_stat}",
            )

        status_lines = [line for line in status_out.splitlines() if line.strip()]
        if not status_lines:
            return DiffResult(
                status="success",
                has_changes=False,
                unified_diff="",
                changed_files=[],
                untracked_files=[],
                summary="No uncommitted changes detected in repository.",
                error_details=None,
            )

        # 5. Extract untracked files
        untracked_files: List[str] = []
        for line in status_lines:
            if line.startswith("??"):
                raw_rel = line[3:].strip()
                untracked_files.append(normalize_rel_path(raw_rel))

        # 6. Extract unified diff and numstat for tracked files
        tracked_diff = ""
        numstat_map: Dict[str, Tuple[int, int]] = {}

        if has_head:
            _, diff_out, _ = await run_subprocess_async(
                [git_cmd, "diff", "HEAD"],
                cwd=norm_workspace,
                env=git_env,
                timeout_seconds=60.0,
            )
            tracked_diff = diff_out

            _, num_out, _ = await run_subprocess_async(
                [git_cmd, "diff", "--numstat", "HEAD"],
                cwd=norm_workspace,
                env=git_env,
                timeout_seconds=30.0,
            )
            for line in num_out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    ins = int(parts[0]) if parts[0].isdigit() else 0
                    dels = int(parts[1]) if parts[1].isdigit() else 0
                    fpath = normalize_numstat_path(parts[2])
                    numstat_map[fpath] = (ins, dels)
        else:
            # Unborn repository handling
            code_tree, tree_sha, _ = await run_subprocess_async(
                [git_cmd, "mktree"],
                cwd=norm_workspace,
                env=git_env,
                timeout_seconds=10.0,
            )
            empty_tree = tree_sha.strip() if code_tree == 0 and tree_sha.strip() else EMPTY_TREE_SHA1

            code_d, diff_out, _ = await run_subprocess_async(
                [git_cmd, "diff", empty_tree],
                cwd=norm_workspace,
                env=git_env,
                timeout_seconds=60.0,
            )
            if code_d == 0:
                tracked_diff = diff_out
                _, num_out, _ = await run_subprocess_async(
                    [git_cmd, "diff", "--numstat", empty_tree],
                    cwd=norm_workspace,
                    env=git_env,
                    timeout_seconds=30.0,
                )
                for line in num_out.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        ins = int(parts[0]) if parts[0].isdigit() else 0
                        dels = int(parts[1]) if parts[1].isdigit() else 0
                        fpath = normalize_numstat_path(parts[2])
                        numstat_map[fpath] = (ins, dels)
            else:
                # Fallback to cached + unstaged
                _, cached_diff, _ = await run_subprocess_async(
                    [git_cmd, "diff", "--cached"],
                    cwd=norm_workspace,
                    env=git_env,
                    timeout_seconds=30.0,
                )
                _, unstaged_diff, _ = await run_subprocess_async(
                    [git_cmd, "diff"],
                    cwd=norm_workspace,
                    env=git_env,
                    timeout_seconds=30.0,
                )
                parts = []
                if cached_diff.strip():
                    parts.append(cached_diff.strip())
                if unstaged_diff.strip():
                    parts.append(unstaged_diff.strip())
                tracked_diff = "\n\n".join(parts)

                _, cached_num, _ = await run_subprocess_async(
                    [git_cmd, "diff", "--numstat", "--cached"],
                    cwd=norm_workspace,
                    env=git_env,
                    timeout_seconds=30.0,
                )
                _, unstaged_num, _ = await run_subprocess_async(
                    [git_cmd, "diff", "--numstat"],
                    cwd=norm_workspace,
                    env=git_env,
                    timeout_seconds=30.0,
                )
                for num_text in (cached_num, unstaged_num):
                    for line in num_text.splitlines():
                        n_parts = line.split("\t")
                        if len(n_parts) >= 3:
                            ins = int(n_parts[0]) if n_parts[0].isdigit() else 0
                            dels = int(n_parts[1]) if n_parts[1].isdigit() else 0
                            fpath = normalize_numstat_path(n_parts[2])
                            prev_ins, prev_dels = numstat_map.get(fpath, (0, 0))
                            numstat_map[fpath] = (prev_ins + ins, prev_dels + dels)

        # 7. Generate synthetic diffs and line stats for untracked files
        synthetic_diffs: List[str] = []
        untracked_stats_map: Dict[str, int] = {}
        for u_path in untracked_files:
            synth_diff, ins_count = generate_untracked_synthetic_diff(norm_workspace, u_path)
            if synth_diff:
                synthetic_diffs.append(synth_diff)
            untracked_stats_map[u_path] = ins_count

        # 8. Build changed_files: List[FileDiffStat]
        changed_files: List[FileDiffStat] = []
        for line in status_lines:
            code = line[:2]
            raw_path = line[3:].strip()
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ")[-1]
            rel_path = normalize_rel_path(raw_path)

            status_char = resolve_file_status(code)
            if status_char == "??":
                ins = untracked_stats_map.get(rel_path, 0)
                dels = 0
            else:
                ins, dels = numstat_map.get(rel_path, (0, 0))

            changed_files.append(
                FileDiffStat(
                    path=rel_path,
                    status=status_char,
                    insertions=ins,
                    deletions=dels,
                )
            )

        # 9. Assemble full unified diff
        diff_chunks = []
        if tracked_diff.strip():
            diff_chunks.append(tracked_diff.strip())
        if synthetic_diffs:
            diff_chunks.extend(synthetic_diffs)
        full_diff = "\n\n".join(diff_chunks).strip()

        # 10. Apply length and byte capping
        truncated = False
        diff_lines = full_diff.splitlines()
        total_line_count = len(diff_lines)

        if total_line_count > effective_max_lines:
            truncated_lines = diff_lines[:effective_max_lines]
            full_diff = "\n".join(truncated_lines)
            truncated = True

        raw_bytes = full_diff.encode("utf-8")
        if len(raw_bytes) > effective_max_bytes:
            # Safe truncation at byte limit
            full_diff = raw_bytes[:effective_max_bytes].decode("utf-8", errors="ignore")
            truncated = True

        if truncated:
            full_diff += (
                f"\n\n[DIFF TRUNCATED: Diff output exceeded limit "
                f"(max lines: {effective_max_lines}, max bytes: {effective_max_bytes})]"
            )

        # 11. Compute summary string
        total_files = len(changed_files)
        total_ins = sum(f.insertions for f in changed_files)
        total_dels = sum(f.deletions for f in changed_files)

        mod_cnt = sum(1 for f in changed_files if f.status == "M")
        add_cnt = sum(1 for f in changed_files if f.status == "A")
        del_cnt = sum(1 for f in changed_files if f.status == "D")
        ren_cnt = sum(1 for f in changed_files if f.status == "R")
        unt_cnt = len(untracked_files)

        desc_items = []
        if mod_cnt:
            desc_items.append(f"{mod_cnt} modified")
        if add_cnt:
            desc_items.append(f"{add_cnt} added")
        if del_cnt:
            desc_items.append(f"{del_cnt} deleted")
        if ren_cnt:
            desc_items.append(f"{ren_cnt} renamed")
        if unt_cnt:
            desc_items.append(f"{unt_cnt} untracked")

        desc_str = f": {', '.join(desc_items)}" if desc_items else ""
        summary_str = f"{total_files} file(s) changed{desc_str} (+{total_ins}, -{total_dels})"

        return DiffResult(
            status="success",
            has_changes=bool(changed_files or untracked_files),
            unified_diff=full_diff,
            changed_files=changed_files,
            untracked_files=untracked_files,
            summary=summary_str,
            error_details=None,
        )


_default_diff_engine = GitDiffEngine()


async def inspect_git_diff(
    workspace_path: str,
    max_diff_lines: Optional[int] = None,
    max_diff_bytes: Optional[int] = None,
) -> DiffResult:
    """Convenience function inspecting git diff using the default GitDiffEngine instance."""
    return await _default_diff_engine.get_diff(
        workspace_path=workspace_path,
        max_diff_lines=max_diff_lines,
        max_diff_bytes=max_diff_bytes,
    )
