"""Comprehensive Unit and Integration Tests for GitDiffEngine."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from mcp_agy.core.diff_engine import (
    GitDiffEngine,
    generate_untracked_synthetic_diff,
    inspect_git_diff,
    is_binary_bytes,
    normalize_numstat_path,
    normalize_rel_path,
    resolve_file_status,
)
from mcp_agy.core.models import DiffResult, FileDiffStat


class TestHelperFunctions:
    def test_normalize_rel_path(self):
        assert normalize_rel_path(' "src/app.py" ') == "src/app.py"
        assert normalize_rel_path("src\\utils\\helper.py") == "src/utils/helper.py"
        assert normalize_rel_path("'docs/readme.md'") == "docs/readme.md"

    def test_normalize_numstat_path(self):
        assert normalize_numstat_path("src/app.py") == "src/app.py"
        assert normalize_numstat_path("{dir_a => dir_b}/file.txt") == "dir_b/file.txt"
        assert normalize_numstat_path("dir/{old => new}.txt") == "dir/new.txt"
        assert normalize_numstat_path("old.txt => new.txt") == "new.txt"

    def test_resolve_file_status(self):
        assert resolve_file_status("??") == "??"
        assert resolve_file_status(" M") == "M"
        assert resolve_file_status("M ") == "M"
        assert resolve_file_status("MM") == "M"
        assert resolve_file_status("A ") == "A"
        assert resolve_file_status("AM") == "A"
        assert resolve_file_status(" D") == "D"
        assert resolve_file_status("D ") == "D"
        assert resolve_file_status("R ") == "R"
        assert resolve_file_status("RM") == "R"

    def test_is_binary_bytes(self):
        assert is_binary_bytes(b"hello\x00world") is True
        assert is_binary_bytes(b"hello world\n") is False


class TestNonGitAndInvalidPaths:
    @pytest.mark.asyncio
    async def test_nonexistent_workspace_returns_error(self, tmp_path):
        bad_path = str(tmp_path / "does_not_exist_12345")
        engine = GitDiffEngine()
        res = await engine.get_diff(bad_path)
        assert res.status == "error"
        assert res.has_changes is False
        assert "not found" in (res.error_details or "").lower() or "not exist" in res.summary.lower()

    @pytest.mark.asyncio
    async def test_empty_whitespace_path_returns_error(self):
        engine = GitDiffEngine()
        res = await engine.get_diff("   \t  ")
        assert res.status == "error"
        assert res.has_changes is False
        assert "cannot be empty" in (res.error_details or "").lower()

    @pytest.mark.asyncio
    async def test_file_instead_of_dir_returns_error(self, tmp_path):
        a_file = tmp_path / "some_file.txt"
        a_file.write_text("just a file\n", encoding="utf-8")
        engine = GitDiffEngine()
        res = await engine.get_diff(str(a_file))
        assert res.status == "error"
        assert res.has_changes is False
        assert "not a directory" in (res.error_details or "").lower()

    @pytest.mark.asyncio
    async def test_non_git_directory_returns_not_a_git_repo(self, non_git_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(non_git_workspace))
        assert res.status == "not_a_git_repo"
        assert res.has_changes is False
        assert "not a git repository" in res.summary.lower()


class TestCleanGitRepository:
    @pytest.mark.asyncio
    async def test_pristine_repository_has_no_changes(self, clean_git_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(clean_git_workspace))
        assert res.status == "success"
        assert res.has_changes is False
        assert res.changed_files == []
        assert res.untracked_files == []
        assert res.unified_diff == ""
        assert "no uncommitted changes" in res.summary.lower()


class TestTrackedAndStagedChanges:
    @pytest.mark.asyncio
    async def test_unstaged_modifications(self, unstaged_changes_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(unstaged_changes_workspace))
        assert res.status == "success"
        assert res.has_changes is True
        paths = {f.path for f in res.changed_files}
        assert "src/app.py" in paths
        app_stat = next(f for f in res.changed_files if f.path == "src/app.py")
        assert app_stat.status == "M"
        assert app_stat.insertions >= 1
        assert "unstaged_edit" in res.unified_diff

    @pytest.mark.asyncio
    async def test_staged_new_file_and_modifications(self, staged_changes_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(staged_changes_workspace))
        assert res.status == "success"
        assert res.has_changes is True
        paths = {f.path for f in res.changed_files}
        assert "src/app.py" in paths
        assert "src/utils.py" in paths
        utils_stat = next(f for f in res.changed_files if f.path == "src/utils.py")
        assert utils_stat.status == "A"
        assert utils_stat.insertions >= 1


class TestUntrackedFilesAndSyntheticDiff:
    @pytest.mark.asyncio
    async def test_untracked_files_synthetic_diff_generation(self, untracked_files_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(untracked_files_workspace))
        assert res.status == "success"
        assert res.has_changes is True
        assert "new_feature.py" in res.untracked_files
        assert "docs/guide.md" in res.untracked_files

        # Synthetic diff verification
        assert "--- /dev/null" in res.unified_diff
        assert "+++ b/new_feature.py" in res.unified_diff
        assert "+++ b/docs/guide.md" in res.unified_diff

        # Changed files contains untracked records
        paths = {f.path for f in res.changed_files}
        assert "new_feature.py" in paths
        assert "docs/guide.md" in paths
        nf_stat = next(f for f in res.changed_files if f.path == "new_feature.py")
        assert nf_stat.status == "??"
        assert nf_stat.insertions >= 1
        assert nf_stat.deletions == 0

    def test_generate_untracked_synthetic_diff_empty_and_single_line(self, tmp_path):
        empty_f = tmp_path / "empty.txt"
        empty_f.write_text("", encoding="utf-8")
        diff_str, count = generate_untracked_synthetic_diff(str(tmp_path), "empty.txt")
        assert count == 0
        assert "new file mode 100644" in diff_str

        single_f = tmp_path / "single.txt"
        single_f.write_text("one line\n", encoding="utf-8")
        diff_str, count = generate_untracked_synthetic_diff(str(tmp_path), "single.txt")
        assert count == 1
        assert "@@ -0,0 +1 @@" in diff_str
        assert "+one line" in diff_str


class TestUnbornGitRepository:
    @pytest.mark.asyncio
    async def test_unborn_repository_with_staged_and_untracked(self, unborn_git_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(unborn_git_workspace))
        assert res.status == "success"
        assert res.has_changes is True
        assert "untracked_unborn.txt" in res.untracked_files
        paths = {f.path for f in res.changed_files}
        assert "staged_unborn.txt" in paths
        assert "untracked_unborn.txt" in paths

    @pytest.mark.asyncio
    async def test_unborn_repository_clean(self, ephemeral_workspace):
        ephemeral_workspace.init_git(initial_commit=False)
        engine = GitDiffEngine()
        res = await engine.get_diff(str(ephemeral_workspace))
        assert res.status == "success"
        assert res.has_changes is False
        assert res.changed_files == []
        assert res.untracked_files == []
        assert res.unified_diff == ""


class TestDirtyWorkspaceAndSummary:
    @pytest.mark.asyncio
    async def test_dirty_workspace_stat_aggregation(self, dirty_git_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(dirty_git_workspace))
        assert res.status == "success"
        assert res.has_changes is True
        assert len(res.changed_files) >= 4

        statuses = {f.status for f in res.changed_files}
        assert "M" in statuses or "A" in statuses or "D" in statuses or "??" in statuses

        # Summary string check
        assert "file(s) changed" in res.summary
        assert "(+" in res.summary
        assert "-)" in res.summary or ", -" in res.summary

        for stat in res.changed_files:
            assert isinstance(stat.insertions, int)
            assert isinstance(stat.deletions, int)

    @pytest.mark.asyncio
    async def test_deleted_tracked_file(self, clean_git_workspace):
        clean_git_workspace.delete_file("src/app.py")
        engine = GitDiffEngine()
        res = await engine.get_diff(str(clean_git_workspace))
        assert res.status == "success"
        assert res.has_changes is True
        del_stat = next(f for f in res.changed_files if f.path == "src/app.py")
        assert del_stat.status == "D"
        assert del_stat.deletions >= 1


class TestLimitsAndRobustness:
    @pytest.mark.asyncio
    async def test_diff_truncation_max_lines(self, large_diff_workspace):
        engine = GitDiffEngine(default_max_diff_lines=30)
        res = await engine.get_diff(str(large_diff_workspace), max_diff_lines=30)
        assert res.status == "success"
        assert "[DIFF TRUNCATED:" in res.unified_diff
        assert "max lines: 30" in res.unified_diff

    @pytest.mark.asyncio
    async def test_diff_truncation_max_bytes(self, large_diff_workspace):
        engine = GitDiffEngine(default_max_diff_bytes=500)
        res = await engine.get_diff(str(large_diff_workspace), max_diff_bytes=500)
        assert res.status == "success"
        assert "[DIFF TRUNCATED:" in res.unified_diff
        assert "max bytes: 500" in res.unified_diff

    @pytest.mark.asyncio
    async def test_binary_files_handling(self, binary_git_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(binary_git_workspace))
        assert res.status == "success"
        assert res.has_changes is True
        assert "assets/icon.bin" in res.untracked_files
        assert "Binary files" in res.unified_diff or "logo.png" in res.unified_diff

    @pytest.mark.asyncio
    async def test_gitignore_rules(self, gitignore_workspace):
        engine = GitDiffEngine()
        res = await engine.get_diff(str(gitignore_workspace))
        assert res.status == "success"
        assert "debug.log" not in res.untracked_files
        assert "build/output.o" not in res.untracked_files
        assert "temp.tmp" not in res.untracked_files
        assert "src/valid.py" in res.untracked_files

    @pytest.mark.asyncio
    async def test_inspect_git_diff_convenience_function(self, clean_git_workspace):
        res = await inspect_git_diff(str(clean_git_workspace))
        assert isinstance(res, DiffResult)
        assert res.status == "success"
        assert res.has_changes is False
