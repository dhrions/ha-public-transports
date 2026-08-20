"""DataUpdateCoordinator for Public Transports."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException
from siri_lite.models import MonitoredCall
from siri_lite.siri_client import SiriClient

from .const import (
    CONF_ACTIVE_END,
    CONF_ACTIVE_START,
    CONF_SCAN_INTERVAL,
    DEFAULT_ACTIVE_END,
    DEFAULT_ACTIVE_START,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    TRANSIT_COMPANIES,
)

_LOGGER = logging.getLogger(__name__)


def scalar(value):
    """Normalize a SIRI field that may be a bare string, a {"value": ...} dict, or a
    list of either (observed: DestinationName as [{"value": "..."}]) into a plain string.

    Some producers (observed on IDFM/PRIM stop-monitoring responses, unlike CTS) wrap
    scalar fields like LineRef/DestinationName this way instead of a plain string.
    siri-lite's MonitoredCall exposes whatever shape the API returned, so any code
    comparing/hashing these fields (filtering, building option lists, display) needs this.
    """
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        return value.get("value")
    return value


def entry_scan_interval(entry: ConfigEntry) -> timedelta:
    """Return the polling period for this entry, options overriding data.

    Falls back to DEFAULT_SCAN_INTERVAL for entries created before the setting existed,
    and clamps to the bounds the options form enforces so a hand-edited .storage value
    can't drive the quota through the floor.
    """
    config = {**entry.data, **entry.options}
    try:
        minutes = int(config.get(CONF_SCAN_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL
    minutes = max(MIN_SCAN_INTERVAL_MINUTES, min(MAX_SCAN_INTERVAL_MINUTES, minutes))
    return timedelta(minutes=minutes)


def parse_hhmm(value, fallback: str) -> time:
    """Parse a "HH:MM" string into a time, falling back on anything unparseable."""
    try:
        hours, minutes = str(value).split(":")
        return time(int(hours), int(minutes))
    except (AttributeError, TypeError, ValueError):
        hours, minutes = fallback.split(":")
        return time(int(hours), int(minutes))


def within_active_window(entry: ConfigEntry, now: datetime) -> bool:
    """Whether `now` (local time) falls inside the entry's active polling window.

    A window whose end is earlier than its start is read as spanning midnight
    (e.g. 22:00 -> 06:00). Start == end means "always active", which is how an entry
    opts out of windowing entirely.
    """
    config = {**entry.data, **entry.options}
    start = parse_hhmm(config.get(CONF_ACTIVE_START), DEFAULT_ACTIVE_START)
    end = parse_hhmm(config.get(CONF_ACTIVE_END), DEFAULT_ACTIVE_END)
    if start == end:
        return True
    current = now.time()
    if start < end:
        return start <= current < end
    return current >= start or current < end


def spec_stop_codes(spec: dict) -> list[str]:
    """Return the physical stop codes a spec reads from, single or pole.

    A "pole" spec (several colocated PRIM stops merged into one sensor) carries
    stop_codes (list); every other spec carries the legacy singular stop_code.
    """
    if spec.get("stop_codes"):
        return spec["stop_codes"]
    if spec.get("stop_code"):
        return [spec["stop_code"]]
    return []


def entry_sense_specs(entry: ConfigEntry) -> list[dict]:
    """Return the list of sense specs an entry exposes — one sensor per spec.

    New shape: entry.data["senses"] is a list of specs, each with stop_code, line_filter,
    line_name, direction_filter, direction_label — one element for a single sense, two for
    a "both senses" entry. Legacy flat entries (pre-v3) are read transparently as a single
    spec so no data has to be rewritten to keep working.
    """
    config = {**entry.data, **entry.options}
    senses = config.get("senses")
    if senses:
        return senses
    return [{
        "stop_code": config.get("stop_code"),
        "line_filter": config.get("line_filter"),
        "line_name": config.get("line_name"),
        "direction_filter": config.get("direction_filter"),
        "direction_label": config.get("direction_label"),
    }]


def call_matches(call: MonitoredCall, line_filter, direction_filter) -> bool:
    """Whether a passage passes a spec's line/direction filter.

    Line matches line_ref or published_line_name (dropdown value vs manual entry).
    Direction matches the SIRI DirectionRef (Aller/Retour) — the real 2-way sense, not the
    per-vehicle terminus (which a forked line like metro 13 multiplies).
    """
    if line_filter and line_filter not in (scalar(call.line_ref), scalar(call.published_line_name)):
        return False
    if direction_filter and scalar(call.direction_ref) != direction_filter:
        return False
    return True


def build_siri_client(transit_info: dict, api_token: str | None, stop_code: str) -> SiriClient:
    """Build a SIRI client for one stop, from a transit company config.

    Shared by the coordinator (live sensor updates) and the config flow (probing the
    available lines/directions at setup), so both hit the exact same endpoint/auth and
    therefore see the same passages.
    """
    url = (
        transit_info["api_url"]
        + transit_info["stop_monitoring_endpoint"]
        + quote(stop_code, safe="")
    )

    headers = {}
    auth = None
    auth_type = transit_info.get("auth_type")
    if api_token:
        if auth_type == "Basic Auth":
            auth = HTTPBasicAuth(api_token, api_token)
        elif auth_type == "apiKey":
            headers["apiKey"] = api_token

    return SiriClient(url=url, headers=headers, auth=auth)


class PublicTransportsDataUpdateCoordinator(DataUpdateCoordinator[list[MonitoredCall]]):
    """Fetch the raw next passages for one or more stop codes, via siri-lite.

    Deliberately unfiltered: a config entry may expose several sensors (both senses),
    each filtering this shared raw feed on its own line/direction. One coordinator is
    created per distinct set of stop codes (a "both senses" CTS entry has two codes, so
    two coordinators; a PRIM entry has one code shared by both direction sensors; a
    "pole" PRIM entry merges several colocated codes into one coordinator).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, stop_codes: list[str]) -> None:
        """Initialize the coordinator for one or more stop codes."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{'+'.join(stop_codes)}",
            update_interval=entry_scan_interval(entry),
        )
        self.entry = entry
        self.stop_codes = stop_codes

        transit_info = TRANSIT_COMPANIES[entry.data["transit_company"]]
        api_token = entry.data.get("api_token")
        self.siri_clients = [
            build_siri_client(transit_info, api_token, stop_code) for stop_code in stop_codes
        ]

    async def _async_update_data(self) -> list[MonitoredCall]:
        """Fetch the raw next calls for every stop code and merge them.

        Merged calls are sorted by expected_arrival_time so downstream code (sensor's
        _calls[0] = next passage) keeps working unchanged whether reading one stop code
        or several. Assumes a consistent timezone offset format across a pole's codes,
        which holds for a single transit company's SIRI feed.

        Outside the configured active window, no API call is issued at all: the last
        known data is returned as-is. Skipping the call rather than stopping the timer
        keeps the coordinator's lifecycle untouched, and the quota is what we protect.
        """
        if not within_active_window(self.entry, dt_util.now()):
            _LOGGER.debug(
                "%s: hors plage active, aucun appel API (dernière donnée conservée)", self.name
            )
            return self.data or []

        try:
            results = await asyncio.gather(*[
                self.hass.async_add_executor_job(client.fetch_next_calls)
                for client in self.siri_clients
            ])
        except RequestException as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        calls = [call for client_calls in results for call in client_calls]
        calls.sort(key=lambda call: call.expected_arrival_time or "")
        return calls
