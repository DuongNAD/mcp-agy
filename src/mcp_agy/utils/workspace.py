"""Workspace safety, boundary validation, and concurrency locking utilities.

Provides path canonicalization, system-critical directory protection,
directory traversal prevention, allowed-roots validation, and per-workspace
asynchronous mutex concurrency locking.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from typing import AsyncIterator, Optional


class WorkspaceError(Exception):
    """Base exception for all workspace safety and isolation errors."""


class WorkspaceSecurityError(WorkspaceError):
    """Raised when a workspace path violates security boundaries or targets sensitive locations."""


class WorkspaceNotFoundError(WorkspaceError, FileNotFoundError):
    """Raised when a required workspace directory does not exist on disk."""


class WorkspaceNotADirectoryError(WorkspaceError, NotADirectoryError):
    """Raised when a workspace path points to a regular file instead of a directory."""


class WorkspaceLockTimeoutError(WorkspaceError, TimeoutError):
    """Raised when acquiring a workspace concurrency lock times out."""


# System-critical roots on Windows
WIN_SENSITIVE_ROOTS: list[str] = [
    os.environ.get("SystemRoot", "C:\\Windows"),
    os.environ.get("ProgramFiles", "C:\\Program Files"),
    os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
    os.environ.get("ProgramData", "C:\\ProgramData"),
]

# System-critical roots on POSIX / Unix / macOS
POSIX_SENSITIVE_ROOTS: list[str] = [
    "/etc",
    "/sys",
    "/proc",
    "/dev",
    "/bin",
    "/sbin",
    "/usr",
    "/lib",
    "/lib64",
    "/lib32",
    "/boot",
    "/root",
    "/var",
    "/opt",
    "/System",
    "/Library",
    "/Applications",
    "/private",
]


def canonicalize_workspace_path(path: str | Path) -> Path:
    """Canonicalize a workspace path, resolving relative segments, home tilde, and symlinks.

    Args:
        path: Filesystem path string or Path object.

    Returns:
        Path: Fully resolved canonical absolute Path.

    Raises:
        WorkspaceSecurityError: If path contains null bytes, is empty, or cannot be resolved.
    """
    if path is None:
        raise WorkspaceSecurityError("Workspace path cannot be None.")

    path_str = str(path)
    if "\x00" in path_str:
        raise WorkspaceSecurityError("Path contains forbidden null bytes.")

    trimmed = path_str.strip()
    if not trimmed:
        raise WorkspaceSecurityError("Workspace path cannot be empty or whitespace only.")

    try:
        expanded = Path(trimmed).expanduser()
        return expanded.resolve()
    except (RuntimeError, OSError) as exc:
        raise WorkspaceSecurityError(f"Failed to resolve workspace path '{path_str}': {exc}") from exc


def get_workspace_key(path: str | Path) -> str:
    """Derive a normalized dictionary key for workspace locking.

    On Windows, keys are lowercased for case-insensitive lookup.
    """
    canonical = canonicalize_workspace_path(path)
    if os.name == "nt":
        return str(canonical).lower()
    return str(canonical)


def is_filesystem_root(path: Path) -> bool:
    """Check whether a path is a direct drive root or filesystem root (e.g. C:\\, D:\\, /)."""
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    # Parent equals itself (e.g. C:\ or /)
    if resolved.parent == resolved:
        return True

    # Matches anchor (e.g. C:\ on Windows or / on POSIX)
    if str(resolved) == resolved.anchor:
        return True

    resolved_str = str(resolved)
    if resolved_str == "/" or resolved == Path("/"):
        return True

    if os.name == "nt" and len(resolved_str) <= 3 and resolved_str.endswith(":\\"):
        return True

    return False


def is_system_critical_path(path: Path) -> tuple[bool, str]:
    """Check whether a path targets a sensitive system directory or raw user profile root.

    Returns:
        tuple[bool, str]: (is_critical, reason_message)
    """
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    # 1. Drive root or filesystem root check
    if is_filesystem_root(resolved):
        return True, f"Access to filesystem root '{resolved}' is denied."

    resolved_str = str(resolved)
    resolved_lower = resolved_str.lower() if os.name == "nt" else resolved_str

    # 2. Check Windows sensitive roots
    if os.name == "nt":
        for sensitive in WIN_SENSITIVE_ROOTS:
            if not sensitive:
                continue
            try:
                sens_path = Path(sensitive).resolve()
                sens_str = str(sens_path).lower()
                if resolved_lower == sens_str or resolved.is_relative_to(sens_path):
                    return True, f"Access to system-critical directory '{resolved}' (inside '{sensitive}') is denied."
            except (ValueError, OSError):
                continue

    # 3. Check POSIX sensitive roots
    for posix_dir in POSIX_SENSITIVE_ROOTS:
        try:
            posix_path = Path(posix_dir).resolve()
            if resolved == posix_path or resolved.is_relative_to(posix_path):
                return True, f"Access to system-critical directory '{resolved}' (inside '{posix_dir}') is denied."
        except (ValueError, OSError):
            continue

    # 4. Raw user profile direct root protection
    try:
        user_home = Path.home().resolve()
        if resolved == user_home:
            return (
                True,
                f"Access to raw user profile root '{resolved}' is denied. Please specify a project subdirectory.",
            )
    except Exception:
        pass

    return False, ""


def validate_workspace_path(
    path: str | Path,
    allowed_roots: Optional[list[str | Path]] = None,
    require_exists: bool = True,
    require_directory: bool = True,
    allow_create: bool = False,
) -> Path:
    """Validate that a workspace path is safe, normalized, and within boundaries.

    Args:
        path: Workspace path string or Path object.
        allowed_roots: Optional list of allowed root directories to constrain access.
        require_exists: If True, path must exist on disk (unless allow_create is True and parent exists).
        require_directory: If True, path must not be a regular file.
        allow_create: If True and require_exists is True, allow non-existent path if parent exists.

    Returns:
        Path: Fully resolved canonical Path.

    Raises:
        WorkspaceSecurityError: If path violates security boundaries or allowed roots.
        WorkspaceNotFoundError: If path does not exist when required.
        WorkspaceNotADirectoryError: If path points to a file when a directory is required.
    """
    canonical = canonicalize_workspace_path(path)

    # Check filesystem root
    if is_filesystem_root(canonical):
        raise WorkspaceSecurityError(f"Access to filesystem root '{canonical}' is denied.")

    # Check system critical directory
    is_crit, reason = is_system_critical_path(canonical)
    if is_crit:
        raise WorkspaceSecurityError(reason)

    # Check allowed roots whitelist if provided
    if allowed_roots is not None and len(allowed_roots) > 0:
        matched = False
        for root in allowed_roots:
            try:
                root_canonical = canonicalize_workspace_path(root)
                if canonical == root_canonical or canonical.is_relative_to(root_canonical):
                    matched = True
                    break
            except Exception:
                continue
        if not matched:
            roots_repr = [str(r) for r in allowed_roots]
            raise WorkspaceSecurityError(
                f"Workspace path '{canonical}' is outside allowed roots: {roots_repr}"
            )

    # Existence check
    if require_exists and not canonical.exists():
        if allow_create and canonical.parent.exists():
            pass  # Allowed for creation
        else:
            raise WorkspaceNotFoundError(
                f"Workspace directory does not exist: {canonical}"
            )

    # Directory check
    if require_directory and canonical.exists() and canonical.is_file():
        raise WorkspaceNotADirectoryError(
            f"Workspace path is a file, not a directory: {canonical}"
        )

    return canonical


class WorkspaceLockManager:
    """Manages per-workspace asynchronous mutex locks to serialize concurrent operations."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_ref_counts: dict[str, int] = {}
        self._internal_lock = asyncio.Lock()

    def is_locked(self, workspace_path: str | Path) -> bool:
        """Check whether the workspace path currently has an active acquired lock."""
        try:
            key = get_workspace_key(workspace_path)
            lock = self._locks.get(key)
            return lock is not None and lock.locked()
        except Exception:
            return False

    def active_locks_count(self) -> int:
        """Return the count of currently locked workspace mutexes."""
        return sum(1 for lock in self._locks.values() if lock.locked())

    def reset(self) -> None:
        """Clear all registered locks."""
        self._locks.clear()
        self._lock_ref_counts.clear()

    @asynccontextmanager
    async def lock(
        self,
        workspace_path: str | Path,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Path]:
        """Acquire an async concurrency lock for a workspace path.

        Args:
            workspace_path: Target workspace path to lock.
            timeout: Optional maximum wait time in seconds before raising WorkspaceLockTimeoutError.

        Yields:
            Path: Canonical Path of the locked workspace.

        Raises:
            WorkspaceLockTimeoutError: If the lock cannot be acquired within timeout seconds.
        """
        canonical = canonicalize_workspace_path(workspace_path)
        key = get_workspace_key(canonical)

        async with self._internal_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
                self._lock_ref_counts[key] = 0
            self._lock_ref_counts[key] += 1
            ws_lock = self._locks[key]

        acquired = False
        try:
            if timeout is not None and timeout > 0:
                try:
                    await asyncio.wait_for(ws_lock.acquire(), timeout=timeout)
                    acquired = True
                except asyncio.TimeoutError as exc:
                    raise WorkspaceLockTimeoutError(
                        f"Timed out after {timeout}s waiting for workspace lock on '{canonical}'."
                    ) from exc
            else:
                await ws_lock.acquire()
                acquired = True

            yield canonical

        finally:
            if acquired:
                ws_lock.release()

            async with self._internal_lock:
                if key in self._lock_ref_counts:
                    self._lock_ref_counts[key] -= 1
                    if self._lock_ref_counts[key] <= 0 and not ws_lock.locked():
                        self._locks.pop(key, None)
                        self._lock_ref_counts.pop(key, None)


_GLOBAL_LOCK_MANAGER: Optional[WorkspaceLockManager] = None


def get_workspace_lock_manager() -> WorkspaceLockManager:
    """Retrieve the global singleton WorkspaceLockManager instance."""
    global _GLOBAL_LOCK_MANAGER
    if _GLOBAL_LOCK_MANAGER is None:
        _GLOBAL_LOCK_MANAGER = WorkspaceLockManager()
    return _GLOBAL_LOCK_MANAGER


def reset_workspace_lock_manager() -> None:
    """Reset the global singleton WorkspaceLockManager instance."""
    global _GLOBAL_LOCK_MANAGER
    if _GLOBAL_LOCK_MANAGER is not None:
        _GLOBAL_LOCK_MANAGER.reset()
    _GLOBAL_LOCK_MANAGER = WorkspaceLockManager()
