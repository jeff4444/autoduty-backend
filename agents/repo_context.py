"""Manages a cloned copy of the target repo for agent file operations.

Handles git cloning, file read/write, edit tracking, and unified diff generation.
"""

import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from config import Config
from models.incident import FileEdit
from utils.logger import get_logger

log = get_logger("repo_context")


class RepoContext:
    """Represents a local clone of the target repository that the agent operates on.

    Tracks every file modification so we can produce per-file unified diffs at the end.
    """

    def __init__(self, repo_url: str, branch: str = "main", github_token: str = "") -> None:
        self.repo_url = repo_url
        self.branch = branch
        self.github_token = github_token or Config.GITHUB_TOKEN

        # Clone destination
        self.clone_dir: Optional[Path] = None

        # Track original file contents for every file we modify
        # key: relative file path, value: original content (before any edits)
        self._originals: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def clone(self) -> Path:
        """Clone the repository to a local temp directory. Returns the clone path."""
        import asyncio

        # Build authenticated URL if we have a token
        clone_url = self.repo_url
        if self.github_token and "github.com" in clone_url:
            clone_url = clone_url.replace(
                "https://github.com",
                f"https://x-access-token:{self.github_token}@github.com",
            )

        # Unique directory per repo + branch
        safe_name = re.sub(r"[^\w\-]", "_", self.repo_url.split("/")[-1].replace(".git", ""))
        self.clone_dir = Path(Config.CLONE_BASE_DIR) / f"{safe_name}_{os.getpid()}"

        # Remove old clone if it exists
        if self.clone_dir.exists():
            shutil.rmtree(self.clone_dir)

        os.makedirs(self.clone_dir.parent, exist_ok=True)

        log.info("Cloning %s (branch %s) to %s", self.repo_url, self.branch, self.clone_dir)

        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth=1", "--branch", self.branch, clone_url, str(self.clone_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            raise RuntimeError(f"git clone failed (exit {proc.returncode}): {error_msg}")

        log.info("Clone complete: %s", self.clone_dir)
        return self.clone_dir

    def cleanup(self) -> None:
        """Remove the cloned repo from disk."""
        if self.clone_dir and self.clone_dir.exists():
            shutil.rmtree(self.clone_dir, ignore_errors=True)
            log.info("Cleaned up clone dir: %s", self.clone_dir)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------
    def _resolve(self, path: str) -> Path:
        """Resolve a relative path to an absolute path within the clone, with safety checks."""
        if self.clone_dir is None:
            raise RuntimeError("Repo not cloned yet. Call clone() first.")

        resolved = (self.clone_dir / path).resolve()

        # Prevent path traversal outside the clone
        if not str(resolved).startswith(str(self.clone_dir.resolve())):
            raise ValueError(f"Path traversal detected: {path}")

        return resolved

    def read_file(self, path: str) -> str:
        """Read a file from the cloned repo."""
        file_path = self._resolve(path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not file_path.is_file():
            raise ValueError(f"Not a file: {path}")
        return file_path.read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> str:
        """Write content to a file, tracking the original for diff generation."""
        file_path = self._resolve(path)

        # Snapshot the original if this is the first edit to this file
        if path not in self._originals:
            if file_path.exists():
                self._originals[path] = file_path.read_text(encoding="utf-8", errors="replace")
            else:
                self._originals[path] = ""  # new file

        # Create parent directories if needed
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

        return f"Wrote {len(content)} chars to {path}"

    def search_and_replace(self, path: str, old: str, new: str) -> str:
        """Find and replace text in a file, tracking the change."""
        content = self.read_file(path)

        if old not in content:
            return f"String not found in {path}: {old[:80]}..."

        # Snapshot original before first edit
        if path not in self._originals:
            self._originals[path] = content

        updated = content.replace(old, new, 1)
        file_path = self._resolve(path)
        file_path.write_text(updated, encoding="utf-8")

        count = content.count(old)
        return f"Replaced 1 occurrence in {path} ({count} total matches found)"

    def grep(self, pattern: str, path: str = ".") -> str:
        """Recursive regex search across the repo. Returns matching lines with file:line context."""
        search_dir = self._resolve(path)
        if not search_dir.exists():
            return f"Path not found: {path}"

        results: list[str] = []
        max_results = 50

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Invalid regex pattern: {e}"

        if search_dir.is_file():
            files = [search_dir]
        else:
            files = sorted(search_dir.rglob("*"))

        for file_path in files:
            if not file_path.is_file():
                continue
            # Skip binary / hidden / node_modules
            rel = str(file_path.relative_to(self.clone_dir))
            if any(part.startswith(".") for part in Path(rel).parts):
                continue
            if "node_modules" in rel or "__pycache__" in rel:
                continue

            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    results.append(f"{rel}:{i}: {line.rstrip()}")
                    if len(results) >= max_results:
                        results.append(f"... (truncated at {max_results} results)")
                        return "\n".join(results)

        if not results:
            return f"No matches found for pattern: {pattern}"
        return "\n".join(results)

    def list_directory(self, path: str = ".") -> str:
        """List files and directories at the given path."""
        dir_path = self._resolve(path)
        if not dir_path.exists():
            return f"Path not found: {path}"
        if not dir_path.is_dir():
            return f"Not a directory: {path}"

        entries: list[str] = []
        for entry in sorted(dir_path.iterdir()):
            rel = str(entry.relative_to(self.clone_dir))
            if entry.name.startswith("."):
                continue
            if entry.name in ("node_modules", "__pycache__", ".git"):
                continue
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"{rel}{suffix}")

        if not entries:
            return f"(empty directory: {path})"
        return "\n".join(entries)

    # ------------------------------------------------------------------
    # Edit tracking management
    # ------------------------------------------------------------------
    def reset_edit_tracking(self) -> None:
        """Re-snapshot current state of all previously edited files as the new baseline.

        Call this between retry attempts so new diffs are computed relative to
        the current (post-edit) state rather than the original clone.
        """
        if self.clone_dir is None:
            return

        new_originals: dict[str, str] = {}
        for rel_path in self._originals:
            file_path = self._resolve(rel_path)
            if file_path.exists():
                new_originals[rel_path] = file_path.read_text(encoding="utf-8", errors="replace")
            else:
                new_originals[rel_path] = ""

        self._originals = new_originals
        log.info("Reset edit tracking — %d file(s) re-snapshotted", len(new_originals))

    # ------------------------------------------------------------------
    # Diff generation
    # ------------------------------------------------------------------
    def get_file_edits(self) -> list[FileEdit]:
        """Generate FileEdit objects with unified diffs for every modified file."""
        edits: list[FileEdit] = []

        for rel_path, original in self._originals.items():
            file_path = self._resolve(rel_path)
            if file_path.exists():
                current = file_path.read_text(encoding="utf-8", errors="replace")
            else:
                current = ""  # file was deleted

            if original == current:
                continue  # no actual change

            diff_lines = difflib.unified_diff(
                original.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            )
            unified_diff = "\n".join(diff_lines)

            edits.append(
                FileEdit(
                    file_path=rel_path,
                    original_content=original,
                    new_content=current,
                    unified_diff=unified_diff,
                )
            )

        return edits
