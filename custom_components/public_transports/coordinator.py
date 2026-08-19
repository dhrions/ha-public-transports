"""DataUpdateCoordinator for Public Transports."""

from __future__ import annotations

import logging
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException
from siri_lite.models import MonitoredCall
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
    """Fetch the raw next passages for ONE stop code, via siri-lite.

    Deliberately unfiltered: a config entry may expose several sensors (both senses),
    each filtering this shared raw feed on its own line/direction. One coordinator is
    created per distinct stop code (a "both senses" CTS entry has two codes, so two
    coordinators; a PRIM entry has one code shared by both direction sensors).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, stop_code: str) -> None:
        """Initialize the coordinator for one stop code."""
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}:{stop_code}", update_interval=DEFAULT_SCAN_INTERVAL)
        self.entry = entry
        self.stop_code = stop_code

        transit_info = TRANSIT_COMPANIES[entry.data["transit_company"]]
        self.siri_client = build_siri_client(
            transit_info, entry.data.get("api_token"), stop_code
        )

    async def _async_update_data(self) -> list[MonitoredCall]:
        """Fetch the raw next calls for this stop code (filtering happens in the sensor)."""
        try:
            return await self.hass.async_add_executor_job(
                self.siri_client.fetch_next_calls
            )
        except RequestException as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
