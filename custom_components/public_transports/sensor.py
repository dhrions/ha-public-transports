"""Sensor platform for Public Transports."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from siri_lite.models import MonitoredCall

from .const import DOMAIN
from .coordinator import (
    PublicTransportsDataUpdateCoordinator,
    call_matches,
    entry_sense_specs,
    quota_key,
    scalar,
    spec_stop_codes,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up one sensor per sense spec of the entry (two for a "both senses" entry).

    Also creates the quota sensor for this entry's (company, endpoint) — but only the
    first time that key is seen: several entries sharing one PRIM token would otherwise
    each add a near-duplicate sensor reading the same producer-side counter. Tracked in
    hass.data (not per-entry) since the quota is shared across entries, not owned by one.
    """
    coordinators = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for index, spec in enumerate(entry_sense_specs(entry)):
        coordinator = coordinators.get(tuple(spec_stop_codes(spec)))
        if coordinator is not None:
            entities.append(PublicTransportsSensor(coordinator, entry, spec, index))

    key = quota_key(entry.data["transit_company"])
    keys_with_entity = hass.data[DOMAIN].setdefault("_quota_keys_with_entity", set())
    if coordinators and key not in keys_with_entity:
        entities.append(PublicTransportsQuotaSensor(hass, key, entry.data["transit_company"]))
        keys_with_entity.add(key)
        hass.data[DOMAIN].setdefault("_quota_owner_entry", {})[key] = entry.entry_id

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
            "stop_codes": spec_stop_codes(self._spec),
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


class PublicTransportsQuotaSensor(SensorEntity):
    """Daily API quota shared by every entry polling the same (company, endpoint) — cf.
    quota_key — plus this integration's own share of it across all of them.

    Reads hass.data[DOMAIN]["_quota_coordinators"][key] live on every poll rather than a
    fixed list captured at construction: that list is a shared, cross-entry registry that
    keeps growing/shrinking as sibling entries (sharing the same token/company) set up or
    unload, and this sensor must reflect all of them, not just the entry that happened to
    create it. should_poll=True (default interval) instead of per-coordinator listeners,
    since the underlying reads are cheap in-memory dict/list lookups, not network calls —
    sidesteps having to re-subscribe listeners whenever a sibling entry joins or leaves.
    """

    _attr_icon = "mdi:gauge"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, key: str, transit_company: str) -> None:
        """Initialize the quota sensor for one (company, endpoint) key."""
        self._hass = hass
        self._key = key
        self._attr_unique_id = f"quota_{key}"
        self._attr_name = f"{transit_company} - quota API"

    @property
    def _coordinators(self) -> list[PublicTransportsDataUpdateCoordinator]:
        """Every coordinator currently sharing this quota key, across all entries."""
        return self._hass.data[DOMAIN].get("_quota_coordinators", {}).get(self._key, [])

    @property
    def _rate_limits(self):
        """Latest RateLimitInfo of each coordinator that has one, non-empty."""
        return [c.last_rate_limit for c in self._coordinators if c.last_rate_limit]

    @property
    def available(self) -> bool:
        """Only meaningful once the provider has reported a quota at least once.

        The own-calls counter (own_calls_today) stays available even without provider
        headers (ex. CTS), but it's not useful shown alone — hide the sensor entirely
        rather than show an empty gauge.
        """
        return bool(self._rate_limits)

    @property
    def native_value(self):
        """Remaining daily quota — the most conservative reading across coordinators."""
        limits = self._rate_limits
        if not limits:
            return None
        remaining = [rl.remaining_day for rl in limits if rl.remaining_day is not None]
        return min(remaining) if remaining else None

    @property
    def extra_state_attributes(self):
        """Full quota picture: provider-reported limits plus this integration's own share."""
        limits = self._rate_limits
        limit_day = next((rl.limit_day for rl in limits if rl.limit_day is not None), None)
        remaining_second = next(
            (rl.remaining_second for rl in limits if rl.remaining_second is not None), None
        )
        limit_second = next((rl.limit_second for rl in limits if rl.limit_second is not None), None)
        return {
            "limit_day": limit_day,
            "remaining_second": remaining_second,
            "limit_second": limit_second,
            # Consommation de CETTE intégration seule — le quota ci-dessus est partagé
            # par tout ce qui utilise le même token (autres apps, siri-lite CLI...).
            "own_calls_today": sum(c.call_count_today for c in self._coordinators),
        }
