"""Binary sensor platform for git-ha-ppens."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GitHaPpensCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up git-ha-ppens binary sensors from a config entry."""
    coordinator: GitHaPpensCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([GitHaPpensDirtySensor(coordinator, entry)])


class GitHaPpensDirtySensor(
    CoordinatorEntity[GitHaPpensCoordinator], BinarySensorEntity
):
    """Binary sensor indicating uncommitted changes in the repository."""

    _attr_has_entity_name = True
    _attr_translation_key = "dirty"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:source-branch-check"

    def __init__(
        self,
        coordinator: GitHaPpensCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_dirty"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "git-ha-ppens",
            "manufacturer": "git-ha-ppens",
            "model": "Git Version Control",
            "sw_version": "0.6.0",
            "entry_type": "service",
            "configuration_url": "https://github.com/manuveli/git-ha-ppens",
        }

    @property
    def is_on(self) -> bool | None:
        """Return True if the repository has uncommitted changes."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.dirty

    @property
    def icon(self) -> str:
        """Return icon based on state."""
        if self.is_on:
            return "mdi:source-branch-minus"
        return "mdi:source-branch-check"

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return None
        data = self.coordinator.data
        all_changes = data.changed_files + data.untracked_files + data.staged_files
        return {
            "changed_files": all_changes,
            "change_count": len(all_changes),
        }
