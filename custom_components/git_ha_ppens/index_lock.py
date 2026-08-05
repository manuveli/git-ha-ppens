"""Home Assistant repair lifecycle for unrecoverable Git index locks."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    REPAIR_STALE_INDEX_LOCK,
    stale_index_lock_issue_id,
)


def create_stale_index_lock_issue(
    hass: HomeAssistant,
    entry_id: str,
    lock_path: str,
) -> None:
    """Create an actionable repair for an index lock needing user attention."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        stale_index_lock_issue_id(entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=REPAIR_STALE_INDEX_LOCK,
        translation_placeholders={"lock_path": lock_path},
    )


def delete_stale_index_lock_issue(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Delete the entry-specific stale index-lock repair."""
    ir.async_delete_issue(
        hass,
        DOMAIN,
        stale_index_lock_issue_id(entry_id),
    )
