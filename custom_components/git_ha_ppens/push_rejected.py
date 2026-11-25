"""Repair lifecycle for pushes rejected by the Git remote."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, REPAIR_PUSH_REJECTED, push_rejected_issue_id
from .git_manager import GitError, RemotePushRejectedError
from .remote_auth import format_error

_LOGGER = logging.getLogger(__name__)

# Passed as a placeholder because translations must not contain URLs.
_GITHUB_EMAIL_SETTINGS_URL = "https://github.com/settings/emails"


def create_push_rejected_issue(
    hass: HomeAssistant,
    entry_id: str,
    operation: str,
    err: RemotePushRejectedError,
) -> None:
    """Create a repair telling the user that the remote rejected a push."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        push_rejected_issue_id(entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=f"{REPAIR_PUSH_REJECTED}_{err.reason}",
        translation_placeholders={
            "operation": operation,
            "error": format_error(err),
            "github_email_settings_url": _GITHUB_EMAIL_SETTINGS_URL,
        },
        data={"entry_id": entry_id},
    )


def delete_push_rejected_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the rejected-push repair after a successful push."""
    ir.async_delete_issue(hass, DOMAIN, push_rejected_issue_id(entry_id))


def create_push_rejected_issue_from_error(
    hass: HomeAssistant,
    entry_id: str,
    operation: str,
    err: GitError,
) -> bool:
    """Create the repair when a Git error represents a rejected push."""
    if not isinstance(err, RemotePushRejectedError):
        return False
    already_reported = (
        ir.async_get(hass).async_get_issue(
            DOMAIN, push_rejected_issue_id(entry_id)
        )
        is not None
    )
    # Automatic pushes are retried. The Git layer already logs each failure, so
    # only announce the repair once to avoid log spam.
    log = _LOGGER.debug if already_reported else _LOGGER.error
    log(
        "Remote rejected the push during %s (%s). "
        "A repair has been created under Settings > System > Repairs: %s",
        operation,
        err.reason,
        err,
    )
    create_push_rejected_issue(hass, entry_id, operation, err)
    return True
