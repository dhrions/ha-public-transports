"""Tests for the Public Transports sensor entity."""

from unittest.mock import Mock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from siri_lite.models import MonitoredCall, RateLimitInfo

from custom_components.public_transports.const import DOMAIN
from custom_components.public_transports.sensor import PublicTransportsQuotaSensor, PublicTransportsSensor

ENTRY_DATA = {
    "city": "Strasbourg",
    "transit_company": "Compagnie des Transports Strasbourgeois",
    "api_token": "fake-token",
    "stop_name": "Homme de Fer",
    "stop_code": "43A",
    # Créneau de silence dégénéré (début == fin) = jamais silencieux, pour que la mise en
    # place du capteur ne dépende pas de l'heure d'exécution (créneau nuit par défaut).
    "quiet_hours_start": "00:00:00",
    "quiet_hours_end": "00:00:00",
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


def test_build_name_includes_line_and_direction_when_filtered():
    """A filtered sense spec must surface its line/direction in the sensor name."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    spec = {"line_name": "13", "direction_label": "Châtillon Montrouge"}

    name = PublicTransportsSensor._build_name(entry, spec)

    assert name == "Homme de Fer 13 → Châtillon Montrouge - prochain passage"


def test_build_name_plain_when_unfiltered():
    """A spec with no line/direction filter must keep the bare stop name."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)

    name = PublicTransportsSensor._build_name(entry, {})

    assert name == "Homme de Fer - prochain passage"


QUOTA_KEY = "IDF Mobilités / RATP::/stop-monitoring?MonitoringRef="


def _fake_coordinator(rate_limit=None, call_count_today=0):
    coordinator = Mock()
    coordinator.last_rate_limit = rate_limit
    coordinator.call_count_today = call_count_today
    return coordinator


def _quota_sensor(hass, coordinators, key=QUOTA_KEY, transit_company="IDF Mobilités / RATP"):
    """Build a quota sensor whose live registry read (cf. sensor.py) sees `coordinators`."""
    hass.data.setdefault(DOMAIN, {}).setdefault("_quota_coordinators", {})[key] = coordinators
    return PublicTransportsQuotaSensor(hass, key, transit_company)


async def test_quota_sensor_unavailable_without_any_rate_limit(hass):
    """CTS doesn't expose rate-limit headers — the sensor must not fake a zero quota."""
    sensor = _quota_sensor(hass, [_fake_coordinator(rate_limit=None, call_count_today=3)])

    assert sensor.available is False
    assert sensor.native_value is None


async def test_quota_sensor_native_value_is_most_conservative_remaining_day(hass):
    """A pole/both-senses entry has several coordinators sharing one token — the sensor
    must report the lowest remaining_day seen, never an average or a random pick."""
    sensor = _quota_sensor(hass, [
        _fake_coordinator(rate_limit=RateLimitInfo(remaining_day=998100, limit_day=1000000)),
        _fake_coordinator(rate_limit=RateLimitInfo(remaining_day=998099, limit_day=1000000)),
    ])

    assert sensor.available is True
    assert sensor.native_value == 998099
    assert sensor.extra_state_attributes["limit_day"] == 1000000


async def test_quota_sensor_own_calls_today_sums_across_coordinators(hass):
    sensor = _quota_sensor(hass, [
        _fake_coordinator(rate_limit=RateLimitInfo(remaining_day=100), call_count_today=4),
        _fake_coordinator(rate_limit=RateLimitInfo(remaining_day=100), call_count_today=6),
    ])

    assert sensor.extra_state_attributes["own_calls_today"] == 10


async def test_quota_sensor_reflects_coordinators_added_by_a_sibling_entry_later(hass):
    """Two entries sharing the same (company, endpoint) key must show ONE shared quota —
    a second entry's coordinator, registered after this sensor was constructed, must still
    be picked up (live registry read, not a snapshot captured at construction).
    """
    sensor = _quota_sensor(hass, [_fake_coordinator(rate_limit=RateLimitInfo(remaining_day=500))])
    assert sensor.native_value == 500

    hass.data[DOMAIN]["_quota_coordinators"][QUOTA_KEY].append(
        _fake_coordinator(rate_limit=RateLimitInfo(remaining_day=100))
    )

    assert sensor.native_value == 100


async def test_quota_sensor_name_derives_from_transit_company(hass):
    sensor = _quota_sensor(hass, [_fake_coordinator()])

    assert sensor._attr_name == "IDF Mobilités / RATP - quota API"


async def test_quota_sensor_entity_category_is_the_enum_not_a_string(hass):
    """entity_registry.async_get_or_create rejects a plain "diagnostic" string with a
    hard ValueError — caught in production (2026-08-22) where it silently dropped the
    quota sensor on every setup, without this local test suite ever detecting it (a bare
    string didn't error against the Mock-based coordinators used above).

    Checked on an instance, not the class: Entity's __init_subclass__ wraps a class-level
    `_attr_entity_category = ...` assignment into a property descriptor, so comparing the
    class attribute directly no longer yields the enum value in recent HA core.
    """
    from homeassistant.const import EntityCategory

    sensor = _quota_sensor(hass, [_fake_coordinator()])

    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
