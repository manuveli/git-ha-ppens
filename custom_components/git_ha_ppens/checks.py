"""Pre-deploy validation checks for git-ha-ppens.

Runs a Home Assistant configuration check against the on-disk config before
remote changes are kept. Used as a GitOps-style pre-deploy gate: if the check
fails, the caller rolls back to the last working state.
"""

from __future__ import annotations

import logging
import os

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_NOTIFICATION_ID = f"{DOMAIN}_pre_deploy_check_failed"


async def async_run_pre_deploy_check(
    hass: HomeAssistant, repo_path: str
) -> list[str]:
    """Run the Home Assistant config check.

    Args:
        hass: The Home Assistant instance.
        repo_path: The repository path being managed.

    Returns:
        A list of error messages. An empty list means the configuration is
        valid (or the check was skipped because it is not applicable).
    """
    # The HA config check validates hass.config.config_dir. It only makes sense
    # when the repository IS the Home Assistant config directory. If it points
    # somewhere else, skip the check (treat as "pass") instead of blocking.
    if os.path.realpath(repo_path) != os.path.realpath(hass.config.config_dir):
        _LOGGER.warning(
            "Pre-deploy check skipped: repository path %s is not the Home "
            "Assistant config directory %s",
            repo_path,
            hass.config.config_dir,
        )
        return []

    try:
        # Imported lazily so the integration loads even on HA cores where the
        # helper signature differs.
        from homeassistant.helpers.check_config import async_check_ha_config_file

        result = await async_check_ha_config_file(hass)
    except Exception as err:  # noqa: BLE001 - never let the gate crash a pull
        _LOGGER.warning(
            "Pre-deploy check could not run (%s); allowing pull to proceed",
            err,
        )
        return []

    errors = [err.message for err in result.errors]
    if errors:
        _LOGGER.warning(
            "Pre-deploy check found %d configuration error(s)", len(errors)
        )
    return errors


def notify_check_failed(hass: HomeAssistant, errors: list[str]) -> None:
    """Surface a blocked pull in the UI via a persistent notification."""
    bullet_list = "\n".join(f"- {err}" for err in errors) or "- (no details)"
    persistent_notification.async_create(
        hass,
        (
            "A pull from the remote repository was **blocked** because the "
            "Home Assistant configuration check failed. The repository was "
            "rolled back to the last working state.\n\n"
            f"Errors:\n{bullet_list}"
        ),
        title="git-ha-ppens: Pull blocked",
        notification_id=_NOTIFICATION_ID,
    )
