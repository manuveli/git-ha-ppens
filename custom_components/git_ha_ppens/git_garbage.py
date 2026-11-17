"""Home Assistant repair lifecycle for large amounts of leftover Git data."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    GC_GARBAGE_WARN_BYTES,
    REPAIR_LARGE_GIT_GARBAGE,
    large_git_garbage_issue_id,
)


def format_gibibytes(size_bytes: int) -> str:
    """Format a byte count as GiB with one decimal for repair texts."""
    return f"{size_bytes / 1024**3:.1f}"


def update_large_git_garbage_issue(
    hass: HomeAssistant,
    entry_id: str,
    garbage_size: int,
) -> None:
    """Create or clear the repair depending on the measured garbage size.

    The issue is not persistent: it is re-evaluated from fresh metrics shortly
    after every start, so a stale warning never survives a cleanup.
    """
    if garbage_size >= GC_GARBAGE_WARN_BYTES:
        ir.async_create_issue(
            hass,
            DOMAIN,
            large_git_garbage_issue_id(entry_id),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=REPAIR_LARGE_GIT_GARBAGE,
            translation_placeholders={
                "garbage_size": format_gibibytes(garbage_size),
            },
        )
        return
    delete_large_git_garbage_issue(hass, entry_id)


def delete_large_git_garbage_issue(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Delete the entry-specific large-garbage repair."""
    ir.async_delete_issue(
        hass,
        DOMAIN,
        large_git_garbage_issue_id(entry_id),
    )
