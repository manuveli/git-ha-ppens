"""Sensor platform for git-ha-ppens."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GitHaPpensCoordinator
from .git_manager import GitStatus


@dataclass(frozen=True, kw_only=True)
class GitHaPpensSensorDescription(SensorEntityDescription):
    """Describes a git-ha-ppens sensor."""

    value_fn: Callable[[GitStatus], str | int | datetime | None] | None = None
    coordinator_value_fn: Callable[[GitHaPpensCoordinator], str | int | datetime | None] | None = None
    extra_attrs_fn: Callable[[GitStatus], dict] | None = None


SENSOR_DESCRIPTIONS: tuple[GitHaPpensSensorDescription, ...] = (
    GitHaPpensSensorDescription(
        key="last_commit",
        translation_key="last_commit",
        icon="mdi:source-commit",
        value_fn=lambda s: s.last_commit_hash_short or "no commits",
        extra_attrs_fn=lambda s: {
            "message": s.last_commit_message,
            "author": s.last_commit_author,
            "full_hash": s.last_commit_hash,
        },
    ),
    GitHaPpensSensorDescription(
        key="last_commit_time",
        translation_key="last_commit_time",
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.last_commit_time,
    ),
    GitHaPpensSensorDescription(
        key="uncommitted_changes",
        translation_key="uncommitted_changes",
        icon="mdi:file-edit-outline",
        native_unit_of_measurement="files",
        value_fn=lambda s: len(s.changed_files) + len(s.untracked_files) + len(s.staged_files),
        extra_attrs_fn=lambda s: {
            "changed_files": s.changed_files,
            "untracked_files": s.untracked_files,
            "staged_files": s.staged_files,
        },
    ),
    GitHaPpensSensorDescription(
        key="branch",
        translation_key="branch",
        icon="mdi:source-branch",
        value_fn=lambda s: s.branch,
    ),
    GitHaPpensSensorDescription(
        key="remote_status",
        translation_key="remote_status",
        icon="mdi:cloud-sync-outline",
        value_fn=lambda s: _format_remote_status(s),
        extra_attrs_fn=lambda s: {
            "ahead": s.ahead,
            "behind": s.behind,
            "remote_configured": s.remote_configured,
            "has_upstream": s.has_upstream,
            "total_commits": s.total_commits,
        },
    ),
    GitHaPpensSensorDescription(
        key="last_fetch_time",
        translation_key="last_fetch_time",
        icon="mdi:cloud-download-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        coordinator_value_fn=lambda c: c.last_fetch_time,
    ),
    GitHaPpensSensorDescription(
        key="last_pull_time",
        translation_key="last_pull_time",
        icon="mdi:source-pull",
        device_class=SensorDeviceClass.TIMESTAMP,
        coordinator_value_fn=lambda c: c.last_pull_time,
    ),
    GitHaPpensSensorDescription(
        key="last_push_time",
        translation_key="last_push_time",
        icon="mdi:source-merge",
        device_class=SensorDeviceClass.TIMESTAMP,
        coordinator_value_fn=lambda c: c.last_push_time,
    ),
    GitHaPpensSensorDescription(
        key="commits_behind",
        translation_key="commits_behind",
        icon="mdi:arrow-down-bold",
        native_unit_of_measurement="commits",
        value_fn=lambda s: max(s.behind, 0),
    ),
    GitHaPpensSensorDescription(
        key="commits_ahead",
        translation_key="commits_ahead",
        icon="mdi:arrow-up-bold",
        native_unit_of_measurement="commits",
        value_fn=lambda s: max(s.ahead, 0),
    ),
)


def _format_remote_status(status: GitStatus) -> str:
    """Format the remote status as a human-readable string."""
    if not status.remote_configured:
        return "no remote"

    if status.total_commits == 0:
        return "not pushed"

    # No upstream tracking branch = never successfully pushed
    if not status.has_upstream:
        return "not pushed"

    parts: list[str] = []
    if status.ahead > 0:
        parts.append(f"ahead {status.ahead}")
    if status.behind > 0:
        parts.append(f"behind {status.behind}")

    return ", ".join(parts) if parts else "in sync"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up git-ha-ppens sensors from a config entry."""
    coordinator: GitHaPpensCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async_add_entities(
        GitHaPpensSensor(coordinator, description, entry)
        for description in SENSOR_DESCRIPTIONS
    )


class GitHaPpensSensor(
    CoordinatorEntity[GitHaPpensCoordinator], SensorEntity
):
    """Represents a git-ha-ppens sensor."""

    entity_description: GitHaPpensSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GitHaPpensCoordinator,
        description: GitHaPpensSensorDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "git-ha-ppens",
            "manufacturer": "git-ha-ppens",
            "model": "Git Version Control",
            "sw_version": "0.6.3",
            "entry_type": "service",
            "configuration_url": "https://github.com/manuveli/git-ha-ppens",
        }

    @property
    def native_value(self) -> str | int | datetime | None:
        """Return the sensor value."""
        if self.entity_description.coordinator_value_fn is not None:
            return self.entity_description.coordinator_value_fn(self.coordinator)
        if self.coordinator.data is None:
            return None
        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(self.coordinator.data)
        return None

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return extra state attributes."""
        if (
            self.coordinator.data is None
            or self.entity_description.extra_attrs_fn is None
        ):
            return None
        return self.entity_description.extra_attrs_fn(self.coordinator.data)
