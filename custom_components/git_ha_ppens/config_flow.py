"""Config flow for git-ha-ppens integration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    AUTH_NONE,
    AUTH_SSH,
    AUTH_TOKEN,
    CONF_AI_AGENT_ID,
    CONF_AI_COMMIT_MESSAGES,
    CONF_AUTH_METHOD,
    CONF_AUTH_TOKEN,
    CONF_AUTO_COMMIT,
    CONF_AUTO_PULL,
    CONF_AUTO_PUSH,
    CONF_COMMIT_INTERVAL,
    CONF_GIT_EMAIL,
    CONF_GIT_USER,
    CONF_REMOTE_URL,
    CONF_REPO_PATH,
    CONF_SCAN_INTERVAL,
    CONF_SSH_KEY_PATH,
    DEFAULT_COMMIT_INTERVAL,
    DEFAULT_REPO_PATH,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .git_manager import GitManager

_LOGGER = logging.getLogger(__name__)


class GitHaPpensConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for git-ha-ppens."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: repository settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            repo_path = user_input[CONF_REPO_PATH]

            # Validate path exists
            if not Path(repo_path).is_dir():
                errors["base"] = "path_not_found"
            else:
                # Check git is installed
                git_manager = GitManager(repo_path)
                if not await git_manager.is_git_installed():
                    errors["base"] = "git_not_installed"
                else:
                    self._data.update(user_input)
                    return await self.async_step_commit_settings()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_REPO_PATH, default=DEFAULT_REPO_PATH
                    ): str,
                    vol.Required(CONF_GIT_USER, default=""): str,
                    vol.Required(CONF_GIT_EMAIL, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_commit_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle auto-commit configuration."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_remote()

        return self.async_show_form(
            step_id="commit_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTO_COMMIT, default=True): bool,
                    vol.Required(CONF_AUTO_PUSH, default=True): bool,
                    vol.Required(CONF_AUTO_PULL, default=False): bool,
                    vol.Required(
                        CONF_COMMIT_INTERVAL,
                        default=DEFAULT_COMMIT_INTERVAL,
                    ): vol.All(int, vol.Range(min=30, max=86400)),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=DEFAULT_SCAN_INTERVAL,
                    ): vol.All(int, vol.Range(min=10, max=3600)),
                    vol.Required(
                        CONF_AI_COMMIT_MESSAGES, default=False
                    ): bool,
                    vol.Optional(
                        CONF_AI_AGENT_ID, default=""
                    ): str,
                }
            ),
        )

    async def async_step_remote(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle remote configuration (optional)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            remote_url = user_input.get(CONF_REMOTE_URL, "")
            auth_method = user_input.get(CONF_AUTH_METHOD, AUTH_NONE)

            # Validate remote URL format if provided
            if remote_url:
                if not (
                    remote_url.startswith("https://")
                    or remote_url.startswith("git@")
                    or remote_url.startswith("ssh://")
                ):
                    errors["base"] = "invalid_remote_url"

                # Validate auth requirements
                if auth_method == AUTH_TOKEN and not user_input.get(CONF_AUTH_TOKEN):
                    errors["base"] = "token_required"
                elif auth_method == AUTH_SSH:
                    ssh_key = user_input.get(CONF_SSH_KEY_PATH, "")
                    if ssh_key and not Path(ssh_key).is_file():
                        errors["base"] = "ssh_key_not_found"

            if not errors:
                self._data.update(user_input)
                # Set defaults for missing optional fields
                self._data.setdefault(CONF_REMOTE_URL, "")
                self._data.setdefault(CONF_AUTH_METHOD, AUTH_NONE)
                self._data.setdefault(CONF_AUTH_TOKEN, "")
                self._data.setdefault(CONF_SSH_KEY_PATH, "")

                # Prevent duplicate entries for the same path
                await self.async_set_unique_id(self._data[CONF_REPO_PATH])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"git-ha-ppens ({self._data[CONF_REPO_PATH]})",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="remote",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_REMOTE_URL, default=""): str,
                    vol.Optional(
                        CONF_AUTH_METHOD, default=AUTH_NONE
                    ): vol.In(
                        {
                            AUTH_NONE: "None",
                            AUTH_TOKEN: "Personal Access Token",
                            AUTH_SSH: "SSH Key",
                        }
                    ),
                    vol.Optional(CONF_AUTH_TOKEN, default=""): str,
                    vol.Optional(CONF_SSH_KEY_PATH, default=""): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> GitHaPpensOptionsFlow:
        """Return the options flow handler."""
        return GitHaPpensOptionsFlow(config_entry)


class GitHaPpensOptionsFlow(OptionsFlow):
    """Handle options flow for git-ha-ppens."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # Merge options with existing data
            new_data = {**self._config_entry.data, **user_input}
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GIT_USER,
                        default=current.get(CONF_GIT_USER, ""),
                    ): str,
                    vol.Required(
                        CONF_GIT_EMAIL,
                        default=current.get(CONF_GIT_EMAIL, ""),
                    ): str,
                    vol.Required(
                        CONF_AUTO_COMMIT,
                        default=current.get(CONF_AUTO_COMMIT, True),
                    ): bool,
                    vol.Required(
                        CONF_AUTO_PUSH,
                        default=current.get(CONF_AUTO_PUSH, True),
                    ): bool,
                    vol.Required(
                        CONF_AUTO_PULL,
                        default=current.get(CONF_AUTO_PULL, False),
                    ): bool,
                    vol.Required(
                        CONF_COMMIT_INTERVAL,
                        default=current.get(
                            CONF_COMMIT_INTERVAL, DEFAULT_COMMIT_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=30, max=86400)),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=10, max=3600)),
                    vol.Optional(
                        CONF_REMOTE_URL,
                        default=current.get(CONF_REMOTE_URL, ""),
                    ): str,
                    vol.Optional(
                        CONF_AUTH_METHOD,
                        default=current.get(CONF_AUTH_METHOD, AUTH_NONE),
                    ): vol.In(
                        {
                            AUTH_NONE: "None",
                            AUTH_TOKEN: "Personal Access Token",
                            AUTH_SSH: "SSH Key",
                        }
                    ),
                    vol.Optional(
                        CONF_AUTH_TOKEN,
                        default=current.get(CONF_AUTH_TOKEN, ""),
                    ): str,
                    vol.Optional(
                        CONF_SSH_KEY_PATH,
                        default=current.get(CONF_SSH_KEY_PATH, ""),
                    ): str,
                    vol.Required(
                        CONF_AI_COMMIT_MESSAGES,
                        default=current.get(CONF_AI_COMMIT_MESSAGES, False),
                    ): bool,
                    vol.Optional(
                        CONF_AI_AGENT_ID,
                        default=current.get(CONF_AI_AGENT_ID, ""),
                    ): str,
                }
            ),
        )
