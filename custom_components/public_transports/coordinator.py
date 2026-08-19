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
    """Fetch next passages for one configured stop, via siri-lite (sync client, executor-dispatched)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=DEFAULT_SCAN_INTERVAL)
        self.entry = entry

        transit_info = TRANSIT_COMPANIES[entry.data["transit_company"]]
        self.siri_client = build_siri_client(
            transit_info, entry.data.get("api_token"), entry.data["stop_code"]
        )

        # Options prennent le pas sur data (filtre ligne/sens modifiable après coup).
        config = {**entry.data, **entry.options}
        self.line_filter = config.get("line_filter")
        self.direction_filter = config.get("direction_filter")

    def _matches_filter(self, call: MonitoredCall) -> bool:
        """Return True if the call passes the configured line/direction filters.

        The line filter matches against either line_ref (value chosen from the dropdown)
        or published_line_name (value typed in the manual fallback). The direction filter
        is a case-insensitive substring of the destination name, so both an exact terminus
        picked from the list and a hand-typed fragment work.
        """
        if self.line_filter:
            if self.line_filter not in (scalar(call.line_ref), scalar(call.published_line_name)):
                return False
        if self.direction_filter:
            destination = (scalar(call.destination_name) or "").casefold()
            if self.direction_filter.casefold() not in destination:
                return False
        return True

    async def _async_update_data(self) -> list[MonitoredCall]:
        """Fetch the next calls for the configured stop, filtered by line/direction."""
        try:
            calls = await self.hass.async_add_executor_job(
                self.siri_client.fetch_next_calls
            )
        except RequestException as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        return [call for call in calls if self._matches_filter(call)]
