"""Constants for the git-ha-ppens integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "git_ha_ppens"
PLATFORMS: Final = ["sensor", "binary_sensor", "button"]

# Configuration keys
CONF_REPO_PATH: Final = "repo_path"
CONF_GIT_USER: Final = "git_user"
CONF_GIT_EMAIL: Final = "git_email"
CONF_AUTO_COMMIT: Final = "auto_commit"
CONF_AUTO_PUSH: Final = "auto_push"
CONF_AUTO_PULL: Final = "auto_pull"
CONF_COMMIT_INTERVAL: Final = "commit_interval"
CONF_REMOTE_URL: Final = "remote_url"
CONF_AUTH_METHOD: Final = "auth_method"
CONF_AUTH_TOKEN: Final = "auth_token"
CONF_SSH_KEY_PATH: Final = "ssh_key_path"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_AI_COMMIT_MESSAGES: Final = "ai_commit_messages"
CONF_AI_AGENT_ID: Final = "ai_agent_id"
CONF_FETCH_INTERVAL: Final = "fetch_interval"
CONF_GITIGNORE_CONTENT: Final = "gitignore_content"
CONF_GITIGNORE_CUSTOM: Final = "gitignore_custom"
CONF_GITIGNORE_INITIALIZED: Final = "gitignore_initialized"
CONF_PRE_DEPLOY_CHECK: Final = "pre_deploy_check"

# Authentication methods
AUTH_NONE: Final = "none"
AUTH_TOKEN: Final = "token"
AUTH_SSH: Final = "ssh"

# Defaults
DEFAULT_REPO_PATH: Final = "/config"
DEFAULT_COMMIT_INTERVAL: Final = 300  # seconds (5 minutes)
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
DEFAULT_FETCH_INTERVAL: Final = 300  # seconds (5 minutes)
DEFAULT_BRANCH: Final = "main"
DEFAULT_AI_DIFF_MAX_CHARS: Final = 4000
DEFAULT_PRE_DEPLOY_CHECK: Final = False

# Persistent runtime storage
STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = f"{DOMAIN}.runtime_state"
STORAGE_LAST_FETCH_TIME: Final = "last_fetch_time"
STORAGE_LAST_PULL_TIME: Final = "last_pull_time"
STORAGE_LAST_PUSH_TIME: Final = "last_push_time"

# Events
EVENT_COMMIT: Final = f"{DOMAIN}_commit"
EVENT_PUSH: Final = f"{DOMAIN}_push"
EVENT_PULL: Final = f"{DOMAIN}_pull"
EVENT_ERROR: Final = f"{DOMAIN}_error"
EVENT_FETCH: Final = f"{DOMAIN}_fetch"
EVENT_SECRET_DETECTED: Final = f"{DOMAIN}_secret_detected"
EVENT_CHECK_FAILED: Final = f"{DOMAIN}_check_failed"

# Sensor keys
SENSOR_LAST_COMMIT: Final = "last_commit"
SENSOR_LAST_COMMIT_TIME: Final = "last_commit_time"
SENSOR_UNCOMMITTED_CHANGES: Final = "uncommitted_changes"
SENSOR_BRANCH: Final = "branch"
SENSOR_REMOTE_STATUS: Final = "remote_status"

# Binary sensor keys
BINARY_SENSOR_DIRTY: Final = "dirty"

# Service names
SERVICE_COMMIT: Final = "commit"
SERVICE_PUSH: Final = "push"
SERVICE_PULL: Final = "pull"
SERVICE_SYNC: Final = "sync"
SERVICE_FETCH: Final = "fetch"
SERVICE_DIFF: Final = "diff"
SERVICE_DISCARD_CHANGES: Final = "discard_changes"

# Service parameters
ATTR_MESSAGE: Final = "message"

# Static entity IDs. These keep technical entity IDs independent from areas,
# device names, and localized entity display names.
SENSOR_ENTITY_IDS: Final = {
    "last_commit": f"sensor.{DOMAIN}_last_commit",
    "last_commit_time": f"sensor.{DOMAIN}_last_commit_time",
    "uncommitted_changes": f"sensor.{DOMAIN}_uncommitted_changes",
    "branch": f"sensor.{DOMAIN}_branch",
    "remote_status": f"sensor.{DOMAIN}_remote_status",
    "last_fetch_time": f"sensor.{DOMAIN}_last_fetch_time",
    "last_pull_time": f"sensor.{DOMAIN}_last_pull_time",
    "last_push_time": f"sensor.{DOMAIN}_last_push_time",
    "commits_behind": f"sensor.{DOMAIN}_commits_behind",
    "commits_ahead": f"sensor.{DOMAIN}_commits_ahead",
}
BINARY_SENSOR_ENTITY_IDS: Final = {
    "dirty": f"binary_sensor.{DOMAIN}_dirty",
}
BUTTON_ENTITY_IDS: Final = {
    "push": f"button.{DOMAIN}_push",
    "pull": f"button.{DOMAIN}_pull",
    "fetch": f"button.{DOMAIN}_fetch",
    "discard_changes": f"button.{DOMAIN}_discard_changes",
}


def button_entity_id_targets(entry_id: str) -> dict[str, str]:
    """Return known button unique IDs and their stable entity IDs."""
    return {
        f"{entry_id}_{key}": entity_id
        for key, entity_id in BUTTON_ENTITY_IDS.items()
    }


# Default .gitignore entries for Home Assistant
DEFAULT_GITIGNORE_ENTRIES: Final = [
    "# git-ha-ppens: Auto-generated .gitignore for Home Assistant",
    "# Sensitive files",
    "secrets.yaml",
    ".storage/",
    ".cloud/",
    "tls/",
    ".ssh/",
    "",
    "# Database and logs",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.log",
    "home-assistant_v2.db",
    "home-assistant.log*",
    "zigbee.db",
    "OZW_Log.txt",
    "",
    "# System files",
    ".HA_VERSION",
    "known_devices.yaml",
    "",
    "# Python cache",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    "",
    "# Other",
    ".git/",
    "deps/",
    "tts/",
    ".venv/",
    "custom_components/",
    "www/snapshots/",
    "",
    "# Zigbee2MQTT",
    "zigbee2mqtt/state.json",
    "zigbee2mqtt/coordinator_backup.json",
    "",
    "# Sensitive/runtime files",
    ".jwt_secret",
    "SERVICE_ACCOUNT.json",
    ".cache/",
    ".claude/",
    "ip_bans.yaml",
    ".ha_run.lock",
    ".exports",
    ".timeline",
    ".vacuum",
    "",
    "# Editor swap files",
    "*.swp",
    "*.swo",
    "",
    "# Claude Code",
    "CLAUDE.md",
]

# Secret detection patterns (regex)
SECRET_PATTERNS: Final = [
    r"(?i)(api[_-]?key|apikey)\s*[:=]\s*['\"]?[\w\-]{16,}",
    r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"]?.+['\"]?",
    r"(?i)(secret|token|access[_-]?key)\s*[:=]\s*['\"]?[\w\-]{16,}",
    r"(?i)(private[_-]?key)\s*[:=]\s*['\"]?[\w\-/+=]+",
    r"ghp_[a-zA-Z0-9]{36}",  # GitHub personal access token
    r"gho_[a-zA-Z0-9]{36}",  # GitHub OAuth token
    r"glpat-[\w\-]{20}",  # GitLab personal access token
    r"sk-[a-zA-Z0-9]{32,}",  # OpenAI-style keys
]
