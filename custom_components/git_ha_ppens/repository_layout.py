"""Repair lifecycle for unsafe embedded Git repositories."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    REPAIR_UNSAFE_REPOSITORY_LAYOUT,
    unsafe_repository_layout_issue_id,
)
from .git_manager import GitError, GitManager, UnsafeRepositoryLayoutError

_MAX_REPAIR_PATHS = 5
_MAX_PATH_LENGTH = 200


def _format_repair_paths(paths: list[str]) -> str:
    """Format a bounded, safe list of repository-relative paths."""
    displayed: list[str] = []
    for path in paths[:_MAX_REPAIR_PATHS]:
        sanitized = (
            path.replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace("`", "\\`")
        )
        if len(sanitized) > _MAX_PATH_LENGTH:
            sanitized = f"{sanitized[: _MAX_PATH_LENGTH - 1]}…"
        displayed.append(f"- `{sanitized}`")
    if len(paths) > _MAX_REPAIR_PATHS:
        displayed.append(f"- … (+{len(paths) - _MAX_REPAIR_PATHS})")
    return "\n".join(displayed)


def create_unsafe_repository_layout_issue(
    hass: HomeAssistant,
    entry_id: str,
    paths: list[str],
) -> None:
    """Create an actionable repair for undeclared embedded repositories."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        unsafe_repository_layout_issue_id(entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=REPAIR_UNSAFE_REPOSITORY_LAYOUT,
        translation_placeholders={"paths": _format_repair_paths(paths)},
    )


def delete_unsafe_repository_layout_issue(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Delete the entry-specific repository-layout repair."""
    ir.async_delete_issue(
        hass,
        DOMAIN,
        unsafe_repository_layout_issue_id(entry_id),
    )


def create_repository_layout_issue_from_error(
    hass: HomeAssistant,
    entry_id: str,
    err: GitError,
) -> bool:
    """Create a layout repair when a Git error represents that condition."""
    if not isinstance(err, UnsafeRepositoryLayoutError):
        return False
    create_unsafe_repository_layout_issue(hass, entry_id, err.paths)
    return True


async def async_ensure_repository_layout_safe(
    hass: HomeAssistant,
    entry_id: str,
    git_manager: GitManager,
) -> None:
    """Check the layout, creating or clearing the matching repair."""
    try:
        await git_manager.assert_repository_layout_safe()
    except UnsafeRepositoryLayoutError as err:
        create_unsafe_repository_layout_issue(hass, entry_id, err.paths)
        raise
    delete_unsafe_repository_layout_issue(hass, entry_id)
