"""Repair lifecycle for rejected remote credentials."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    AUTH_TOKEN,
    CONF_AUTH_METHOD,
    DOMAIN,
    REPAIR_REMOTE_AUTHENTICATION,
    REPAIR_REMOTE_AUTHENTICATION_TOKEN,
    remote_authentication_issue_id,
)
from .git_manager import GitError, RemoteAuthenticationError

_LOGGER = logging.getLogger(__name__)

_MAX_ERROR_LENGTH = 300

# Operations that only read from the remote. Many hosts allow anonymous reads of
# public repositories, so a successful read must not clear a repair raised by a
# rejected write.
READ_OPERATIONS: frozenset[str] = frozenset(
    {"fetch", "auto_fetch", "pull", "auto_pull"}
)
_ACCESS_READ = "read"
_ACCESS_WRITE = "write"


def _format_error(err: GitError) -> str:
    """Return a bounded, single-line error suitable for a repair description."""
    text = " ".join(str(err).split()).replace("`", "'")
    if len(text) > _MAX_ERROR_LENGTH:
        text = f"{text[: _MAX_ERROR_LENGTH - 1]}…"
    return text or "unknown"


def _uses_token_auth(hass: HomeAssistant, entry_id: str) -> bool:
    """Return whether the config entry authenticates with an access token."""
    entry = hass.config_entries.async_get_entry(entry_id)
    return entry is not None and entry.data.get(CONF_AUTH_METHOD) == AUTH_TOKEN


def create_remote_authentication_issue(
    hass: HomeAssistant,
    entry_id: str,
    operation: str,
    err: GitError,
) -> None:
    """Create a repair telling the user that the remote rejected credentials."""
    token_auth = _uses_token_auth(hass, entry_id)
    issue_id = remote_authentication_issue_id(entry_id)
    access = _ACCESS_READ if operation in READ_OPERATIONS else _ACCESS_WRITE
    existing = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    if (
        existing is not None
        and (existing.data or {}).get("access") == _ACCESS_WRITE
    ):
        # Keep the stronger signal: a rejected write is not resolved by reads.
        access = _ACCESS_WRITE
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=token_auth,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=(
            REPAIR_REMOTE_AUTHENTICATION_TOKEN
            if token_auth
            else REPAIR_REMOTE_AUTHENTICATION
        ),
        translation_placeholders={
            "operation": operation,
            "error": _format_error(err),
        },
        data={
            "entry_id": entry_id,
            "access": access,
        },
    )


def delete_remote_authentication_issue(
    hass: HomeAssistant,
    entry_id: str,
    *,
    write_succeeded: bool = True,
) -> None:
    """Delete the remote authentication repair after a successful operation.

    Args:
        write_succeeded: False when only a read (fetch or pull) succeeded. A
            read cannot prove that write credentials work, so a repair raised
            by a rejected push is kept in that case.
    """
    if not write_succeeded:
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, remote_authentication_issue_id(entry_id)
        )
        if issue is None:
            return
        if (issue.data or {}).get("access") != _ACCESS_READ:
            return
    ir.async_delete_issue(
        hass,
        DOMAIN,
        remote_authentication_issue_id(entry_id),
    )


def create_remote_authentication_issue_from_error(
    hass: HomeAssistant,
    entry_id: str,
    operation: str,
    err: GitError,
) -> bool:
    """Create the repair when a Git error represents rejected credentials."""
    if not isinstance(err, RemoteAuthenticationError):
        return False
    _LOGGER.error(
        "Remote rejected the configured credentials during %s. "
        "A repair has been created under Settings > System > Repairs: %s",
        operation,
        err,
    )
    create_remote_authentication_issue(hass, entry_id, operation, err)
    return True
