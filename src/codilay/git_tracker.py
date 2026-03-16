"""Git integration — tracks changes between documented commits and HEAD."""

import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"


@dataclass
class FileChange:
    """Represents a single file change detected by git."""

    change_type: ChangeType
    path: str
    old_path: Optional[str] = None  # only for renames
    similarity: Optional[int] = None  # rename similarity %

    def __repr__(self):
        if self.change_type == ChangeType.RENAMED:
            return f"{self.change_type.value:10s} {self.old_path} → {self.path}"
        return f"{self.change_type.value:10s} {self.path}"


@dataclass
class GitDiffResult:
    """Complete result of a git diff analysis."""

    base_commit: str
    head_commit: str
    commits_behind: int
    changes: List[FileChange] = field(default_factory=list)
    commit_messages: List[str] = field(default_factory=list)

    @property
    def added(self) -> List[FileChange]:
        return [c for c in self.changes if c.change_type == ChangeType.ADDED]

    @property
    def modified(self) -> List[FileChange]:
        return [c for c in self.changes if c.change_type == ChangeType.MODIFIED]

    @property
    def deleted(self) -> List[FileChange]:
        return [c for c in self.changes if c.change_type == ChangeType.DELETED]

    @property
    def renamed(self) -> List[FileChange]:
        return [c for c in self.changes if c.change_type == ChangeType.RENAMED]

    @property
    def all_affected_paths(self) -> List[str]:
        """All paths that are affected — current paths + old paths from renames."""
        paths = set()
        for c in self.changes:
            paths.add(c.path)
            if c.old_path:
                paths.add(c.old_path)
        return list(paths)

    @property
    def files_to_process(self) -> List[str]:
        """Files that need re-processing (excludes deleted)."""
        return [c.path for c in self.changes if c.change_type != ChangeType.DELETED]

    @property
    def summary_lines(self) -> List[str]:
        """Human-readable summary lines."""
        lines = []
        for c in self.changes:
            if c.change_type == ChangeType.RENAMED:
                lines.append(f"  [cyan]renamed[/cyan]   {c.old_path} → [bold]{c.path}[/bold]")
            elif c.change_type == ChangeType.ADDED:
                lines.append(f"  [green]added[/green]     [bold]{c.path}[/bold]")
            elif c.change_type == ChangeType.MODIFIED:
                lines.append(f"  [yellow]modified[/yellow]  [bold]{c.path}[/bold]")
            elif c.change_type == ChangeType.DELETED:
                lines.append(f"  [red]deleted[/red]   [bold]{c.path}[/bold]")
        return lines


class GitTracker:
    """
    Tracks git state and computes precise diffs between documented
    commits and current HEAD.
    """

    def __init__(self, repo_path: str):
        self.repo_path = os.path.abspath(repo_path)
        self._validate_repo()

    def _validate_repo(self):
        """Check if the target path is a git repository."""
        self._is_git_repo = os.path.isdir(os.path.join(self.repo_path, ".git"))

    @property
    def is_git_repo(self) -> bool:
        return self._is_git_repo

    def _run_git(self, *args, check: bool = True) -> Optional[str]:
        """Run a git command and return stdout."""
        try:
            result = subprocess.run(
                ["git"] + list(args),
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if check and result.returncode != 0:
                return None
            return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

    def get_current_commit(self) -> Optional[str]:
        """Get the current HEAD commit hash."""
        return self._run_git("rev-parse", "HEAD")

    def get_current_commit_short(self) -> Optional[str]:
        """Get short HEAD hash."""
        return self._run_git("rev-parse", "--short", "HEAD")

    def get_commit_message(self, commit_hash: str) -> Optional[str]:
        """Get the subject line of a commit message."""
        return self._run_git("log", "-1", "--format=%s", commit_hash)

    def is_commit_valid(self, commit_hash: str) -> bool:
        """Check if a commit hash exists in the repo."""
        result = self._run_git("cat-file", "-t", commit_hash, check=False)
        return result == "commit"

    def get_commit_count_between(self, base: str, head: str = "HEAD") -> int:
        """Count commits between two refs."""
        output = self._run_git("rev-list", "--count", f"{base}..{head}")
        if output is None:
            return -1
        try:
            return int(output)
        except ValueError:
            return -1

    def get_commit_messages_between(self, base: str, head: str = "HEAD") -> List[str]:
        """Get commit messages between two refs."""
        output = self._run_git("log", "--oneline", "--no-decorate", f"{base}..{head}")
        if not output:
            return []
        return output.strip().split("\n")

    def get_diff(self, base_commit: str, head: str = "HEAD") -> Optional[GitDiffResult]:
        """
        Get detailed diff between base_commit and head.
        Uses --name-status with -M (rename detection) and -C (copy detection).
        """
        if not self.is_git_repo:
            return None

        if not self.is_commit_valid(base_commit):
            return None

        head_commit = self.get_current_commit()
        if not head_commit:
            return None

        commits_behind = self.get_commit_count_between(base_commit, head)
        commit_messages = self.get_commit_messages_between(base_commit, head)

        # Get changes with rename and copy detection
        output = self._run_git("diff", "--name-status", "-M", "-C", base_commit, head)

        if output is None:
            return None

        changes = self._parse_name_status(output)

        return GitDiffResult(
            base_commit=base_commit,
            head_commit=head_commit,
            commits_behind=commits_behind,
            changes=changes,
            commit_messages=commit_messages,
        )

    def get_uncommitted_changes(self) -> List[FileChange]:
        """Get changes in working tree + staging area (not yet committed)."""
        changes = []

        # Staged changes
        staged_output = self._run_git("diff", "--name-status", "-M", "-C", "--cached")
        if staged_output:
            changes.extend(self._parse_name_status(staged_output))

        # Unstaged changes
        unstaged_output = self._run_git("diff", "--name-status", "-M", "-C")
        if unstaged_output:
            # Avoid duplicates
            existing_paths = {c.path for c in changes}
            for c in self._parse_name_status(unstaged_output):
                if c.path not in existing_paths:
                    changes.append(c)

        # Untracked files
        untracked_output = self._run_git("ls-files", "--others", "--exclude-standard")
        if untracked_output:
            existing_paths = {c.path for c in changes}
            for line in untracked_output.split("\n"):
                line = line.strip()
                if line and line not in existing_paths:
                    changes.append(
                        FileChange(
                            change_type=ChangeType.ADDED,
                            path=line,
                        )
                    )

        return changes

    def get_full_diff(self, base_commit: str) -> Optional[GitDiffResult]:
        """
        Get all changes from base_commit to current working state.
        Includes committed changes + uncommitted changes.
        """
        if not self.is_git_repo:
            return None

        # Get committed changes (base → HEAD)
        committed_diff = self.get_diff(base_commit)
        if committed_diff is None:
            return None

        # Get uncommitted changes (HEAD → working tree)
        uncommitted = self.get_uncommitted_changes()

        # Merge: uncommitted changes override committed ones for the same path
        committed_paths = {}
        for c in committed_diff.changes:
            committed_paths[c.path] = c
            if c.old_path:
                committed_paths[c.old_path] = c

        merged_changes = list(committed_diff.changes)
        for c in uncommitted:
            if c.path not in committed_paths:
                merged_changes.append(c)
            else:
                # File was changed in commits AND has further uncommitted changes.
                # Upgrade the committed entry to MODIFIED so it still gets
                # re-processed (e.g. a file added in a previous commit that has
                # further edits in the working tree must not be silently dropped).
                existing = committed_paths[c.path]
                if existing.change_type == ChangeType.ADDED:
                    # Stay as ADDED — it will be picked up for processing
                    pass
                elif existing.change_type != ChangeType.DELETED:
                    # Ensure it is at least MODIFIED so it is included in
                    # files_to_process even if git diff showed a rename/copy
                    existing.change_type = ChangeType.MODIFIED

        committed_diff.changes = merged_changes
        return committed_diff

    def _parse_name_status(self, output: str) -> List[FileChange]:
        """Parse git diff --name-status output."""
        changes = []
        for line in output.strip().split("\n"):
            if not line.strip():
                continue

            parts = line.split("\t")
            if len(parts) < 2:
                continue

            status = parts[0].strip()

            if status == "A":
                changes.append(
                    FileChange(
                        change_type=ChangeType.ADDED,
                        path=parts[1],
                    )
                )

            elif status == "M":
                changes.append(
                    FileChange(
                        change_type=ChangeType.MODIFIED,
                        path=parts[1],
                    )
                )

            elif status == "D":
                changes.append(
                    FileChange(
                        change_type=ChangeType.DELETED,
                        path=parts[1],
                    )
                )

            elif status.startswith("R"):
                # R100, R095, etc. — rename with similarity score
                similarity = None
                if len(status) > 1:
                    try:
                        similarity = int(status[1:])
                    except ValueError:
                        pass

                if len(parts) >= 3:
                    changes.append(
                        FileChange(
                            change_type=ChangeType.RENAMED,
                            path=parts[2],  # new path
                            old_path=parts[1],  # old path
                            similarity=similarity,
                        )
                    )

            elif status.startswith("C"):
                # Copy
                similarity = None
                if len(status) > 1:
                    try:
                        similarity = int(status[1:])
                    except ValueError:
                        pass

                if len(parts) >= 3:
                    changes.append(
                        FileChange(
                            change_type=ChangeType.COPIED,
                            path=parts[2],
                            old_path=parts[1],
                            similarity=similarity,
                        )
                    )

        return changes

    def get_file_at_commit(self, filepath: str, commit: str) -> Optional[str]:
        """Get the contents of a file at a specific commit."""
        return self._run_git("show", f"{commit}:{filepath}", check=False)

    def get_blame_summary(self, filepath: str) -> Optional[Dict[str, int]]:
        """Get a summary of who changed what in a file (lines per author)."""
        output = self._run_git("blame", "--line-porcelain", filepath)
        if not output:
            return None

        authors = {}
        for line in output.split("\n"):
            if line.startswith("author "):
                author = line[7:]
                authors[author] = authors.get(author, 0) + 1
        return authors
