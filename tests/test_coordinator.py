"""Tests for the PublicTransportsDataUpdateCoordinator."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests.exceptions import RequestException
from siri_lite.models import MonitoredCall

from custom_components.public_transports.const import DOMAIN, TRANSIT_COMPANIES
from custom_components.public_transports.coordinator import (
    PublicTransportsDataUpdateCoordinator,
    build_siri_client,
    call_matches,
    scalar,
    spec_stop_codes,
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
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])

    expected_calls = [MonitoredCall(stop_point_name="Homme de Fer", expected_arrival_time="2099-01-01T00:05:00+00:00")]

    with patch.object(
        hass, "async_add_executor_job", new=AsyncMock(return_value=expected_calls)
    ) as mock_executor:
        result = await coordinator._async_update_data()

    mock_executor.assert_awaited_once_with(coordinator.siri_clients[0].fetch_next_calls)
    assert result == expected_calls


async def test_update_data_raises_update_failed_on_request_error(hass):
    """A RequestException from the sync client must become UpdateFailed."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])

    with patch.object(
        coordinator.siri_clients[0], "fetch_next_calls", side_effect=RequestException("boom")
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


def test_init_builds_url_and_basic_auth_from_entry_data(hass):
    """URL and auth must be built from stop_code/api_token, token used as both user and password."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])

    assert coordinator.siri_clients[0].api_request.url == (
        "https://api.cts-strasbourg.eu/v1/siri/2.0/stop-monitoring?MonitoringRef=43A"
    )
    auth = coordinator.siri_clients[0].api_request.auth
    assert auth.username == "fake-token"
    assert auth.password == "fake-token"


async def test_update_data_merges_and_sorts_multiple_stop_codes(hass):
    """A pole coordinator fetches every stop code and returns one time-sorted list."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A", "43B"])

    later = MonitoredCall(stop_point_name="A", expected_arrival_time="2099-01-01T00:10:00+00:00")
    sooner = MonitoredCall(stop_point_name="B", expected_arrival_time="2099-01-01T00:03:00+00:00")

    def fake_fetch(client):
        return [later] if client is coordinator.siri_clients[0] else [sooner]

    with patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=lambda fn: fake_fetch(fn.__self__))):
        result = await coordinator._async_update_data()

    assert result == [sooner, later]


def test_spec_stop_codes_prefers_pole_list_over_legacy_single():
    assert spec_stop_codes({"stop_codes": ["A", "B"], "stop_code": "A"}) == ["A", "B"]


def test_spec_stop_codes_falls_back_to_legacy_single():
    assert spec_stop_codes({"stop_code": "A"}) == ["A"]


def test_spec_stop_codes_empty_when_neither_present():
    assert spec_stop_codes({}) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("13", "13"),
        (None, None),
        ({"value": "13"}, "13"),
        ([{"value": "13"}], "13"),
        ([], None),
    ],
)
def test_scalar_normalizes_all_siri_shapes(value, expected):
    """PRIM wraps scalars as {"value": ...} or [{"value": ...}]; CTS doesn't."""
    assert scalar(value) == expected


def test_call_matches_rejects_wrong_line():
    """A line_filter that matches neither line_ref nor published_line_name must reject."""
    call = MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller")
    assert call_matches(call, line_filter="99", direction_filter=None) is False


def test_call_matches_rejects_wrong_direction():
    """A direction_filter that doesn't match direction_ref must reject."""
    call = MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller")
    assert call_matches(call, line_filter=None, direction_filter="Retour") is False


def test_call_matches_accepts_matching_line_and_direction():
    call = MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller")
    assert call_matches(call, line_filter="13", direction_filter="Aller") is True


def test_build_siri_client_uses_apikey_header_for_prim():
    """PRIM auth is a plain apiKey header, not Basic Auth."""
    transit_info = TRANSIT_COMPANIES["IDF Mobilités / RATP"]
    client = build_siri_client(transit_info, "fake-prim-key", "STIF:StopArea:SP:45102:")

    assert client.api_request.auth is None
    assert client.api_request.headers.get("apiKey") == "fake-prim-key"
