"""DataUpdateCoordinator for git-ha-ppens."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .git_manager import GitError, GitManager, GitStatus

_LOGGER = logging.getLogger(__name__)


class GitHaPpensCoordinator(DataUpdateCoordinator[GitStatus]):
    """Coordinator to poll git repository status."""

    def __init__(
        self,
        hass: HomeAssistant,
        git_manager: GitManager,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.git_manager = git_manager

    async def _async_update_data(self) -> GitStatus:
        """Fetch git status from the repository."""
        try:
            return await self.git_manager.get_status()
        except GitError as err:
            raise UpdateFailed(f"Error fetching git status: {err}") from err
