"""DataUpdateCoordinator for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_FETCH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_ERROR,
    EVENT_FETCH,
    EVENT_PULL,
)
from .git_manager import GitError, GitManager, GitStatus

_LOGGER = logging.getLogger(__name__)


class GitHaPpensCoordinator(DataUpdateCoordinator[GitStatus]):
    """Coordinator to poll git repository status."""

    def __init__(
        self,
        hass: HomeAssistant,
        git_manager: GitManager,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        auto_pull: bool = False,
        remote_configured: bool = False,
        fetch_interval: int = DEFAULT_FETCH_INTERVAL,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.git_manager = git_manager
        self._auto_pull = auto_pull
        self._remote_configured = remote_configured
        self._fetch_interval = fetch_interval
        self.git_lock = asyncio.Lock()

        self._last_fetch_time: datetime | None = None
        self._last_pull_time: datetime | None = None
        self._last_push_time: datetime | None = None

    @property
    def last_fetch_time(self) -> datetime | None:
        """Return the last fetch timestamp."""
        return self._last_fetch_time

    @property
    def last_pull_time(self) -> datetime | None:
        """Return the last pull timestamp."""
        return self._last_pull_time

    @property
    def last_push_time(self) -> datetime | None:
        """Return the last push timestamp."""
        return self._last_push_time

    def record_push_time(self) -> None:
        """Record that a push just happened."""
        self._last_push_time = datetime.now(tz=timezone.utc)

    def record_pull_time(self) -> None:
        """Record that a pull just happened."""
        self._last_pull_time = datetime.now(tz=timezone.utc)

    async def _async_update_data(self) -> GitStatus:
        """Fetch git status from the repository."""
        # Fetch from remote if enough time has passed
        if self._remote_configured:
            await self._maybe_fetch()

        try:
            status = await self.git_manager.get_status()
        except GitError as err:
            raise UpdateFailed(f"Error fetching git status: {err}") from err

        # Auto-pull if enabled and remote has new commits
        if self._auto_pull and self._remote_configured and status.behind > 0:
            if self.git_lock.locked():
                _LOGGER.debug(
                    "Skipping auto-pull: another git operation in progress"
                )
                return status

            async with self.git_lock:
                try:
                    commits_pulled = await self.git_manager.pull(backup=True)
                    self._last_pull_time = datetime.now(tz=timezone.utc)
                    self.hass.bus.async_fire(
                        EVENT_PULL,
                        {"commits_pulled": commits_pulled, "auto": True},
                    )
                    _LOGGER.info(
                        "Auto-pull: %d commit(s) pulled from remote",
                        commits_pulled,
                    )

                    # Re-fetch status so sensors reflect the post-pull state
                    try:
                        status = await self.git_manager.get_status()
                    except GitError:
                        pass

                except GitError as err:
                    _LOGGER.warning("Auto-pull failed: %s", err)
                    self.hass.bus.async_fire(
                        EVENT_ERROR,
                        {"operation": "auto_pull", "error": str(err)},
                    )

        return status

    async def _maybe_fetch(self) -> None:
        """Fetch from remote if the fetch interval has elapsed."""
        now = datetime.now(tz=timezone.utc)

        if self._last_fetch_time is not None:
            elapsed = (now - self._last_fetch_time).total_seconds()
            if elapsed < self._fetch_interval:
                return

        if self.git_lock.locked():
            _LOGGER.debug("Skipping fetch: another git operation in progress")
            return

        async with self.git_lock:
            try:
                await self.git_manager.fetch()
                self._last_fetch_time = datetime.now(tz=timezone.utc)
                self.hass.bus.async_fire(
                    EVENT_FETCH,
                    {"auto": True},
                )
                _LOGGER.debug("Auto-fetch completed")
            except GitError as err:
                _LOGGER.warning("Auto-fetch failed: %s", err)
                # Record time even on failure to avoid hammering a broken remote
                self._last_fetch_time = datetime.now(tz=timezone.utc)
                self.hass.bus.async_fire(
                    EVENT_ERROR,
                    {"operation": "auto_fetch", "error": str(err)},
                )
