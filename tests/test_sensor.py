"""Tests for the Public Transports sensor entity."""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from siri_lite.models import MonitoredCall

from custom_components.public_transports.const import DOMAIN

ENTRY_DATA = {
    "city": "Strasbourg",
    "transit_company": "Compagnie des Transports Strasbourgeois",
    "api_token": "fake-token",
    "stop_name": "Homme de Fer",
    "stop_code": "43A",
}


async def test_sensor_state_and_attributes(hass):
    """State is the minutes to the next passage; attributes carry line/destination/next_passages."""
    calls = [
        MonitoredCall(
            stop_point_name="Homme de Fer",
            expected_arrival_time="2099-01-01T00:05:00+00:00",
            line_ref="A",
            published_line_name="Ligne A",
            destination_name="Illkirch",
        ),
        MonitoredCall(
            stop_point_name="Homme de Fer",
            expected_arrival_time="2099-01-01T00:15:00+00:00",
            line_ref="A",
            published_line_name="Ligne A",
            destination_name="Illkirch",
        ),
    ]

    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=calls,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.homme_de_fer_prochain_passage")
    assert state is not None
    assert state.attributes["line"] == "A"
    assert state.attributes["published_line_name"] == "Ligne A"
    assert state.attributes["destination"] == "Illkirch"
    assert len(state.attributes["next_passages"]) == 2


async def test_sensor_state_none_when_no_next_call(hass):
    """State is None when the coordinator returns no passage."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.homme_de_fer_prochain_passage")
    assert state is not None
    assert state.state == "unknown"
