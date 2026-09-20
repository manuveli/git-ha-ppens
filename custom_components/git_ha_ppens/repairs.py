"""Home Assistant repair flows for git-ha-ppens."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .const import (
    CONF_GIT_EMAIL,
    CONF_GIT_USER,
    CONF_REPO_PATH,
    DOMAIN,
    REPAIR_UNSAFE_REPOSITORY_LAYOUT,
)
from .git_manager import GitError, GitManager
from .repository_layout import delete_unsafe_repository_layout_issue
from .repository_migration import (
    RepositoryMigrationAssessment,
    RepositoryMigrationError,
    async_assess_esphome_repository_migration,
    async_migrate_esphome_repository,
)

_CONFIRM_ESPHOME_STOPPED = "confirm_esphome_stopped"
_MIGRATION_LOCKS = f"{DOMAIN}_repository_migration_locks"

_LOGGER = logging.getLogger(__name__)


class UnsafeRepositoryLayoutRepair(RepairsFlow):
    """Guide or migrate an unsafe embedded repository."""

    def __init__(
        self,
        issue_id: str,
        data: dict[str, str | int | float | None] | None,
    ) -> None:
        """Initialize the repair flow."""
        super().__init__()
        self.issue_id = issue_id
        self.data = data
        self._last_assessment: RepositoryMigrationAssessment | None = None

    @property
    def entry_id(self) -> str:
        """Return the affected config entry ID."""
        if isinstance(self.data, dict) and isinstance(
            self.data.get("entry_id"), str
        ):
            return cast(str, self.data["entry_id"])
        prefix = f"{REPAIR_UNSAFE_REPOSITORY_LAYOUT}_"
        return self.issue_id.removeprefix(prefix)

    def _manager(self) -> GitManager | None:
        """Build a manager from the current config entry."""
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return None
        return GitManager(
            entry.data[CONF_REPO_PATH],
            entry.data.get(CONF_GIT_USER, ""),
            entry.data.get(CONF_GIT_EMAIL, ""),
        )

    def _migration_lock(self) -> asyncio.Lock:
        """Return the entry-specific migration lock."""
        locks: dict[str, asyncio.Lock] = self.hass.data.setdefault(
            _MIGRATION_LOCKS, {}
        )
        return locks.setdefault(self.entry_id, asyncio.Lock())

    async def _assess(
        self, manager: GitManager
    ) -> RepositoryMigrationAssessment:
        """Refresh and remember the automatic-migration assessment."""
        self._last_assessment = (
            await async_assess_esphome_repository_migration(manager)
        )
        return self._last_assessment

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Offer only repair paths that are safe for the current layout."""
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="entry_removed")
        assessment = await self._assess(manager)
        try:
            paths = await manager.get_unsafe_repository_paths()
        except GitError:
            paths = []
        menu_options = ["manual"]
        if assessment.eligible:
            menu_options.insert(0, "migrate_esphome")
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            description_placeholders={
                "paths": ", ".join(paths[:5]) or "unknown",
                "reason": assessment.reason,
            },
        )

    async def async_step_migrate_esphome(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Confirm ownership is quiescent, then perform the migration."""
        errors: dict[str, str] = {}
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="entry_removed")

        if user_input is not None:
            if not user_input.get(_CONFIRM_ESPHOME_STOPPED, False):
                errors["base"] = "confirm_required"
            else:
                try:
                    async with self._migration_lock():
                        runtime = (
                            self.hass.data.get(DOMAIN, {}).get(
                                self.entry_id, {}
                            )
                        )
                        coordinator = (
                            runtime.get("coordinator")
                            if isinstance(runtime, dict)
                            else None
                        )
                        if coordinator is None:
                            result = await async_migrate_esphome_repository(
                                manager
                            )
                        else:
                            async with coordinator.git_lock:
                                result = (
                                    await async_migrate_esphome_repository(
                                        manager
                                    )
                                )
                    delete_unsafe_repository_layout_issue(
                        self.hass, self.entry_id
                    )
                    self.hass.config_entries.async_schedule_reload(
                        self.entry_id
                    )
                    return self.async_create_entry(
                        title="",
                        data={},
                        description="migration_successful",
                        description_placeholders={
                            "backup_path": result.backup_path
                        },
                    )
                except RepositoryMigrationError as err:
                    _LOGGER.exception(
                        "ESPHome repository migration stopped safely: %s",
                        err.reason,
                    )
                    self._last_assessment = RepositoryMigrationAssessment(
                        False, err.reason
                    )
                    errors["base"] = "migration_failed"

        assessment = self._last_assessment or await self._assess(manager)
        return self.async_show_form(
            step_id="migrate_esphome",
            data_schema=vol.Schema(
                {vol.Required(_CONFIRM_ESPHOME_STOPPED, default=False): bool}
            ),
            errors=errors,
            description_placeholders={"reason": assessment.reason},
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Recheck the layout after the user completes the manual repair."""
        errors: dict[str, str] = {}
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="entry_removed")

        paths: list[str] = []
        if user_input is not None:
            try:
                paths = await manager.get_unsafe_repository_paths()
            except GitError as err:
                _LOGGER.warning(
                    "Could not recheck repository layout: %s", err
                )
                errors["base"] = "recheck_failed"
            else:
                if paths:
                    errors["base"] = "still_unsafe"
                else:
                    delete_unsafe_repository_layout_issue(
                        self.hass, self.entry_id
                    )
                    self.hass.config_entries.async_schedule_reload(
                        self.entry_id
                    )
                    return self.async_create_entry(
                        title="",
                        data={},
                        description="manual_fix_complete",
                    )

        if not paths:
            try:
                paths = await manager.get_unsafe_repository_paths()
            except GitError:
                paths = []
        displayed_paths = ", ".join(paths[:5]) or "unknown"
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"paths": displayed_paths},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the matching Home Assistant repair flow."""
    del hass
    prefix = f"{REPAIR_UNSAFE_REPOSITORY_LAYOUT}_"
    if issue_id.startswith(prefix):
        return UnsafeRepositoryLayoutRepair(issue_id, data)
    raise ValueError(f"Unknown repair issue: {issue_id}")
