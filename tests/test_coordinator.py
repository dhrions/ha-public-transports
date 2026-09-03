"""Tests for the PublicTransportsDataUpdateCoordinator."""

import math
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests.exceptions import RequestException
from siri_lite.models import MonitoredCall, RateLimitInfo

from datetime import datetime, timedelta, timezone

from custom_components.public_transports.const import (
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MAX_WALKING_TIME_MINUTES,
    MIN_SCAN_INTERVAL_SECONDS,
    TRANSIT_COMPANIES,
)
from custom_components.public_transports.coordinator import (
    PublicTransportsDataUpdateCoordinator,
    build_siri_client,
    call_is_reachable,
    call_matches,
    entry_quiet_hours,
    entry_scan_interval,
    entry_stop_code_count,
    entry_walking_time,
    estimate_daily_calls,
    scalar,
    spec_stop_codes,
    spec_walking_time,
)

from .conftest import BASE_ENTRY_DATA

ENTRY_DATA = {
    **BASE_ENTRY_DATA,
    # Créneau de silence dégénéré (début == fin) = jamais silencieux. Sans cela, les tests
    # qui appellent _async_update_data dépendraient de l'heure : le créneau nuit par défaut
    # (23:00-07:00) les ferait sauter l'appel API et échouer s'ils tournaient la nuit.
    "quiet_hours_start": "00:00:00",
    "quiet_hours_end": "00:00:00",
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


def test_call_is_reachable_rejects_departed_passage():
    """A passage whose arrival is in the past is already departed — must be dropped, not
    shown as "0 min" (the pre-quiet-hours cache re-served all night, cf. incident nocturne)."""
    now = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)
    call = MonitoredCall(expected_arrival_time="2026-09-01T22:50:00+00:00")
    assert call_is_reachable(call, now=now) is False


def test_call_is_reachable_accepts_future_passage():
    now = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)
    call = MonitoredCall(expected_arrival_time="2026-09-02T03:05:00+00:00")
    assert call_is_reachable(call, now=now) is True


def test_call_is_reachable_keeps_call_with_unparseable_arrival():
    """An absent/unparseable arrival time is kept rather than silently dropped on a
    producer quirk."""
    assert call_is_reachable(MonitoredCall(expected_arrival_time=None)) is True
    assert call_is_reachable(MonitoredCall(expected_arrival_time="not-a-date")) is True


def test_call_is_reachable_drops_passage_arriving_before_the_walking_margin():
    """A positive margin (walking time) drops a passage too close to catch, even though it
    is still in the future."""
    now = datetime(2026, 9, 2, 3, 0, tzinfo=timezone.utc)
    call = MonitoredCall(expected_arrival_time="2026-09-02T03:03:00+00:00")  # dans 3 min
    assert call_is_reachable(call, 5 * 60, now=now) is False  # 5 min de marche
    assert call_is_reachable(call, 2 * 60, now=now) is True   # 2 min de marche


def test_entry_scan_interval_falls_back_to_default_when_unset():
    entry = _make_entry()
    assert entry_scan_interval(entry) == DEFAULT_SCAN_INTERVAL


def test_entry_scan_interval_reads_from_options():
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={"scan_interval": 300})
    assert entry_scan_interval(entry) == timedelta(seconds=300)


def test_entry_scan_interval_reads_from_data_when_no_options():
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, "scan_interval": 120})
    assert entry_scan_interval(entry) == timedelta(seconds=120)


@pytest.mark.parametrize(
    ("stored", "expected_seconds"),
    [
        (0, None),  # 0 = falsy -> default, handled separately below
        (-5, MIN_SCAN_INTERVAL_SECONDS),
        (99999, MAX_SCAN_INTERVAL_SECONDS),
    ],
)
def test_entry_scan_interval_clamps_out_of_range(stored, expected_seconds):
    """A hand-edited .storage value (bypassing the dropdown) must not hammer the API."""
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, "scan_interval": stored})
    if expected_seconds is None:
        assert entry_scan_interval(entry) == DEFAULT_SCAN_INTERVAL
    else:
        assert entry_scan_interval(entry) == timedelta(seconds=expected_seconds)


def test_entry_scan_interval_falls_back_on_unparseable_value():
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, "scan_interval": "abc"})
    assert entry_scan_interval(entry) == DEFAULT_SCAN_INTERVAL


def test_entry_walking_time_defaults_to_zero_when_unset():
    assert entry_walking_time(_make_entry()) == 0


def test_entry_walking_time_reads_from_options():
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={"walking_time": 7})
    assert entry_walking_time(entry) == 7


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(-3, 0), (99999, MAX_WALKING_TIME_MINUTES), ("abc", 0), (None, 0)],
)
def test_entry_walking_time_clamps_or_defaults(stored, expected):
    """A hand-edited .storage value can't go negative or past the cap; garbage -> 0."""
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, "walking_time": stored})
    assert entry_walking_time(entry) == expected


def test_spec_walking_time_prefers_spec_override_over_entry_default():
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={"walking_time": 5})
    assert spec_walking_time(entry, {"walking_time": 12}) == 12


def test_spec_walking_time_falls_back_to_entry_default_when_no_override():
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={"walking_time": 5})
    assert spec_walking_time(entry, {"line_filter": "13"}) == 5


def test_spec_walking_time_keeps_explicit_zero_override():
    """An explicit 0 on a spec must override a non-zero entry default, not be read as
    "unset" (0 is falsy but a legitimate "no margin" choice)."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={"walking_time": 5})
    assert spec_walking_time(entry, {"walking_time": 0}) == 0


def test_build_siri_client_uses_apikey_header_for_prim():
    """PRIM auth is a plain apiKey header, not Basic Auth."""
    transit_info = TRANSIT_COMPANIES["IDF Mobilités / RATP"]
    client = build_siri_client(transit_info, "fake-prim-key", "STIF:StopArea:SP:45102:")

    assert client.api_request.auth is None
    assert client.api_request.headers.get("apiKey") == "fake-prim-key"


async def test_call_count_today_increments_per_successful_update(hass):
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A", "43B"])

    with patch.object(hass, "async_add_executor_job", new=AsyncMock(return_value=[])):
        assert coordinator.call_count_today == 0
        await coordinator._async_update_data()
        assert coordinator.call_count_today == 2
        await coordinator._async_update_data()
        assert coordinator.call_count_today == 4


async def test_call_count_today_not_incremented_on_failure(hass):
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])

    with patch.object(
        coordinator.siri_clients[0], "fetch_next_calls", side_effect=RequestException("boom")
    ):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.call_count_today == 0


async def test_last_rate_limit_updated_from_most_conservative_client(hass):
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A", "43B"])
    coordinator.siri_clients[0].last_rate_limit = None
    coordinator.siri_clients[1].last_rate_limit = None

    def fake_fetch(client):
        if client is coordinator.siri_clients[0]:
            client.last_rate_limit = RateLimitInfo(remaining_day=500)
        else:
            client.last_rate_limit = RateLimitInfo(remaining_day=100)
        return []

    with patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=lambda fn: fake_fetch(fn.__self__))):
        await coordinator._async_update_data()

    assert coordinator.last_rate_limit.remaining_day == 100


async def test_last_rate_limit_stays_none_when_no_client_reports_one(hass):
    entry = _make_entry()
    entry.add_to_hass(hass)
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])

    with patch.object(hass, "async_add_executor_job", new=AsyncMock(return_value=[])):
        await coordinator._async_update_data()

    assert coordinator.last_rate_limit is None


def test_estimate_daily_calls_no_quiet_hours():
    """1 call/second, 1 stop code -> 86400 calls/day."""
    assert estimate_daily_calls(1, 1) == 86400


def test_estimate_daily_calls_multiplies_by_code_count():
    """A pole of 3 stop codes multiplies the per-cycle cost."""
    assert estimate_daily_calls(60, 3) == 1440 * 3


def test_estimate_daily_calls_zero_when_no_codes_or_no_interval():
    assert estimate_daily_calls(60, 0) == 0
    assert estimate_daily_calls(0, 1) == 0


def test_estimate_daily_calls_shrinks_with_quiet_hours():
    """22:00 -> 06:00 removes 8h/day from the active polling window."""
    full_day = estimate_daily_calls(60, 1)
    with_quiet = estimate_daily_calls(60, 1, "22:00:00", "06:00:00")
    assert with_quiet < full_day
    assert with_quiet == math.ceil((86400 - 8 * 3600) / 60)


def test_estimate_daily_calls_quiet_hours_same_start_end_ignored():
    """An accidental identical start/end must not be read as a 24h blackout."""
    assert estimate_daily_calls(60, 1, "08:00:00", "08:00:00") == estimate_daily_calls(60, 1)


def test_entry_quiet_hours_defaults_when_unset():
    """A fresh entry (no quiet keys) is protected by the default night window."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={k: v for k, v in ENTRY_DATA.items() if not k.startswith("quiet_hours")},
    )
    assert entry_quiet_hours(entry) == (DEFAULT_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_END)


def test_entry_quiet_hours_defaults_when_only_one_side_set():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            k: v for k, v in ENTRY_DATA.items() if not k.startswith("quiet_hours")
        } | {"quiet_hours_start": "22:00:00"},
    )
    assert entry_quiet_hours(entry) == (DEFAULT_QUIET_HOURS_START, DEFAULT_QUIET_HOURS_END)


def test_entry_quiet_hours_reads_both_sides():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA, "quiet_hours_start": "22:00:00", "quiet_hours_end": "06:00:00"},
    )
    assert entry_quiet_hours(entry) == ("22:00:00", "06:00:00")


def test_entry_quiet_hours_equal_bounds_opt_out_is_preserved():
    """An explicit equal-bounds window (opt-out) must not be overridden by the default."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA, "quiet_hours_start": "00:00:00", "quiet_hours_end": "00:00:00"},
    )
    assert entry_quiet_hours(entry) == ("00:00:00", "00:00:00")


def test_entry_stop_code_count_sums_distinct_code_sets():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **ENTRY_DATA,
            "senses": [
                {"stop_code": "43A"},
                {"stop_code": "43B"},
            ],
        },
    )
    assert entry_stop_code_count(entry) == 2


def test_entry_stop_code_count_dedupes_shared_code_set():
    """Both-senses spec pair sharing the same single stop code counts it once."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **ENTRY_DATA,
            "senses": [
                {"stop_code": "43A", "direction_filter": "Aller"},
                {"stop_code": "43A", "direction_filter": "Retour"},
            ],
        },
    )
    assert entry_stop_code_count(entry) == 1


def _entry_with_quiet(hass, start, end):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA, "quiet_hours_start": start, "quiet_hours_end": end},
    )
    entry.add_to_hass(hass)
    return entry


async def test_update_data_skips_api_call_during_quiet_hours(hass):
    """Frozen at 03:00, inside a 23:00-07:00 window: no HTTP call, last data re-served."""
    entry = _entry_with_quiet(hass, "23:00:00", "07:00:00")
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])
    coordinator.data = [MonitoredCall(stop_point_name="cached")]

    with (
        patch(
            "custom_components.public_transports.coordinator.dt_util.now",
            return_value=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc),
        ),
        patch.object(hass, "async_add_executor_job", new=AsyncMock()) as mock_executor,
    ):
        result = await coordinator._async_update_data()

    mock_executor.assert_not_awaited()
    assert result == coordinator.data


async def test_update_data_calls_api_outside_quiet_hours(hass):
    """Frozen at noon, outside the 23:00-07:00 window: the API is called normally."""
    entry = _entry_with_quiet(hass, "23:00:00", "07:00:00")
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


async def test_update_data_fetches_on_first_refresh_even_during_quiet_hours(hass):
    """self.data is still None before the first successful refresh — must not skip blindly."""
    entry = _entry_with_quiet(hass, "23:00:00", "07:00:00")
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])
    assert coordinator.data is None

    with (
        patch(
            "custom_components.public_transports.coordinator.dt_util.now",
            return_value=datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc),
        ),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(return_value=[])) as mock_executor,
    ):
        await coordinator._async_update_data()

    mock_executor.assert_awaited_once()


def test_quiet_hours_are_time_of_day_only_no_weekday_restriction(hass):
    """Quiet hours check the time of day, never the weekday — a weekend behaves like any
    day. Locks the same intentional invariant the (dropped) active-window code had.
    """
    entry = _entry_with_quiet(hass, "23:00:00", "07:00:00")
    coordinator = PublicTransportsDataUpdateCoordinator(hass, entry, ["43A"])
    saturday_3am = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)
    saturday_noon = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    assert saturday_3am.weekday() == 5  # Saturday

    with patch(
        "custom_components.public_transports.coordinator.dt_util.now", return_value=saturday_3am
    ):
        assert coordinator._in_quiet_hours() is True  # 03:00 quiet, weekend included
    with patch(
        "custom_components.public_transports.coordinator.dt_util.now", return_value=saturday_noon
    ):
        assert coordinator._in_quiet_hours() is False  # noon active, weekend included
