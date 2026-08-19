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
from .coordinator import PublicTransportsDataUpdateCoordinator, scalar

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
        self._attr_name = self._build_name(entry)

    @staticmethod
    def _build_name(entry: ConfigEntry) -> str:
        """Build a name that surfaces the line/direction filter when set.

        Ex. "Gaîté L13 → Châtillon Montrouge - prochain passage" when filtered,
        or "Gaîté - prochain passage" when tracking the whole stop.
        """
        config = {**entry.data, **entry.options}
        name = config["stop_name"]
        line_name = config.get("line_name")
        direction = config.get("direction_label")
        if line_name:
            name += f" {line_name}"
        if direction:
            name += f" → {direction}"
        return f"{name} - prochain passage"

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
            "stop_code": self._entry.data.get("stop_code"),
            "line": scalar(call.line_ref),
            "published_line_name": scalar(call.published_line_name),
            "destination": scalar(call.destination_name),
            "next_passages": [
                {
                    "time": c.extract_remaining_time_before_arrival(unit="minutes"),
                    "line": scalar(c.published_line_name) or scalar(c.line_ref),
                    "destination": scalar(c.destination_name),
                }
                for c in calls
            ],
        }
