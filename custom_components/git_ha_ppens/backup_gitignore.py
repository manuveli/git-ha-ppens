"""Installation-specific protection for Home Assistant backup archives."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from homeassistant.helpers.hassio import is_hassio

from .const import (
    CONF_GITIGNORE_CUSTOM,
    CONF_GITIGNORE_MIGRATION_VERSION,
    CORE_BACKUP_GITIGNORE_PATTERNS,
    CORE_BACKUP_GITIGNORE_PROBES,
    GITIGNORE_MIGRATION_VERSION,
)
from .git_manager import GitError, GitManager

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_BACKUP_GITIGNORE_COMMENT = (
    "# Home Assistant Core/Container backup archives (added by git-ha-ppens)"
)


def is_core_config_repository(hass: HomeAssistant, repo_path: str) -> bool:
    """Return whether Core/Container writes backups inside this repository."""
    return not is_hassio(hass) and os.path.realpath(repo_path) == os.path.realpath(
        hass.config.config_dir
    )


async def async_migrate_backup_gitignore(
    hass: HomeAssistant,
    entry: ConfigEntry,
    git_manager: GitManager,
) -> bool:
    """Apply the current backup-ignore migration once when it is relevant.

    Return True only when this entry's migration version was advanced. A
    failure is intentionally non-fatal and leaves the old version in place so
    the migration is retried on the next integration load.
    """
    current_version = entry.data.get(CONF_GITIGNORE_MIGRATION_VERSION, 0)
    if current_version >= GITIGNORE_MIGRATION_VERSION:
        return False
    if not is_core_config_repository(hass, git_manager.repo_path):
        return False

    custom_gitignore = entry.data.get(CONF_GITIGNORE_CUSTOM, False)
    try:
        missing_patterns = await git_manager.get_unignored_gitignore_patterns(
            CORE_BACKUP_GITIGNORE_PROBES
        )
        if custom_gitignore:
            protected_patterns = tuple(
                pattern
                for pattern in CORE_BACKUP_GITIGNORE_PATTERNS
                if pattern not in missing_patterns
            )
            if protected_patterns:
                await git_manager.untrack_matching_files(protected_patterns)
            if missing_patterns:
                _LOGGER.warning(
                    "The manually managed .gitignore does not protect Home "
                    "Assistant backup archives matching %s. Add the missing "
                    "pattern(s) with Configure > Edit .gitignore. Existing "
                    "Git history is not changed automatically.",
                    ", ".join(missing_patterns),
                )
        else:
            if missing_patterns:
                await git_manager.ensure_gitignore_patterns(
                    {
                        pattern: CORE_BACKUP_GITIGNORE_PROBES[pattern]
                        for pattern in missing_patterns
                    },
                    comment=_BACKUP_GITIGNORE_COMMENT,
                )
            await git_manager.untrack_matching_files(
                CORE_BACKUP_GITIGNORE_PATTERNS
            )
    except GitError as err:
        _LOGGER.warning(
            "Could not migrate Home Assistant backup archive ignore rules; "
            "the integration will retry on the next load: %s",
            err,
        )
        return False

    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_GITIGNORE_MIGRATION_VERSION: GITIGNORE_MIGRATION_VERSION,
        },
    )
    return True
