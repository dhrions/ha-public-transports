"""Sensor platform for Public Transports."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from siri_lite.models import MonitoredCall

from .const import DOMAIN
from .coordinator import PublicTransportsDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PublicTransportsSensor(coordinator, entry)])


class PublicTransportsSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing the remaining time before the next passage at a stop."""

    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:bus-clock"

    def __init__(
        self, coordinator: PublicTransportsDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_next_passage"
        self._attr_name = f"{entry.data['stop_name']} - prochain passage"

    @property
    def _next_call(self) -> MonitoredCall | None:
        calls = self.coordinator.data or []
        return calls[0] if calls else None

    @property
    def native_value(self):
        """Return the remaining time (minutes) before the next passage."""
        call = self._next_call
        if call is None:
            return None
        return call.extract_remaining_time_before_arrival(unit="minutes")

    @property
    def extra_state_attributes(self):
        """Return additional attributes about the next passages."""
        call = self._next_call
        if call is None:
            return {}
        calls = self.coordinator.data or []
        return {
            "line": call.line_ref,
            "published_line_name": call.published_line_name,
            "destination": call.destination_name,
            "next_passages": [
                c.extract_remaining_time_before_arrival(unit="minutes") for c in calls
            ],
        }
