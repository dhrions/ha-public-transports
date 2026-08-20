"""Targeted tests for PublicTransportsConfigFlow — pure functions and branches where a
real bug was found live (stop_code fallback, sense/line splitting), not a full step walk.
"""

from pytest_homeassistant_custom_component.common import MockConfigEntry
from siri_lite.models import MonitoredCall

from custom_components.public_transports.config_flow import (
    BOTH_SENSES,
    SPLIT_LINES,
    PublicTransportsConfigFlow,
    _directions_from_calls,
    _lines_from_calls,
)
from custom_components.public_transports.const import DOMAIN

# Already-resolved published_line_name (!= line_ref) so resolve_line_names() never calls
# resolve_line_label() (the only aiohttp-touching path in these steps) — see config_flow.py.
CALLS_TWO_LINES_TWO_SENSES = [
    MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller", destination_name="Asnières"),
    MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Retour", destination_name="Châtillon Montrouge"),
    MonitoredCall(line_ref="C01384", published_line_name="6", direction_ref="Aller", destination_name="Nation"),
]


def _flow(hass=None):
    flow = PublicTransportsConfigFlow()
    if hass is not None:
        flow.hass = hass
    return flow


def test_lines_from_calls_dedupes_by_line_ref():
    lines = _lines_from_calls(CALLS_TWO_LINES_TWO_SENSES)
    assert lines == {"C01383": "13", "C01384": "6"}


def test_lines_from_calls_falls_back_to_ref_when_no_published_name():
    lines = _lines_from_calls([MonitoredCall(line_ref="C01383")])
    assert lines == {"C01383": "C01383"}


def test_directions_from_calls_groups_forked_line_terminuses_by_sense():
    """A forked line (metro 13) has several terminuses for a single real sense."""
    calls = CALLS_TWO_LINES_TWO_SENSES + [
        MonitoredCall(line_ref="C01383", published_line_name="13", direction_ref="Aller", destination_name="Saint-Denis"),
    ]
    senses = _directions_from_calls(calls, line_ref="C01383")
    assert senses == {
        "Aller": "Asnières / Saint-Denis",
        "Retour": "Châtillon Montrouge",
    }


def test_directions_from_calls_ignores_calls_without_direction_ref():
    calls = [MonitoredCall(line_ref="C01383", published_line_name="13", destination_name="Asnières")]
    assert _directions_from_calls(calls) == {}


def test_spec_falls_back_to_single_candidate_code():
    """Non-ambiguous CTS stop: stop_code is never assigned explicitly, only candidate_codes
    is populated — without the fallback the spec silently got stop_code=None (bug found on
    "Wolfisheim Henri Rendu" this session).
    """
    flow = _flow()
    flow.candidate_codes = ["103A"]
    flow.line_filter = "A"
    flow.line_name = "Ligne A"

    spec = flow._spec(direction_filter="Aller", direction_label="Illkirch")

    assert spec == {
        "stop_code": "103A",
        "line_filter": "A",
        "line_name": "Ligne A",
        "direction_filter": "Aller",
        "direction_label": "Illkirch",
    }


def test_spec_uses_explicit_stop_code_over_fallback():
    flow = _flow()
    flow.candidate_codes = ["103A", "103B"]

    spec = flow._spec(stop_code="103B")

    assert spec["stop_code"] == "103B"


def test_codes_from_candidates_always_lists_every_code():
    """Barr-like case: 43B has live traffic, 43A doesn't — both must still appear."""
    flow = _flow()
    flow.candidate_codes = ["43A", "43B"]
    flow.line_filter = None
    flow.candidate_calls = [
        (MonitoredCall(line_ref="A", destination_name="Illkirch"), "43B"),
    ]

    options = flow._codes_from_candidates()

    assert options == {"43A": "43A", "43B": "Illkirch"}


def test_codes_from_candidates_filters_by_selected_line():
    flow = _flow()
    flow.candidate_codes = ["43A"]
    flow.line_filter = "B"
    flow.candidate_calls = [
        (MonitoredCall(line_ref="A", destination_name="Illkirch"), "43A"),
        (MonitoredCall(line_ref="B", destination_name="Robertsau"), "43A"),
    ]

    options = flow._codes_from_candidates()

    assert options == {"43A": "Robertsau"}


async def test_select_line_split_lines_creates_one_spec_per_line_and_sense(hass):
    """SPLIT_LINES ("une ligne par capteur") must fan out per (line x real sense), with a
    single merged spec for lines that only expose one sense — reproduces the manual
    Châtelet-Les Halles test (3 lines, one forked -> N sensors).
    """
    flow = _flow(hass)
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]

    result = await flow.async_step_select_line({"line": SPLIT_LINES})

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    assert len(specs) == 3  # line 13: 2 senses, line 6: 1 (merged)
    line_13_specs = [s for s in specs if s["line_filter"] == "C01383"]
    assert {s["direction_filter"] for s in line_13_specs} == {"Aller", "Retour"}
    line_6_specs = [s for s in specs if s["line_filter"] == "C01384"]
    assert len(line_6_specs) == 1
    assert line_6_specs[0]["direction_filter"] == "Aller"


async def test_select_line_skips_form_when_only_one_line(hass):
    """A single circulating line must be auto-selected, no form shown."""
    flow = _flow(hass)
    flow.available_calls = [MonitoredCall(line_ref="A", published_line_name="Ligne A", direction_ref="Aller")]
    flow.candidate_codes = ["43A"]
    flow.stop_code = "43A"

    result = await flow.async_step_select_line()

    assert result["type"] == "create_entry"
    assert flow.line_filter == "A"
    assert flow.line_name == "Ligne A"


async def test_select_direction_ambiguous_cts_single_sense_choice_sets_stop_code(hass):
    """Ambiguous CTS stop: choosing one code becomes stop_code, no DirectionRef involved."""
    flow = _flow(hass)
    flow.candidate_codes = ["43A", "43B"]
    flow.candidate_calls = [
        (MonitoredCall(line_ref="A", destination_name="Illkirch"), "43A"),
        (MonitoredCall(line_ref="A", destination_name="Robertsau"), "43B"),
    ]

    result = await flow.async_step_select_direction({"direction": "43B"})

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    assert len(specs) == 1
    assert specs[0]["stop_code"] == "43B"
    assert specs[0]["direction_filter"] is None


async def test_select_direction_ambiguous_cts_both_senses_creates_one_spec_per_code(hass):
    flow = _flow(hass)
    flow.candidate_codes = ["43A", "43B"]
    flow.candidate_calls = [
        (MonitoredCall(line_ref="A", destination_name="Illkirch"), "43A"),
        (MonitoredCall(line_ref="A", destination_name="Robertsau"), "43B"),
    ]

    result = await flow.async_step_select_direction({"direction": BOTH_SENSES})

    specs = result["data"]["senses"]
    assert {s["stop_code"] for s in specs} == {"43A", "43B"}


async def test_select_direction_real_sense_both_senses_creates_one_spec_per_direction(hass):
    """Non-ambiguous stop (PRIM/single-code CTS): senses come from DirectionRef, "les deux
    sens" fans out to one spec per direction on the same stop_code.
    """
    flow = _flow(hass)
    flow.candidate_codes = ["STIF:StopArea:SP:45102:"]
    flow.stop_code = "STIF:StopArea:SP:45102:"
    flow.line_filter = "C01383"
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES

    result = await flow.async_step_select_direction({"direction": BOTH_SENSES})

    specs = result["data"]["senses"]
    assert len(specs) == 2
    assert {s["direction_filter"] for s in specs} == {"Aller", "Retour"}
    assert all(s["stop_code"] == "STIF:StopArea:SP:45102:" for s in specs)


async def test_select_direction_single_sense_skips_form(hass):
    flow = _flow(hass)
    flow.candidate_codes = ["43A"]
    flow.stop_code = "43A"
    flow.line_filter = "A"
    flow.available_calls = [
        MonitoredCall(line_ref="A", direction_ref="Aller", destination_name="Illkirch"),
    ]

    result = await flow.async_step_select_direction()

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    assert len(specs) == 1
    assert specs[0]["direction_filter"] == "Aller"


def test_reused_token_returns_existing_token_for_same_company(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"transit_company": "Compagnie des Transports Strasbourgeois", "api_token": "existing-token"},
    )
    entry.add_to_hass(hass)

    flow = _flow(hass)
    flow.transit_company = "Compagnie des Transports Strasbourgeois"

    assert flow._reused_token() == "existing-token"


def test_spec_with_stop_codes_keeps_primary_stop_code_and_full_list():
    """A pole spec carries both stop_code (primary, back-compat) and stop_codes (full list)."""
    flow = _flow()
    flow.line_filter = None
    flow.line_name = None

    spec = flow._spec("A", stop_codes=["A", "B", "C"])

    assert spec["stop_code"] == "A"
    assert spec["stop_codes"] == ["A", "B", "C"]


def test_spec_without_stop_codes_has_no_stop_codes_key():
    """Non-pole specs stay byte-identical to before — no stop_codes key at all."""
    flow = _flow()
    flow.candidate_codes = ["A"]

    spec = flow._spec("A")

    assert "stop_codes" not in spec


async def test_select_direction_pole_mode_skips_ambiguous_cts_branch(hass):
    """pole_codes set + candidate_codes > 1 must NOT trigger the CTS "pick one code as the
    sense" branch — it must merge into shared specs via the real-sense (PRIM) branch
    instead, since candidate_codes here means "codes to merge", not "codes to choose from".
    """
    flow = _flow(hass)
    flow.pole_codes = ["A", "B"]
    flow.candidate_codes = ["A", "B"]
    flow.stop_code = "A"
    flow.line_filter = "C01383"
    flow.available_calls = [
        MonitoredCall(line_ref="C01383", direction_ref="Aller", destination_name="Asnières"),
    ]

    result = await flow.async_step_select_direction()

    assert result["type"] == "create_entry"
    specs = result["data"]["senses"]
    assert len(specs) == 1
    assert specs[0]["stop_codes"] == ["A", "B"]
    assert specs[0]["stop_code"] == "A"
    assert specs[0]["direction_filter"] == "Aller"


async def test_select_direction_pole_mode_both_senses_shares_stop_codes(hass):
    flow = _flow(hass)
    flow.pole_codes = ["A", "B"]
    flow.candidate_codes = ["A", "B"]
    flow.stop_code = "A"
    flow.line_filter = "C01383"
    flow.available_calls = CALLS_TWO_LINES_TWO_SENSES

    result = await flow.async_step_select_direction({"direction": BOTH_SENSES})

    specs = result["data"]["senses"]
    assert len(specs) == 2
    assert {s["direction_filter"] for s in specs} == {"Aller", "Retour"}
    assert all(s["stop_codes"] == ["A", "B"] for s in specs)


def test_reused_token_returns_none_for_different_company(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"transit_company": "Compagnie des Transports Strasbourgeois", "api_token": "existing-token"},
    )
    entry.add_to_hass(hass)

    flow = _flow(hass)
    flow.transit_company = "IDF Mobilités / RATP"

    assert flow._reused_token() is None
