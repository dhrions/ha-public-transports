"""DataUpdateCoordinator for Public Transports."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException
from siri_lite.models import MonitoredCall
from siri_lite.siri_client import SiriClient

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, TRANSIT_COMPANIES

_LOGGER = logging.getLogger(__name__)


class PublicTransportsDataUpdateCoordinator(DataUpdateCoordinator[list[MonitoredCall]]):
    """Fetch next passages for one configured stop, via siri-lite (sync client, executor-dispatched)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=DEFAULT_SCAN_INTERVAL)
        self.entry = entry

        transit_info = TRANSIT_COMPANIES[entry.data["transit_company"]]
        url = (
            transit_info["api_url"]
            + transit_info["stop_monitoring_endpoint"]
            + entry.data["stop_code"]
        )

        headers = {}
        auth = None
        api_token = entry.data.get("api_token")
        auth_type = transit_info.get("auth_type")
        if api_token:
            if auth_type == "Basic Auth":
                auth = HTTPBasicAuth(api_token, api_token)
            elif auth_type == "apiKey":
                headers["apiKey"] = api_token

        self.siri_client = SiriClient(url=url, headers=headers, auth=auth)

    async def _async_update_data(self) -> list[MonitoredCall]:
        """Fetch the next calls for the configured stop."""
        try:
            return await self.hass.async_add_executor_job(
                self.siri_client.fetch_next_calls
            )
        except RequestException as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
