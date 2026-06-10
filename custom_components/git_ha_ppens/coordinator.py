"""DataUpdateCoordinator for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ai_commit import async_generate_ai_commit_message
from .checks import async_run_pre_deploy_check, notify_check_failed
from .const import (
    DEFAULT_FETCH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_CHECK_FAILED,
    EVENT_COMMIT,
    EVENT_ERROR,
    EVENT_FETCH,
    EVENT_PULL,
    EVENT_PUSH,
    STORAGE_KEY_PREFIX,
    STORAGE_LAST_FETCH_TIME,
    STORAGE_LAST_PULL_TIME,
    STORAGE_LAST_PUSH_TIME,
    STORAGE_VERSION,
)
from .git_manager import GitError, GitManager, GitStatus, PreDeployCheckError

_LOGGER = logging.getLogger(__name__)


class GitHaPpensCoordinator(DataUpdateCoordinator[GitStatus]):
    """Coordinator to poll git repository status."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        git_manager: GitManager,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        auto_pull: bool = False,
        remote_configured: bool = False,
        fetch_interval: int = DEFAULT_FETCH_INTERVAL,
        pre_deploy_check: bool = False,
        ai_commit_enabled: bool = False,
        ai_agent_id: str = "",
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.git_manager = git_manager
        self._entry_id = entry_id
        self._store: Store[dict[str, str]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}"
        )
        self._auto_pull = auto_pull
        self._remote_configured = remote_configured
        self._fetch_interval = fetch_interval
        self._pre_deploy_check = pre_deploy_check
        self._ai_commit_enabled = ai_commit_enabled
        self._ai_agent_id = ai_agent_id
        self.git_lock = asyncio.Lock()

        self._last_fetch_time: datetime | None = None
        self._last_pull_time: datetime | None = None
        self._last_push_time: datetime | None = None
        # Remote SHA whose auto-pull was blocked by a failed pre-deploy check;
        # skip re-attempting (and re-running the heavy check) until it changes.
        self._blocked_remote_sha: str | None = None

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

    @property
    def remote_configured(self) -> bool:
        """Return whether a remote repository is configured."""
        return self._remote_configured

    async def async_load_stored_timestamps(self) -> None:
        """Load persisted operation timestamps for this config entry."""
        try:
            data = await self._store.async_load()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not load stored runtime state for git-ha-ppens entry %s: %s",
                self._entry_id,
                err,
            )
            return

        if not data:
            return

        if not isinstance(data, Mapping):
            _LOGGER.warning(
                "Ignoring invalid stored runtime state for git-ha-ppens entry %s",
                self._entry_id,
            )
            return

        self._last_fetch_time = self._parse_stored_timestamp(
            data, STORAGE_LAST_FETCH_TIME
        )
        self._last_pull_time = self._parse_stored_timestamp(
            data, STORAGE_LAST_PULL_TIME
        )
        self._last_push_time = self._parse_stored_timestamp(
            data, STORAGE_LAST_PUSH_TIME
        )

    async def async_record_fetch_time(self) -> None:
        """Record that a fetch just happened."""
        self._last_fetch_time = datetime.now(tz=timezone.utc)
        await self._async_save_timestamps()

    async def async_record_push_time(self) -> None:
        """Record that a push just happened."""
        self._last_push_time = datetime.now(tz=timezone.utc)
        await self._async_save_timestamps()

    async def async_record_pull_time(self) -> None:
        """Record that a pull just happened."""
        self._last_pull_time = datetime.now(tz=timezone.utc)
        await self._async_save_timestamps()

    def _parse_stored_timestamp(
        self, data: Mapping[str, Any], key: str
    ) -> datetime | None:
        """Parse a stored ISO timestamp as UTC."""
        value = data.get(key)
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            _LOGGER.warning("Ignoring invalid %s value in stored runtime state", key)
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            _LOGGER.warning(
                "Ignoring malformed %s value in stored runtime state: %s",
                key,
                value,
            )
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _async_save_timestamps(self) -> None:
        """Persist operation timestamps for this config entry."""
        data = {
            STORAGE_LAST_FETCH_TIME: self._format_timestamp(self._last_fetch_time),
            STORAGE_LAST_PULL_TIME: self._format_timestamp(self._last_pull_time),
            STORAGE_LAST_PUSH_TIME: self._format_timestamp(self._last_push_time),
        }
        try:
            await self._store.async_save(
                {key: value for key, value in data.items() if value is not None}
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not save runtime state for git-ha-ppens entry %s: %s",
                self._entry_id,
                err,
            )

    @staticmethod
    def _format_timestamp(value: datetime | None) -> str | None:
        """Format a timestamp for storage."""
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def pre_deploy_validator(
        self,
    ) -> Callable[[], Awaitable[list[str]]] | None:
        """Return a validation callback for pull(), or None if disabled."""
        if not self._pre_deploy_check:
            return None

        async def _validate() -> list[str]:
            return await async_run_pre_deploy_check(
                self.hass, self.git_manager.repo_path
            )

        return _validate

    async def async_manual_push(self) -> int:
        """Push local commits and publish the manual operation result."""
        try:
            async with self.git_lock:
                commits_pushed = await self.git_manager.push()
                await self.async_record_push_time()
        except GitError as err:
            self.hass.bus.async_fire(
                EVENT_ERROR, {"operation": "push", "error": str(err)}
            )
            raise

        self.hass.bus.async_fire(EVENT_PUSH, {"commits_pushed": commits_pushed})
        _LOGGER.info("Pushed %d commit(s) to remote", commits_pushed)
        await self.async_request_refresh()
        return commits_pushed

    async def async_manual_commit_and_push(self) -> int:
        """Commit all pending changes and immediately push them."""
        try:
            async with self.git_lock:
                message = None
                if self._ai_commit_enabled:
                    try:
                        diff = await self.git_manager.get_diff()
                        porcelain = await self.git_manager._run_git(
                            "status", "--porcelain", check=False
                        )
                        if diff or porcelain:
                            message = await async_generate_ai_commit_message(
                                self.hass,
                                diff,
                                porcelain,
                                self._ai_agent_id,
                            )
                    except GitError:
                        pass

                commit_info = await self.git_manager.commit(message)
                if commit_info:
                    self.hass.bus.async_fire(
                        EVENT_COMMIT,
                        {
                            "hash": commit_info.hash_short,
                            "message": commit_info.message,
                            "author": commit_info.author,
                            "auto": False,
                        },
                    )
                    _LOGGER.info(
                        "Push button committed: %s - %s",
                        commit_info.hash_short,
                        commit_info.message,
                    )

                commits_pushed = await self.git_manager.push()
                await self.async_record_push_time()
        except GitError as err:
            self.hass.bus.async_fire(
                EVENT_ERROR, {"operation": "push", "error": str(err)}
            )
            raise

        self.hass.bus.async_fire(
            EVENT_PUSH, {"commits_pushed": commits_pushed, "auto": False}
        )
        _LOGGER.info("Push button pushed %d commit(s) to remote", commits_pushed)
        await self.async_request_refresh()
        return commits_pushed

    async def async_manual_pull(self) -> int:
        """Pull remote commits and publish the manual operation result."""
        try:
            async with self.git_lock:
                commits_pulled = await self.git_manager.pull(
                    backup=True, validate=self.pre_deploy_validator()
                )
                await self.async_record_pull_time()
        except PreDeployCheckError as err:
            self.hass.bus.async_fire(
                EVENT_CHECK_FAILED, {"errors": err.errors, "auto": False}
            )
            notify_check_failed(self.hass, err.errors)
            await self.async_request_refresh()
            raise
        except GitError as err:
            self.hass.bus.async_fire(
                EVENT_ERROR, {"operation": "pull", "error": str(err)}
            )
            raise

        self.hass.bus.async_fire(
            EVENT_PULL, {"commits_pulled": commits_pulled, "auto": False}
        )
        _LOGGER.info("Pulled %d commit(s) from remote", commits_pulled)
        await self.async_request_refresh()
        return commits_pulled

    async def async_manual_fetch(self) -> None:
        """Fetch remote commits and publish the manual operation result."""
        try:
            async with self.git_lock:
                await self.git_manager.fetch()
                await self.async_record_fetch_time()
        except GitError as err:
            self.hass.bus.async_fire(
                EVENT_ERROR, {"operation": "fetch", "error": str(err)}
            )
            raise

        self.hass.bus.async_fire(EVENT_FETCH, {"auto": False})
        _LOGGER.info("Fetched from remote")
        await self.async_request_refresh()

    async def async_discard_changes(self) -> int:
        """Discard staged and unstaged changes to tracked files."""
        try:
            async with self.git_lock:
                discarded_files = await self.git_manager.discard_changes()
        except GitError as err:
            self.hass.bus.async_fire(
                EVENT_ERROR,
                {"operation": "discard_changes", "error": str(err)},
            )
            raise

        _LOGGER.info(
            "Discarded local changes in %d tracked file(s)",
            discarded_files,
        )
        await self.async_request_refresh()
        return discarded_files

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
                _LOGGER.debug("Skipping auto-pull: another git operation in progress")
                return status

            # Skip remote commits already blocked by a failed pre-deploy check
            # until the remote advances, to avoid re-running the heavy check
            # (and re-notifying) on every poll.
            upstream_sha = await self.git_manager.get_upstream_sha()
            if (
                self._blocked_remote_sha is not None
                and upstream_sha == self._blocked_remote_sha
            ):
                _LOGGER.debug(
                    "Skipping auto-pull: remote %s previously blocked by "
                    "pre-deploy check",
                    upstream_sha[:8],
                )
                return status

            async with self.git_lock:
                try:
                    commits_pulled = await self.git_manager.pull(
                        backup=True, validate=self.pre_deploy_validator()
                    )
                    await self.async_record_pull_time()
                    self._blocked_remote_sha = None
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

                except PreDeployCheckError as err:
                    _LOGGER.warning("Auto-pull blocked by pre-deploy check: %s", err)
                    self._blocked_remote_sha = upstream_sha or None
                    self.hass.bus.async_fire(
                        EVENT_CHECK_FAILED,
                        {"errors": err.errors, "auto": True},
                    )
                    notify_check_failed(self.hass, err.errors)
                    # Re-fetch status so sensors reflect the rolled-back state
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
                await self.async_record_fetch_time()
                self.hass.bus.async_fire(
                    EVENT_FETCH,
                    {"auto": True},
                )
                _LOGGER.debug("Auto-fetch completed")
            except GitError as err:
                _LOGGER.warning("Auto-fetch failed: %s", err)
                # Record time even on failure to avoid hammering a broken remote
                await self.async_record_fetch_time()
                self.hass.bus.async_fire(
                    EVENT_ERROR,
                    {"operation": "auto_fetch", "error": str(err)},
                )
