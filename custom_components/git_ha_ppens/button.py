"""Button platform for git-ha-ppens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GitHaPpensCoordinator
from .git_manager import GitError, PreDeployCheckError

GitOperation = Literal["push", "pull", "fetch", "discard_changes"]


@dataclass(frozen=True, kw_only=True)
class GitHaPpensButtonDescription(ButtonEntityDescription):
    """Describe a git-ha-ppens button."""

    operation: GitOperation


BUTTON_DESCRIPTIONS: tuple[GitHaPpensButtonDescription, ...] = (
    GitHaPpensButtonDescription(
        key="push",
        translation_key="push",
        icon="mdi:source-merge",
        operation="push",
    ),
    GitHaPpensButtonDescription(
        key="pull",
        translation_key="pull",
        icon="mdi:source-pull",
        operation="pull",
    ),
    GitHaPpensButtonDescription(
        key="fetch",
        translation_key="fetch",
        icon="mdi:cloud-download-outline",
        operation="fetch",
    ),
    GitHaPpensButtonDescription(
        key="discard_changes",
        translation_key="discard_changes",
        icon="mdi:undo-variant",
        entity_registry_enabled_default=False,
        operation="discard_changes",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up git-ha-ppens buttons from a config entry."""
    coordinator: GitHaPpensCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    entity_ids: dict[str, str] = hass.data[DOMAIN][entry.entry_id]["entity_ids"][
        "button"
    ]
    async_add_entities(
        GitHaPpensButton(coordinator, description, entry, entity_ids[description.key])
        for description in BUTTON_DESCRIPTIONS
        if description.operation == "discard_changes" or coordinator.remote_configured
    )


class GitHaPpensButton(CoordinatorEntity[GitHaPpensCoordinator], ButtonEntity):
    """Run a manual git operation for one configured repository."""

    entity_description: GitHaPpensButtonDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GitHaPpensCoordinator,
        description: GitHaPpensButtonDescription,
        entry: ConfigEntry,
        entity_id: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        self.entity_id = entity_id
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "git-ha-ppens",
            "manufacturer": "git-ha-ppens",
            "model": "Git Version Control",
            "sw_version": "1.1.1",
            "entry_type": "service",
            "configuration_url": "https://github.com/manuveli/git-ha-ppens",
        }

    async def async_press(self) -> None:
        """Run the configured git operation."""
        try:
            if self.entity_description.operation == "push":
                await self.coordinator.async_manual_commit_and_push()
            elif self.entity_description.operation == "pull":
                await self.coordinator.async_manual_pull()
            elif self.entity_description.operation == "fetch":
                await self.coordinator.async_manual_fetch()
            else:
                await self.coordinator.async_discard_changes()
        except PreDeployCheckError as err:
            raise HomeAssistantError(
                f"Pull blocked by pre-deploy check: {'; '.join(err.errors)}"
            ) from err
        except GitError as err:
            raise HomeAssistantError(
                f"Git {self.entity_description.operation} failed: {err}"
            ) from err

    @property
    def available(self) -> bool:
        """Return whether the configured operation can currently run."""
        if self.entity_description.operation != "discard_changes":
            return super().available

        data = self.coordinator.data
        return bool(
            super().available
            and data is not None
            and data.total_commits > 0
            and (data.changed_files or data.staged_files)
        )
