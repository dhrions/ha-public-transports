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
from .coordinator import PublicTransportsDataUpdateCoordinator, call_matches, entry_sense_specs, scalar

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up one sensor per sense spec of the entry (two for a "both senses" entry)."""
    coordinators = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for index, spec in enumerate(entry_sense_specs(entry)):
        coordinator = coordinators.get(spec.get("stop_code"))
        if coordinator is not None:
            entities.append(PublicTransportsSensor(coordinator, entry, spec, index))
    async_add_entities(entities)


class PublicTransportsSensor(CoordinatorEntity, SensorEntity):
    """Remaining time before the next passage at a stop, for one line/direction sense."""

    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:bus-clock"

    def __init__(
        self,
        coordinator: PublicTransportsDataUpdateCoordinator,
        entry: ConfigEntry,
        spec: dict,
        index: int,
    ) -> None:
        """Initialize the sensor for one sense spec of the entry."""
        super().__init__(coordinator)
        self._entry = entry
        self._spec = spec
        # index 0 garde l'ancien unique_id historique pour ne pas orpheliner les entités
        # déjà enregistrées ; les capteurs supplémentaires (2ᵉ sens) sont suffixés.
        self._attr_unique_id = (
            f"{entry.entry_id}_next_passage" if index == 0
            else f"{entry.entry_id}_next_passage_{index}"
        )
        self._attr_name = self._build_name(entry, spec)

    @staticmethod
    def _build_name(entry: ConfigEntry, spec: dict) -> str:
        """Build a name that surfaces this sensor's line/direction, when set.

        Ex. "Gaîté 13 → Châtillon Montrouge - prochain passage" when filtered,
        or "Gaîté - prochain passage" when tracking the whole stop.
        """
        name = entry.data["stop_name"]
        line_name = spec.get("line_name")
        direction = spec.get("direction_label")
        if line_name:
            name += f" {line_name}"
        if direction:
            name += f" → {direction}"
        return f"{name} - prochain passage"

    @property
    def _calls(self) -> list[MonitoredCall]:
        """This sensor's slice of the shared raw feed (filtered by its own spec)."""
        raw = self.coordinator.data or []
        return [
            call for call in raw
            if call_matches(call, self._spec.get("line_filter"), self._spec.get("direction_filter"))
        ]

    @property
    def native_value(self):
        """Return the remaining time (minutes) before the next matching passage."""
        calls = self._calls
        if not calls:
            return None
        return calls[0].extract_remaining_time_before_arrival(unit="minutes")

    @property
    def extra_state_attributes(self):
        """Return additional attributes about the next passages."""
        calls = self._calls
        if not calls:
            return {}
        call = calls[0]
        times = [c.extract_remaining_time_before_arrival(unit="minutes") for c in calls]
        return {
            "stop_code": self._spec.get("stop_code"),
            "line": scalar(call.line_ref),
            "published_line_name": scalar(call.published_line_name),
            "destination": scalar(call.destination_name),
            # Liste plate des minutes des prochains passages, pratique sur un dashboard /
            # en template (next_times[1] = passage suivant) sans fouiller next_passages.
            "next_times": times,
            "next_passages": [
                {
                    "time": time,
                    "line": scalar(c.published_line_name) or scalar(c.line_ref),
                    "destination": scalar(c.destination_name),
                }
                for c, time in zip(calls, times)
            ],
        }
