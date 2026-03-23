"""git-ha-ppens: Git version control for Home Assistant."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .const import (
    ATTR_MESSAGE,
    AUTH_SSH,
    AUTH_TOKEN,
    CONF_AUTH_METHOD,
    CONF_AUTH_TOKEN,
    CONF_AUTO_COMMIT,
    CONF_COMMIT_INTERVAL,
    CONF_GIT_EMAIL,
    CONF_GIT_USER,
    CONF_REMOTE_URL,
    CONF_REPO_PATH,
    CONF_SCAN_INTERVAL,
    CONF_SSH_KEY_PATH,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_COMMIT,
    EVENT_ERROR,
    EVENT_PULL,
    EVENT_PUSH,
    EVENT_SECRET_DETECTED,
    SERVICE_COMMIT,
    SERVICE_PULL,
    SERVICE_PUSH,
    SERVICE_SYNC,
)
from .coordinator import GitHaPpensCoordinator
from .file_watcher import GitFileWatcher
from .git_manager import GitError, GitManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_COMMIT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MESSAGE): str,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up git-ha-ppens from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    data = entry.data
    repo_path = data[CONF_REPO_PATH]
    git_user = data.get(CONF_GIT_USER, "")
    git_email = data.get(CONF_GIT_EMAIL, "")

    # Create git manager
    git_manager = GitManager(repo_path, git_user, git_email)

    # Verify git is installed
    if not await git_manager.is_git_installed():
        _LOGGER.error("Git is not installed. Please install git first")
        return False

    # Initialize repository
    try:
        await git_manager.init_repo()
    except GitError as err:
        _LOGGER.error("Failed to initialize git repository: %s", err)
        return False

    # Setup .gitignore
    try:
        gitignore_updated = await git_manager.setup_gitignore()
        if gitignore_updated:
            _LOGGER.info("Updated .gitignore with security defaults")
    except GitError as err:
        _LOGGER.warning("Failed to update .gitignore: %s", err)

    # Configure remote if specified
    remote_url = data.get(CONF_REMOTE_URL, "")
    if remote_url:
        try:
            auth_method = data.get(CONF_AUTH_METHOD, "")
            if auth_method == AUTH_TOKEN:
                token = data.get(CONF_AUTH_TOKEN, "")
                if token:
                    await git_manager.configure_token_auth(remote_url, token)
                else:
                    await git_manager.set_remote(remote_url)
            elif auth_method == AUTH_SSH:
                ssh_key = data.get(CONF_SSH_KEY_PATH, "")
                await git_manager.set_remote(remote_url)
                if ssh_key:
                    await git_manager.configure_ssh_auth(ssh_key)
            else:
                await git_manager.set_remote(remote_url)
        except GitError as err:
            _LOGGER.warning("Failed to configure remote: %s", err)

    # Create coordinator
    scan_interval = data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = GitHaPpensCoordinator(hass, git_manager, scan_interval)
    await coordinator.async_config_entry_first_refresh()

    # Setup file watcher for auto-commit
    file_watcher: GitFileWatcher | None = None
    if data.get(CONF_AUTO_COMMIT, False):
        commit_interval = data.get(CONF_COMMIT_INTERVAL, 300)
        file_watcher = GitFileWatcher(
            hass, git_manager, coordinator, repo_path, commit_interval
        )
        await file_watcher.async_start()
        _LOGGER.info(
            "Auto-commit enabled with %ds debounce interval", commit_interval
        )

    # Store references
    hass.data[DOMAIN][entry.entry_id] = {
        "git_manager": git_manager,
        "coordinator": coordinator,
        "file_watcher": file_watcher,
    }

    # Forward platform setup
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    _register_services(hass, entry)

    # Run initial secret scan
    try:
        findings = await git_manager.scan_for_secrets()
        if findings:
            _LOGGER.warning(
                "Secret scan found %d potential secret(s) in tracked files. "
                "Check your .gitignore!",
                len(findings),
            )
            hass.bus.async_fire(
                EVENT_SECRET_DETECTED,
                {"findings": findings, "count": len(findings)},
            )
    except GitError:
        pass

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def _async_update_options(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update - reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Stop file watcher
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    file_watcher: GitFileWatcher | None = entry_data.get("file_watcher")
    if file_watcher:
        await file_watcher.async_stop()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # Remove services if no more entries
        if not hass.data[DOMAIN]:
            for service in (SERVICE_COMMIT, SERVICE_PUSH, SERVICE_PULL, SERVICE_SYNC):
                hass.services.async_remove(DOMAIN, service)

    return unload_ok


def _register_services(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register git-ha-ppens services."""

    def _get_manager_and_coordinator(
        call: ServiceCall,
    ) -> tuple[GitManager, GitHaPpensCoordinator]:
        """Get the git manager and coordinator for the first config entry."""
        for eid, data in hass.data[DOMAIN].items():
            if isinstance(data, dict) and "git_manager" in data:
                return data["git_manager"], data["coordinator"]
        raise GitError("No git-ha-ppens instance configured")

    async def async_handle_commit(call: ServiceCall) -> None:
        """Handle the commit service call."""
        try:
            git_manager, coordinator = _get_manager_and_coordinator(call)
            message = call.data.get(ATTR_MESSAGE)
            commit_info = await git_manager.commit(message)

            if commit_info:
                hass.bus.async_fire(
                    EVENT_COMMIT,
                    {
                        "hash": commit_info.hash_short,
                        "message": commit_info.message,
                        "author": commit_info.author,
                    },
                )
                _LOGGER.info(
                    "Commit created: %s - %s",
                    commit_info.hash_short,
                    commit_info.message,
                )
            else:
                _LOGGER.info("Nothing to commit - working tree clean")

            await coordinator.async_request_refresh()

        except GitError as err:
            _LOGGER.error("Commit failed: %s", err)
            hass.bus.async_fire(
                EVENT_ERROR, {"operation": "commit", "error": str(err)}
            )

    async def async_handle_push(call: ServiceCall) -> None:
        """Handle the push service call."""
        try:
            git_manager, coordinator = _get_manager_and_coordinator(call)
            commits_pushed = await git_manager.push()

            hass.bus.async_fire(
                EVENT_PUSH, {"commits_pushed": commits_pushed}
            )
            _LOGGER.info("Pushed %d commit(s) to remote", commits_pushed)
            await coordinator.async_request_refresh()

        except GitError as err:
            _LOGGER.error("Push failed: %s", err)
            hass.bus.async_fire(
                EVENT_ERROR, {"operation": "push", "error": str(err)}
            )

    async def async_handle_pull(call: ServiceCall) -> None:
        """Handle the pull service call."""
        try:
            git_manager, coordinator = _get_manager_and_coordinator(call)
            commits_pulled = await git_manager.pull(backup=True)

            hass.bus.async_fire(
                EVENT_PULL, {"commits_pulled": commits_pulled}
            )
            _LOGGER.info("Pulled %d commit(s) from remote", commits_pulled)
            await coordinator.async_request_refresh()

        except GitError as err:
            _LOGGER.error("Pull failed: %s", err)
            hass.bus.async_fire(
                EVENT_ERROR, {"operation": "pull", "error": str(err)}
            )

    async def async_handle_sync(call: ServiceCall) -> None:
        """Handle the sync service call (commit + push)."""
        try:
            git_manager, coordinator = _get_manager_and_coordinator(call)
            message = call.data.get(ATTR_MESSAGE)

            # Commit first
            commit_info = await git_manager.commit(message)
            if commit_info:
                hass.bus.async_fire(
                    EVENT_COMMIT,
                    {
                        "hash": commit_info.hash_short,
                        "message": commit_info.message,
                        "author": commit_info.author,
                    },
                )
                _LOGGER.info(
                    "Sync - committed: %s", commit_info.hash_short
                )

            # Then push
            commits_pushed = await git_manager.push()
            hass.bus.async_fire(
                EVENT_PUSH, {"commits_pushed": commits_pushed}
            )
            _LOGGER.info("Sync - pushed %d commit(s)", commits_pushed)

            await coordinator.async_request_refresh()

        except GitError as err:
            _LOGGER.error("Sync failed: %s", err)
            hass.bus.async_fire(
                EVENT_ERROR, {"operation": "sync", "error": str(err)}
            )

    # Only register services once
    if not hass.services.has_service(DOMAIN, SERVICE_COMMIT):
        hass.services.async_register(
            DOMAIN, SERVICE_COMMIT, async_handle_commit, schema=SERVICE_COMMIT_SCHEMA
        )
        hass.services.async_register(
            DOMAIN, SERVICE_PUSH, async_handle_push
        )
        hass.services.async_register(
            DOMAIN, SERVICE_PULL, async_handle_pull
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SYNC, async_handle_sync, schema=SERVICE_COMMIT_SCHEMA
        )
