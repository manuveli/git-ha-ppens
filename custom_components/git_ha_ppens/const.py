"""Constants for the git-ha-ppens integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "git_ha_ppens"
PLATFORMS: Final = ["sensor", "binary_sensor"]

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

# Authentication methods
AUTH_NONE: Final = "none"
AUTH_TOKEN: Final = "token"
AUTH_SSH: Final = "ssh"

# Defaults
DEFAULT_REPO_PATH: Final = "/config"
DEFAULT_COMMIT_INTERVAL: Final = 300  # seconds (5 minutes)
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
DEFAULT_BRANCH: Final = "main"

# Events
EVENT_COMMIT: Final = f"{DOMAIN}_commit"
EVENT_PUSH: Final = f"{DOMAIN}_push"
EVENT_PULL: Final = f"{DOMAIN}_pull"
EVENT_ERROR: Final = f"{DOMAIN}_error"
EVENT_SECRET_DETECTED: Final = f"{DOMAIN}_secret_detected"

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

# Service parameters
ATTR_MESSAGE: Final = "message"

# Default .gitignore entries for Home Assistant
DEFAULT_GITIGNORE_ENTRIES: Final = [
    "# git-ha-ppens: Auto-generated .gitignore for Home Assistant",
    "# Sensitive files",
    "secrets.yaml",
    ".storage/",
    ".cloud/",
    "tls/",
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

# File patterns to ignore when watching for changes
WATCHER_IGNORE_PATTERNS: Final = [
    ".git",
    ".storage",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.log",
    "home-assistant_v2.db",
    "home-assistant.log",
    "OZW_Log.txt",
    "deps",
    "tts",
    ".venv",
]
