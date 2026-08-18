"""Tests for the PublicTransportsDataUpdateCoordinator."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests.exceptions import RequestException
from siri_lite.models import MonitoredCall

from custom_components.public_transports.const import DOMAIN
from custom_components.public_transports.coordinator import (
    PublicTransportsDataUpdateCoordinator,
)

ENTRY_DATA = {
    "city": "Strasbourg",
    "transit_company": "Compagnie des Transports Strasbourgeois",
    "api_token": "fake-token",
    "stop_name": "Homme de Fer",
    "stop_code": "43A",
}


def _make_entry():
    return MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)


async def test_update_data_dispatches_via_executor_job(hass):
    """fetch_next_calls (sync/requests) must never be awaited directly."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry)

    expected_calls = [MonitoredCall(stop_point_name="Homme de Fer")]

    with patch.object(
        hass, "async_add_executor_job", new=AsyncMock(return_value=expected_calls)
    ) as mock_executor:
        result = await coordinator._async_update_data()

    mock_executor.assert_awaited_once_with(coordinator.siri_client.fetch_next_calls)
    assert result == expected_calls


async def test_update_data_raises_update_failed_on_request_error(hass):
    """A RequestException from the sync client must become UpdateFailed."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry)

    with patch.object(
        coordinator.siri_client, "fetch_next_calls", side_effect=RequestException("boom")
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


def test_init_builds_url_and_basic_auth_from_entry_data(hass):
    """URL and auth must be built from stop_code/api_token, token used as both user and password."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry)

    assert coordinator.siri_client.api_request.url == (
        "https://api.cts-strasbourg.eu/v1/siri/2.0/stop-monitoring?MonitoringRef=43A"
    )
    auth = coordinator.siri_client.api_request.auth
    assert auth.username == "fake-token"
    assert auth.password == "fake-token"
