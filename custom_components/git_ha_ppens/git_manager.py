"""Git operations manager for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
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
    changed_files: list[str] = field(default_factory=list)


@dataclass
class PullResult:
    """Represents the result of a pull operation."""

    commits_pulled: int
    changed_files: list[str] = field(default_factory=list)


@dataclass
class RestoreFileChange:
    """Describe one tracked path changed by a restore."""

    status: str
    path: str
    old_path: str | None = None


@dataclass
class RestorePreview:
    """Describe the effect of restoring a historical commit."""

    source_head: str
    target: CommitInfo
    commits: list[CommitInfo] = field(default_factory=list)
    changed_files: list[RestoreFileChange] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    binary_files: int = 0


@dataclass
class RestoreResult:
    """Describe a completed snapshot restore commit."""

    commit: CommitInfo
    target: CommitInfo
    commits_restored: int
    changed_files: list[str] = field(default_factory=list)


class GitError(Exception):
    """Raised when a git operation fails."""


class PreDeployCheckError(GitError):
    """Raised when a pre-deploy check fails and the pull was rolled back.

    Subclasses GitError so existing ``except GitError`` paths still handle it,
    while callers that want to react specifically can catch this first.
    """

    def __init__(self, errors: list[str]) -> None:
        """Store the individual check error messages."""
        self.errors = errors
        joined = "; ".join(errors) if errors else "unknown error"
        super().__init__(f"Pre-deploy check failed: {joined}")


class RestoreError(GitError):
    """Base error for snapshot restore operations."""


class InvalidRestoreTargetError(RestoreError):
    """Raised when a restore target is invalid or unsafe."""


class DirtyWorkingTreeError(RestoreError):
    """Raised when a restore is attempted with local changes."""


class StaleRestorePreviewError(RestoreError):
    """Raised when HEAD changed after the restore preview was created."""


class RestoreValidationError(RestoreError):
    """Raised when the restored snapshot fails Home Assistant validation."""

    def __init__(self, errors: list[str]) -> None:
        """Store validation errors."""
        self.errors = errors
        joined = "; ".join(errors) if errors else "unknown error"
        super().__init__(f"Restored configuration is invalid: {joined}")


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
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": os.path.realpath(self._repo_path),
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

            # Verify .git was actually created
            git_dir = repo_path / ".git"
            if not git_dir.exists():
                raise GitError(
                    f"git init succeeded but .git directory was not created at "
                    f"{self._repo_path}. Check directory permissions and ownership."
                )

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

    async def get_head_sha(self) -> str:
        """Return the current HEAD commit SHA (empty string if unborn)."""
        result = await self._run_git(
            "rev-parse", "--verify", "HEAD", check=False
        )
        if result and len(result) >= 40 and "fatal" not in result.lower():
            return result
        return ""

    async def reset_hard(self, ref: str) -> None:
        """Hard-reset the working tree to the given ref (rollback)."""
        await self._run_git("reset", "--hard", ref)
        _LOGGER.info("Rolled back working tree to %s", ref[:8])

    async def discard_changes(self) -> int:
        """Discard staged and unstaged changes to tracked files.

        Untracked files are intentionally preserved.

        Returns:
            Number of tracked files restored to HEAD.
        """
        if not await self.has_commits():
            return 0

        changed_output = await self._run_git(
            "diff", "--name-only", "HEAD", check=False
        )
        changed_files = {
            path.strip() for path in changed_output.splitlines() if path.strip()
        }
        if not changed_files:
            return 0

        await self.reset_hard("HEAD")
        _LOGGER.info(
            "Discarded local changes in %d tracked file(s)",
            len(changed_files),
        )
        return len(changed_files)

    async def get_upstream_sha(self) -> str:
        """Return the upstream tracking branch SHA (empty string if none)."""
        result = await self._run_git(
            "rev-parse", "--verify", "@{u}", check=False
        )
        if result and len(result) >= 40 and "fatal" not in result.lower():
            return result
        return ""

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
        changed_files_output = await self._run_git(
            "diff", "--cached", "--name-only"
        )
        changed_files = sorted(
            path.strip()
            for path in changed_files_output.splitlines()
            if path.strip()
        )

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
                changed_files=changed_files,
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

    async def fetch(self) -> None:
        """Fetch from the configured remote without merging.

        Raises:
            GitError: If remote is not configured or fetch fails.
        """
        if not await self.is_remote_configured():
            raise GitError(
                "Remote 'origin' is not configured."
            )

        branch = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        await self._run_git("fetch", "origin", branch)
        _LOGGER.debug("Fetched from origin/%s", branch)

    async def pull(
        self,
        backup: bool = True,
        validate: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> PullResult:
        """Pull from the configured remote.

        Args:
            backup: If True, create a backup commit of uncommitted changes first.
            validate: Optional async callback run after merging new commits. It
                returns a list of error messages; if non-empty, the pull is
                rolled back to the pre-pull state and PreDeployCheckError is
                raised.

        Returns:
            Pull result containing the number of commits and changed files.

        Raises:
            PreDeployCheckError: If validation fails (after rolling back).
            GitError: If the pull itself fails.
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

        # Capture HEAD right before the merge so a rollback restores this exact
        # state (including the backup commit above) and only discards the merge.
        pre_pull_head = await self.get_head_sha()

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

        # Pre-deploy gate: validate the merged result before keeping it.
        if validate is not None and pulled > 0 and pre_pull_head:
            errors = await validate()
            if errors:
                _LOGGER.warning(
                    "Pre-deploy check failed after pull; rolling back to %s",
                    pre_pull_head[:8],
                )
                await self.reset_hard(pre_pull_head)
                raise PreDeployCheckError(errors)

        changed_files: list[str] = []
        if pre_pull_head:
            changed_files_output = await self._run_git(
                "diff",
                "--name-only",
                pre_pull_head,
                "HEAD",
            )
            changed_files = sorted(
                path.strip()
                for path in changed_files_output.splitlines()
                if path.strip()
            )

        _LOGGER.info("Pulled %d commit(s) from origin/%s", pulled, branch)
        return PullResult(
            commits_pulled=pulled,
            changed_files=changed_files,
        )

    async def _get_commit_info(self, commit: str) -> CommitInfo:
        """Return metadata for one already validated commit SHA."""
        output = await self._run_git(
            "show",
            "-s",
            "--format=%H%x00%h%x00%s%x00%an%x00%aI",
            commit,
        )
        parts = output.split("\x00")
        if len(parts) != 5:
            raise GitError(f"Could not read commit metadata for {commit[:8]}")
        try:
            timestamp = datetime.fromisoformat(parts[4])
        except ValueError as err:
            raise GitError(
                f"Could not parse commit timestamp for {commit[:8]}"
            ) from err
        return CommitInfo(
            hash=parts[0],
            hash_short=parts[1],
            message=parts[2],
            author=parts[3],
            timestamp=timestamp,
        )

    async def resolve_restore_target(self, commit_ref: str) -> CommitInfo:
        """Resolve and validate a historical commit on the current branch."""
        normalized_ref = commit_ref.strip()
        if re.fullmatch(r"[0-9a-fA-F]{7,40}", normalized_ref) is None:
            raise InvalidRestoreTargetError(
                "Enter a commit SHA containing 7 to 40 hexadecimal characters"
            )

        resolved = await self._run_git(
            "rev-parse",
            "--verify",
            f"{normalized_ref}^{{commit}}",
            check=False,
        )
        if re.fullmatch(r"[0-9a-f]{40}", resolved) is None:
            raise InvalidRestoreTargetError(
                "The selected commit could not be found or is ambiguous"
            )

        head = await self.get_head_sha()
        if not head:
            raise InvalidRestoreTargetError("The repository has no commits")
        if resolved == head:
            raise InvalidRestoreTargetError(
                "The current commit cannot be restored because it is already active"
            )

        merge_base = await self._run_git(
            "merge-base", resolved, head, check=False
        )
        if merge_base != resolved:
            raise InvalidRestoreTargetError(
                "The selected commit is not an ancestor of the current branch"
            )
        return await self._get_commit_info(resolved)

    async def is_worktree_clean(self) -> bool:
        """Return whether tracked, staged, and untracked files are clean."""
        return not bool(await self._run_git("status", "--porcelain"))

    async def _get_restore_commits(
        self, target_hash: str, source_head: str
    ) -> list[CommitInfo]:
        """Return commits newer than the restore target."""
        output = await self._run_git(
            "log",
            "--format=%H%x00%h%x00%s%x00%an%x00%aI%x00",
            f"{target_hash}..{source_head}",
        )
        fields = output.rstrip("\x00\n").split("\x00") if output else []
        commits: list[CommitInfo] = []
        for index in range(0, len(fields), 5):
            parts = fields[index : index + 5]
            if len(parts) != 5:
                raise GitError("Could not parse restore commit history")
            try:
                timestamp = datetime.fromisoformat(parts[4].strip())
            except ValueError as err:
                raise GitError("Could not parse restore commit history") from err
            commits.append(
                CommitInfo(
                    hash=parts[0].lstrip("\n"),
                    hash_short=parts[1],
                    message=parts[2],
                    author=parts[3],
                    timestamp=timestamp,
                )
            )
        return commits

    async def _get_restore_file_changes(
        self, target_hash: str, source_head: str
    ) -> list[RestoreFileChange]:
        """Return path changes between a target snapshot and current HEAD."""
        output = await self._run_git(
            "diff", "--name-status", "-z", target_hash, source_head
        )
        tokens = output.split("\x00") if output else []
        changes: list[RestoreFileChange] = []
        index = 0
        while index < len(tokens) and tokens[index]:
            status = tokens[index]
            index += 1
            if status.startswith(("R", "C")):
                if index + 1 >= len(tokens):
                    raise GitError("Could not parse renamed restore paths")
                old_path, new_path = tokens[index], tokens[index + 1]
                index += 2
                path = new_path
            else:
                if index >= len(tokens):
                    raise GitError("Could not parse restore paths")
                old_path = None
                path = tokens[index]
                index += 1
            changes.append(
                RestoreFileChange(
                    status=status,
                    path=path,
                    old_path=old_path,
                )
            )
        return changes

    async def _get_restore_numstat(
        self, target_hash: str, source_head: str
    ) -> tuple[int, int, int]:
        """Return total additions, deletions, and binary file count."""
        output = await self._run_git(
            "diff", "--numstat", "-z", target_hash, source_head
        )
        additions = deletions = binary_files = 0
        for record in output.split("\x00") if output else []:
            if not record:
                continue
            parts = record.split("\t", 2)
            if len(parts) < 2:
                continue
            added, deleted = parts[:2]
            if added == "-" or deleted == "-":
                binary_files += 1
                continue
            try:
                additions += int(added)
                deletions += int(deleted)
            except ValueError:
                continue
        return additions, deletions, binary_files

    async def get_restore_preview(self, commit_ref: str) -> RestorePreview:
        """Build a non-mutating preview of restoring a historical commit."""
        target = await self.resolve_restore_target(commit_ref)
        source_head = await self.get_head_sha()
        commits = await self._get_restore_commits(target.hash, source_head)
        changed_files = await self._get_restore_file_changes(
            source_head, target.hash
        )
        if not changed_files:
            raise InvalidRestoreTargetError(
                "The selected commit has the same tracked file tree as HEAD"
            )
        additions, deletions, binary_files = await self._get_restore_numstat(
            source_head, target.hash
        )
        return RestorePreview(
            source_head=source_head,
            target=target,
            commits=commits,
            changed_files=changed_files,
            additions=additions,
            deletions=deletions,
            binary_files=binary_files,
        )

    async def restore_snapshot(
        self,
        target_ref: str,
        expected_head: str,
        validate: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> RestoreResult:
        """Restore a historical tree as a new commit without rewriting history."""
        current_head = await self.get_head_sha()
        if current_head != expected_head:
            raise StaleRestorePreviewError(
                "Repository history changed after the restore preview was created"
            )
        if not await self.is_worktree_clean():
            raise DirtyWorkingTreeError(
                "Commit, sync, or discard all local changes before restoring"
            )

        preview = await self.get_restore_preview(target_ref)
        if preview.source_head != expected_head:
            raise StaleRestorePreviewError(
                "Repository history changed after the restore preview was created"
            )

        original_head = current_head
        restore_verified = False
        restore_commit: CommitInfo | None = None
        try:
            await self._run_git(
                "read-tree", "--reset", "-u", preview.target.hash
            )
            staged_paths = await self._run_git(
                "diff", "--cached", "--name-only"
            )
            if not staged_paths:
                raise InvalidRestoreTargetError(
                    "The selected commit has the same tracked file tree as HEAD"
                )

            if validate is not None:
                errors = await validate()
                if errors:
                    raise RestoreValidationError(errors)

            subject = (
                f"revert: restore configuration to {preview.target.hash_short}"
            )
            body = (
                f"Restores tracked configuration snapshot from commit "
                f"{preview.target.hash}.\n\nOriginal subject: "
                f"{preview.target.message}"
            )
            await self._run_git("commit", "-m", subject, "-m", body)

            restore_commit = await self._get_commit_info("HEAD")
            restore_tree = await self._run_git("rev-parse", "HEAD^{tree}")
            target_tree = await self._run_git(
                "rev-parse", f"{preview.target.hash}^{{tree}}"
            )
            if restore_tree != target_tree:
                raise RestoreError(
                    "Restore commit tree does not match the selected target tree"
                )
            restore_verified = True
        except BaseException:
            if not restore_verified:
                try:
                    await asyncio.shield(self.reset_hard(original_head))
                except GitError as rollback_err:
                    _LOGGER.critical(
                        "Could not roll back failed restore to %s: %s",
                        original_head[:8],
                        rollback_err,
                    )
            raise

        if restore_commit is None:
            raise RestoreError("Restore commit metadata is unavailable")

        return RestoreResult(
            commit=restore_commit,
            target=preview.target,
            commits_restored=len(preview.commits),
            changed_files=[change.path for change in preview.changed_files],
        )

    async def get_log(self, count: int = 10) -> list[CommitInfo]:
        """Get recent commit history."""
        if count <= 0:
            return []
        commits: list[CommitInfo] = []
        try:
            log_output = await self._run_git(
                "log",
                "-n",
                str(count),
                "--format=%H%x00%h%x00%s%x00%an%x00%aI%x00",
            )
            if not log_output:
                return commits

            fields = log_output.rstrip("\x00\n").split("\x00")
            for index in range(0, len(fields), 5):
                parts = fields[index : index + 5]
                if len(parts) != 5:
                    raise GitError("Could not parse commit history")
                try:
                    timestamp = datetime.fromisoformat(parts[4].strip())
                except ValueError:
                    timestamp = datetime.now(tz=timezone.utc)
                commits.append(
                    CommitInfo(
                        hash=parts[0].lstrip("\n"),
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

    async def setup_gitignore(self, skip_defaults: bool = False) -> bool:
        """Create or update .gitignore with security defaults."""
        return await asyncio.to_thread(self._setup_gitignore_sync, skip_defaults)

    def _setup_gitignore_sync(self, skip_defaults: bool = False) -> bool:
        """Synchronous implementation of setup_gitignore."""
        if skip_defaults:
            # User manages .gitignore via UI; don't append defaults
            return False

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

    async def apply_gitignore(self) -> None:
        """Remove tracked files that are now covered by .gitignore.

        Runs 'git rm -r --cached .' followed by 'git add -A'
        to re-apply .gitignore rules to the index.
        """
        await self._run_git("rm", "-r", "--cached", ".", check=False)
        await self._run_git("add", "-A")
        _LOGGER.info("Re-applied .gitignore rules to tracked files")

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
