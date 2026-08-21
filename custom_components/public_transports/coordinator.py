"""DataUpdateCoordinator for Public Transports."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import time, timedelta
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException
from siri_lite.models import MonitoredCall, RateLimitInfo
from siri_lite.siri_client import SiriClient

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, TRANSIT_COMPANIES

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


def entry_scan_interval(entry: ConfigEntry) -> timedelta:
    """Return the entry's configured refresh interval, falling back to the default.

    Global to the entry (not per-sensor) — sensors of the same entry share a coordinator
    when they read the same stop codes, so a per-sensor interval would either be ignored
    or force splitting coordinators and multiplying API calls.
    """
    config = {**entry.data, **entry.options}
    seconds = config.get("scan_interval")
    if not seconds:
        return DEFAULT_SCAN_INTERVAL
    return timedelta(seconds=seconds)


def entry_quiet_hours(entry: ConfigEntry) -> tuple[str, str] | None:
    """Return the entry's configured quiet-hours window (start, end) as "HH:MM:SS"
    strings, or None if unset. During this window the coordinator skips API calls
    entirely (ex. la nuit) instead of merely reducing frequency.
    """
    config = {**entry.data, **entry.options}
    start = config.get("quiet_hours_start")
    end = config.get("quiet_hours_end")
    if not start or not end:
        return None
    return start, end


def _quiet_duration_seconds(start: str | None, end: str | None) -> int:
    """Seconds/day covered by a quiet-hours window, handling midnight wraparound
    (ex. 22:00 -> 06:00). Equal start/end is treated as "no window" rather than 24h —
    an accidental identical pick shouldn't silently stop all polling.
    """
    if not start or not end:
        return 0
    start_t, end_t = time.fromisoformat(start), time.fromisoformat(end)
    if start_t == end_t:
        return 0
    start_s = start_t.hour * 3600 + start_t.minute * 60 + start_t.second
    end_s = end_t.hour * 3600 + end_t.minute * 60 + end_t.second
    return end_s - start_s if end_s > start_s else 86400 - start_s + end_s


def entry_stop_code_count(entry: ConfigEntry) -> int:
    """Total distinct stop codes polled per refresh cycle across the entry's coordinators.

    Mirrors the coordinator-creation grouping in __init__.py (one coordinator per distinct
    stop-code set) — needed to project how many HTTP calls a scan interval implies.
    """
    code_sets = {tuple(spec_stop_codes(spec)) for spec in entry_sense_specs(entry) if spec_stop_codes(spec)}
    return sum(len(codes) for codes in code_sets)


def estimate_daily_calls(
    scan_interval_seconds: int, code_count: int, quiet_start: str | None = None, quiet_end: str | None = None
) -> int:
    """Project how many API calls/day a scan interval implies for this entry.

    code_count is the number of distinct stop codes polled each cycle (summed across the
    entry's coordinators). Quiet hours, if set, shrink the active polling window.
    """
    if scan_interval_seconds <= 0 or code_count <= 0:
        return 0
    active_seconds = 86400 - _quiet_duration_seconds(quiet_start, quiet_end)
    return math.ceil(active_seconds / scan_interval_seconds) * code_count


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
            hass, _LOGGER, name=f"{DOMAIN}:{'+'.join(stop_codes)}", update_interval=entry_scan_interval(entry)
        )
        self.entry = entry
        self.stop_codes = stop_codes
        # Compteur des appels faits PAR CETTE intégration (pas le quota du producteur,
        # qui reflète tout ce qui partage le même token — cf. last_rate_limit). Remis à
        # zéro au changement de jour ; perdu à un redémarrage de HA (pas persisté), donc
        # sous-estime le compte réel après un redémarrage en cours de journée.
        self._call_count = 0
        self._call_count_date = None
        # Dernier quota rapporté par le producteur (siri-lite RateLimitInfo), ou None
        # avant le premier appel réussi ou si le producteur n'expose pas ces headers
        # (observé absent sur CTS, présent sur PRIM).
        self.last_rate_limit: RateLimitInfo | None = None

        transit_info = TRANSIT_COMPANIES[entry.data["transit_company"]]
        api_token = entry.data.get("api_token")
        self.siri_clients = [
            build_siri_client(transit_info, api_token, stop_code) for stop_code in stop_codes
        ]

    @property
    def call_count_today(self) -> int:
        """Number of API calls this coordinator has made today (local counter)."""
        return self._call_count if self._call_count_date == dt_util.now().date() else 0

    def _in_quiet_hours(self) -> bool:
        """Whether the entry's configured quiet-hours window covers the current time."""
        quiet = entry_quiet_hours(self.entry)
        if not quiet:
            return False
        start, end = time.fromisoformat(quiet[0]), time.fromisoformat(quiet[1])
        if start == end:
            return False
        now = dt_util.now().time()
        return start <= now < end if start < end else now >= start or now < end

    async def _async_update_data(self) -> list[MonitoredCall]:
        """Fetch the raw next calls for every stop code and merge them.

        Merged calls are sorted by expected_arrival_time so downstream code (sensor's
        _calls[0] = next passage) keeps working unchanged whether reading one stop code
        or several. Assumes a consistent timezone offset format across a pole's codes,
        which holds for a single transit company's SIRI feed.

        During a configured quiet-hours window, skips the HTTP calls entirely and
        re-serves the last known data — except on the very first refresh (self.data is
        still None), which always fetches so a sensor set up at 3am isn't stuck unknown.
        """
        if self.data is not None and self._in_quiet_hours():
            return self.data

        try:
            results = await asyncio.gather(*[
                self.hass.async_add_executor_job(client.fetch_next_calls)
                for client in self.siri_clients
            ])
        except RequestException as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        today = dt_util.now().date()
        if self._call_count_date != today:
            self._call_count_date = today
            self._call_count = 0
        self._call_count += len(self.siri_clients)

        rate_limits = [client.last_rate_limit for client in self.siri_clients if client.last_rate_limit]
        if rate_limits:
            # Le plus conservateur des lectures de ce cycle (même token partagé entre
            # les codes d'un pôle) plutôt que la dernière au hasard de l'ordre du gather.
            self.last_rate_limit = min(
                rate_limits,
                key=lambda rl: rl.remaining_day if rl.remaining_day is not None else float("inf"),
            )

        calls = [call for client_calls in results for call in client_calls]
        calls.sort(key=lambda call: call.expected_arrival_time or "")
        return calls
