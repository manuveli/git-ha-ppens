"""Diagnostics support for git-ha-ppens."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_AUTH_TOKEN, CONF_REMOTE_URL, CONF_SSH_KEY_PATH, DOMAIN
from .git_manager import GitManager


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    git_manager: GitManager | None = entry_data.get("git_manager")
    file_watcher = entry_data.get("file_watcher")

    diagnostics: dict[str, Any] = {
        "config_entry": _redact_config(dict(entry.data)),
    }

    if git_manager:
        # Git info
        diagnostics["git"] = {
            "version": await git_manager.get_git_version(),
            "repo_initialized": await git_manager.is_repo_initialized(),
        }

        # Current status
        try:
            status = await git_manager.get_status()
            diagnostics["status"] = {
                "branch": status.branch,
                "dirty": status.dirty,
                "changed_files_count": len(status.changed_files),
                "untracked_files_count": len(status.untracked_files),
                "staged_files_count": len(status.staged_files),
                "last_commit_hash": status.last_commit_hash_short,
                "last_commit_time": (
                    status.last_commit_time.isoformat()
                    if status.last_commit_time
                    else None
                ),
                "total_commits": status.total_commits,
                "remote_configured": status.remote_configured,
                "ahead": status.ahead,
                "behind": status.behind,
            }
        except Exception:  # noqa: BLE001
            diagnostics["status"] = {"error": "Failed to get status"}

        # Recent commits (last 5)
        try:
            commits = await git_manager.get_log(5)
            diagnostics["recent_commits"] = [
                {
                    "hash": c.hash_short,
                    "message": c.message,
                    "author": c.author,
                    "timestamp": c.timestamp.isoformat(),
                }
                for c in commits
            ]
        except Exception:  # noqa: BLE001
            diagnostics["recent_commits"] = []

    # File watcher status
    if file_watcher is not None:
        diagnostics["file_watcher"] = {
            "running": file_watcher.is_running,
        }

    return diagnostics


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive values from configuration."""
    redacted = dict(config)

    # Redact token
    if CONF_AUTH_TOKEN in redacted and redacted[CONF_AUTH_TOKEN]:
        redacted[CONF_AUTH_TOKEN] = "**REDACTED**"

    # Redact token from remote URL
    if CONF_REMOTE_URL in redacted and redacted[CONF_REMOTE_URL]:
        redacted[CONF_REMOTE_URL] = re.sub(
            r"://[^@]+@", "://***@", redacted[CONF_REMOTE_URL]
        )

    # Redact SSH key path partially
    if CONF_SSH_KEY_PATH in redacted and redacted[CONF_SSH_KEY_PATH]:
        redacted[CONF_SSH_KEY_PATH] = "**REDACTED**"

    return redacted
