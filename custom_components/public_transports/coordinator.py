"""DataUpdateCoordinator for Public Transports."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import unicodedata
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

from .const import (
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MAX_WALKING_TIME_MINUTES,
    MIN_SCAN_INTERVAL_SECONDS,
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


def spec_key(spec: dict) -> str:
    """Short stable digest of a spec's own filter (line/direction/destination) — the
    content-derived suffix of a sensor's unique_id.

    Sensor unique_ids used to be purely positional (index in entry.data["senses"]):
    editing the destination filter in Options can shrink/grow/reorder that list, which
    silently reassigned an existing history to a different sensor (ex. unchecking one
    terminus shifted every later spec's index down). Hashing the filter fields instead
    ties a sensor's identity to WHAT it tracks, not WHERE it sits in the list, so
    re-ordering or adding/removing sibling specs no longer moves anyone else's history.
    Truncated to 8 hex chars: not a security digest, just enough to avoid collisions
    across the handful of specs one entry ever has.
    """
    raw = "|".join(str(spec.get(field) or "") for field in ("line_filter", "direction_filter", "destination_filter"))
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


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
    """Return the entry's configured refresh interval (seconds), falling back to default.

    Global to the entry (not per-sensor) — sensors of the same entry share a coordinator
    when they read the same stop codes, so a per-sensor interval would either be ignored
    or force splitting coordinators and multiplying API calls.

    Clamped to [MIN, MAX]_SCAN_INTERVAL_SECONDS at read time, not only at form entry: a
    value hand-edited in .storage (bypassing the dropdown) can't drive the interval below
    1s and hammer the API. Unparseable values fall back to the default.
    """
    config = {**entry.data, **entry.options}
    raw = config.get("scan_interval")
    if not raw:
        return DEFAULT_SCAN_INTERVAL
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL
    seconds = max(MIN_SCAN_INTERVAL_SECONDS, min(MAX_SCAN_INTERVAL_SECONDS, seconds))
    return timedelta(seconds=seconds)


def entry_quiet_hours(entry: ConfigEntry) -> tuple[str, str]:
    """Return the entry's quiet-hours window (start, end) as "HH:MM:SS" strings.

    Falls back to the default night window (DEFAULT_QUIET_HOURS_*) for an entry that never
    set one, so a freshly created entry is protected before the user ever opens Options
    ("défaut sûr"). During this window the coordinator skips API calls entirely (ex. la
    nuit) instead of merely reducing frequency. To poll around the clock, set start == end
    in Options — _in_quiet_hours and _quiet_duration_seconds both read an equal-bounds
    window as "no window".
    """
    config = {**entry.data, **entry.options}
    start = config.get("quiet_hours_start")
    end = config.get("quiet_hours_end")
    if not start or not end:
        return DEFAULT_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_END
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


def call_is_reachable(call: MonitoredCall, min_remaining_seconds: int = 0, now=None) -> bool:
    """Whether a passage is still catchable: its expected arrival is at least
    min_remaining_seconds in the future.

    min_remaining_seconds=0 (default) just means "not already departed". siri-lite's
    extract_remaining_time_before_arrival clamps negatives to 0, so by the minutes value
    alone a stale passage whose arrival is in the past is indistinguishable from one
    arriving "now"; this recomputes the raw sign from expected_arrival_time to drop
    already-departed passages — notably the pre-quiet-hours cache re-served untouched all
    night (cf. _async_update_data), which would otherwise show every line stuck at "0 min"
    hours after the last real service.

    A positive min_remaining_seconds models the walking/travel time to reach the stop
    (cf. entry_walking_time): passages arriving sooner than that are dropped so the sensor
    surfaces the next departure the user can actually make, not one leaving as they set out.

    An absent or unparseable arrival time is kept (returns True) rather than silently
    dropped: better a possibly-stale passage than hiding one on a producer quirk.
    """
    arrival = dt_util.parse_datetime(call.expected_arrival_time or "")
    if arrival is None:
        return True
    if now is None:
        now = dt_util.utcnow()
    return (arrival - now).total_seconds() >= min_remaining_seconds


def entry_walking_time(entry: ConfigEntry) -> int:
    """Return the entry's walking time (minutes) to reach the stop, clamped to [0, MAX].

    Global to the entry (not per-sensor), like entry_scan_interval. 0 disables the filter.
    Clamped at read time — not only at form entry — so a value hand-edited in .storage
    (bypassing the number field) can't go negative or absurdly large. Unparseable values
    fall back to 0 (no filter).
    """
    config = {**entry.data, **entry.options}
    clamped = _clamp_walking_time(config.get("walking_time"))
    return clamped if clamped is not None else 0


def _clamp_walking_time(raw) -> int | None:
    """Parse/clamp a raw walking-time value to [0, MAX] minutes, or return the sentinel.

    Returns None (meaning "unset / no value") for a falsy or unparseable input, so a spec
    override can distinguish "inherit the entry default" (None) from an explicit 0 minutes.
    """
    if raw is None or raw == "":
        return None
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, min(MAX_WALKING_TIME_MINUTES, minutes))


def spec_walking_time(entry: ConfigEntry, spec: dict) -> int:
    """Effective walking time (minutes) for one sensor: the spec's own override if set,
    else the entry-level default (entry_walking_time).

    This is the cascade the user configures — an entry-wide default (which, for a pole
    entry, covers the whole correspondence zone), overridable per sensor (line × sense)
    because one platform/direction can be a few minutes closer or farther than another.
    """
    override = _clamp_walking_time(spec.get("walking_time"))
    return override if override is not None else entry_walking_time(entry)


def normalize_destination(name: str | None) -> str:
    """Fold a destination_name to a diacritics/case-insensitive comparison key.

    destination_name is free text set by the producer, not a stable code (unlike
    direction_ref) — a same real terminus can vary in accents/casing across passages
    ("Saint-Denis - Université" vs "St-Denis - Universite"). Comparing on this folded
    form makes destination_filter tolerant to that instead of silently matching nothing.
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return folded.strip().casefold()


def call_matches(call: MonitoredCall, line_filter, direction_filter, destination_filter=None) -> bool:
    """Whether a passage passes a spec's line/direction/destination filter.

    Line matches line_ref or published_line_name (dropdown value vs manual entry).
    Direction matches the SIRI DirectionRef (Aller/Retour) — the real 2-way sense, not the
    per-vehicle terminus (which a forked line like metro 13 multiplies).
    Destination (optional, finer than direction) matches destination_name — the per-vehicle
    terminus on a forked sense (ex. metro 13 nord: Asnières-Gennevilliers vs
    Saint-Denis Université) — compared normalize_destination-folded so a producer's
    accent/casing variants of the same real terminus still match.
    """
    if line_filter and line_filter not in (scalar(call.line_ref), scalar(call.published_line_name)):
        return False
    if direction_filter and scalar(call.direction_ref) != direction_filter:
        return False
    if destination_filter and normalize_destination(scalar(call.destination_name)) != normalize_destination(destination_filter):
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


def quota_key(transit_company: str) -> str:
    """Key identifying which entries share one daily API quota.

    The quota is enforced by the producer per (company, endpoint) — confirmed on PRIM,
    whose stop-monitoring quota is separate from its other endpoints — not per config
    entry. Several entries for the same company/stop-monitoring endpoint (ex. two PRIM
    stops on the same token) must therefore report the SAME quota sensor rather than each
    showing a near-duplicate reading of the same shared counter. Every TRANSIT_COMPANIES
    entry has exactly one stop_monitoring_endpoint today, so this reduces in practice to
    one key per company — modeled explicitly by endpoint so a second endpoint, if one is
    ever added, naturally gets its own quota key instead of silently sharing this one.
    """
    endpoint = TRANSIT_COMPANIES.get(transit_company, {}).get("stop_monitoring_endpoint", "")
    return f"{transit_company}::{endpoint}"


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
        """Whether the entry's quiet-hours window covers the current time.

        entry_quiet_hours always returns a window (the default night one when unset), so
        opting out is expressed by an equal-bounds window rather than by no window at all.
        """
        start_s, end_s = entry_quiet_hours(self.entry)
        start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
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
