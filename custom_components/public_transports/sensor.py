"""Sensor platform for Public Transports."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from siri_lite.models import MonitoredCall

from .const import DOMAIN
from .coordinator import (
    PublicTransportsDataUpdateCoordinator,
    call_is_reachable,
    call_matches,
    entry_sense_specs,
    quota_key,
    scalar,
    spec_stop_codes,
    spec_walking_time,
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
        entities.append(PublicTransportsCallsTodaySensor(hass, key, entry.data["transit_company"]))
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
        """This sensor's slice of the shared raw feed (filtered by its own spec).

        Also drops passages the user can't (or no longer can) catch, via call_is_reachable
        with the entry's walking time as margin:
        - margin 0 (default) drops only already-departed passages — the coordinator
          re-serves the pre-quiet-hours cache untouched all night, so without this every
          line would show a stale "0 min" hours after the last real service;
        - a positive walking time additionally drops passages arriving too soon to reach.
        Once nothing qualifies, this yields an empty list and the sensor reads None ("no
        catchable passage") rather than 0.
        """
        raw = self.coordinator.data or []
        margin = spec_walking_time(self._entry, self._spec) * 60
        return [
            call for call in raw
            if call_matches(call, self._spec.get("line_filter"), self._spec.get("direction_filter"))
            and call_is_reachable(call, margin)
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
    # MEASUREMENT (not TOTAL_INCREASING) : la valeur baisse au fil de la journée puis
    # remonte au reset quotidien du producteur, pas un compteur monotone. Sans state_class,
    # HA affiche l'historique en barres colorées catégorielles plutôt qu'un vrai graphique.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "requêtes"

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
        """Only meaningful once the provider has reported a quota at least once — hide the
        sensor entirely rather than show an empty gauge. PublicTransportsCallsTodaySensor,
        which only depends on the local counter, is available independently of this."""
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
        """Full quota picture: provider-reported limits.

        La consommation propre à cette intégration (own_calls_today) est désormais un
        capteur à part entière (PublicTransportsCallsTodaySensor) plutôt qu'un attribut
        ici : HA ne conserve pas d'historique par attribut, et l'exposer aux deux endroits
        entretenait la confusion (cliquer sur l'attribut rouvrait le graphique de CE
        capteur, pas un historique des appels).
        """
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
        }


class PublicTransportsCallsTodaySensor(SensorEntity):
    """Number of API calls made today by this integration, for one (company, endpoint) key.

    Shares the same coordinator registry as PublicTransportsQuotaSensor (cf. its docstring)
    but is a separate entity — not an attribute of the quota sensor — specifically so HA
    keeps a real state history for it (attributes have none) and a dashboard can plot a
    genuinely rising curve rather than reading the quota sensor's falling one in reverse.
    """

    _attr_icon = "mdi:counter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # TOTAL_INCREASING : compteur qui grimpe pendant la journée puis retombe à 0 au
    # changement de jour (cf. PublicTransportsDataUpdateCoordinator.call_count_today) — HA
    # traite nativement cette rechute comme un nouveau cycle plutôt que comme une anomalie.
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "requêtes"

    def __init__(self, hass: HomeAssistant, key: str, transit_company: str) -> None:
        """Initialize the calls-today sensor for one (company, endpoint) key."""
        self._hass = hass
        self._key = key
        self._attr_unique_id = f"calls_today_{key}"
        self._attr_name = f"{transit_company} - appels effectués aujourd'hui"

    @property
    def _coordinators(self) -> list[PublicTransportsDataUpdateCoordinator]:
        """Every coordinator currently sharing this quota key, across all entries."""
        return self._hass.data[DOMAIN].get("_quota_coordinators", {}).get(self._key, [])

    @property
    def available(self) -> bool:
        """Available as soon as a coordinator exists — no dependency on provider headers
        (unlike PublicTransportsQuotaSensor), since this counter is purely local."""
        return bool(self._coordinators)

    @property
    def native_value(self):
        """Total calls made today by this integration, across every coordinator sharing
        this key."""
        return sum(c.call_count_today for c in self._coordinators)
