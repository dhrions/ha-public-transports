"""Tests for the PublicTransportsDataUpdateCoordinator."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests.exceptions import RequestException
from siri_lite.models import MonitoredCall

from custom_components.public_transports.const import (
    CONF_ACTIVE_END,
    CONF_ACTIVE_START,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    TRANSIT_COMPANIES,
)
from custom_components.public_transports.coordinator import (
    PublicTransportsDataUpdateCoordinator,
    build_siri_client,
    call_matches,
    entry_scan_interval,
    scalar,
    spec_stop_codes,
    within_active_window,
)

ENTRY_DATA = {
    "city": "Strasbourg",
    "transit_company": "Compagnie des Transports Strasbourgeois",
    "api_token": "fake-token",
    "stop_name": "Homme de Fer",
    "stop_code": "43A",
    # Fenêtre dégénérée (début == fin) = toujours active. Sans cela, les tests qui
    # appellent _async_update_data dépendraient de l'heure à laquelle la suite tourne :
    # hors 07:00-20:00 le coordinateur saute l'appel API et ils échoueraient la nuit.
    CONF_ACTIVE_START: "00:00",
    CONF_ACTIVE_END: "00:00",
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


# --- Fréquence de sondage et plage active (protection du quota API) -------------------


def test_entry_scan_interval_defaults_when_unset():
    """A pre-existing entry, created before the setting, keeps the default."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    assert entry_scan_interval(entry) == DEFAULT_SCAN_INTERVAL


def test_entry_scan_interval_reads_options_over_data():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA, CONF_SCAN_INTERVAL: 2},
        options={CONF_SCAN_INTERVAL: 10},
    )
    assert entry_scan_interval(entry).total_seconds() == 10 * 60


@pytest.mark.parametrize(
    ("stored", "expected_minutes"),
    [
        (0, MIN_SCAN_INTERVAL_MINUTES),
        (-5, MIN_SCAN_INTERVAL_MINUTES),
        (9999, MAX_SCAN_INTERVAL_MINUTES),
    ],
)
def test_entry_scan_interval_clamps_out_of_range(stored, expected_minutes):
    """A hand-edited .storage value must not be able to hammer the API."""
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, CONF_SCAN_INTERVAL: stored})
    assert entry_scan_interval(entry).total_seconds() == expected_minutes * 60


def test_entry_scan_interval_falls_back_on_unparseable_value():
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, CONF_SCAN_INTERVAL: "abc"})
    assert entry_scan_interval(entry) == DEFAULT_SCAN_INTERVAL


def _entry_with_window(start, end):
    return MockConfigEntry(
        domain=DOMAIN, data={**ENTRY_DATA, CONF_ACTIVE_START: start, CONF_ACTIVE_END: end}
    )


@pytest.mark.parametrize(
    ("hour", "inside"),
    [(6, False), (7, True), (12, True), (19, True), (20, False), (23, False)],
)
def test_within_active_window_daytime_range(hour, inside):
    """End is exclusive: 20:00 with a 07:00-20:00 window is already outside."""
    entry = _entry_with_window("07:00", "20:00")
    now = datetime(2026, 8, 20, hour, 0, tzinfo=timezone.utc)
    assert within_active_window(entry, now) is inside


@pytest.mark.parametrize(
    ("hour", "inside"),
    [(21, False), (22, True), (23, True), (0, True), (5, True), (6, False)],
)
def test_within_active_window_spans_midnight(hour, inside):
    """An end earlier than the start reads as wrapping past midnight."""
    entry = _entry_with_window("22:00", "06:00")
    now = datetime(2026, 8, 20, hour, 0, tzinfo=timezone.utc)
    assert within_active_window(entry, now) is inside


def test_within_active_window_equal_bounds_is_always_active():
    """start == end is the opt-out: poll around the clock."""
    entry = _entry_with_window("00:00", "00:00")
    assert within_active_window(entry, datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)) is True


def test_within_active_window_falls_back_on_garbage_bounds():
    """Unparseable bounds must not disable polling — they fall back to the default."""
    entry = _entry_with_window("nonsense", None)
    assert within_active_window(entry, datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)) is True
    assert within_active_window(entry, datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)) is False


async def test_update_data_skips_api_call_outside_active_window(hass):
    """The whole point: no HTTP call at all outside the window."""
    entry = _entry_with_window("07:00", "20:00")
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])
    coordinator.data = ["previous"]

    with (
        patch(
            "custom_components.public_transports.coordinator.dt_util.now",
            return_value=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc),
        ),
        patch.object(hass, "async_add_executor_job", new=AsyncMock()) as mock_executor,
    ):
        result = await coordinator._async_update_data()

    mock_executor.assert_not_awaited()
    assert result == ["previous"]


async def test_update_data_calls_api_inside_active_window(hass):
    entry = _entry_with_window("07:00", "20:00")
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])
    expected = [MonitoredCall(stop_point_name="Homme de Fer")]

    with (
        patch(
            "custom_components.public_transports.coordinator.dt_util.now",
            return_value=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        ),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(return_value=expected)),
    ):
        result = await coordinator._async_update_data()

    assert result == expected


def test_coordinator_uses_entry_interval(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, CONF_SCAN_INTERVAL: 5})
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])
    assert coordinator.update_interval.total_seconds() == 5 * 60
