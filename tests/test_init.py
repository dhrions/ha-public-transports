"""Test component setup."""
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.public_transports.const import DOMAIN
from custom_components.public_transports.coordinator import quota_key

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


PRIM_ENTRY_A = {
    "city": "Paris",
    "transit_company": "IDF Mobilités / RATP",
    "api_token": "shared-token",
    "stop_name": "Gaîté",
    "stop_code": "STIF:StopArea:SP:45102:",
}
PRIM_ENTRY_B = {
    "city": "Paris",
    "transit_company": "IDF Mobilités / RATP",
    "api_token": "shared-token",
    "stop_name": "Jean Zay - Maine",
    "stop_code": "STIF:StopArea:SP:59070:",
}


def _quota_states(hass):
    return [s for s in hass.states.async_all() if s.entity_id.endswith("_quota_api")]


async def test_two_entries_sharing_a_company_get_one_shared_quota_sensor(hass):
    """Two PRIM stops on the same token must show ONE quota sensor, not a near-duplicate
    per entry reading the same producer-side counter twice (observed live 2026-08-22:
    Gaîté and Jean Zay each showed their own ~998410 reading of the identical quota).
    """
    entry_a = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_A)
    entry_b = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_B)
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        # The domain's first-ever entry setup bootstraps the whole component, which loads
        # every pending entry of that domain in one shot — a second explicit async_setup()
        # call for the other entry would then 500 on an already-loaded entry. One call
        # suffices to exercise both entries' async_setup_entry.
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()

    assert entry_b.state is ConfigEntryState.LOADED
    assert len(_quota_states(hass)) == 1


async def test_unloading_the_non_owning_sibling_keeps_the_shared_quota_sensor(hass):
    """Removing the entry that did NOT create the quota sensor must not disturb it.

    Which of the two entries ends up owning the shared entity depends on bootstrap
    processing order, not on which variable is named _a/_b — so the owner is looked up
    from the integration's own registry rather than assumed.
    """
    entry_a = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_A)
    entry_b = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_B)
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()

        key = quota_key("IDF Mobilités / RATP")
        owner_id = hass.data[DOMAIN]["_quota_owner_entry"][key]
        non_owner = entry_b if owner_id == entry_a.entry_id else entry_a

        assert await hass.config_entries.async_unload(non_owner.entry_id)
        await hass.async_block_till_done()

    assert len(_quota_states(hass)) == 1


async def test_unloading_the_owner_removes_the_quota_sensor_until_something_reloads(hass):
    """Documents the accepted limitation (cf. async_unload_entry docstring): removing the
    OWNING entry turns the shared quota sensor unavailable even if a sibling entry is
    still active, until some entry sharing the key reloads and reclaims ownership. HA
    keeps the entity registered (state -> "unavailable") rather than deleting it outright.
    """
    entry_a = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_A)
    entry_b = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_B)
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()

        key = quota_key("IDF Mobilités / RATP")
        owner_id = hass.data[DOMAIN]["_quota_owner_entry"][key]
        owner = entry_a if owner_id == entry_a.entry_id else entry_b

        assert await hass.config_entries.async_unload(owner.entry_id)
        await hass.async_block_till_done()

    states = _quota_states(hass)
    assert len(states) == 1
    assert states[0].state == "unavailable"


async def test_reloading_the_owning_entry_recreates_the_shared_quota_sensor(hass):
    """The entry that created the quota sensor gets reloaded (ex. an options change) —
    its own quota sensor must come back, not vanish permanently.
    """
    entry_a = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_A)
    entry_a.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()
        assert len(_quota_states(hass)) == 1

        hass.config_entries.async_update_entry(entry_a, options={"scan_interval": 120})
        await hass.async_block_till_done()

    assert len(_quota_states(hass)) == 1
