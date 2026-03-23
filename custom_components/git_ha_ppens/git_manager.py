"""Git operations manager for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .const import DEFAULT_GITIGNORE_ENTRIES, SECRET_PATTERNS

_LOGGER = logging.getLogger(__name__)


@dataclass
class GitStatus:
    """Represents the current git repository status."""

    branch: str = "unknown"
    dirty: bool = False
    changed_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    staged_files: list[str] = field(default_factory=list)
    last_commit_hash: str = ""
    last_commit_hash_short: str = ""
    last_commit_message: str = ""
    last_commit_author: str = ""
    last_commit_time: datetime | None = None
    ahead: int = 0
    behind: int = 0
    remote_configured: bool = False
    total_commits: int = 0


@dataclass
class CommitInfo:
    """Represents a single commit."""

    hash: str
    hash_short: str
    message: str
    author: str
    timestamp: datetime


class GitError(Exception):
    """Raised when a git operation fails."""


class GitManager:
    """Manages git operations for the Home Assistant config directory."""

    def __init__(
        self,
        repo_path: str,
        git_user: str = "",
        git_email: str = "",
    ) -> None:
        """Initialize the git manager."""
        self._repo_path = repo_path
        self._git_user = git_user
        self._git_email = git_email

    @property
    def repo_path(self) -> str:
        """Return the repository path."""
        return self._repo_path

    async def _run_git(
        self,
        *args: str,
        check: bool = True,
        capture_stderr: bool = True,
    ) -> str:
        """Run a git command and return stdout.

        Args:
            *args: Git command arguments (without 'git' prefix).
            check: If True, raise GitError on non-zero exit code.
            capture_stderr: If True, capture stderr for error messages.

        Returns:
            The stdout output of the command.

        Raises:
            GitError: If the command fails and check is True.
        """
        cmd = ["git", "-C", self._repo_path, *args]
        _LOGGER.debug("Running git command: %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE if capture_stderr else None,
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "LC_ALL": "C",
                },
            )
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = (
                stderr_bytes.decode("utf-8", errors="replace").strip()
                if stderr_bytes
                else ""
            )
        except FileNotFoundError as err:
            raise GitError("Git is not installed or not found in PATH") from err
        except OSError as err:
            raise GitError(f"Failed to run git: {err}") from err

        if check and process.returncode != 0:
            error_msg = stderr or stdout or f"Git command failed with code {process.returncode}"
            _LOGGER.error("Git command failed: %s -> %s", " ".join(cmd), error_msg)
            raise GitError(error_msg)

        return stdout

    async def is_git_installed(self) -> bool:
        """Check if git is installed and available."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            return process.returncode == 0 and b"git version" in stdout
        except (FileNotFoundError, OSError):
            return False

    async def get_git_version(self) -> str:
        """Return the installed git version string."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            return stdout.decode("utf-8", errors="replace").strip()
        except (FileNotFoundError, OSError):
            return "not installed"

    async def is_repo_initialized(self) -> bool:
        """Check if the path is an initialized git repository."""
        try:
            result = await self._run_git(
                "rev-parse", "--is-inside-work-tree", check=False
            )
            return result == "true"
        except GitError:
            return False

    async def init_repo(self) -> None:
        """Initialize a git repository and configure user settings."""
        repo_path = Path(self._repo_path)
        if not repo_path.is_dir():
            raise GitError(f"Repository path does not exist: {self._repo_path}")

        if not await self.is_repo_initialized():
            await self._run_git("init")
            _LOGGER.info("Initialized git repository at %s", self._repo_path)

        # Configure user
        if self._git_user:
            await self._run_git("config", "user.name", self._git_user)
        if self._git_email:
            await self._run_git("config", "user.email", self._git_email)

        # Set default branch name
        await self._run_git("config", "init.defaultBranch", "main", check=False)

    async def get_status(self) -> GitStatus:
        """Get the complete repository status."""
        status = GitStatus()

        if not await self.is_repo_initialized():
            return status

        # Get current branch
        try:
            status.branch = await self._run_git(
                "rev-parse", "--abbrev-ref", "HEAD"
            )
        except GitError:
            status.branch = "unknown"

        # Get changed/staged/untracked files
        try:
            porcelain = await self._run_git("status", "--porcelain")
            if porcelain:
                for line in porcelain.splitlines():
                    if len(line) < 4:
                        continue
                    index_status = line[0]
                    worktree_status = line[1]
                    filepath = line[3:]

                    if index_status == "?" and worktree_status == "?":
                        status.untracked_files.append(filepath)
                    elif index_status in "MADRC":
                        status.staged_files.append(filepath)
                    if worktree_status in "MD":
                        status.changed_files.append(filepath)

                status.dirty = True
            else:
                status.dirty = False
        except GitError:
            pass

        # Get last commit info
        try:
            log_format = await self._run_git(
                "log", "-1", "--format=%H%n%h%n%s%n%an%n%aI"
            )
            if log_format:
                parts = log_format.splitlines()
                if len(parts) >= 5:
                    status.last_commit_hash = parts[0]
                    status.last_commit_hash_short = parts[1]
                    status.last_commit_message = parts[2]
                    status.last_commit_author = parts[3]
                    try:
                        status.last_commit_time = datetime.fromisoformat(parts[4])
                    except ValueError:
                        status.last_commit_time = None
        except GitError:
            # No commits yet
            pass

        # Get total commit count
        try:
            count_str = await self._run_git("rev-list", "--count", "HEAD")
            status.total_commits = int(count_str)
        except (GitError, ValueError):
            status.total_commits = 0

        # Get ahead/behind remote
        try:
            remote_branch = await self._run_git(
                "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                check=False,
            )
            if remote_branch:
                status.remote_configured = True
                ab_output = await self._run_git(
                    "rev-list", "--left-right", "--count", f"HEAD...@{{u}}"
                )
                if ab_output:
                    parts = ab_output.split()
                    if len(parts) == 2:
                        status.ahead = int(parts[0])
                        status.behind = int(parts[1])
            else:
                # Check if remote origin exists even without tracking
                remotes = await self._run_git("remote", check=False)
                status.remote_configured = bool(remotes.strip())
        except (GitError, ValueError):
            pass

        return status

    async def commit(self, message: str | None = None) -> CommitInfo | None:
        """Stage all changes and create a commit.

        Args:
            message: Commit message. If None, auto-generates one.

        Returns:
            CommitInfo for the new commit, or None if nothing to commit.
        """
        # Check for changes
        porcelain = await self._run_git("status", "--porcelain")
        if not porcelain:
            _LOGGER.debug("Nothing to commit")
            return None

        # Auto-generate message if not provided
        if not message:
            message = self._generate_commit_message(porcelain)

        # Stage all changes (respecting .gitignore)
        await self._run_git("add", "-A")

        # Commit
        await self._run_git("commit", "-m", message)

        # Get the commit info
        log_output = await self._run_git(
            "log", "-1", "--format=%H%n%h%n%s%n%an%n%aI"
        )
        parts = log_output.splitlines()
        if len(parts) >= 5:
            try:
                timestamp = datetime.fromisoformat(parts[4])
            except ValueError:
                timestamp = datetime.now(tz=timezone.utc)

            commit_info = CommitInfo(
                hash=parts[0],
                hash_short=parts[1],
                message=parts[2],
                author=parts[3],
                timestamp=timestamp,
            )
            _LOGGER.info("Created commit %s: %s", commit_info.hash_short, message)
            return commit_info

        return None

    def _generate_commit_message(self, porcelain_output: str) -> str:
        """Generate a commit message from porcelain status output."""
        files: list[str] = []
        for line in porcelain_output.splitlines():
            if len(line) >= 4:
                filepath = line[3:].strip()
                # Get just the filename, not the full path
                filename = Path(filepath).name
                if filename and filename not in files:
                    files.append(filename)

        if not files:
            return "Auto: configuration updated"

        if len(files) <= 3:
            return f"Auto: {', '.join(files)} changed"

        return f"Auto: {len(files)} files changed"

    async def push(self) -> int:
        """Push commits to the configured remote.

        Returns:
            Number of commits that were pushed.
        """
        # Get ahead count before push
        try:
            ab_output = await self._run_git(
                "rev-list", "--left-right", "--count", "HEAD...@{u}"
            )
            ahead = int(ab_output.split()[0]) if ab_output else 0
        except (GitError, ValueError, IndexError):
            ahead = 0

        # Get the current branch
        branch = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")

        await self._run_git("push", "-u", "origin", branch)
        _LOGGER.info("Pushed %d commit(s) to origin/%s", ahead, branch)
        return ahead

    async def pull(self, backup: bool = True) -> int:
        """Pull from the configured remote.

        Args:
            backup: If True, create a backup commit of uncommitted changes first.

        Returns:
            Number of commits pulled.
        """
        # Backup uncommitted changes before pull
        if backup:
            porcelain = await self._run_git("status", "--porcelain")
            if porcelain:
                await self._run_git("add", "-A")
                await self._run_git(
                    "commit", "-m",
                    "Backup: auto-saved before pull"
                )
                _LOGGER.info("Created backup commit before pull")

        # Get commit count before pull
        try:
            count_before = int(
                await self._run_git("rev-list", "--count", "HEAD")
            )
        except (GitError, ValueError):
            count_before = 0

        branch = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        await self._run_git("pull", "origin", branch)

        # Count new commits
        try:
            count_after = int(
                await self._run_git("rev-list", "--count", "HEAD")
            )
            pulled = count_after - count_before
        except (GitError, ValueError):
            pulled = 0

        _LOGGER.info("Pulled %d commit(s) from origin/%s", pulled, branch)
        return pulled

    async def get_log(self, count: int = 10) -> list[CommitInfo]:
        """Get recent commit history.

        Args:
            count: Number of commits to retrieve.

        Returns:
            List of CommitInfo objects.
        """
        commits: list[CommitInfo] = []
        try:
            log_output = await self._run_git(
                "log", f"-{count}", "--format=%H%n%h%n%s%n%an%n%aI%n---"
            )
            if not log_output:
                return commits

            entries = log_output.split("---\n")
            for entry in entries:
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.splitlines()
                if len(parts) >= 5:
                    try:
                        timestamp = datetime.fromisoformat(parts[4])
                    except ValueError:
                        timestamp = datetime.now(tz=timezone.utc)

                    commits.append(
                        CommitInfo(
                            hash=parts[0],
                            hash_short=parts[1],
                            message=parts[2],
                            author=parts[3],
                            timestamp=timestamp,
                        )
                    )
        except GitError:
            pass

        return commits

    async def get_diff(self) -> str:
        """Get the current diff of uncommitted changes."""
        try:
            diff = await self._run_git("diff")
            staged_diff = await self._run_git("diff", "--cached")
            combined = ""
            if diff:
                combined += diff
            if staged_diff:
                if combined:
                    combined += "\n"
                combined += staged_diff
            return combined
        except GitError:
            return ""

    async def setup_gitignore(self) -> bool:
        """Create or update .gitignore with security defaults.

        Returns:
            True if .gitignore was created or modified.
        """
        gitignore_path = Path(self._repo_path) / ".gitignore"
        existing_entries: set[str] = set()
        modified = False

        if gitignore_path.exists():
            content = gitignore_path.read_text(encoding="utf-8")
            existing_entries = {
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
        else:
            content = ""

        # Check which default entries are missing
        entries_to_add: list[str] = []
        for entry in DEFAULT_GITIGNORE_ENTRIES:
            clean = entry.strip()
            if not clean or clean.startswith("#"):
                # Keep comments and blank lines for new files
                if not existing_entries:
                    entries_to_add.append(entry)
                continue
            if clean not in existing_entries:
                entries_to_add.append(entry)
                modified = True

        if modified or not gitignore_path.exists():
            if content and not content.endswith("\n"):
                content += "\n"
            if content and entries_to_add:
                content += "\n# Added by git-ha-ppens\n"

            content += "\n".join(entries_to_add) + "\n"
            gitignore_path.write_text(content, encoding="utf-8")
            _LOGGER.info("Updated .gitignore at %s", gitignore_path)
            return True

        return False

    async def scan_for_secrets(self) -> list[dict[str, str]]:
        """Scan staged files for potential secrets.

        Returns:
            List of dicts with 'file', 'line', and 'pattern' keys.
        """
        findings: list[dict[str, str]] = []

        try:
            # Get list of staged files
            staged = await self._run_git("diff", "--cached", "--name-only", check=False)
            if not staged:
                # Also check tracked files if nothing staged
                staged = await self._run_git(
                    "ls-files", "--modified", check=False
                )
            if not staged:
                return findings

            compiled_patterns = [re.compile(p) for p in SECRET_PATTERNS]

            for filepath in staged.splitlines():
                filepath = filepath.strip()
                if not filepath:
                    continue
                # Only scan text-like files
                if not filepath.endswith(
                    (".yaml", ".yml", ".json", ".conf", ".cfg", ".ini", ".txt", ".env")
                ):
                    continue

                full_path = Path(self._repo_path) / filepath
                if not full_path.is_file():
                    continue

                try:
                    content = full_path.read_text(encoding="utf-8", errors="ignore")
                    for line_num, line in enumerate(content.splitlines(), 1):
                        for pattern in compiled_patterns:
                            if pattern.search(line):
                                findings.append(
                                    {
                                        "file": filepath,
                                        "line": str(line_num),
                                        "pattern": pattern.pattern[:50],
                                    }
                                )
                                break  # One finding per line is enough
                except OSError:
                    continue

        except GitError:
            pass

        if findings:
            _LOGGER.warning(
                "Secret scan found %d potential secret(s) in tracked files",
                len(findings),
            )

        return findings

    async def set_remote(self, url: str) -> None:
        """Configure the remote origin URL."""
        # Check if remote exists
        remotes = await self._run_git("remote", check=False)
        if "origin" in remotes.splitlines():
            await self._run_git("remote", "set-url", "origin", url)
        else:
            await self._run_git("remote", "add", "origin", url)
        _LOGGER.info("Remote origin set to %s", self._redact_url(url))

    async def configure_token_auth(self, url: str, token: str) -> None:
        """Configure token-based authentication by embedding in the remote URL."""
        # For HTTPS URLs, embed the token
        if url.startswith("https://"):
            # https://token@github.com/user/repo.git
            authed_url = url.replace("https://", f"https://oauth2:{token}@")
            await self.set_remote(authed_url)
        else:
            _LOGGER.warning("Token auth only works with HTTPS URLs")

    async def configure_ssh_auth(self, ssh_key_path: str) -> None:
        """Configure SSH-based authentication."""
        key_path = Path(ssh_key_path)
        if not key_path.is_file():
            raise GitError(f"SSH key not found: {ssh_key_path}")

        # Set GIT_SSH_COMMAND in repo config
        ssh_command = f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=accept-new"
        await self._run_git(
            "config", "core.sshCommand", ssh_command
        )
        _LOGGER.info("SSH authentication configured with key: %s", ssh_key_path)

    @staticmethod
    def _redact_url(url: str) -> str:
        """Redact sensitive parts of a URL for logging."""
        # Remove tokens/passwords from URLs
        redacted = re.sub(r"://[^@]+@", "://***@", url)
        return redacted
