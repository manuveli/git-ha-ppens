"""DataUpdateCoordinator for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, EVENT_ERROR, EVENT_PULL
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
        self.git_lock = asyncio.Lock()

    async def _async_update_data(self) -> GitStatus:
        """Fetch git status from the repository."""
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
                        pass  # Return pre-pull status if re-fetch fails

                except GitError as err:
                    _LOGGER.warning("Auto-pull failed: %s", err)
                    self.hass.bus.async_fire(
                        EVENT_ERROR,
                        {"operation": "auto_pull", "error": str(err)},
                    )

        return status
