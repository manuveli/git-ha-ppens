"""Config flow for git-ha-ppens integration."""

from __future__ import annotations

import asyncio
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
from homeassistant.helpers.selector import (
    ConversationAgentSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .const import (
    AUTH_NONE,
    AUTH_SSH,
    AUTH_TOKEN,
    AUTO_GC_INTERVAL_MAX_DAYS,
    AUTO_GC_INTERVAL_MIN_DAYS,
    CONF_AI_AGENT_ID,
    CONF_AI_COMMIT_MESSAGES,
    CONF_AUTH_METHOD,
    CONF_AUTH_TOKEN,
    CONF_AUTH_USERNAME,
    CONF_AUTO_COMMIT,
    CONF_AUTO_GC,
    CONF_AUTO_GC_INTERVAL_DAYS,
    CONF_AUTO_PULL,
    CONF_AUTO_PUSH,
    CONF_BRANCH_RESET_CONFIRM,
    CONF_BRANCH_TARGET,
    CONF_COMMIT_INTERVAL,
    CONF_FETCH_INTERVAL,
    CONF_GIT_EMAIL,
    CONF_GIT_USER,
    CONF_GITIGNORE_CONTENT,
    CONF_GITIGNORE_CUSTOM,
    CONF_NEW_BRANCH,
    CONF_PRE_DEPLOY_CHECK,
    CONF_REMOTE_URL,
    CONF_REPO_PATH,
    CONF_RESTORE_CONFIRM,
    CONF_RESTORE_PUSH,
    CONF_RESTORE_TARGET,
    CONF_SCAN_INTERVAL,
    CONF_SSH_KEY_PATH,
    DEFAULT_AUTO_GC,
    DEFAULT_AUTO_GC_INTERVAL_DAYS,
    DEFAULT_COMMIT_INTERVAL,
    DEFAULT_FETCH_INTERVAL,
    DEFAULT_REPO_PATH,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TOKEN_AUTH_USERNAME,
    DOMAIN,
    RESTORE_HISTORY_LIMIT,
    RESTORE_PREVIEW_COMMIT_LIMIT,
    RESTORE_PREVIEW_FILE_LIMIT,
    is_local_http_remote_url,
)
from .coordinator import GitHaPpensCoordinator
from .git_manager import (
    BranchError,
    CommitInfo,
    DirtyWorkingTreeError,
    GitError,
    GitManager,
    InvalidRestoreTargetError,
    RestorePreview,
    RestoreValidationError,
    StaleRestorePreviewError,
)

_LOGGER = logging.getLogger(__name__)


def _validate_remote_settings(user_input: dict[str, Any]) -> dict[str, str]:
    """Validate remote authentication settings shared by setup and options."""
    errors: dict[str, str] = {}
    remote_url = user_input.get(CONF_REMOTE_URL, "").strip()
    auth_method = user_input.get(CONF_AUTH_METHOD, AUTH_NONE)

    if not remote_url:
        return errors

    if remote_url.startswith("http://"):
        # Plain HTTP exposes tokens and configuration contents, so it is only
        # accepted for self-hosted servers inside the local network (#91).
        if not is_local_http_remote_url(remote_url):
            errors["base"] = "http_requires_local_host"
            return errors
    elif not remote_url.startswith(("https://", "git@", "ssh://")):
        errors["base"] = "invalid_remote_url"
        return errors

    if auth_method == AUTH_TOKEN:
        if not remote_url.startswith(("https://", "http://")):
            errors["base"] = "token_requires_https"
        elif not user_input.get(CONF_AUTH_TOKEN, "").strip():
            errors["base"] = "token_required"
        elif not user_input.get(
            CONF_AUTH_USERNAME, DEFAULT_TOKEN_AUTH_USERNAME
        ).strip():
            errors["base"] = "auth_username_required"
    elif auth_method == AUTH_SSH:
        ssh_key = user_input.get(CONF_SSH_KEY_PATH, "")
        if ssh_key and not Path(ssh_key).is_file():
            errors["base"] = "ssh_key_not_found"

    return errors


def _normalize_remote_settings(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize auth data and discard a username when token auth is unused."""
    normalized = dict(user_input)
    remote_url = normalized.get(CONF_REMOTE_URL, "").strip()
    auth_method = normalized.get(CONF_AUTH_METHOD, AUTH_NONE)
    normalized[CONF_REMOTE_URL] = remote_url

    if auth_method == AUTH_TOKEN and remote_url:
        normalized[CONF_AUTH_TOKEN] = normalized.get(
            CONF_AUTH_TOKEN, ""
        ).strip()
        normalized[CONF_AUTH_USERNAME] = normalized.get(
            CONF_AUTH_USERNAME, DEFAULT_TOKEN_AUTH_USERNAME
        ).strip()
    else:
        normalized.pop(CONF_AUTH_USERNAME, None)

    return normalized


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
                    vol.Required(CONF_PRE_DEPLOY_CHECK, default=False): bool,
                    vol.Required(
                        CONF_COMMIT_INTERVAL,
                        default=DEFAULT_COMMIT_INTERVAL,
                    ): vol.All(int, vol.Range(min=30, max=86400)),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=DEFAULT_SCAN_INTERVAL,
                    ): vol.All(int, vol.Range(min=10, max=3600)),
                    vol.Required(
                        CONF_FETCH_INTERVAL,
                        default=DEFAULT_FETCH_INTERVAL,
                    ): vol.All(int, vol.Range(min=60, max=3600)),
                    vol.Required(
                        CONF_AI_COMMIT_MESSAGES, default=False
                    ): bool,
                    vol.Optional(
                        CONF_AI_AGENT_ID, default=""
                    ): ConversationAgentSelector({}),
                }
            ),
        )

    async def async_step_remote(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle remote configuration (optional)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_remote_settings(user_input)

            if not errors:
                self._data.update(_normalize_remote_settings(user_input))
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
                    vol.Optional(
                        CONF_AUTH_USERNAME,
                        default=DEFAULT_TOKEN_AUTH_USERNAME,
                    ): str,
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
        self._restore_preview: RestorePreview | None = None
        self._restore_source_step = "restore_recent"
        self._branch_reset_preview: dict[str, Any] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show menu with options categories."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "branch", "gitignore", "restore"],
        )

    async def async_step_branch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between switching branches and resetting one to origin."""
        return self.async_show_menu(
            step_id="branch",
            menu_options=["branch_select", "branch_reset"],
        )

    async def _async_branch_options(
        self, manager: GitManager, *, local_only: bool
    ) -> tuple[dict[str, str], str]:
        """Build selector options from the branches of the repository."""
        local_branches = await manager.list_local_branches()
        current = await manager.get_current_branch()
        options: dict[str, str] = {}
        for name in local_branches:
            if name == current:
                options[name] = f"{name} · current"
            else:
                options[name] = name
        if local_only:
            return options, current
        for remote_ref in await manager.list_remote_branches():
            remote_name = remote_ref.removeprefix("origin/")
            if remote_name in options:
                options[remote_name] = f"{remote_name} · tracks {remote_ref}"
                continue
            options[remote_ref] = f"{remote_ref} · new local branch"
        return options, current

    @staticmethod
    def _branch_error_key(err: BranchError) -> str:
        """Map a branch failure reason to a localized form error."""
        return {
            "dirty": "branch_dirty_working_tree",
            "invalid": "invalid_branch_target",
            "exists": "branch_already_exists",
            "no_remote": "branch_reset_requires_remote",
            "missing_remote_branch": "branch_remote_missing",
        }.get(err.reason, "branch_operation_failed")

    async def _async_show_branch_select(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the dynamic branch selector."""
        options: dict[str, str] = {}
        current = ""
        form_errors = dict(errors or {})
        runtime = self._entry_runtime()
        if runtime is None:
            form_errors.setdefault("base", "integration_not_loaded")
        else:
            options, current = await self._async_branch_options(
                runtime[0], local_only=False
            )
            if not options:
                form_errors.setdefault("base", "no_branches")

        schema: dict[Any, Any] = {}
        if options:
            schema[vol.Required(CONF_BRANCH_TARGET)] = SelectSelector(
                SelectSelectorConfig(
                    options=options, mode=SelectSelectorMode.DROPDOWN
                )
            )
        schema[vol.Optional(CONF_NEW_BRANCH, default="")] = TextSelector(
            TextSelectorConfig(multiline=False)
        )

        return self.async_show_form(
            step_id="branch_select",
            data_schema=vol.Schema(schema),
            errors=form_errors,
            description_placeholders={
                "current_branch": self._escape_markdown(
                    current or "detached HEAD"
                ),
                "branch_count": str(len(options)),
            },
        )

    async def async_step_branch_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch to the branch chosen in the selector."""
        if user_input is None:
            return await self._async_show_branch_select()

        runtime = self._entry_runtime()
        if runtime is None:
            return await self._async_show_branch_select(
                {"base": "integration_not_loaded"}
            )

        new_branch = str(user_input.get(CONF_NEW_BRANCH, "")).strip()
        target = new_branch or user_input.get(CONF_BRANCH_TARGET, "")
        if not target:
            return await self._async_show_branch_select({"base": "no_branches"})

        try:
            branch = await runtime[1].async_switch_branch(
                target, create=bool(new_branch)
            )
        except BranchError as err:
            return await self._async_show_branch_select(
                {"base": self._branch_error_key(err)}
            )
        except GitError as err:
            _LOGGER.error("Branch switch failed: %s", err)
            return await self._async_show_branch_select(
                {"base": "branch_operation_failed"}
            )

        return self.async_abort(
            reason="branch_switched",
            description_placeholders={
                "branch": self._escape_markdown(branch),
            },
        )

    async def _async_show_branch_reset(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the selector for the branch that origin should overwrite."""
        options: dict[str, str] = {}
        form_errors = dict(errors or {})
        runtime = self._entry_runtime()
        if runtime is None:
            form_errors.setdefault("base", "integration_not_loaded")
        elif not runtime[1].remote_configured:
            form_errors.setdefault("base", "branch_reset_requires_remote")
        else:
            options, _ = await self._async_branch_options(
                runtime[0], local_only=True
            )
            if not options:
                form_errors.setdefault("base", "no_branches")

        schema: dict[Any, Any] = {}
        if options:
            schema[vol.Required(CONF_BRANCH_TARGET)] = SelectSelector(
                SelectSelectorConfig(
                    options=options, mode=SelectSelectorMode.DROPDOWN
                )
            )

        return self.async_show_form(
            step_id="branch_reset",
            data_schema=vol.Schema(schema),
            errors=form_errors,
        )

    async def async_step_branch_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fetch the remote revision and preview the destructive reset."""
        if user_input is None:
            return await self._async_show_branch_reset()

        runtime = self._entry_runtime()
        if runtime is None:
            return await self._async_show_branch_reset(
                {"base": "integration_not_loaded"}
            )

        target = str(user_input.get(CONF_BRANCH_TARGET, "")).strip()
        manager = runtime[0]
        try:
            remote_ref = await manager.fetch_remote_branch(target)
            ahead, behind = await manager.get_branch_divergence(
                target, remote_ref
            )
        except BranchError as err:
            return await self._async_show_branch_reset(
                {"base": self._branch_error_key(err)}
            )
        except GitError as err:
            _LOGGER.error("Could not read origin/%s: %s", target, err)
            return await self._async_show_branch_reset(
                {"base": "branch_reset_failed"}
            )

        self._branch_reset_preview = {
            "branch": target,
            "remote_ref": remote_ref,
            "ahead": ahead,
            "behind": behind,
        }
        return self._async_show_branch_reset_confirm()

    def _async_show_branch_reset_confirm(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the destructive reset confirmation form."""
        preview = self._branch_reset_preview
        if preview is None:
            return self.async_abort(reason="branch_reset_preview_missing")

        return self.async_show_form(
            step_id="branch_reset_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_BRANCH_RESET_CONFIRM, default=False): bool}
            ),
            errors=errors or {},
            description_placeholders={
                "branch": self._escape_markdown(preview["branch"]),
                "remote_ref": self._escape_markdown(preview["remote_ref"]),
                "discarded_commits": str(preview["ahead"]),
                "incoming_commits": str(preview["behind"]),
            },
        )

    async def async_step_branch_reset_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Overwrite the local branch with the remote revision."""
        if user_input is None:
            return self._async_show_branch_reset_confirm()
        if not user_input[CONF_BRANCH_RESET_CONFIRM]:
            return self._async_show_branch_reset_confirm(
                {CONF_BRANCH_RESET_CONFIRM: "confirm_branch_reset"}
            )

        preview = self._branch_reset_preview
        if preview is None:
            return self.async_abort(reason="branch_reset_preview_missing")
        runtime = self._entry_runtime()
        if runtime is None:
            return self._async_show_branch_reset_confirm(
                {"base": "integration_not_loaded"}
            )

        try:
            branch = await runtime[1].async_reset_branch_to_origin(
                preview["branch"]
            )
        except BranchError as err:
            return await self._async_show_branch_reset(
                {"base": self._branch_error_key(err)}
            )
        except GitError as err:
            _LOGGER.error("Branch reset failed: %s", err)
            return await self._async_show_branch_reset(
                {"base": "branch_reset_failed"}
            )

        return self.async_abort(
            reason="branch_reset",
            description_placeholders={
                "branch": self._escape_markdown(branch),
                "remote_ref": self._escape_markdown(preview["remote_ref"]),
            },
        )

    async def async_step_restore(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how to select a historical restore target."""
        return self.async_show_menu(
            step_id="restore",
            menu_options=["restore_recent", "restore_sha"],
        )

    def _entry_runtime(
        self,
    ) -> tuple[GitManager, GitHaPpensCoordinator] | None:
        """Return the loaded manager and coordinator for this entry."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(
            self._config_entry.entry_id
        )
        if not isinstance(entry_data, dict):
            return None
        manager = entry_data.get("git_manager")
        coordinator = entry_data.get("coordinator")
        if not isinstance(manager, GitManager) or not isinstance(
            coordinator, GitHaPpensCoordinator
        ):
            return None
        return manager, coordinator

    @staticmethod
    def _restore_commit_label(commit: CommitInfo) -> str:
        """Return a concise label for a commit selector option."""
        timestamp = dt_util.as_local(commit.timestamp).strftime("%Y-%m-%d %H:%M")
        message = " ".join(commit.message.split())
        if len(message) > 72:
            message = f"{message[:69]}..."
        return f"{commit.hash_short} · {timestamp} · {message}"

    async def _async_show_restore_recent(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show a selector with the most recent historical commits."""
        runtime = self._entry_runtime()
        options: dict[str, str] = {}
        form_errors = dict(errors or {})
        head_commit = "—"
        if runtime is None:
            form_errors.setdefault("base", "integration_not_loaded")
        else:
            commits = await runtime[0].get_log(RESTORE_HISTORY_LIMIT + 1)
            if commits:
                head_commit = self._escape_markdown(
                    self._restore_commit_label(commits[0])
                )
            options = {
                commit.hash: self._restore_commit_label(commit)
                for commit in commits[1 : RESTORE_HISTORY_LIMIT + 1]
            }
            if not options:
                form_errors.setdefault("base", "no_restore_commits")

        return self.async_show_form(
            step_id="restore_recent",
            data_schema=vol.Schema(
                {vol.Required(CONF_RESTORE_TARGET): vol.In(options)}
            ),
            errors=form_errors,
            description_placeholders={"head_commit": head_commit},
        )

    async def async_step_restore_recent(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one of the recent historical commits."""
        if user_input is None:
            return await self._async_show_restore_recent()
        self._restore_source_step = "restore_recent"
        return await self._async_prepare_restore(
            user_input[CONF_RESTORE_TARGET]
        )

    async def _async_show_restore_sha(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the manual SHA restore form."""
        form_errors = dict(errors or {})
        if self._entry_runtime() is None:
            form_errors.setdefault("base", "integration_not_loaded")
        return self.async_show_form(
            step_id="restore_sha",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RESTORE_TARGET): TextSelector(
                        TextSelectorConfig(multiline=False)
                    )
                }
            ),
            errors=form_errors,
        )

    async def async_step_restore_sha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an older historical commit by SHA."""
        if user_input is None:
            return await self._async_show_restore_sha()
        self._restore_source_step = "restore_sha"
        return await self._async_prepare_restore(
            user_input[CONF_RESTORE_TARGET]
        )

    async def _async_show_restore_source_error(
        self, error: str
    ) -> ConfigFlowResult:
        """Return to the active target selector with an error."""
        if self._restore_source_step == "restore_sha":
            return await self._async_show_restore_sha({"base": error})
        return await self._async_show_restore_recent({"base": error})

    async def _async_prepare_restore(
        self, target_ref: str
    ) -> ConfigFlowResult:
        """Validate a restore target and build its confirmation preview."""
        runtime = self._entry_runtime()
        if runtime is None:
            return await self._async_show_restore_source_error(
                "integration_not_loaded"
            )
        manager, _ = runtime
        try:
            if not await manager.is_worktree_clean():
                raise DirtyWorkingTreeError(
                    "Local changes must be handled before restoring"
                )
            self._restore_preview = await manager.get_restore_preview(target_ref)
        except DirtyWorkingTreeError:
            return await self._async_show_restore_source_error(
                "dirty_working_tree"
            )
        except InvalidRestoreTargetError:
            return await self._async_show_restore_source_error(
                "invalid_restore_target"
            )
        except GitError as err:
            _LOGGER.warning("Could not prepare restore preview: %s", err)
            return await self._async_show_restore_source_error(
                "restore_preview_failed"
            )
        return self._async_show_restore_confirm()

    @staticmethod
    def _escape_markdown(value: str) -> str:
        """Escape repository-provided text embedded in markdown descriptions."""
        escaped = value.replace("\\", "\\\\")
        for character in ("`", "*", "_", "[", "]", "<", ">"):
            escaped = escaped.replace(character, f"\\{character}")
        return escaped

    @staticmethod
    def _format_restore_preview(preview: RestorePreview) -> dict[str, str]:
        """Build bounded description placeholders for the confirmation form."""
        commits = preview.commits[:RESTORE_PREVIEW_COMMIT_LIMIT]
        commit_lines = [
            f"- `{commit.hash_short}` "
            f"{GitHaPpensOptionsFlow._escape_markdown(commit.message)}"
            for commit in commits
        ]
        remaining_commits = len(preview.commits) - len(commits)
        if remaining_commits:
            commit_lines.append(f"- … (+{remaining_commits})")

        files = preview.changed_files[:RESTORE_PREVIEW_FILE_LIMIT]
        file_lines = []
        for change in files:
            path = GitHaPpensOptionsFlow._escape_markdown(change.path)
            if change.old_path is not None:
                old_path = GitHaPpensOptionsFlow._escape_markdown(
                    change.old_path
                )
                path = f"{old_path} → {path}"
            file_lines.append(f"- `{change.status}` {path}")
        remaining_files = len(preview.changed_files) - len(files)
        if remaining_files:
            file_lines.append(f"- … (+{remaining_files})")

        return {
            "target_hash": preview.target.hash_short,
            "target_message": GitHaPpensOptionsFlow._escape_markdown(
                preview.target.message
            ),
            "target_author": GitHaPpensOptionsFlow._escape_markdown(
                preview.target.author
            ),
            "target_time": dt_util.as_local(preview.target.timestamp).strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            ),
            "commit_count": str(len(preview.commits)),
            "commits": "\n".join(commit_lines) or "—",
            "file_count": str(len(preview.changed_files)),
            "files": "\n".join(file_lines) or "—",
            "additions": str(preview.additions),
            "deletions": str(preview.deletions),
            "binary_files": str(preview.binary_files),
        }

    def _async_show_restore_confirm(
        self,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show the destructive restore confirmation form."""
        if self._restore_preview is None:
            return self.async_abort(reason="restore_preview_missing")

        runtime = self._entry_runtime()
        remote_configured = bool(runtime and runtime[1].remote_configured)
        schema: dict[Any, Any] = {
            vol.Required(CONF_RESTORE_CONFIRM, default=False): bool
        }
        if remote_configured:
            schema[
                vol.Required(
                    CONF_RESTORE_PUSH,
                    default=self._config_entry.data.get(CONF_AUTO_PUSH, True),
                )
            ] = bool

        return self.async_show_form(
            step_id="restore_confirm",
            data_schema=vol.Schema(schema),
            errors=errors or {},
            description_placeholders=self._format_restore_preview(
                self._restore_preview
            ),
        )

    async def async_step_restore_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Execute a confirmed historical snapshot restore."""
        if user_input is None:
            return self._async_show_restore_confirm()
        if not user_input[CONF_RESTORE_CONFIRM]:
            return self._async_show_restore_confirm(
                {CONF_RESTORE_CONFIRM: "confirm_restore"}
            )
        if self._restore_preview is None:
            return self.async_abort(reason="restore_preview_missing")
        runtime = self._entry_runtime()
        if runtime is None:
            return self._async_show_restore_confirm(
                {"base": "integration_not_loaded"}
            )

        coordinator = runtime[1]
        try:
            result = await coordinator.async_restore_snapshot(
                self._restore_preview.target.hash,
                self._restore_preview.source_head,
                push=bool(user_input.get(CONF_RESTORE_PUSH, False)),
            )
        except DirtyWorkingTreeError:
            return await self._async_show_restore_source_error(
                "dirty_working_tree"
            )
        except StaleRestorePreviewError:
            return await self._async_show_restore_source_error(
                "stale_restore_preview"
            )
        except InvalidRestoreTargetError:
            return await self._async_show_restore_source_error(
                "invalid_restore_target"
            )
        except RestoreValidationError as err:
            _LOGGER.warning("Restore validation failed: %s", "; ".join(err.errors))
            return self._async_show_restore_confirm(
                {"base": "restore_validation_failed"}
            )
        except GitError as err:
            _LOGGER.error("Restore failed: %s", err)
            return self._async_show_restore_confirm({"base": "restore_failed"})

        placeholders = {
            "target_hash": result.restore.target.hash_short,
            "restore_hash": result.restore.commit.hash_short,
        }
        if result.push_error is not None:
            return self.async_abort(
                reason="restore_success_push_failed",
                description_placeholders=placeholders,
            )
        return self.async_abort(
            reason="restore_success",
            description_placeholders=placeholders,
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage general options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_remote_settings(user_input)
            if not errors:
                normalized = _normalize_remote_settings(user_input)
                # Merge options with existing data
                new_data = {**self._config_entry.data, **normalized}
                if CONF_AUTH_USERNAME not in normalized:
                    new_data.pop(CONF_AUTH_USERNAME, None)
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=new_data
                )
                return self.async_create_entry(title="", data=normalized)

        current = self._config_entry.data

        return self.async_show_form(
            step_id="general",
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
                        CONF_PRE_DEPLOY_CHECK,
                        default=current.get(CONF_PRE_DEPLOY_CHECK, False),
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
                    vol.Required(
                        CONF_FETCH_INTERVAL,
                        default=current.get(
                            CONF_FETCH_INTERVAL, DEFAULT_FETCH_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=60, max=3600)),
                    vol.Required(
                        CONF_AUTO_GC,
                        default=current.get(CONF_AUTO_GC, DEFAULT_AUTO_GC),
                    ): bool,
                    vol.Required(
                        CONF_AUTO_GC_INTERVAL_DAYS,
                        default=current.get(
                            CONF_AUTO_GC_INTERVAL_DAYS,
                            DEFAULT_AUTO_GC_INTERVAL_DAYS,
                        ),
                    ): vol.All(
                        int,
                        vol.Range(
                            min=AUTO_GC_INTERVAL_MIN_DAYS,
                            max=AUTO_GC_INTERVAL_MAX_DAYS,
                        ),
                    ),
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
                        CONF_AUTH_USERNAME,
                        default=current.get(
                            CONF_AUTH_USERNAME,
                            DEFAULT_TOKEN_AUTH_USERNAME,
                        ),
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
                    ): ConversationAgentSelector({}),
                }
            ),
            errors=errors,
        )

    async def async_step_gitignore(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage .gitignore entries."""
        if user_input is not None:
            gitignore_content = user_input[CONF_GITIGNORE_CONTENT]
            # Normalize line endings
            gitignore_content = gitignore_content.replace("\r\n", "\n")

            # Store custom flag in config entry data
            new_data = {
                **self._config_entry.data,
                CONF_GITIGNORE_CUSTOM: True,
            }
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=new_data
            )

            # Write .gitignore to disk
            repo_path = self._config_entry.data[CONF_REPO_PATH]
            await asyncio.to_thread(self._write_gitignore, repo_path, gitignore_content)

            # Apply gitignore rules to git index
            entry_data = self.hass.data.get(DOMAIN, {}).get(
                self._config_entry.entry_id, {}
            )
            git_manager = entry_data.get("git_manager")
            if git_manager:
                try:
                    await git_manager.apply_gitignore()
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("Failed to apply .gitignore changes to git index")

            return self.async_create_entry(title="", data={})

        # Read current .gitignore content from disk
        repo_path = self._config_entry.data[CONF_REPO_PATH]
        current_content = await asyncio.to_thread(self._read_gitignore, repo_path)

        return self.async_show_form(
            step_id="gitignore",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GITIGNORE_CONTENT,
                        default=current_content,
                    ): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
        )

    @staticmethod
    def _read_gitignore(repo_path: str) -> str:
        """Read .gitignore content from disk."""
        gitignore_path = Path(repo_path) / ".gitignore"
        if gitignore_path.exists():
            return gitignore_path.read_text(encoding="utf-8")
        return ""

    @staticmethod
    def _write_gitignore(repo_path: str, content: str) -> None:
        """Write .gitignore content to disk."""
        gitignore_path = Path(repo_path) / ".gitignore"
        if content and not content.endswith("\n"):
            content += "\n"
        gitignore_path.write_text(content, encoding="utf-8")
