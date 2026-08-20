"""Test component setup."""
from unittest.mock import patch

from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.public_transports.const import DOMAIN

ENTRY_DATA = {
    "city": "Strasbourg",
    "transit_company": "Compagnie des Transports Strasbourgeois",
    "api_token": "fake-token",
    "stop_name": "Homme de Fer",
    "stop_code": "43A",
}


async def test_async_setup(hass):
    """Test the component gets setup."""
    assert await async_setup_component(hass, DOMAIN, {}) is True


async def test_async_setup_entry_creates_coordinator_and_forwards_sensor_platform(hass):
    """Setting up an entry must create a coordinator and forward to the sensor platform."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.entry_id in hass.data[DOMAIN]
    assert hass.states.get("sensor.homme_de_fer_prochain_passage") is not None


async def test_async_unload_entry(hass):
    """Unloading an entry must remove it from hass.data and unload the sensor platform."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.entry_id not in hass.data[DOMAIN]
    state = hass.states.get("sensor.homme_de_fer_prochain_passage")
    assert state is not None
    assert state.state == "unavailable"


async def test_options_update_reloads_entry(hass):
    """Changing an entry's options must reload it (new filters take effect)."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        hass.config_entries.async_update_entry(entry, options={"line_filter": "A"})
        await hass.async_block_till_done()

    assert entry.entry_id in hass.data[DOMAIN]
    assert hass.states.get("sensor.homme_de_fer_prochain_passage") is not None
