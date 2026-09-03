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
        """This sensor's slice of the shared raw feed (filtered by its own spec) — the full
        list of upcoming passages, backing the attributes (next_times / next_passages).

        Drops already-departed passages (call_is_reachable with the default margin 0): the
        coordinator re-serves the pre-quiet-hours cache untouched all night, so without this
        every line would show a stale "0 min" hours after the last real service. Once every
        passage is in the past this yields an empty list and the sensor reads None.

        The walking-time offset is deliberately NOT applied here — only to the state
        (native_value). A passage too imminent to catch stays listed in the attributes (so
        a dashboard can still surface "there's one in 3 min, but you'd miss it"); only which
        passage is the headline value changes.
        """
        raw = self.coordinator.data or []
        return [
            call for call in raw
            if call_matches(call, self._spec.get("line_filter"), self._spec.get("direction_filter"))
            and call_is_reachable(call)
        ]

    @property
    def _reachable_call(self) -> MonitoredCall | None:
        """The first upcoming passage the user can still catch — the one at least the
        walking time (spec_walking_time) ahead. Drives the state and its scalar attributes.
        None when no passage is far enough ahead to reach.
        """
        margin = spec_walking_time(self._entry, self._spec) * 60
        return next((c for c in self._calls if call_is_reachable(c, margin)), None)

    @property
    def native_value(self):
        """Return the minutes before the user must leave to catch the next reachable
        passage: that passage's remaining time minus the walking time (spec_walking_time).

        With the default walking time 0 this is just the real arrival minutes (backward
        compatible). A positive walking time turns the state into "leave in X min" — a
        passage 20 min away with an 8 min walk reads 12. Passages too soon to reach are
        skipped (cf. _reachable_call) but stay in the attributes. None when none is reachable.
        """
        call = self._reachable_call
        if call is None:
            return None
        remaining = call.extract_remaining_time_before_arrival(unit="minutes")
        return max(0, remaining - spec_walking_time(self._entry, self._spec))

    @property
    def extra_state_attributes(self):
        """Return additional attributes about the next passages.

        next_times / next_passages list the *real* arrival minutes of every upcoming
        passage (the objective timetable — the walking offset is not applied to them, so a
        template can still recompute anything). The scalar head fields (line/destination…)
        describe the passage the STATE refers to — the reachable one, not necessarily
        calls[0]: on a forked line, describing the imminent-but-unreachable passage next to
        a "leave in 12 min" state would show the wrong terminus. Fall back to calls[0] when
        nothing is reachable (state is None then, but we still describe what's coming).
        """
        calls = self._calls
        if not calls:
            return {}
        call = self._reachable_call or calls[0]
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


class _QuotaRegistryEntity:
    """Shared access to hass.data[DOMAIN]["_quota_coordinators"][key] — the live registry
    of coordinators feeding both the quota sensor and the calls-today sensor for a given
    (company, endpoint) key. Mixed into both, ahead of SensorEntity in the MRO."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        self._hass = hass
        self._key = key

    @property
    def _coordinators(self) -> list[PublicTransportsDataUpdateCoordinator]:
        """Every coordinator currently sharing this quota key, across all entries."""
        return self._hass.data[DOMAIN].get("_quota_coordinators", {}).get(self._key, [])


class PublicTransportsQuotaSensor(_QuotaRegistryEntity, SensorEntity):
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
        super().__init__(hass, key)
        self._attr_unique_id = f"quota_{key}"
        self._attr_name = f"{transit_company} - quota API"

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


class PublicTransportsCallsTodaySensor(_QuotaRegistryEntity, SensorEntity):
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
        super().__init__(hass, key)
        self._attr_unique_id = f"calls_today_{key}"
        self._attr_name = f"{transit_company} - appels effectués aujourd'hui"

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
