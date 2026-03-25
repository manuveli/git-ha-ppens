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
    has_upstream: bool = False


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
            stdout = stdout_bytes.decode("utf-8", errors="replace").rstrip()
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

        # Set default branch name for future inits
        await self._run_git("config", "init.defaultBranch", "main", check=False)

        # Normalize branch to "main" if repo has no commits yet (unborn HEAD)
        if not await self.has_commits():
            current_branch = await self._run_git(
                "rev-parse", "--abbrev-ref", "HEAD", check=False
            )
            if current_branch and current_branch != "main" and "fatal" not in current_branch:
                await self._run_git("branch", "-M", "main", check=False)
                _LOGGER.info("Renamed branch from %s to main", current_branch)

    async def has_commits(self) -> bool:
        """Check if the repository has any commits."""
        try:
            result = await self._run_git(
                "rev-parse", "--verify", "HEAD", check=False
            )
            return bool(result) and len(result) >= 40 and "fatal" not in result.lower()
        except GitError:
            return False

    async def _has_upstream(self) -> bool:
        """Check if the current branch has an upstream tracking branch."""
        try:
            result = await self._run_git(
                "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                check=False,
            )
            return bool(result) and "fatal" not in result.lower() and "error" not in result.lower()
        except GitError:
            return False

    async def is_remote_configured(self) -> bool:
        """Check if the remote 'origin' is configured with a valid URL."""
        try:
            result = await self._run_git(
                "remote", "get-url", "origin", check=False
            )
            return bool(result) and "fatal" not in result.lower() and "error" not in result.lower()
        except GitError:
            return False

    async def get_remote_url(self) -> str:
        """Return the configured remote URL (empty string if not set)."""
        try:
            result = await self._run_git(
                "remote", "get-url", "origin", check=False
            )
            if result and "fatal" not in result.lower() and "error" not in result.lower():
                return result
        except GitError:
            pass
        return ""

    async def get_status(self) -> GitStatus:
        """Get the complete repository status."""
        status = GitStatus()

        if not await self.is_repo_initialized():
            return status

        # Get current branch
        try:
            branch = await self._run_git(
                "rev-parse", "--abbrev-ref", "HEAD", check=False
            )
            status.branch = branch if branch and "fatal" not in branch else "unknown"
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
                "log", "-1", "--format=%H%n%h%n%s%n%an%n%aI", check=False
            )
            if log_format and "fatal" not in log_format.lower():
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
            pass

        # Get total commit count
        try:
            count_str = await self._run_git("rev-list", "--count", "HEAD", check=False)
            if count_str and "fatal" not in count_str.lower():
                status.total_commits = int(count_str)
        except (GitError, ValueError):
            status.total_commits = 0

        # Get ahead/behind remote
        try:
            status.has_upstream = await self._has_upstream()
            if status.has_upstream:
                status.remote_configured = True
                ab_output = await self._run_git(
                    "rev-list", "--left-right", "--count", "HEAD...@{u}",
                    check=False,
                )
                if ab_output and "fatal" not in ab_output.lower():
                    parts = ab_output.split()
                    if len(parts) == 2:
                        status.ahead = int(parts[0])
                        status.behind = int(parts[1])
            else:
                # No tracking branch — check if remote origin exists
                remotes = await self._run_git("remote", check=False)
                status.remote_configured = bool(remotes.strip())
                # Signal "not pushed" when remote exists but no tracking
                if status.remote_configured and status.total_commits > 0:
                    status.ahead = -1  # Convention: -1 = never pushed
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

        Handles multiple scenarios robustly:
        1. Normal push (fast-forward)
        2. Divergent histories (remote initialized with README)
        3. First push (no upstream tracking branch)

        Returns:
            Number of commits that were pushed.

        Raises:
            GitError: If push fails after all retry attempts.
        """
        # Pre-check: verify remote is configured before attempting any push
        if not await self.is_remote_configured():
            raise GitError(
                "Remote 'origin' is not configured. "
                "Please set a valid remote URL in the integration options "
                "(Settings → Devices & Services → git-ha-ppens → Configure)."
            )

        branch = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        has_upstream = await self._has_upstream()

        # Count commits to push
        ahead = 0
        if has_upstream:
            try:
                ab_output = await self._run_git(
                    "rev-list", "--left-right", "--count", "HEAD...@{u}",
                    check=False,
                )
                if ab_output and "fatal" not in ab_output.lower():
                    ahead = int(ab_output.split()[0])
            except (GitError, ValueError, IndexError):
                pass
        else:
            # No upstream — all local commits need pushing
            try:
                count_str = await self._run_git(
                    "rev-list", "--count", "HEAD", check=False
                )
                if count_str and "fatal" not in count_str.lower():
                    ahead = int(count_str)
            except (GitError, ValueError):
                ahead = 0

        # === ATTEMPT 1: Normal push ===
        try:
            await self._run_git("push", "-u", "origin", branch)
            _LOGGER.info("Push successful: %d commit(s) to origin/%s", ahead, branch)
            return ahead
        except GitError as err:
            error_lower = str(err).lower()
            _LOGGER.warning("Push attempt 1 failed: %s", err)

            # Check if it's a permission/auth/config error — don't retry these
            if any(keyword in error_lower for keyword in [
                "permission denied",
                "403",
                "401",
                "authentication",
                "could not read username",
                "invalid credentials",
                "authorization failed",
                "does not appear to be a git repository",
                "could not read from remote repository",
                "repository not found",
            ]):
                _LOGGER.error(
                    "Push failed due to authentication/permission/configuration error. "
                    "Check: 1) Remote URL is correct, "
                    "2) Repository exists on GitHub, "
                    "3) PAT has 'repo' scope. Error: %s",
                    err,
                )
                raise

            # For any other error (divergent history, rejected, etc.), try to resolve
            _LOGGER.info("Attempting to resolve push conflict...")

        # === ATTEMPT 2: Fetch + merge with allow-unrelated-histories ===
        try:
            # First try to fetch remote state
            await self._run_git("fetch", "origin", branch)

            # Check if remote branch exists and has commits
            remote_ref = f"origin/{branch}"
            remote_exists = await self._run_git(
                "rev-parse", "--verify", remote_ref, check=False
            )
            has_remote_commits = (
                bool(remote_exists)
                and len(remote_exists) >= 40
                and "fatal" not in remote_exists.lower()
            )

            if has_remote_commits:
                # Remote has commits — merge allowing unrelated histories
                _LOGGER.info(
                    "Remote has existing commits — merging with allow-unrelated-histories"
                )
                await self._run_git(
                    "merge", "--allow-unrelated-histories", "--no-edit",
                    remote_ref,
                )
            # else: remote branch is empty, straight push should work

            # Retry push
            await self._run_git("push", "-u", "origin", branch)
            _LOGGER.info(
                "Push successful after merge: %d commit(s) to origin/%s",
                ahead, branch,
            )
            return ahead

        except GitError as merge_err:
            merge_error_lower = str(merge_err).lower()
            _LOGGER.warning("Push attempt 2 (merge) failed: %s", merge_err)

            # If merge conflict, try force push as last resort
            if "conflict" in merge_error_lower or "merge" in merge_error_lower:
                _LOGGER.warning("Merge conflict detected, aborting merge...")
                await self._run_git("merge", "--abort", check=False)

        # === ATTEMPT 3: Force push with lease (safe force push) ===
        try:
            _LOGGER.warning(
                "Attempting force push with lease as last resort "
                "(this overwrites remote but is safe for single-user repos)"
            )
            await self._run_git("push", "--force-with-lease", "-u", "origin", branch)
            _LOGGER.info(
                "Force push successful: %d commit(s) to origin/%s",
                ahead, branch,
            )
            return ahead
        except GitError as force_err:
            _LOGGER.error(
                "All push attempts failed. Last error: %s. "
                "Please check: 1) PAT has 'repo' scope, "
                "2) Remote URL is correct, "
                "3) Repository exists on GitHub.",
                force_err,
            )
            raise

    async def pull(self, backup: bool = True) -> int:
        """Pull from the configured remote.

        Args:
            backup: If True, create a backup commit of uncommitted changes first.

        Returns:
            Number of commits pulled.
        """
        # Pre-check: verify remote is configured
        if not await self.is_remote_configured():
            raise GitError(
                "Remote 'origin' is not configured. "
                "Please set a valid remote URL in the integration options "
                "(Settings → Devices & Services → git-ha-ppens → Configure)."
            )

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
        """Get recent commit history."""
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
        """Create or update .gitignore with security defaults."""
        return await asyncio.to_thread(self._setup_gitignore_sync)

    def _setup_gitignore_sync(self) -> bool:
        """Synchronous implementation of setup_gitignore."""
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

        entries_to_add: list[str] = []
        for entry in DEFAULT_GITIGNORE_ENTRIES:
            clean = entry.strip()
            if not clean or clean.startswith("#"):
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
        """Scan staged files for potential secrets."""
        findings: list[dict[str, str]] = []

        try:
            staged = await self._run_git("diff", "--cached", "--name-only", check=False)
            if not staged:
                staged = await self._run_git(
                    "ls-files", "--modified", check=False
                )
            if not staged:
                return findings

            compiled_patterns = [re.compile(p) for p in SECRET_PATTERNS]

            filepaths = [
                fp.strip()
                for fp in staged.splitlines()
                if fp.strip()
                and fp.strip().endswith(
                    (".yaml", ".yml", ".json", ".conf", ".cfg", ".ini", ".txt", ".env")
                )
            ]

            if filepaths:
                findings = await asyncio.to_thread(
                    self._scan_files_for_secrets_sync, filepaths, compiled_patterns
                )

        except GitError:
            pass

        if findings:
            _LOGGER.warning(
                "Secret scan found %d potential secret(s) in tracked files",
                len(findings),
            )

        return findings

    def _scan_files_for_secrets_sync(
        self,
        filepaths: list[str],
        compiled_patterns: list[re.Pattern[str]],
    ) -> list[dict[str, str]]:
        """Synchronous implementation of file scanning for secrets."""
        findings: list[dict[str, str]] = []

        for filepath in filepaths:
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
                            break
            except OSError:
                continue

        return findings

    async def set_remote(self, url: str) -> None:
        """Configure the remote origin URL."""
        remotes = await self._run_git("remote", check=False)
        if "origin" in remotes.splitlines():
            await self._run_git("remote", "set-url", "origin", url)
        else:
            await self._run_git("remote", "add", "origin", url)
        _LOGGER.info("Remote origin set to %s", self._redact_url(url))

    async def configure_token_auth(self, url: str, token: str) -> None:
        """Configure token-based authentication by embedding in the remote URL."""
        if url.startswith("https://"):
            authed_url = url.replace("https://", f"https://oauth2:{token}@")
            await self.set_remote(authed_url)
        else:
            _LOGGER.warning("Token auth only works with HTTPS URLs")

    async def configure_ssh_auth(self, ssh_key_path: str) -> None:
        """Configure SSH-based authentication."""
        key_path = Path(ssh_key_path)
        if not key_path.is_file():
            raise GitError(f"SSH key not found: {ssh_key_path}")

        ssh_command = f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=accept-new"
        await self._run_git(
            "config", "core.sshCommand", ssh_command
        )
        _LOGGER.info("SSH authentication configured with key: %s", ssh_key_path)

    @staticmethod
    def _redact_url(url: str) -> str:
        """Redact sensitive parts of a URL for logging."""
        redacted = re.sub(r"://[^@]+@", "://***@", url)
        return redacted
