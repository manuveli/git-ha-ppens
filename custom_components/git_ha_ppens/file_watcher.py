"""File watcher for auto-commit functionality in git-ha-ppens."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path

from homeassistant.core import HomeAssistant
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .ai_commit import async_generate_ai_commit_message
from .const import (
    DEFAULT_SOPS_SECRETS_FILES,
    EVENT_COMMIT,
    EVENT_ERROR,
    EVENT_PUSH,
)
from .coordinator import GitHaPpensCoordinator
from .git_manager import GitError, GitManager, IndexLockError, PreDeployCheckError
from .sops_manager import SopsError
from .index_lock import (
    create_stale_index_lock_issue,
    delete_stale_index_lock_issue,
)
from .repository_layout import (
    async_ensure_repository_layout_safe,
    create_repository_layout_issue_from_error,
)

_LOGGER = logging.getLogger(__name__)


class _ChangeCollector(FileSystemEventHandler):
    """Collects file system change events for debounced processing."""

    # The .git directory must always be ignored regardless of .gitignore content.
    _ALWAYS_IGNORE: frozenset[str] = frozenset({".git"})

    def __init__(
        self,
        repo_path: str,
        on_change: Callable[[], None] | None = None,
        sops_enabled: bool = False,
        sops_secrets_files: Sequence[str] = DEFAULT_SOPS_SECRETS_FILES,
    ) -> None:
        """Initialize the change collector."""
        super().__init__()
        self._repo_path = repo_path
        self._changed_files: set[str] = set()
        self._on_change = on_change
        self._ignore_patterns: list[str] = []
        self._sops_enabled = sops_enabled
        self._sops_secrets_files = tuple(sops_secrets_files)
        self.suppress_secrets = False

    def _load_gitignore(self) -> None:
        """Load ignore patterns from the .gitignore file on disk.

        Patterns are re-read on every call so user edits take effect
        without restarting Home Assistant.
        """
        gitignore_path = Path(self._repo_path) / ".gitignore"
        patterns: list[str] = []
        if gitignore_path.exists():
            try:
                content = gitignore_path.read_text(encoding="utf-8")
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        # Normalise: remove trailing slashes so "deps/" matches
                        # bare directory names the same way "deps" does.
                        patterns.append(stripped.rstrip("/"))
            except OSError:
                _LOGGER.warning("Could not read .gitignore, using empty ignore list")
        self._ignore_patterns = patterns

    @property
    def changed_files(self) -> set[str]:
        """Return the set of changed files."""
        return self._changed_files

    def clear(self) -> None:
        """Clear collected changes."""
        self._changed_files.clear()

    def _should_ignore(self, path: str) -> bool:
        """Check if a path should be ignored based on .gitignore contents."""
        path_obj = Path(path)
        parts = path_obj.parts
        rel_path = self._get_relative_path(path)
        relative_parts = Path(rel_path).parts

        # Always ignore the .git directory itself
        if ".git" in parts:
            return True

        name_lower = path_obj.name.lower()
        # Temporary files (including the atomic-write temp files SOPS
        # handling creates) are never worth a commit.
        if name_lower.endswith(".tmp") or ".tmp." in name_lower:
            return True

        # Generated SOPS encrypted sidecars and .sops metadata are written by
        # this integration itself. Only skip them when SOPS is enabled so
        # repositories that do not use it keep their previous behaviour.
        if self._sops_enabled and (
            ".enc." in name_lower
            or name_lower.startswith(".sops")
            or name_lower.endswith((".sops.yaml", ".sops.yml"))
        ):
            return True

        # Check if this is a configured secrets file or matches a secret pattern
        is_secret = False
        rel_lower = rel_path.lower()
        is_example = (
            ".example." in name_lower
            or ".sample." in name_lower
            or ".template." in name_lower
        )
        if not is_example:
            if name_lower == "secrets.yaml":
                is_secret = True
            else:
                for pattern in self._sops_secrets_files:
                    pat_lower = pattern.lower()
                    if (
                        rel_lower == pat_lower
                        or name_lower == pat_lower
                        or fnmatchcase(rel_lower, pat_lower)
                        or fnmatchcase(name_lower, pat_lower)
                    ):
                        is_secret = True
                        break

        if is_secret:
            if self.suppress_secrets:
                return True
            if self._sops_enabled:
                return False

        # Re-read .gitignore so edits take effect without restart
        self._load_gitignore()

        # Case-sensitive throughout this loop, matching git's own .gitignore
        # semantics on the platforms this integration actually runs on
        # (Linux / HA OS / Docker all use case-sensitive filesystems, and
        # git's pattern matching is case-sensitive regardless of platform).
        # Case-folding here previously made this watcher heuristic treat
        # differently-cased paths as ignored when git would still track and
        # commit them -- the auto-commit debounce would then never fire for
        # a real change, relying only on the slower periodic fallback poll.
        for pattern in self._ignore_patterns:
            # Root-relative path globs such as "backups/*.tar" should match
            # only files at that exact depth, not nested or similarly named
            # directories elsewhere in the repository.
            if "/" in pattern:
                pattern_parts = Path(pattern.lstrip("/")).parts
                if len(relative_parts) == len(pattern_parts) and all(
                    fnmatchcase(path_part, pattern_part)
                    for path_part, pattern_part in zip(
                        relative_parts, pattern_parts, strict=True
                    )
                ):
                    return True
            # Check directory names (e.g. ".storage", "deps")
            if pattern in parts:
                return True
            # Check file extensions (e.g. "*.db", "*.log")
            if pattern.startswith("*.") and path_obj.suffix == pattern[1:]:
                return True
            # Check exact filename (e.g. "secrets.yaml", "CLAUDE.md")
            if path_obj.name == pattern:
                return True
            # Check wildcard filename (e.g. "*service_account*.json")
            if any(char in pattern for char in ("*", "?", "[", "]")):
                if fnmatchcase(path_obj.name, pattern) or fnmatchcase(rel_path, pattern):
                    return True

        return False

    def _get_relative_path(self, path: str) -> str:
        """Get the path relative to the repository root."""
        try:
            return str(Path(path).relative_to(self._repo_path))
        except ValueError:
            return path

    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modification events."""
        if event.is_directory:
            return
        self._handle_event(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        """Handle file creation events."""
        if event.is_directory:
            return
        self._handle_event(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Handle file deletion events."""
        if event.is_directory:
            return
        self._handle_event(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Handle file move events."""
        if event.is_directory:
            return
        self._handle_event(event.src_path)
        if hasattr(event, "dest_path"):
            self._handle_event(event.dest_path)

    def _handle_event(self, path: str) -> None:
        """Process a file system event."""
        if self._should_ignore(path):
            return

        relative = self._get_relative_path(path)
        self._changed_files.add(relative)
        _LOGGER.debug("File change detected: %s", relative)

        if self._on_change:
            self._on_change()


class GitFileWatcher:
    """Watches for file changes and auto-commits after a debounce interval."""

    def __init__(
        self,
        hass: HomeAssistant,
        git_manager: GitManager,
        coordinator: GitHaPpensCoordinator,
        repo_path: str,
        debounce_seconds: int = 300,
        auto_push: bool = False,
        remote_configured: bool = False,
        git_lock: asyncio.Lock | None = None,
        ai_commit_enabled: bool = False,
        ai_agent_id: str = "",
        entry_id: str = "",
        sops_enabled: bool = False,
        sops_secrets_files: Sequence[str] = DEFAULT_SOPS_SECRETS_FILES,
    ) -> None:
        """Initialize the file watcher."""
        self._hass = hass
        self._git_manager = git_manager
        self._coordinator = coordinator
        self._repo_path = repo_path
        self._debounce_seconds = debounce_seconds
        self._auto_push = auto_push
        self._remote_configured = remote_configured
        self._git_lock = git_lock
        self._ai_commit_enabled = ai_commit_enabled
        self._ai_agent_id = ai_agent_id
        self._entry_id = entry_id
        self._sops_enabled = sops_enabled
        self._sops_secrets_files = tuple(sops_secrets_files)
        self._observer: Observer | None = None
        self._change_collector: _ChangeCollector | None = None
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Return True if the file watcher is active."""
        return self._running

    @contextmanager
    def suppress_secrets(self) -> Generator[None, None, None]:
        """Temporarily suppress watcher events for secrets files.

        Best-effort only: watchdog delivers filesystem events asynchronously
        on its own OS thread, so an event for a write made just before
        __exit__ can still arrive after the flag is cleared. The actual loop
        prevention against re-triggering an auto-commit for our own
        encrypt/decrypt writes comes from SopsManager's content-hash cache
        (_plain_hashes), which is exact regardless of timing; this flag is a
        cheap optimization on top of that to avoid unnecessary
        git-status/hash-cache round trips while a suppressed operation is
        in flight, not the correctness guarantee itself.
        """
        if self._change_collector is not None:
            self._change_collector.suppress_secrets = True
        try:
            yield
        finally:
            if self._change_collector is not None:
                self._change_collector.suppress_secrets = False

    async def async_start(self) -> None:
        """Start watching for file changes."""
        if self._running:
            return

        self._change_collector = _ChangeCollector(
            self._repo_path,
            on_change=self.schedule_commit,
            sops_enabled=self._sops_enabled,
            sops_secrets_files=self._sops_secrets_files,
        )
        await self._hass.async_add_executor_job(
            self._change_collector._load_gitignore
        )
        self._observer = Observer()
        self._observer.schedule(
            self._change_collector,
            self._repo_path,
            recursive=True,
        )

        # Start observer in executor to avoid blocking
        await self._hass.async_add_executor_job(self._observer.start)
        self._running = True
        _LOGGER.info(
            "File watcher started for %s (debounce: %ds)",
            self._repo_path,
            self._debounce_seconds,
        )

    async def async_stop(self) -> None:
        """Stop watching for file changes."""
        if not self._running:
            return

        # Cancel pending debounce
        if self._debounce_handle:
            self._debounce_handle.cancel()
            self._debounce_handle = None

        if self._observer:
            self._observer.stop()
            await self._hass.async_add_executor_job(self._observer.join)
            self._observer = None

        self._running = False
        _LOGGER.info("File watcher stopped")

    def schedule_commit(self) -> None:
        """Schedule an auto-commit after the debounce interval.

        Thread-safe: can be called from the watchdog background thread.
        """
        self._hass.loop.call_soon_threadsafe(self._schedule_commit_on_loop)

    def _schedule_commit_on_loop(self) -> None:
        """Schedule the debounced commit on the event loop (must run on loop thread)."""
        if self._debounce_handle:
            self._debounce_handle.cancel()

        self._debounce_handle = self._hass.loop.call_later(
            self._debounce_seconds,
            lambda: self._hass.async_create_task(self._async_auto_commit()),
        )

    async def _async_auto_commit(self) -> None:
        """Perform the auto-commit."""
        if not self._change_collector:
            return

        changed = self._change_collector.changed_files.copy()
        if not changed:
            return

        self._change_collector.clear()

        await self._async_auto_commit_inner()

    async def _async_auto_commit_inner(self) -> None:
        """Perform the auto-commit, optionally guarded by the shared git lock."""
        if self._git_lock:
            async with self._git_lock:
                await self._do_commit_and_push()
        else:
            await self._do_commit_and_push()

    async def _do_commit_and_push(self) -> None:
        """Execute the actual commit and push sequence."""
        try:
            await async_ensure_repository_layout_safe(
                self._hass, self._entry_id, self._git_manager
            )
            message = None
            if self._ai_commit_enabled:
                try:
                    diff = await self._git_manager.get_ai_diff()
                    porcelain = await self._git_manager._run_git(
                        "status", "--porcelain", check=False
                    )
                    if diff or porcelain:
                        message = await async_generate_ai_commit_message(
                            self._hass, diff, porcelain, self._ai_agent_id
                        )
                except GitError:
                    pass

            commit_info = await self._git_manager.commit(message)
            if commit_info:
                delete_stale_index_lock_issue(self._hass, self._entry_id)
                self._hass.bus.async_fire(
                    EVENT_COMMIT,
                    {
                        "hash": commit_info.hash_short,
                        "message": commit_info.message,
                        "author": commit_info.author,
                        "changed_files": commit_info.changed_files,
                        "auto": True,
                    },
                )
                _LOGGER.info(
                    "Auto-commit: %s - %s",
                    commit_info.hash_short,
                    commit_info.message,
                )

                # Auto-push if enabled and remote is configured
                if self._auto_push and self._remote_configured:
                    try:
                        commits_pushed = await self._git_manager.push(
                            validate=self._coordinator.pre_deploy_validator()
                        )
                        await self._coordinator.async_record_push_time()
                        self._hass.bus.async_fire(
                            EVENT_PUSH,
                            {"commits_pushed": commits_pushed, "auto": True},
                        )
                        _LOGGER.info(
                            "Auto-push: %d commit(s) pushed to remote",
                            commits_pushed,
                        )
                    except PreDeployCheckError as push_err:
                        _LOGGER.warning(
                            "Auto-push blocked by pre-deploy check: %s",
                            push_err,
                        )
                        await self._coordinator.async_handle_pre_deploy_failure(
                            push_err.errors,
                            auto=True,
                        )
                    except GitError as push_err:
                        self._coordinator.record_auto_push_failure()
                        create_repository_layout_issue_from_error(
                            self._hass, self._entry_id, push_err
                        )
                        _LOGGER.error("Auto-push failed: %s", push_err)
                        self._hass.bus.async_fire(
                            EVENT_ERROR,
                            {"operation": "auto_push", "error": str(push_err)},
                        )

                await self._coordinator.async_request_refresh()
        except (GitError, SopsError) as err:
            if isinstance(err, GitError):
                create_repository_layout_issue_from_error(
                    self._hass, self._entry_id, err
                )
                if isinstance(err, IndexLockError) and err.requires_repair:
                    create_stale_index_lock_issue(
                        self._hass,
                        self._entry_id,
                        err.lock_path,
                    )
            _LOGGER.error("Auto-commit failed: %s", err)
            self._hass.bus.async_fire(
                EVENT_ERROR,
                {"operation": "auto_commit", "error": str(err)},
            )

    @staticmethod
    def _find_changed_plain_secret_sync(sops_mgr) -> str | None:
        """Return the relative path of the first changed plain secret, or None.

        Runs glob resolution and file reads, which are blocking I/O and must
        not execute directly on the event loop.
        """
        plain_files = sops_mgr.resolve_plain_secret_files()
        for rel in plain_files:
            plain_path = sops_mgr._repo_path / rel
            enc_path = sops_mgr._repo_path / sops_mgr.get_encrypted_relative_path(rel)
            if not plain_path.is_file():
                continue
            try:
                content_bytes = plain_path.read_bytes()
            except OSError:
                continue
            current_hash = hashlib.sha256(content_bytes).hexdigest()
            cached_hash = sops_mgr._plain_hashes.get(rel)
            if not enc_path.is_file() or cached_hash != current_hash:
                return rel
        return None

    async def async_check_and_commit(self) -> None:
        """Check for changes and commit if any exist.

        Called periodically as a fallback when watchdog events may not fire
        (e.g. on Docker overlay filesystems). Also handles accumulated changes
        from the file watcher.
        """
        try:
            if not await self._git_manager.is_index_lock_present():
                delete_stale_index_lock_issue(self._hass, self._entry_id)
        except GitError:
            # Keep an existing persistent repair when the lock cannot be checked.
            pass

        # First check collector for watchdog-detected changes
        if self._change_collector and self._change_collector.changed_files:
            await self._async_auto_commit()
            return

        # Check plain secret changes when SOPS is active (they are ignored in .gitignore,
        # so git status --porcelain won't see them on filesystems where watchdog misses events)
        sops_mgr = self._git_manager._sops_manager
        if sops_mgr is not None:
            changed_secret = await asyncio.to_thread(
                self._find_changed_plain_secret_sync, sops_mgr
            )
            if changed_secret is not None:
                _LOGGER.debug(
                    "Periodic check detected plain secret update: %s", changed_secret
                )
                await self._async_auto_commit_inner()
                return

        # Fallback: ask git directly if there are uncommitted changes
        try:
            porcelain = await self._git_manager._run_git(
                "status", "--porcelain", check=False
            )
            if porcelain and porcelain.strip():
                _LOGGER.debug(
                    "Periodic check found uncommitted changes (watchdog fallback)"
                )
                await self._async_auto_commit_inner()
        except GitError:
            pass
