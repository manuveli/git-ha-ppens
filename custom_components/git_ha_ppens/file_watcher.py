"""File watcher for auto-commit functionality in git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from homeassistant.core import HomeAssistant

from .ai_commit import async_generate_ai_commit_message
from .const import EVENT_COMMIT, EVENT_ERROR, EVENT_PUSH, WATCHER_IGNORE_PATTERNS
from .coordinator import GitHaPpensCoordinator
from .git_manager import GitError, GitManager

_LOGGER = logging.getLogger(__name__)


class _ChangeCollector(FileSystemEventHandler):
    """Collects file system change events for debounced processing."""

    def __init__(
        self,
        repo_path: str,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the change collector."""
        super().__init__()
        self._repo_path = repo_path
        self._changed_files: set[str] = set()
        self._on_change = on_change

    @property
    def changed_files(self) -> set[str]:
        """Return the set of changed files."""
        return self._changed_files

    def clear(self) -> None:
        """Clear collected changes."""
        self._changed_files.clear()

    def _should_ignore(self, path: str) -> bool:
        """Check if a path should be ignored."""
        path_obj = Path(path)
        parts = path_obj.parts

        for pattern in WATCHER_IGNORE_PATTERNS:
            # Check directory names
            if pattern in parts:
                return True
            # Check file extensions
            if pattern.startswith("*.") and path_obj.suffix == pattern[1:]:
                return True
            # Check exact filename
            if path_obj.name == pattern:
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
        self._observer: Observer | None = None
        self._change_collector: _ChangeCollector | None = None
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Return True if the file watcher is active."""
        return self._running

    async def async_start(self) -> None:
        """Start watching for file changes."""
        if self._running:
            return

        self._change_collector = _ChangeCollector(
            self._repo_path, on_change=self.schedule_commit
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
            message = None
            if self._ai_commit_enabled:
                try:
                    diff = await self._git_manager.get_diff()
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
                self._hass.bus.async_fire(
                    EVENT_COMMIT,
                    {
                        "hash": commit_info.hash_short,
                        "message": commit_info.message,
                        "author": commit_info.author,
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
                        commits_pushed = await self._git_manager.push()
                        self._hass.bus.async_fire(
                            EVENT_PUSH,
                            {"commits_pushed": commits_pushed, "auto": True},
                        )
                        _LOGGER.info(
                            "Auto-push: %d commit(s) pushed to remote",
                            commits_pushed,
                        )
                    except GitError as push_err:
                        _LOGGER.error("Auto-push failed: %s", push_err)
                        self._hass.bus.async_fire(
                            EVENT_ERROR,
                            {"operation": "auto_push", "error": str(push_err)},
                        )

                await self._coordinator.async_request_refresh()
        except GitError as err:
            _LOGGER.error("Auto-commit failed: %s", err)
            self._hass.bus.async_fire(
                EVENT_ERROR,
                {"operation": "auto_commit", "error": str(err)},
            )

    async def async_check_and_commit(self) -> None:
        """Check for changes and commit if any exist.

        Called periodically as a fallback when watchdog events may not fire
        (e.g. on Docker overlay filesystems). Also handles accumulated changes
        from the file watcher.
        """
        # First check collector for watchdog-detected changes
        if self._change_collector and self._change_collector.changed_files:
            await self._async_auto_commit()
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
                await self._async_auto_commit()
        except GitError:
            pass
