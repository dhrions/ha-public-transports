"""Test component setup."""
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.public_transports import async_migrate_entry
from custom_components.public_transports.const import DOMAIN

from .conftest import BASE_ENTRY_DATA as ENTRY_DATA


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


async def test_unloading_either_stop_entry_keeps_the_shared_quota_sensor(hass):
    """The quota sensor is now owned by a dedicated hub entry, never by a stop entry —
    unloading a stop entry (either one, no "owner" distinction left to make) must not
    disturb it."""
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

        assert await hass.config_entries.async_unload(entry_a.entry_id)
        await hass.async_block_till_done()

    assert len(_quota_states(hass)) == 1


async def test_unloading_the_hub_itself_removes_the_quota_sensor(hass):
    """The hub is the sole owner of the quota sensor: unloading IT (not a stop entry)
    is what makes the sensor unavailable, unlike the old first-entry-wins ownership."""
    entry_a = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_A)
    entry_a.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()

        hub = next(
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get("kind") == "quota_hub"
        )
        assert await hass.config_entries.async_unload(hub.entry_id)
        await hass.async_block_till_done()

    states = _quota_states(hass)
    assert len(states) == 1
    assert states[0].state == "unavailable"


async def test_hub_entry_creates_no_coordinator(hass):
    """A quota-hub entry carries no stop: its own setup must create zero coordinators
    (no API call of its own), only the two diagnostic sensors."""
    hub = MockConfigEntry(
        domain=DOMAIN,
        data={"kind": "quota_hub", "transit_company": "IDF Mobilités / RATP"},
    )
    hub.add_to_hass(hass)

    assert await hass.config_entries.async_setup(hub.entry_id)
    await hass.async_block_till_done()

    assert hass.data[DOMAIN][hub.entry_id] == {}


async def test_second_stop_entry_of_the_same_company_does_not_duplicate_the_hub(hass):
    """Two stop entries sharing a company must provision exactly one hub, not two."""
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

    hubs = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("kind") == "quota_hub"]
    assert len(hubs) == 1


async def test_removing_the_last_stop_entry_removes_its_hub(hass):
    """Deleting the only stop entry of a company must cascade-remove its now-orphaned
    quota hub (async_remove_entry)."""
    entry = MockConfigEntry(domain=DOMAIN, data=PRIM_ENTRY_A)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    hubs = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("kind") == "quota_hub"]
    assert hubs == []


async def test_removing_one_of_several_stop_entries_keeps_the_hub(hass):
    """Deleting one stop entry while a sibling still shares the company must leave the
    hub in place."""
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

        assert await hass.config_entries.async_remove(entry_a.entry_id)
        await hass.async_block_till_done()

    hubs = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("kind") == "quota_hub"]
    assert len(hubs) == 1


async def test_migrate_entry_v1_drops_stale_direction_filter(hass):
    """v1 -> v2 : direction_filter/direction_label carried a terminus (DestinationName),
    incompatible with the DirectionRef-based filtering introduced in v2 — must be dropped
    rather than silently misinterpreted as a DirectionRef."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={**ENTRY_DATA, "direction_filter": "Vers Illkirch", "direction_label": "Vers Illkirch"},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert "direction_filter" not in entry.data
    assert "direction_label" not in entry.data
    assert entry.data["senses"] == [{
        "stop_code": "43A",
        "line_filter": None,
        "line_name": None,
        "direction_filter": None,
        "direction_label": None,
    }]


async def test_migrate_entry_v2_flattens_to_senses(hass):
    """v2 -> v3 : flat line_filter/stop_code fields become a one-element "senses" list —
    entry_sense_specs already reads both forms, but the migration must materialize it so
    no plat/ambiguous field survives."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={**ENTRY_DATA, "line_filter": "A", "line_name": "Ligne A"},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert "line_filter" not in entry.data
    assert "line_name" not in entry.data
    assert entry.data["senses"] == [{
        "stop_code": "43A",
        "line_filter": "A",
        "line_name": "Ligne A",
        "direction_filter": None,
        "direction_label": None,
    }]


async def test_migrate_entry_already_at_v3_is_a_noop(hass):
    """An entry already on the latest schema must not be rewritten."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={**ENTRY_DATA, "senses": [{"stop_code": "43A"}]},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert entry.data["senses"] == [{"stop_code": "43A"}]


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
