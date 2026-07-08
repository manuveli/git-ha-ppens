"""git-ha-ppens: Git version control for Home Assistant."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from .ai_commit import async_generate_ai_commit_message
from .const import (
    ATTR_MESSAGE,
    AUTH_SSH,
    AUTH_TOKEN,
    CONF_AI_AGENT_ID,
    CONF_AI_COMMIT_MESSAGES,
    CONF_AUTH_METHOD,
    CONF_AUTH_TOKEN,
    CONF_AUTO_COMMIT,
    CONF_AUTO_PULL,
    CONF_AUTO_PUSH,
    CONF_COMMIT_INTERVAL,
    CONF_FETCH_INTERVAL,
    CONF_GIT_EMAIL,
    CONF_GIT_USER,
    CONF_GITIGNORE_CUSTOM,
    CONF_GITIGNORE_INITIALIZED,
    CONF_PRE_DEPLOY_CHECK,
    CONF_REMOTE_URL,
    CONF_REPO_PATH,
    CONF_SCAN_INTERVAL,
    CONF_SSH_KEY_PATH,
    DEFAULT_FETCH_INTERVAL,
    DEFAULT_PRE_DEPLOY_CHECK,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENTITY_ID_KEYS,
    EVENT_COMMIT,
    EVENT_ERROR,
    EVENT_PUSH,
    EVENT_SECRET_DETECTED,
    SERVICE_COMMIT,
    SERVICE_DISCARD_CHANGES,
    SERVICE_DIFF,
    SERVICE_FETCH,
    SERVICE_PULL,
    SERVICE_PUSH,
    SERVICE_SYNC,
    stable_entity_id_targets,
)
from .coordinator import GitHaPpensCoordinator
from .file_watcher import GitFileWatcher
from .git_manager import GitError, GitManager, PreDeployCheckError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

SERVICE_COMMIT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MESSAGE): str,
    }
)

BUTTON_ENTITY_ID_MIGRATION_NOTIFICATION_ID = (
    f"{DOMAIN}_button_entity_id_migration"
)
ENTRY_ENTITY_IDS = "entity_ids"


def _unique_id(entry: ConfigEntry, key: str) -> str:
    """Return the entity unique ID for a config entry entity key."""
    return f"{entry.entry_id}_{key}"


def _entity_id_available_for_unique_id(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_id: str,
    unique_id: str,
) -> bool:
    """Return whether an entity ID can be used by the given unique ID."""
    registry_entry = entity_registry.async_get(entity_id)
    if registry_entry is not None:
        return registry_entry.unique_id == unique_id
    return hass.states.async_available(entity_id)


def _targets_available_for_entry(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entry: ConfigEntry,
    targets: dict[str, dict[str, str]],
) -> bool:
    """Return whether all targets are available for this config entry."""
    return all(
        _entity_id_available_for_unique_id(
            hass,
            entity_registry,
            entity_id,
            _unique_id(entry, key),
        )
        for platform_targets in targets.values()
        for key, entity_id in platform_targets.items()
    )


def _build_entry_entity_id_targets(
    entry: ConfigEntry,
    *,
    include_repo_slug: bool,
    include_entry_id: bool,
) -> dict[str, dict[str, str]]:
    """Build stable entity ID targets for a config entry."""
    repo_path = entry.data[CONF_REPO_PATH]
    return {
        platform: stable_entity_id_targets(
            entry.entry_id,
            repo_path,
            platform,
            keys,
            include_repo_slug=include_repo_slug,
            include_entry_id=include_entry_id,
        )
        for platform, keys in ENTITY_ID_KEYS.items()
    }


def _is_primary_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return whether this is the first git-ha-ppens config entry."""
    entries = hass.config_entries.async_entries(DOMAIN)
    return not entries or entries[0].entry_id == entry.entry_id


def _resolve_entry_entity_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, dict[str, str]]:
    """Resolve stable entity IDs for this config entry without collisions."""
    entity_registry = er.async_get(hass)
    targets = _build_entry_entity_id_targets(
        entry,
        include_repo_slug=not _is_primary_entry(hass, entry),
        include_entry_id=False,
    )
    if _targets_available_for_entry(hass, entity_registry, entry, targets):
        return targets

    targets = _build_entry_entity_id_targets(
        entry, include_repo_slug=True, include_entry_id=False
    )
    if _targets_available_for_entry(hass, entity_registry, entry, targets):
        return targets

    return _build_entry_entity_id_targets(
        entry, include_repo_slug=True, include_entry_id=True
    )


def _migrate_button_entity_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    button_entity_ids: dict[str, str],
) -> None:
    """Rename existing button entity IDs to the stable git-ha-ppens IDs."""
    entity_registry = er.async_get(hass)
    renamed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str, str]] = []

    for key, target_entity_id in button_entity_ids.items():
        unique_id = _unique_id(entry, key)
        current_entity_id = entity_registry.async_get_entity_id(
            Platform.BUTTON, DOMAIN, unique_id
        )
        if current_entity_id is None or current_entity_id == target_entity_id:
            continue

        target_entry = entity_registry.async_get(target_entity_id)
        if target_entry is not None and target_entry.unique_id != unique_id:
            reason = "target entity ID is already registered"
            skipped.append((current_entity_id, target_entity_id, reason))
            _LOGGER.warning(
                "Could not migrate %s to %s: %s",
                current_entity_id,
                target_entity_id,
                reason,
            )
            continue

        if target_entry is None and not hass.states.async_available(target_entity_id):
            reason = "target entity ID is already in use"
            skipped.append((current_entity_id, target_entity_id, reason))
            _LOGGER.warning(
                "Could not migrate %s to %s: %s",
                current_entity_id,
                target_entity_id,
                reason,
            )
            continue

        try:
            entity_registry.async_update_entity(
                current_entity_id, new_entity_id=target_entity_id
            )
        except ValueError as err:
            skipped.append((current_entity_id, target_entity_id, str(err)))
            _LOGGER.warning(
                "Could not migrate %s to %s: %s",
                current_entity_id,
                target_entity_id,
                err,
            )
            continue

        renamed.append((current_entity_id, target_entity_id))
        _LOGGER.info(
            "Migrated button entity ID from %s to %s",
            current_entity_id,
            target_entity_id,
        )

    if not renamed and not skipped:
        return

    message_parts: list[str] = []
    if renamed:
        message_parts.append(
            "Renamed git-ha-ppens button entity IDs to stable, "
            f"language-independent IDs for `{entry.title}`:\n\n"
            + "\n".join(f"- `{old}` -> `{new}`" for old, new in renamed)
        )
    if skipped:
        message_parts.append(
            "Skipped these button entity ID migrations because the target ID "
            "was not free:\n\n"
            + "\n".join(
                f"- `{old}` -> `{new}` ({reason})"
                for old, new, reason in skipped
            )
        )

    message_parts.append(
        "Home Assistant keeps entity history for registry renames, but it does "
        "not update existing automations, dashboards, scripts, Node-RED, "
        "AppDaemon, or other external references automatically. Please update "
        "any references to the old button entity IDs."
    )

    persistent_notification.async_create(
        hass,
        "\n\n".join(message_parts),
        title="git-ha-ppens button entity IDs changed",
        notification_id=f"{BUTTON_ENTITY_ID_MIGRATION_NOTIFICATION_ID}_{entry.entry_id}",
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

    # Setup .gitignore (only on first setup, not every restart)
    gitignore_initialized = data.get(CONF_GITIGNORE_INITIALIZED, False)
    if not gitignore_initialized:
        try:
            skip_defaults = data.get(CONF_GITIGNORE_CUSTOM, False)
            gitignore_updated = await git_manager.setup_gitignore(
                skip_defaults=skip_defaults
            )
            if gitignore_updated:
                _LOGGER.info("Updated .gitignore with security defaults")
                if await git_manager.has_commits():
                    await git_manager.apply_gitignore()
        except GitError as err:
            _LOGGER.warning("Failed to update .gitignore: %s", err)

        hass.config_entries.async_update_entry(
            entry, data={**data, CONF_GITIGNORE_INITIALIZED: True}
        )

    # Configure remote if specified (must happen BEFORE initial commit so push works)
    remote_url = data.get(CONF_REMOTE_URL, "")
    initial_push_succeeded = False
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

            # Verify remote was actually configured correctly
            if await git_manager.is_remote_configured():
                configured_url = await git_manager.get_remote_url()
                _LOGGER.info(
                    "Remote origin verified: %s",
                    GitManager._redact_url(configured_url),
                )
            else:
                _LOGGER.error(
                    "Remote origin could not be verified after configuration. "
                    "The URL '%s' may be invalid. Push/pull will be disabled. "
                    "Check the remote URL in the integration options.",
                    remote_url,
                )
                remote_url = ""  # Prevent push attempts
        except GitError as err:
            _LOGGER.error(
                "Failed to configure remote: %s. Push/pull will be disabled.",
                err,
            )
            remote_url = ""  # Prevent push attempts

    # Create initial commit if repository has no commits yet
    if not await git_manager.has_commits():
        try:
            commit_info = await git_manager.commit("Initial commit by git-ha-ppens")
            if commit_info:
                _LOGGER.info("Created initial commit: %s", commit_info.hash_short)
                # Auto-push initial commit if remote is configured
                if remote_url and data.get(CONF_AUTO_PUSH, True):
                    try:
                        commits_pushed = await git_manager.push()
                        initial_push_succeeded = True
                        _LOGGER.info(
                            "Initial push: %d commit(s) pushed to remote",
                            commits_pushed,
                        )
                    except GitError as push_err:
                        _LOGGER.warning(
                            "Initial push failed (will retry on next auto-push): %s",
                            push_err,
                        )
            else:
                _LOGGER.warning("Initial commit returned None — no changes to commit")
        except GitError as err:
            _LOGGER.error(
                "Failed to create initial commit: %s. "
                "Check file permissions in %s and .gitignore configuration.",
                err,
                repo_path,
            )

    # Create coordinator
    scan_interval = data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    auto_pull = data.get(CONF_AUTO_PULL, False)
    fetch_interval = data.get(CONF_FETCH_INTERVAL, DEFAULT_FETCH_INTERVAL)
    pre_deploy_check = data.get(CONF_PRE_DEPLOY_CHECK, DEFAULT_PRE_DEPLOY_CHECK)
    coordinator = GitHaPpensCoordinator(
        hass,
        entry.entry_id,
        git_manager,
        scan_interval,
        auto_pull=auto_pull,
        remote_configured=bool(remote_url),
        fetch_interval=fetch_interval,
        pre_deploy_check=pre_deploy_check,
        ai_commit_enabled=data.get(CONF_AI_COMMIT_MESSAGES, False),
        ai_agent_id=data.get(CONF_AI_AGENT_ID, ""),
    )
    await coordinator.async_load_stored_timestamps()
    if initial_push_succeeded:
        await coordinator.async_record_push_time()
    await coordinator.async_config_entry_first_refresh()

    # Setup file watcher for auto-commit
    file_watcher: GitFileWatcher | None = None
    periodic_unsub = None
    if data.get(CONF_AUTO_COMMIT, False):
        commit_interval = data.get(CONF_COMMIT_INTERVAL, 300)
        auto_push = data.get(CONF_AUTO_PUSH, True)
        ai_commit_enabled = data.get(CONF_AI_COMMIT_MESSAGES, False)
        ai_agent_id = data.get(CONF_AI_AGENT_ID, "") if ai_commit_enabled else ""
        file_watcher = GitFileWatcher(
            hass,
            git_manager,
            coordinator,
            repo_path,
            commit_interval,
            auto_push=auto_push,
            remote_configured=bool(remote_url),
            git_lock=coordinator.git_lock,
            ai_commit_enabled=ai_commit_enabled,
            ai_agent_id=ai_agent_id,
        )
        await file_watcher.async_start()
        _LOGGER.info(
            "Auto-commit enabled with %ds debounce interval (auto-push: %s)",
            commit_interval,
            auto_push,
        )

        # Periodic fallback: check for changes even if watchdog misses events
        async def _periodic_check(_now) -> None:
            """Periodic fallback to catch changes watchdog may have missed."""
            if file_watcher and file_watcher.is_running:
                await file_watcher.async_check_and_commit()

        periodic_unsub = async_track_time_interval(
            hass, _periodic_check, timedelta(seconds=commit_interval)
        )

    entity_ids = _resolve_entry_entity_ids(hass, entry)

    # Store references
    hass.data[DOMAIN][entry.entry_id] = {
        "git_manager": git_manager,
        "coordinator": coordinator,
        "file_watcher": file_watcher,
        "periodic_unsub": periodic_unsub,
        ENTRY_ENTITY_IDS: entity_ids,
    }

    _migrate_button_entity_ids(hass, entry, entity_ids["button"])

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


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update - reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Cancel periodic fallback
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    periodic_unsub = entry_data.get("periodic_unsub")
    if periodic_unsub:
        periodic_unsub()

    # Stop file watcher
    file_watcher: GitFileWatcher | None = entry_data.get("file_watcher")
    if file_watcher:
        await file_watcher.async_stop()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # Remove services if no more entries
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_COMMIT,
                SERVICE_PUSH,
                SERVICE_PULL,
                SERVICE_FETCH,
                SERVICE_SYNC,
                SERVICE_DIFF,
                SERVICE_DISCARD_CHANGES,
            ):
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

            if not message and entry.data.get(CONF_AI_COMMIT_MESSAGES, False):
                try:
                    diff = await git_manager.get_diff()
                    porcelain = await git_manager._run_git(
                        "status", "--porcelain", check=False
                    )
                    if diff or porcelain:
                        message = await async_generate_ai_commit_message(
                            hass,
                            diff,
                            porcelain,
                            entry.data.get(CONF_AI_AGENT_ID, ""),
                        )
                except GitError:
                    pass

            commit_info = await git_manager.commit(message)

            if commit_info:
                hass.bus.async_fire(
                    EVENT_COMMIT,
                    {
                        "hash": commit_info.hash_short,
                        "message": commit_info.message,
                        "author": commit_info.author,
                        "changed_files": commit_info.changed_files,
                        "auto": False,
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
            hass.bus.async_fire(EVENT_ERROR, {"operation": "commit", "error": str(err)})

    async def async_handle_push(call: ServiceCall) -> None:
        """Handle the push service call."""
        try:
            _, coordinator = _get_manager_and_coordinator(call)
            await coordinator.async_manual_push()
        except GitError as err:
            _LOGGER.error("Push failed: %s", err)

    async def async_handle_pull(call: ServiceCall) -> None:
        """Handle the pull service call."""
        try:
            _, coordinator = _get_manager_and_coordinator(call)
            await coordinator.async_manual_pull()
        except PreDeployCheckError as err:
            _LOGGER.warning("Pull blocked by pre-deploy check: %s", err)
        except GitError as err:
            _LOGGER.error("Pull failed: %s", err)

    async def async_handle_fetch(call: ServiceCall) -> None:
        """Handle the fetch service call."""
        try:
            _, coordinator = _get_manager_and_coordinator(call)
            await coordinator.async_manual_fetch()
        except GitError as err:
            _LOGGER.error("Fetch failed: %s", err)

    async def async_handle_discard_changes(call: ServiceCall) -> None:
        """Handle the discard changes service call."""
        try:
            _, coordinator = _get_manager_and_coordinator(call)
            await coordinator.async_discard_changes()
        except GitError as err:
            _LOGGER.error("Discard changes failed: %s", err)

    async def async_handle_sync(call: ServiceCall) -> None:
        """Handle the sync service call (commit + push)."""
        try:
            git_manager, coordinator = _get_manager_and_coordinator(call)
            message = call.data.get(ATTR_MESSAGE)

            if not message and entry.data.get(CONF_AI_COMMIT_MESSAGES, False):
                try:
                    diff = await git_manager.get_diff()
                    porcelain = await git_manager._run_git(
                        "status", "--porcelain", check=False
                    )
                    if diff or porcelain:
                        message = await async_generate_ai_commit_message(
                            hass,
                            diff,
                            porcelain,
                            entry.data.get(CONF_AI_AGENT_ID, ""),
                        )
                except GitError:
                    pass

            # Commit first
            commit_info = await git_manager.commit(message)
            if commit_info:
                hass.bus.async_fire(
                    EVENT_COMMIT,
                    {
                        "hash": commit_info.hash_short,
                        "message": commit_info.message,
                        "author": commit_info.author,
                        "changed_files": commit_info.changed_files,
                        "auto": False,
                    },
                )
                _LOGGER.info("Sync - committed: %s", commit_info.hash_short)

            # Then push
            commits_pushed = await git_manager.push()
            await coordinator.async_record_push_time()
            hass.bus.async_fire(EVENT_PUSH, {"commits_pushed": commits_pushed})
            _LOGGER.info("Sync - pushed %d commit(s)", commits_pushed)

            await coordinator.async_request_refresh()

        except GitError as err:
            _LOGGER.error("Sync failed: %s", err)
            hass.bus.async_fire(EVENT_ERROR, {"operation": "sync", "error": str(err)})

    async def async_handle_diff(call: ServiceCall) -> dict:
        """Handle the diff service call."""
        try:
            git_manager, coordinator = _get_manager_and_coordinator(call)
            diff = await git_manager.get_diff()
            porcelain = await git_manager._run_git("status", "--porcelain", check=False)
            return {
                "diff": diff or "",
                "summary": porcelain or "",
            }
        except GitError as err:
            _LOGGER.error("Diff failed: %s", err)
            return {"diff": "", "summary": "", "error": str(err)}

    # Only register services once
    if not hass.services.has_service(DOMAIN, SERVICE_COMMIT):
        hass.services.async_register(
            DOMAIN, SERVICE_COMMIT, async_handle_commit, schema=SERVICE_COMMIT_SCHEMA
        )
        hass.services.async_register(DOMAIN, SERVICE_PUSH, async_handle_push)
        hass.services.async_register(DOMAIN, SERVICE_PULL, async_handle_pull)
        hass.services.async_register(DOMAIN, SERVICE_FETCH, async_handle_fetch)
        hass.services.async_register(
            DOMAIN,
            SERVICE_DISCARD_CHANGES,
            async_handle_discard_changes,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SYNC, async_handle_sync, schema=SERVICE_COMMIT_SCHEMA
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_DIFF,
            async_handle_diff,
            supports_response=SupportsResponse.ONLY,
        )
