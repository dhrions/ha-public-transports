"""Tests for diagnostics.py."""
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from siri_lite.models import MonitoredCall, RateLimitInfo

from custom_components.public_transports.const import DOMAIN
from custom_components.public_transports.diagnostics import (
    _call_summary,
    _redact_rate_limit,
    async_get_config_entry_diagnostics,
)

ENTRY_DATA = {
    "city": "Strasbourg",
    "transit_company": "Compagnie des Transports Strasbourgeois",
    "api_token": "fake-token",
    "stop_name": "Homme de Fer",
    "stop_code": "43A",
}


async def test_diagnostics_redacts_the_api_token(hass):
    """api_token is the only secret carried by an entry — it must never appear in the
    diagnostics dump, in either data or options."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options={"api_token": "fake-token"})
    entry.add_to_hass(hass)

    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=[],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["data"]["api_token"] == "**REDACTED**"
    assert diagnostics["entry"]["options"]["api_token"] == "**REDACTED**"
    assert "fake-token" not in str(diagnostics)


async def test_diagnostics_reports_coordinator_state_and_raw_calls(hass):
    """coordinators diagnostics must expose the live state (success, quota, count) and a
    sample of the raw, unfiltered calls the coordinator actually received."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    calls = [MonitoredCall(line_ref="A", published_line_name="A", expected_arrival_time="2099-01-01T00:05:00+00:00")]
    with patch(
        "custom_components.public_transports.coordinator.SiriClient.fetch_next_calls",
        return_value=calls,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["version"] == entry.version
    coordinator_diag = diagnostics["coordinators"][0]
    assert coordinator_diag["stop_codes"] == ["43A"]
    assert coordinator_diag["last_update_success"] is True
    assert coordinator_diag["raw_calls_count"] == 1
    assert coordinator_diag["raw_calls_sample"] == [_call_summary(calls[0])]


def test_redact_rate_limit_flattens_known_fields():
    """_redact_rate_limit must flatten a RateLimitInfo to a plain dict with exactly the
    fields the diagnostics UI expects."""
    rate_limit = RateLimitInfo(remaining_day=100, limit_day=1000, remaining_second=5, limit_second=10)

    assert _redact_rate_limit(rate_limit) == {
        "remaining_day": 100,
        "limit_day": 1000,
        "remaining_second": 5,
        "limit_second": 10,
    }


def test_redact_rate_limit_none_before_first_call():
    """Before any successful call, the coordinator has no RateLimitInfo yet — must return
    None rather than raising on a missing object."""
    assert _redact_rate_limit(None) is None
